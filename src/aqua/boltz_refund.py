"""Refund of Boltz v2 submarine swaps on Liquid — recovers a stuck L-BTC lockup.

Two paths, both spending the taproot lockup output (see docs/submarine-swap-ln-refund.md):
cooperative (MuSig2 key-path, works immediately) and unilateral (script-path on
the refund leaf, only valid once the chain passes the swap's timeout block).

Everything is reconstructed from the swap's own data; nothing here reads or
writes wallet state.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import struct
from dataclasses import dataclass
from typing import Any, Callable, Optional

import wallycore as wally
from coincurve import PrivateKey

from ._bip327 import (
    SessionContext,
    get_xonly_pk,
    key_agg_and_tweak,
    nonce_agg,
    nonce_gen,
    partial_sig_agg,
    partial_sig_verify,
    schnorr_verify,
)
from ._bip327 import sign as musig_sign

logger = logging.getLogger(__name__)

# Elements uses its own tapleaf version and its own tagged-hash domain strings.
LEAF_VERSION_LIQUID = 0xC4
_TAG_LEAF = "TapLeaf/elements"
_TAG_BRANCH = "TapBranch/elements"
_TAG_TWEAK = "TapTweak/elements"
_TAG_SIGHASH = "TapSighash/elements"

# Asset ids and genesis hashes in internal (reversed) byte order.
LBTC_ASSET_ID = {
    "mainnet": bytes.fromhex(
        "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
    )[::-1],
    "testnet": bytes.fromhex(
        "144c654344aa716d6f3abcc1ca90e5641e4e2a7f633bc09fe3baf64585819a49"
    )[::-1],
}
GENESIS_BLOCK_HASH = {
    "mainnet": bytes.fromhex(
        "1466275836220db2944ca059a3a10ef6fd2ea684b0688d2c379296888a206003"
    )[::-1],
    "testnet": bytes.fromhex(
        "a771da8e52ee6ad581ed1e9a99825e5b3b7992225534eaa2ae23244fe26ab1c1"
    )[::-1],
}
ADDRESS_PREFIXES = {
    "mainnet": ("lq", "ex"),
    "testnet": ("tlq", "tex"),
}

# RBF-signalling and below 0xffffffff, which CHECKLOCKTIMEVERIFY requires.
_SEQUENCE = 0xFFFFFFFD
_SIGHASH_DEFAULT = 0x00
_DUMMY_SIGNATURE = bytes(64)

# A refund spends one small input; anything above this signals a fee-maths bug.
MAX_REFUND_FEE_SATS = 1_000
MIN_FEE_RATE = 0.1  # Liquid's minimum relay rate, in sat/vbyte.


class RefundError(ValueError):
    """A refund could not be built, signed or broadcast."""


class LockupSpentError(RefundError):
    """The lockup output is already spent, so there is nothing left to refund."""


# No UTXO lookup exists; a spent lockup only shows up in the refusal wording.
_SPENT_LOCKUP_SIGNALS = (
    "no unspent lockup",
    "already spent",
    "lockup not found",
    "missing inputs",
    "bad-txns-inputs-missingorspent",
)


def _looks_like_spent_lockup(message: str) -> bool:
    lowered = message.lower()
    return any(signal in lowered for signal in _SPENT_LOCKUP_SIGNALS)


@dataclass(frozen=True)
class SwapTree:
    """The reconstructed taproot tree of a submarine swap lockup."""

    claim_leaf: bytes
    refund_leaf: bytes
    claim_leaf_hash: bytes
    refund_leaf_hash: bytes
    merkle_root: bytes
    internal_key_xonly: bytes
    output_key_xonly: bytes
    parity: int
    scriptpubkey: bytes
    tap_tweak: bytes
    claim_public_key: bytes
    refund_public_key: bytes

    def control_block(self) -> bytes:
        """Control block for a script-path spend of the refund leaf."""
        return (
            bytes([LEAF_VERSION_LIQUID | self.parity])
            + self.internal_key_xonly
            + self.claim_leaf_hash
        )


@dataclass(frozen=True)
class LockupUtxo:
    """The unblinded lockup output being refunded."""

    txid: bytes  # internal byte order
    vout: int
    scriptpubkey: bytes
    asset_commitment: bytes
    value_commitment: bytes
    value: int
    asset: bytes  # internal byte order
    abf: bytes
    vbf: bytes
    tx: Any  # wally tx handle of the lockup transaction


def _push(data: bytes) -> bytes:
    """Minimal script push for the <=75 byte operands a swap tree uses."""
    if len(data) >= 76:
        raise RefundError(f"unexpected script operand of {len(data)} bytes")
    return bytes([len(data)]) + data


def encode_script_num(value: int) -> bytes:
    """Encode a non-negative integer as a minimally-sized CScriptNum."""
    if value < 0:
        raise RefundError("script numbers must be non-negative here")
    if value == 0:
        return b""
    out = bytearray()
    while value:
        out.append(value & 0xFF)
        value >>= 8
    if out[-1] & 0x80:
        out.append(0x00)
    return bytes(out)


def _varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def _varslice(data: bytes) -> bytes:
    return _varint(len(data)) + data


def _xonly(pubkey: bytes) -> bytes:
    if len(pubkey) == 32:
        return pubkey
    if len(pubkey) != 33:
        raise RefundError(f"expected a 33-byte compressed pubkey, got {len(pubkey)}")
    return pubkey[1:]


def _leaf_hash(script: bytes) -> bytes:
    return wally.bip340_tagged_hash(
        bytes([LEAF_VERSION_LIQUID]) + _varslice(script), _TAG_LEAF
    )


def build_swap_tree(
    payment_hash: bytes,
    claim_public_key: bytes,
    refund_public_key: bytes,
    timeout_block_height: int,
) -> SwapTree:
    """Rebuild a submarine swap's taproot tree from its public parameters.

    Mirrors boltz-core's Liquid SwapTree: the key-path is the MuSig2 aggregate
    of [claim, refund] (that order is fixed by the protocol, never key-sorted).
    """
    if len(payment_hash) != 32:
        raise RefundError(f"payment hash must be 32 bytes, got {len(payment_hash)}")

    claim_x = _xonly(claim_public_key)
    refund_x = _xonly(refund_public_key)

    # OP_HASH160 <ripemd160(payment_hash)> OP_EQUALVERIFY <claim key> OP_CHECKSIG
    claim_leaf = (
        b"\xa9"
        + _push(hashlib.new("ripemd160", payment_hash).digest())
        + b"\x88"
        + _push(claim_x)
        + b"\xac"
    )
    # <refund key> OP_CHECKSIGVERIFY <timeout> OP_CHECKLOCKTIMEVERIFY
    refund_leaf = (
        _push(refund_x)
        + b"\xad"
        + _push(encode_script_num(timeout_block_height))
        + b"\xb1"
    )

    claim_hash = _leaf_hash(claim_leaf)
    refund_hash = _leaf_hash(refund_leaf)
    merkle_root = wally.bip340_tagged_hash(
        b"".join(sorted([claim_hash, refund_hash])), _TAG_BRANCH
    )

    keyagg = key_agg_and_tweak([claim_public_key, refund_public_key], [], [])
    internal_x = get_xonly_pk(keyagg)

    tap_tweak = wally.bip340_tagged_hash(internal_x + merkle_root, _TAG_TWEAK)
    tweaked = wally.ec_public_key_bip341_tweak(
        b"\x02" + internal_x, merkle_root, wally.EC_FLAG_ELEMENTS
    )
    output_key = bytes(tweaked[1:])
    parity = 1 if tweaked[0] == 0x03 else 0

    return SwapTree(
        claim_leaf=claim_leaf,
        refund_leaf=refund_leaf,
        claim_leaf_hash=claim_hash,
        refund_leaf_hash=refund_hash,
        merkle_root=merkle_root,
        internal_key_xonly=internal_x,
        output_key_xonly=output_key,
        parity=parity,
        scriptpubkey=b"\x51\x20" + output_key,
        tap_tweak=tap_tweak,
        claim_public_key=claim_public_key,
        refund_public_key=refund_public_key,
    )


def find_and_unblind_lockup(
    lockup_tx_hex: str,
    tree: SwapTree,
    blinding_key: bytes,
    network: str,
    expected_amount: Optional[int] = None,
) -> LockupUtxo:
    """Locate the swap's lockup output in its transaction and unblind it.

    Matching on the reconstructed scriptPubKey is the guard for the whole
    crypto stack: a wrong key, tag or leaf version cannot produce a match.
    """
    tx = wally.tx_from_hex(
        lockup_tx_hex,
        wally.WALLY_TX_FLAG_USE_ELEMENTS | wally.WALLY_TX_FLAG_USE_WITNESS,
    )

    vout = None
    for i in range(wally.tx_get_num_outputs(tx)):
        if bytes(wally.tx_get_output_script(tx, i) or b"") == tree.scriptpubkey:
            vout = i
            break
    if vout is None:
        raise RefundError(
            "No output of the lockup transaction matches the reconstructed swap "
            f"script ({tree.scriptpubkey.hex()}). The claim public key, blinding "
            "key or timeout block height does not belong to this swap."
        )

    asset_commitment = bytes(wally.tx_get_output_asset(tx, vout))
    value_commitment = bytes(wally.tx_get_output_value(tx, vout))
    nonce = bytes(wally.tx_get_output_nonce(tx, vout))
    rangeproof = bytes(wally.tx_get_output_rangeproof(tx, vout) or b"")
    if not rangeproof or len(asset_commitment) != 33:
        raise RefundError(
            f"Lockup output {vout} is not confidential; refunds expect a blinded "
            "lockup."
        )

    try:
        # Liquid CT uses the SHA256 of the ECDH point directly as the nonce.
        nonce_hash = wally.ecdh_nonce_hash(nonce, blinding_key)
        value, asset_buf, abf_buf, vbf_buf = wally.asset_unblind_with_nonce(
            nonce_hash,
            rangeproof,
            value_commitment,
            tree.scriptpubkey,
            asset_commitment,
        )
    except Exception as exc:
        raise RefundError(
            f"Could not unblind the lockup output with the given blinding key: {exc!r}"
        ) from exc

    asset = bytes(asset_buf)
    abf = bytes(abf_buf)
    vbf = bytes(vbf_buf)

    expected_asset = LBTC_ASSET_ID.get(network)
    if expected_asset and asset != expected_asset:
        raise RefundError(
            f"Lockup output holds asset {asset[::-1].hex()}, not L-BTC; refusing "
            "to build a refund."
        )
    if expected_amount is not None and value != expected_amount:
        raise RefundError(
            f"Lockup output holds {value} sats but the swap expected "
            f"{expected_amount}; refusing to build a refund."
        )

    return LockupUtxo(
        txid=bytes(wally.tx_get_txid(tx)),
        vout=vout,
        scriptpubkey=tree.scriptpubkey,
        asset_commitment=asset_commitment,
        value_commitment=value_commitment,
        value=value,
        asset=asset,
        abf=abf,
        vbf=vbf,
        tx=tx,
    )


def _decode_confidential_address(address: str, network: str) -> tuple[bytes, bytes]:
    """Return (blinding_pubkey, scriptPubKey) for a confidential Liquid address."""
    blech32, bech32 = ADDRESS_PREFIXES[network]
    if not address.lower().startswith(blech32):
        raise RefundError(
            f"Refund destination must be a confidential Liquid address "
            f"(starts with '{blech32}'); got {address[:12]}…"
        )
    try:
        blinding_pubkey = bytes(
            wally.confidential_addr_segwit_to_ec_public_key(address, blech32)
        )
        unconfidential = wally.confidential_addr_to_addr_segwit(
            address, blech32, bech32
        )
        spk = bytes(wally.addr_segwit_to_bytes(unconfidential, bech32, 0))
    except Exception as exc:
        raise RefundError(f"Invalid Liquid address {address[:12]}…: {exc!r}") from exc
    return blinding_pubkey, spk


def build_refund_transaction(
    utxo: LockupUtxo,
    destination_address: str,
    fee: int,
    locktime: int,
    network: str,
) -> Any:
    """Build and blind the refund transaction, leaving input 0 unsigned.

    The sighash covers the blinded outputs, so nothing may change afterward
    except replacing that dummy witness.
    """
    if fee <= 0:
        raise RefundError("refund fee must be positive")
    amount = utxo.value - fee
    if amount <= 0:
        raise RefundError(
            f"Lockup holds {utxo.value} sats, which does not cover the {fee} sat "
            "network fee."
        )

    blinding_pubkey, dest_spk = _decode_confidential_address(destination_address, network)

    psbt = wally.psbt_init(
        wally.WALLY_PSBT_VERSION_2, 1, 2, 0, wally.WALLY_PSBT_INIT_PSET
    )
    wally.psbt_set_fallback_locktime(psbt, locktime)

    tx_input = wally.tx_input_init(utxo.txid, utxo.vout, _SEQUENCE, None, None)
    wally.psbt_add_tx_input_at(psbt, 0, 0, tx_input)
    wally.psbt_set_input_witness_utxo_from_tx(psbt, 0, utxo.tx, utxo.vout)
    wally.psbt_set_input_utxo_rangeproof(
        psbt, 0, wally.tx_get_output_rangeproof(utxo.tx, utxo.vout)
    )

    # Destination stays confidential — see "Outputs" in docs/submarine-swap-ln-refund.md.
    # Elements tags explicit (unblinded) assets and values with a 0x01 prefix.
    explicit_asset = b"\x01" + utxo.asset
    dest_out = wally.tx_elements_output_init(
        dest_spk, explicit_asset, wally.tx_confidential_value_from_satoshi(amount), None
    )
    wally.psbt_add_tx_output_at(psbt, 0, 0, dest_out)
    wally.psbt_set_output_blinding_public_key(psbt, 0, blinding_pubkey)
    wally.psbt_set_output_blinder_index(psbt, 0, 0)

    # Elements carries the fee as an explicit, unblinded output.
    fee_out = wally.tx_elements_output_init(
        None, explicit_asset, wally.tx_confidential_value_from_satoshi(fee)
    )
    wally.psbt_add_tx_output_at(psbt, 1, 0, fee_out)

    values, vbfs, assets, abfs = (wally.map_init(1, None) for _ in range(4))
    wally.map_add_integer(
        values, 0, wally.tx_confidential_value_from_satoshi(utxo.value)
    )
    wally.map_add_integer(vbfs, 0, utxo.vbf)
    wally.map_add_integer(assets, 0, utxo.asset)
    wally.map_add_integer(abfs, 0, utxo.abf)
    # 5 x 32B per blinded output: abf, vbf, ephemeral key, rangeproof, surjection seed.
    wally.psbt_blind(psbt, values, vbfs, assets, abfs, secrets.token_bytes(5 * 32), 0, 0)

    stack = wally.tx_witness_stack_init(1)
    wally.tx_witness_stack_add(stack, _DUMMY_SIGNATURE)
    wally.psbt_set_input_final_witness(psbt, 0, stack)

    return wally.psbt_extract(psbt, 0)


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _commitment(value: Optional[bytes]) -> bytes:
    """Serialize an Elements confidential field, which is 0x00 when absent.

    wally's getters hand back a zero-filled buffer for absent fields rather
    than an empty one, so the 0x00 prefix is what distinguishes them.
    """
    if not value or value[0] == 0x00:
        return b"\x00"
    return bytes(value)


def elements_taproot_sighash(
    tx: Any,
    index: int,
    scriptpubkeys: list[bytes],
    asset_commitments: list[bytes],
    value_commitments: list[bytes],
    genesis_block_hash: bytes,
    leaf_hash: Optional[bytes] = None,
) -> bytes:
    """BIP-341 signature hash as modified by Elements (SIGHASH_DEFAULT only).

    `leaf_hash` selects a script-path spend; omit it for the key path. See
    docs/submarine-swap-ln-refund.md for why libwally can't compute this for Liquid itself.
    """
    num_inputs = wally.tx_get_num_inputs(tx)
    num_outputs = wally.tx_get_num_outputs(tx)

    prevouts = b""
    sequences = b""
    outpoint_flags = b""
    issuances = b""
    issuance_rangeproofs = b""
    for i in range(num_inputs):
        prevouts += bytes(wally.tx_get_input_txhash(tx, i)) + struct.pack(
            "<I", wally.tx_get_input_index(tx, i)
        )
        sequences += struct.pack("<I", wally.tx_get_input_sequence(tx, i))
        outpoint_flags += b"\x00"  # neither a pegin nor an issuance
        issuances += b"\x00"
        issuance_rangeproofs += b"\x00\x00"

    asset_amounts = b""
    for asset, value in zip(asset_commitments, value_commitments):
        asset_amounts += _commitment(asset) + _commitment(value)

    outputs = b""
    output_witnesses = b""
    for i in range(num_outputs):
        outputs += (
            _commitment(wally.tx_get_output_asset(tx, i))
            + _commitment(wally.tx_get_output_value(tx, i))
            + _commitment(wally.tx_get_output_nonce(tx, i))
            + _varslice(bytes(wally.tx_get_output_script(tx, i) or b""))
        )
        output_witnesses += _varslice(
            bytes(wally.tx_get_output_surjectionproof(tx, i) or b"")
        ) + _varslice(bytes(wally.tx_get_output_rangeproof(tx, i) or b""))

    # ext_flag 1 marks the tapscript extension; the annex bit stays clear.
    spend_type = 2 if leaf_hash else 0

    preimage = (
        genesis_block_hash
        + genesis_block_hash
        + bytes([_SIGHASH_DEFAULT])
        + struct.pack("<I", wally.tx_get_version(tx))
        + struct.pack("<I", wally.tx_get_locktime(tx))
        + _sha256(outpoint_flags)
        + _sha256(prevouts)
        + _sha256(asset_amounts)
        + _sha256(b"".join(_varslice(spk) for spk in scriptpubkeys))
        + _sha256(sequences)
        + _sha256(issuances)
        + _sha256(issuance_rangeproofs)
        + _sha256(outputs)
        + _sha256(output_witnesses)
        + bytes([spend_type])
        + struct.pack("<I", index)
    )
    if leaf_hash:
        # key_version 0 selects the current tapscript, per BIP-341.
        preimage += leaf_hash + b"\x00" + struct.pack("<I", 0xFFFFFFFF)

    # bytes, not wally's bytearray: coincurve's signers reject the latter.
    return bytes(wally.bip340_tagged_hash(preimage, _TAG_SIGHASH))


def keypath_sighash_wally(
    tx: Any,
    index: int,
    scriptpubkeys: list[bytes],
    asset_commitments: list[bytes],
    value_commitments: list[bytes],
    genesis_block_hash: bytes,
) -> bytes:
    """Key-path sighash via libwally — the cross-check for the manual version."""
    scripts = wally.map_init(len(scriptpubkeys), None)
    assets = wally.map_init(len(asset_commitments), None)
    values = wally.map_init(len(value_commitments), None)
    for i, spk in enumerate(scriptpubkeys):
        wally.map_add_integer(scripts, i, spk)
        wally.map_add_integer(assets, i, asset_commitments[i])
        wally.map_add_integer(values, i, value_commitments[i])
    return bytes(
        wally.tx_get_input_signature_hash(
            tx,
            index,
            scripts,
            assets,
            values,
            None,
            0,
            0xFFFFFFFF,
            None,
            genesis_block_hash,
            _SIGHASH_DEFAULT,
            wally.WALLY_SIGTYPE_SW_V1,
            None,
        )
    )


def _attach_witness(tx: Any, index: int, items: list[bytes]) -> Any:
    stack = wally.tx_witness_stack_init(len(items))
    for item in items:
        wally.tx_witness_stack_add(stack, item)
    wally.tx_set_input_witness(tx, index, stack)
    return tx


def _estimate_fee(tx: Any, fee_rate: float) -> int:
    """Fee from the discounted vsize Liquid applies to confidential transactions."""
    weight = wally.tx_get_weight(tx) - wally.tx_get_elements_weight_discount(tx, 0)
    vsize = wally.tx_vsize_from_weight(weight)
    fee = int(-(-(vsize * max(fee_rate, MIN_FEE_RATE)) // 1))  # ceil
    return max(fee, 1)


def _build_signed_refund(
    *,
    tree: SwapTree,
    utxo: LockupUtxo,
    destination_address: str,
    network: str,
    locktime: int,
    fee_rate: float,
    sign_input: Callable[[Any, bytes], list[bytes]],
) -> tuple[Any, int]:
    """Build the refund tx at a settled fee, then hand it to `sign_input`.

    Built twice: the draft only measures size (rangeproofs dominate it) before
    the real fee is known. `sign_input(tx, message)` returns the witness stack.
    """
    genesis = GENESIS_BLOCK_HASH[network]
    # Nominal fee: size doesn't depend on it, and even the smallest lockup covers 1 sat.
    draft = build_refund_transaction(utxo, destination_address, 1, locktime, network)
    fee = _estimate_fee(draft, fee_rate)
    if fee > MAX_REFUND_FEE_SATS:
        raise RefundError(
            f"Refund fee of {fee} sats exceeds the {MAX_REFUND_FEE_SATS} sat safety "
            f"cap at {fee_rate} sat/vb; refusing to build it."
        )

    tx = build_refund_transaction(utxo, destination_address, fee, locktime, network)
    message = elements_taproot_sighash(
        tx,
        0,
        [utxo.scriptpubkey],
        [utxo.asset_commitment],
        [utxo.value_commitment],
        genesis,
        leaf_hash=None if locktime == 0 else tree.refund_leaf_hash,
    )
    _attach_witness(tx, 0, sign_input(tx, message))
    return tx, fee


def refund_cooperative(
    *,
    swap_id: str,
    tree: SwapTree,
    utxo: LockupUtxo,
    refund_private_key: bytes,
    destination_address: str,
    network: str,
    client: Any,
    fee_rate: float,
) -> tuple[str, int]:
    """Refund via a MuSig2 key-path spend co-signed by the provider.

    Valid before the timeout, but only while the provider cosigns; it answers
    400 otherwise and the caller falls back to the unilateral path.
    """
    pubkeys = [tree.claim_public_key, tree.refund_public_key]
    our_pubkey = tree.refund_public_key

    def sign_input(tx: Any, message: bytes) -> list[bytes]:
        # Fresh nonce per attempt — reuse leaks the key; see docs/submarine-swap-ln-refund.md.
        secnonce, our_pubnonce = nonce_gen(
            refund_private_key,
            our_pubkey,
            tree.output_key_xonly,
            message,
            None,
        )
        response = client.post_refund_signature(
            swap_id,
            pub_nonce=our_pubnonce.hex(),
            transaction_hex=wally.tx_to_hex(tx, wally.WALLY_TX_FLAG_USE_WITNESS),
            index=0,
        )
        try:
            their_pubnonce = bytes.fromhex(response["pubNonce"])
            their_psig = bytes.fromhex(response["partialSignature"])
        except (KeyError, ValueError) as exc:
            raise RefundError(
                f"Provider returned a malformed cooperative refund response: {exc!r}"
            ) from exc

        # Provider first, matching the key order of the aggregate.
        pubnonces = [their_pubnonce, our_pubnonce]
        aggnonce = nonce_agg(pubnonces)
        session = SessionContext(
            aggnonce, pubkeys, [tree.tap_tweak], [True], message
        )

        if not partial_sig_verify(
            their_psig, pubnonces, pubkeys, [tree.tap_tweak], [True], message, 0
        ):
            raise RefundError(
                "Provider's partial signature is invalid for this transaction; "
                "refusing to broadcast."
            )
        our_psig = musig_sign(secnonce, refund_private_key, session)
        signature = partial_sig_agg([their_psig, our_psig], session)
        if not schnorr_verify(message, tree.output_key_xonly, signature):
            raise RefundError(
                "Aggregated signature failed verification; refusing to broadcast."
            )
        return [signature]

    tx, fee = _build_signed_refund(
        tree=tree,
        utxo=utxo,
        destination_address=destination_address,
        network=network,
        locktime=0,
        fee_rate=fee_rate,
        sign_input=sign_input,
    )
    return wally.tx_to_hex(tx, wally.WALLY_TX_FLAG_USE_WITNESS), fee


def refund_unilateral(
    *,
    tree: SwapTree,
    utxo: LockupUtxo,
    refund_private_key: bytes,
    destination_address: str,
    network: str,
    timeout_block_height: int,
    fee_rate: float,
) -> tuple[str, int]:
    """Refund via a script-path spend of the refund leaf, without the provider.

    Only relayable once the chain tip reaches `timeout_block_height`, because
    the leaf's CHECKLOCKTIMEVERIFY gates it.
    """
    signer = PrivateKey(refund_private_key)

    def sign_input(tx: Any, message: bytes) -> list[bytes]:
        signature = signer.sign_schnorr(message, b"")
        return [signature, tree.refund_leaf, tree.control_block()]

    tx, fee = _build_signed_refund(
        tree=tree,
        utxo=utxo,
        destination_address=destination_address,
        network=network,
        locktime=timeout_block_height,
        fee_rate=fee_rate,
        sign_input=sign_input,
    )
    return wally.tx_to_hex(tx, wally.WALLY_TX_FLAG_USE_WITNESS), fee


def refund_submarine_swap(
    *,
    swap_id: str,
    refund_private_key: str,
    claim_public_key: str,
    blinding_key: str,
    payment_hash: str,
    timeout_block_height: int,
    lockup_tx_hex: str,
    destination_address: str,
    network: str,
    client: Any,
    tip_height: int,
    broadcast: Callable[[str], str],
    expected_amount: Optional[int] = None,
    fee_rate: Optional[float] = None,
    dry_run: bool = False,
) -> dict:
    """Recover a stuck submarine swap lockup back to `destination_address`.

    Tries the cooperative refund first and falls back to the unilateral one
    once the chain has passed the swap's timeout block.
    """
    if network not in GENESIS_BLOCK_HASH:
        raise RefundError(f"Refunds are only supported on: {', '.join(GENESIS_BLOCK_HASH)}")

    refund_privkey = bytes.fromhex(refund_private_key)
    tree = build_swap_tree(
        bytes.fromhex(payment_hash),
        bytes.fromhex(claim_public_key),
        PrivateKey(refund_privkey).public_key.format(compressed=True),
        timeout_block_height,
    )
    utxo = find_and_unblind_lockup(
        lockup_tx_hex,
        tree,
        bytes.fromhex(blinding_key),
        network,
        expected_amount=expected_amount,
    )

    if fee_rate is None:
        try:
            fee_rate = float(client.get_chain_fees().get("L-BTC", MIN_FEE_RATE))
        except Exception as exc:
            logger.warning("Could not read the provider's fee rate (%r); using the minimum.", exc)
            fee_rate = MIN_FEE_RATE

    timed_out = tip_height >= timeout_block_height
    cooperative_error: Optional[str] = None
    try:
        tx_hex, fee = refund_cooperative(
            swap_id=swap_id,
            tree=tree,
            utxo=utxo,
            refund_private_key=refund_privkey,
            destination_address=destination_address,
            network=network,
            client=client,
            fee_rate=fee_rate,
        )
        refund_type = "cooperative"
    except Exception as exc:
        cooperative_error = str(exc)
        logger.info("Cooperative refund unavailable for swap %s: %s", swap_id, exc)
        if _looks_like_spent_lockup(cooperative_error):
            raise LockupSpentError(
                f"The lockup of swap {swap_id} is already spent, so there is nothing "
                f"left to refund — it was claimed or refunded elsewhere. The provider "
                f"said: {cooperative_error}"
            ) from exc
        if not timed_out:
            blocks_left = timeout_block_height - tip_height
            raise RefundError(
                f"The provider would not cosign a refund for swap {swap_id} "
                f"({cooperative_error}). Spending the refund branch without it only "
                f"becomes valid at Liquid block {timeout_block_height}, "
                f"{blocks_left} blocks away (~{blocks_left} minutes at one block per "
                f"minute); the tip is {tip_height}."
            ) from exc
        tx_hex, fee = refund_unilateral(
            tree=tree,
            utxo=utxo,
            refund_private_key=refund_privkey,
            destination_address=destination_address,
            network=network,
            timeout_block_height=timeout_block_height,
            fee_rate=fee_rate,
        )
        refund_type = "unilateral"

    result = {
        "swap_id": swap_id,
        "refund_type": refund_type,
        "amount": utxo.value - fee,
        "fee": fee,
        "destination_address": destination_address,
        "lockup_vout": utxo.vout,
        "network": network,
    }
    if cooperative_error:
        result["cooperative_error"] = cooperative_error
    if dry_run:
        result["dry_run"] = True
        result["tx_hex"] = tx_hex
        return result

    try:
        result["refund_txid"] = broadcast(tx_hex)
    except Exception as exc:
        if _looks_like_spent_lockup(str(exc)):
            raise LockupSpentError(
                f"The lockup of swap {swap_id} was spent before this refund could be "
                f"broadcast; nothing is left to recover. The node said: {exc}"
            ) from exc
        raise
    return result
