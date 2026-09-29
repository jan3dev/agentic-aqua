"""Submarine swap refund crypto.

The swap-tree fixtures come from two real mainnet swaps whose lockups were
recovered with this code; `EXPECTED_SCRIPTPUBKEY` is the scriptPubKey those
lockup outputs actually carry on chain, so a regression in the script layout,
the Elements tapleaf version, the tagged-hash domains or the MuSig2 key order
cannot pass unnoticed.
"""

import pytest
import wallycore as wally
from coincurve import PrivateKey

import aqua.boltz_refund as boltz_refund
from aqua.boltz_refund import (
    GENESIS_BLOCK_HASH,
    LEAF_VERSION_LIQUID,
    MAX_REFUND_FEE_SATS,
    LockupSpentError,
    LockupUtxo,
    RefundError,
    _build_signed_refund,
    _looks_like_spent_lockup,
    _varslice,
    build_refund_transaction,
    build_swap_tree,
    elements_taproot_sighash,
    encode_script_num,
    find_and_unblind_lockup,
    keypath_sighash_wally,
)

# Mainnet swap 1NoxvTZ4 (lockup 29b60512…cad9, vout 0).
SWAP = {
    "claim_public_key": "036b6d1cd7bfc192668e65de9b1118796d7d4da553a4308c439b2f2fa3f5631965",
    "refund_public_key": "03107072a5fc9da2485246662e7ff5c15a1b2cc008ba38a0f5784ee275997d2a6e",
    "payment_hash": "202f415c748c09cc30bdb6e6da2f208a585b160f18e9553344f5212755421281",
    "timeout_block_height": 4113159,
}
EXPECTED_SCRIPTPUBKEY = (
    "5120dd795759bff13b3ca1b5581d3631bd4a6a511d7e90602ea48fb46733841d2025"
)
EXPECTED_CLAIM_LEAF = (
    "a914f05637bc61e31732e48e212bae8908e3c0ade9d788"
    "206b6d1cd7bfc192668e65de9b1118796d7d4da553a4308c439b2f2fa3f5631965ac"
)
EXPECTED_REFUND_LEAF = (
    "20107072a5fc9da2485246662e7ff5c15a1b2cc008ba38a0f5784ee275997d2a6ead0307c33eb1"
)

# Second recovered swap, 1Ndrn9qc.
SWAP_2 = {
    "claim_public_key": "02576014710d8035fa452d5577d94f2cfee15dcd04a6e9c2cf1c4efc770249d8d8",
    "refund_public_key": "0229ebc306c557b135c41afb070f9ad369cf515cc00568aeba22e3674a6c1455eb",
    "payment_hash": "83be29542888a7b74abd1aa8f530b26b2623ed299a9c03d8951c6b49120ff2b2",
    "timeout_block_height": 4113988,
}
EXPECTED_SCRIPTPUBKEY_2 = (
    "51203aa16b95bea267c55770a2ed2bed7a0804b6f86726922f3dea8faa2809ce7f54"
)

# A real mainnet confidential address; refunds must go to a blinded destination.
CONFIDENTIAL_ADDRESS = (
    "lq1qq0zmq2kew3e7j2fzraexl7y4exl2pvunzgtxm9l35r8r7hx6azk847pwj04ahlg4ay"
    "3yraak9hv8w7uspvtxcylngtw0a58a7"
)


def tree_from(swap: dict):
    return build_swap_tree(
        bytes.fromhex(swap["payment_hash"]),
        bytes.fromhex(swap["claim_public_key"]),
        bytes.fromhex(swap["refund_public_key"]),
        swap["timeout_block_height"],
    )


class TestScriptNum:
    def test_zero_is_empty(self):
        assert encode_script_num(0) == b""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (1, "01"),
            (127, "7f"),
            (128, "8000"),  # high bit set -> pad, else it reads as negative
            (255, "ff00"),
            (4113159, "07c33e"),
            (4113988, "44c63e"),
        ],
    )
    def test_minimal_encoding(self, value, expected):
        assert encode_script_num(value).hex() == expected

    def test_negative_rejected(self):
        with pytest.raises(RefundError):
            encode_script_num(-1)


class TestSwapTree:
    def test_scriptpubkey_matches_onchain_lockup(self):
        assert tree_from(SWAP).scriptpubkey.hex() == EXPECTED_SCRIPTPUBKEY

    def test_second_swap_scriptpubkey_matches(self):
        assert tree_from(SWAP_2).scriptpubkey.hex() == EXPECTED_SCRIPTPUBKEY_2

    def test_leaf_scripts(self):
        tree = tree_from(SWAP)
        assert tree.claim_leaf.hex() == EXPECTED_CLAIM_LEAF
        assert tree.refund_leaf.hex() == EXPECTED_REFUND_LEAF

    def test_key_order_is_not_interchangeable(self):
        """Boltz aggregates [claim, refund]; the reverse yields a different output."""
        swapped = build_swap_tree(
            bytes.fromhex(SWAP["payment_hash"]),
            bytes.fromhex(SWAP["refund_public_key"]),
            bytes.fromhex(SWAP["claim_public_key"]),
            SWAP["timeout_block_height"],
        )
        assert swapped.scriptpubkey.hex() != EXPECTED_SCRIPTPUBKEY

    def test_control_block_shape(self):
        tree = tree_from(SWAP)
        control = tree.control_block()
        assert len(control) == 65
        assert control[0] == LEAF_VERSION_LIQUID | tree.parity
        assert control[1:33] == tree.internal_key_xonly
        assert control[33:] == tree.claim_leaf_hash

    def test_merkle_root_is_order_independent(self):
        """Leaf hashes are sorted before branching, per BIP-341."""
        tree = tree_from(SWAP)
        expected = wally.bip340_tagged_hash(
            b"".join(sorted([tree.claim_leaf_hash, tree.refund_leaf_hash])),
            "TapBranch/elements",
        )
        assert tree.merkle_root == expected

    def test_rejects_short_payment_hash(self):
        with pytest.raises(RefundError, match="32 bytes"):
            build_swap_tree(
                b"\x00" * 31,
                bytes.fromhex(SWAP["claim_public_key"]),
                bytes.fromhex(SWAP["refund_public_key"]),
                1,
            )

    def test_rejects_malformed_pubkey(self):
        with pytest.raises(RefundError, match="compressed pubkey"):
            build_swap_tree(
                bytes.fromhex(SWAP["payment_hash"]),
                b"\x02" * 20,
                bytes.fromhex(SWAP["refund_public_key"]),
                1,
            )

    def test_timeout_changes_the_address(self):
        other = build_swap_tree(
            bytes.fromhex(SWAP["payment_hash"]),
            bytes.fromhex(SWAP["claim_public_key"]),
            bytes.fromhex(SWAP["refund_public_key"]),
            SWAP["timeout_block_height"] + 1,
        )
        assert other.scriptpubkey.hex() != EXPECTED_SCRIPTPUBKEY


def build_fixture_transaction():
    """A minimal Elements transaction: one taproot input, two outputs."""
    tx = wally.tx_init(2, 0, 1, 2)
    wally.tx_add_elements_raw_input(
        tx,
        bytes(range(32)),
        0,
        0xFFFFFFFD,
        None, None, None, None, None, None, None, None, None,
        0,
    )
    asset = b"\x01" + bytes.fromhex(
        "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
    )[::-1]
    wally.tx_add_elements_raw_output(
        tx,
        bytes.fromhex("0014" + "11" * 20),
        asset,
        wally.tx_confidential_value_from_satoshi(900),
        None, None, None,
        0,
    )
    # Elements carries the fee as an explicit output with no script.
    wally.tx_add_elements_raw_output(
        tx,
        None,
        asset,
        wally.tx_confidential_value_from_satoshi(100),
        None, None, None,
        0,
    )
    return tx


class TestSighash:
    """The manual sighash is the only way to sign the script path.

    libwally hardcodes Bitcoin's tapleaf version inside its own script-path
    sighash, so the refund leaf needs a hand-rolled one. Pinning the key-path
    result against wally proves the shared structure is right.
    """

    def setup_method(self):
        self.tx = build_fixture_transaction()
        self.spk = bytes.fromhex(EXPECTED_SCRIPTPUBKEY)
        self.asset = b"\x0a" + bytes(32)
        self.value = b"\x08" + bytes(32)
        self.genesis = GENESIS_BLOCK_HASH["mainnet"]

    def _args(self):
        return (self.tx, 0, [self.spk], [self.asset], [self.value])

    def test_keypath_matches_wally(self):
        assert elements_taproot_sighash(
            *self._args(), self.genesis
        ) == keypath_sighash_wally(*self._args(), self.genesis)

    def test_scriptpath_differs_from_keypath(self):
        tree = tree_from(SWAP)
        keypath = elements_taproot_sighash(*self._args(), self.genesis)
        scriptpath = elements_taproot_sighash(
            *self._args(), self.genesis, leaf_hash=tree.refund_leaf_hash
        )
        assert keypath != scriptpath

    def test_scriptpath_is_leaf_specific(self):
        tree = tree_from(SWAP)
        claim_path = elements_taproot_sighash(
            *self._args(), self.genesis, leaf_hash=tree.claim_leaf_hash
        )
        refund_path = elements_taproot_sighash(
            *self._args(), self.genesis, leaf_hash=tree.refund_leaf_hash
        )
        assert claim_path != refund_path

    def test_genesis_hash_binds_the_chain(self):
        """Elements mixes the genesis hash in so signatures cannot cross chains."""
        assert elements_taproot_sighash(
            *self._args(), self.genesis
        ) != elements_taproot_sighash(*self._args(), GENESIS_BLOCK_HASH["testnet"])


class TestFindAndUnblindLockup:
    def test_rejects_transaction_without_the_swap_output(self):
        tree = tree_from(SWAP)
        tx_hex = wally.tx_to_hex(
            build_fixture_transaction(), wally.WALLY_TX_FLAG_USE_WITNESS
        )
        with pytest.raises(RefundError, match="does not belong to this swap"):
            find_and_unblind_lockup(tx_hex, tree, bytes(32), "mainnet")


def test_refund_public_key_derives_from_the_stored_private_key():
    """The refund key is what actually unlocks the leaf; the tree must use its pair."""
    privkey = PrivateKey(bytes.fromhex("11" * 32))
    pubkey = privkey.public_key.format(compressed=True)
    tree = build_swap_tree(
        bytes.fromhex(SWAP["payment_hash"]),
        bytes.fromhex(SWAP["claim_public_key"]),
        pubkey,
        SWAP["timeout_block_height"],
    )
    assert tree.refund_leaf[1:33] == pubkey[1:]


class TestSpentLockupDetection:
    """A refused refund must not be reported as 'wait for the timeout'."""

    @pytest.mark.parametrize(
        "message",
        [
            "no unspent lockup transaction found for this swap",
            "Indra API error (400): No Unspent Lockup transaction",
            "bad-txns-inputs-missingorspent",
            "sendrawtransaction RPC error: Missing inputs",
        ],
    )
    def test_recognises_a_spent_lockup(self, message):
        assert _looks_like_spent_lockup(message)

    @pytest.mark.parametrize(
        "message",
        [
            "cooperative refunds are disabled",
            "Indra API unreachable (POST /v2/swap/submarine/x/refund): timed out",
        ],
    )
    def test_leaves_other_refusals_alone(self, message):
        assert not _looks_like_spent_lockup(message)

    def test_is_a_refund_error(self):
        assert issubclass(LockupSpentError, RefundError)

def _scriptpath_sighash_wally(tx, index, spks, assets, values, genesis, script):
    """wally's own Elements script-path sighash, for cross-checking.

    It derives the tapleaf hash with Bitcoin's leaf version rather than the
    Elements one, which is precisely why production cannot use it.
    """
    scripts = wally.map_init(len(spks), None)
    asset_map = wally.map_init(len(assets), None)
    value_map = wally.map_init(len(values), None)
    for i, spk in enumerate(spks):
        wally.map_add_integer(scripts, i, spk)
        wally.map_add_integer(asset_map, i, assets[i])
        wally.map_add_integer(value_map, i, values[i])
    return bytes(
        wally.tx_get_input_signature_hash(
            tx, index, scripts, asset_map, value_map,
            script, 0, 0xFFFFFFFF, None, genesis, 0, wally.WALLY_SIGTYPE_SW_V1, None,
        )
    )


class TestScriptPathSighashAgainstWally:
    """Pins the script-path structure, which the key-path cross-check cannot reach.

    The two implementations differ only in the tapleaf version (Elements 0xc4
    vs the 0xc0 wally hardcodes); feeding this module wally's version makes
    them comparable, so a match proves the rest of the extension. `TestSwapTree`
    pins the 0xc4 half against a real on-chain scriptPubKey.
    """

    def setup_method(self):
        self.tx = build_fixture_transaction()
        self.spk = bytes.fromhex(EXPECTED_SCRIPTPUBKEY)
        self.asset = b"\x0a" + bytes(32)
        self.value = b"\x08" + bytes(32)
        self.genesis = GENESIS_BLOCK_HASH["mainnet"]
        self.script = tree_from(SWAP).refund_leaf

    def test_matches_wally_at_the_same_leaf_version(self):
        bitcoin_leaf = wally.bip340_tagged_hash(
            b"\xc0" + _varslice(self.script), "TapLeaf/elements"
        )
        mine = elements_taproot_sighash(
            self.tx, 0, [self.spk], [self.asset], [self.value],
            self.genesis, leaf_hash=bitcoin_leaf,
        )
        theirs = _scriptpath_sighash_wally(
            self.tx, 0, [self.spk], [self.asset], [self.value],
            self.genesis, self.script,
        )
        assert mine == theirs

    def test_returns_bytes_not_bytearray(self):
        """coincurve's schnorr signer rejects the bytearray wally hands back."""
        result = elements_taproot_sighash(
            self.tx, 0, [self.spk], [self.asset], [self.value], self.genesis
        )
        assert isinstance(result, bytes)
        PrivateKey(bytes.fromhex("11" * 32)).sign_schnorr(result, b"")


class TestControlBlockValidates:
    """Replays what a node does when validating a script-path spend."""

    def test_taproot_commitment_reconstructs_from_the_control_block(self):
        tree = tree_from(SWAP)
        control = tree.control_block()

        leaf = wally.bip340_tagged_hash(
            bytes([control[0] & 0xFE]) + _varslice(tree.refund_leaf),
            "TapLeaf/elements",
        )
        root = bytes(leaf)
        for offset in range(33, len(control), 32):
            root = bytes(
                wally.bip340_tagged_hash(
                    b"".join(sorted([root, control[offset:offset + 32]])),
                    "TapBranch/elements",
                )
            )
        tweaked = wally.ec_public_key_bip341_tweak(
            b"\x02" + control[1:33], root, wally.EC_FLAG_ELEMENTS
        )

        assert control[0] & 0xFE == LEAF_VERSION_LIQUID
        assert bytes(tweaked[1:]) == tree.output_key_xonly
        assert control[0] & 1 == (1 if tweaked[0] == 0x03 else 0)


class TestRefundTransactionShape:
    """Structure of the built transaction, with no provider or chain involved."""

    def _utxo(self, value=1022):
        """A lockup UTXO stub; only build_refund_transaction's inputs matter here."""
        lockup = build_fixture_transaction()
        asset = bytes.fromhex(
            "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
        )[::-1]
        return LockupUtxo(
            txid=bytes(range(32)),
            vout=0,
            scriptpubkey=bytes.fromhex(EXPECTED_SCRIPTPUBKEY),
            asset_commitment=b"\x01" + asset,
            value_commitment=wally.tx_confidential_value_from_satoshi(value),
            value=value,
            asset=asset,
            abf=bytes(32),
            vbf=bytes(32),
            tx=lockup,
        )

    def test_rejects_a_fee_the_lockup_cannot_cover(self):
        with pytest.raises(RefundError, match="does not cover"):
            build_refund_transaction(
                self._utxo(value=10), CONFIDENTIAL_ADDRESS, 50, 0, "mainnet"
            )

    def test_rejects_an_unconfidential_destination(self):
        with pytest.raises(RefundError, match="confidential"):
            build_refund_transaction(
                self._utxo(), "ex1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", 19, 0, "mainnet"
            )

    def test_rejects_a_non_positive_fee(self):
        with pytest.raises(RefundError, match="fee must be positive"):
            build_refund_transaction(self._utxo(), CONFIDENTIAL_ADDRESS, 0, 0, "mainnet")

    def test_the_smallest_lockup_still_clears_the_fee_guard(self):
        """Sizing the draft off the fee cap would make small swaps unrefundable.

        A minimum-size Boltz swap locks up ~121 sats while the real fee is ~19.
        """
        with pytest.raises(RefundError, match="does not cover"):
            build_refund_transaction(
                self._utxo(value=121), CONFIDENTIAL_ADDRESS, MAX_REFUND_FEE_SATS, 0, "mainnet"
            )
        # Past the guard at the nominal fee; fails later in blinding instead.
        with pytest.raises(ValueError) as exc_info:
            build_refund_transaction(
                self._utxo(value=121), CONFIDENTIAL_ADDRESS, 1, 0, "mainnet"
            )
        assert "does not cover" not in str(exc_info.value)


class TestFeeSizingWitness:
    """The fee draft must carry the witness shape the signed tx will have."""

    @pytest.mark.parametrize(
        "locktime,expected_items", [(0, 1), (SWAP["timeout_block_height"], 3)]
    )
    def test_draft_witness_matches_spend_path(self, monkeypatch, locktime, expected_items):
        tree = tree_from(SWAP)
        sized = []

        def fake_estimate(tx, fee_rate):
            sized.append(wally.tx_get_input_witness_num_items(tx, 0))
            return 20

        def fake_build(*args):
            tx = build_fixture_transaction()
            boltz_refund._attach_witness(tx, 0, [bytes(64)])  # as the real builder does
            return tx

        monkeypatch.setattr(boltz_refund, "build_refund_transaction", fake_build)
        monkeypatch.setattr(boltz_refund, "_estimate_fee", fake_estimate)
        monkeypatch.setattr(boltz_refund, "elements_taproot_sighash", lambda *a, **k: bytes(32))

        _build_signed_refund(
            tree=tree,
            utxo=TestRefundTransactionShape()._utxo(),
            destination_address=CONFIDENTIAL_ADDRESS,
            network="mainnet",
            locktime=locktime,
            fee_rate=0.1,
            sign_input=lambda tx, message: [bytes(64)],
        )
        assert sized == [expected_items]


# --- Full confidential refund path ------------------------------------------
#
# Everything below runs against a lockup blinded in-test with known keys, so
# the unblind, the signatures and the refund output can all be checked end to
# end. The oracles stay independent of the module: L-BTC asset ids come from
# the display-hex literals, the key-path sighash from libwally, and signatures
# are verified with coincurve rather than the in-tree BIP-340 code.

LBTC_DISPLAY_HEX = {
    "mainnet": "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d",
    "testnet": "144c654344aa716d6f3abcc1ca90e5641e4e2a7f633bc09fe3baf64585819a49",
}
ADDRESS_HRPS = {"mainnet": ("ex", "lq"), "testnet": ("tex", "tlq")}

REFUND_SK = bytes.fromhex("22" * 32)
CLAIM_SK = bytes.fromhex("33" * 32)
LOCKUP_BLINDING_SK = bytes.fromhex("44" * 32)
LOCKUP_EPHEMERAL_SK = bytes.fromhex("55" * 32)
LOCKUP_ABF = bytes.fromhex("66" * 32)
LOCKUP_VBF = bytes.fromhex("77" * 32)
DEST_BLINDING_SK = bytes.fromhex("99" * 32)
PAYMENT_HASH = bytes.fromhex("88" * 32)
TIMEOUT = 4_000_000
LOCKUP_VALUE = 50_000
LOCKUP_VOUT = 1  # a decoy output sits at vout 0


def _pub(sk: bytes) -> bytes:
    return PrivateKey(sk).public_key.format(compressed=True)


def _local_tree():
    return build_swap_tree(PAYMENT_HASH, _pub(CLAIM_SK), _pub(REFUND_SK), TIMEOUT)


def _blinded_lockup(tree, asset_internal: bytes, value: int = LOCKUP_VALUE):
    """A lockup tx whose vout 1 pays `tree` confidentially to LOCKUP_BLINDING_SK."""
    generator = bytes(wally.asset_generator_from_bytes(asset_internal, LOCKUP_ABF))
    value_commitment = bytes(wally.asset_value_commitment(value, LOCKUP_VBF, generator))
    rangeproof = bytes(
        wally.asset_rangeproof(
            value,
            bytes(wally.ec_public_key_from_private_key(LOCKUP_BLINDING_SK)),
            LOCKUP_EPHEMERAL_SK,
            asset_internal,
            LOCKUP_ABF,
            LOCKUP_VBF,
            value_commitment,
            tree.scriptpubkey,
            generator,
            1, 0, 52,
        )
    )
    tx = wally.tx_init(2, 0, 1, 2)
    wally.tx_add_elements_raw_input(
        tx, bytes(range(32)), 0, 0xFFFFFFFD,
        None, None, None, None, None, None, None, None, None, 0,
    )
    wally.tx_add_elements_raw_output(
        tx,
        bytes.fromhex("0014" + "11" * 20),
        b"\x01" + asset_internal,
        wally.tx_confidential_value_from_satoshi(1),
        None, None, None, 0,
    )
    wally.tx_add_elements_raw_output(
        tx,
        tree.scriptpubkey,
        generator,
        value_commitment,
        bytes(wally.ec_public_key_from_private_key(LOCKUP_EPHEMERAL_SK)),
        None,
        rangeproof,
        0,
    )
    return tx


def _lockup_hex(network="mainnet", tree=None, asset_internal=None):
    tree = tree or _local_tree()
    if asset_internal is None:
        asset_internal = bytes.fromhex(LBTC_DISPLAY_HEX[network])[::-1]
    return wally.tx_to_hex(
        _blinded_lockup(tree, asset_internal), wally.WALLY_TX_FLAG_USE_WITNESS
    )


def _destination(network="mainnet"):
    bech32, blech32 = ADDRESS_HRPS[network]
    unconfidential = wally.addr_segwit_from_bytes(
        bytes.fromhex("0014" + "12" * 20), bech32, 0
    )
    return wally.confidential_addr_from_addr_segwit(
        unconfidential, bech32, blech32,
        bytes(wally.ec_public_key_from_private_key(DEST_BLINDING_SK)),
    )


def _parse(tx_hex):
    return wally.tx_from_hex(
        tx_hex, wally.WALLY_TX_FLAG_USE_ELEMENTS | wally.WALLY_TX_FLAG_USE_WITNESS
    )


def _lockup_prevout(lockup_hex):
    """(scriptpubkey, asset commitment, value commitment) straight from the lockup tx."""
    lockup = _parse(lockup_hex)
    return (
        bytes(wally.tx_get_output_script(lockup, LOCKUP_VOUT)),
        bytes(wally.tx_get_output_asset(lockup, LOCKUP_VOUT)),
        bytes(wally.tx_get_output_value(lockup, LOCKUP_VOUT)),
    )


def _witness(tx, index=0):
    return [
        bytes(wally.tx_get_input_witness(tx, index, i))
        for i in range(wally.tx_get_input_witness_num_items(tx, index))
    ]


def _unblind_destination(tx):
    """Unblind refund output 0 with the destination's blinding key."""
    nonce_hash = wally.ecdh_nonce_hash(
        bytes(wally.tx_get_output_nonce(tx, 0)), DEST_BLINDING_SK
    )
    value, asset, _abf, _vbf = wally.asset_unblind_with_nonce(
        nonce_hash,
        bytes(wally.tx_get_output_rangeproof(tx, 0)),
        bytes(wally.tx_get_output_value(tx, 0)),
        bytes(wally.tx_get_output_script(tx, 0)),
        bytes(wally.tx_get_output_asset(tx, 0)),
    )
    return value, bytes(asset)


def _discounted_vsize(tx):
    weight = wally.tx_get_weight(tx) - wally.tx_get_elements_weight_discount(tx, 0)
    return wally.tx_vsize_from_weight(weight)


def _xonly_verify(xonly: bytes, signature: bytes, message: bytes) -> bool:
    from coincurve import PublicKeyXOnly

    return PublicKeyXOnly(xonly).verify(signature, message)


class FakeBoltz:
    """Plays the provider's half of the MuSig2 cooperative refund.

    It signs with the claim key over the libwally key-path sighash of the tx
    it was sent, so it agrees with the module only if the module's own
    sighash is right.
    """

    def __init__(self, lockup_hex, *, refusal=None, fees=None, fees_error=None):
        self.prevout = _lockup_prevout(lockup_hex)
        self.refusal = refusal
        self.fees = {"L-BTC": 0.1} if fees is None else fees
        self.fees_error = fees_error
        self.posted = []
        self.fee_calls = 0

    def get_chain_fees(self):
        self.fee_calls += 1
        if self.fees_error:
            raise self.fees_error
        return self.fees

    def post_refund_signature(self, swap_id, *, pub_nonce, transaction_hex, index):
        self.posted.append(transaction_hex)
        if self.refusal:
            raise self.refusal
        tree = _local_tree()
        spk, asset, value = self.prevout
        message = keypath_sighash_wally(
            _parse(transaction_hex), index, [spk], [asset], [value],
            GENESIS_BLOCK_HASH["mainnet"],
        )
        pubkeys = [tree.claim_public_key, tree.refund_public_key]
        secnonce, server_pubnonce = boltz_refund.nonce_gen(
            CLAIM_SK, tree.claim_public_key, tree.output_key_xonly, message, None
        )
        aggnonce = boltz_refund.nonce_agg([server_pubnonce, bytes.fromhex(pub_nonce)])
        session = boltz_refund.SessionContext(
            aggnonce, pubkeys, [tree.tap_tweak], [True], message
        )
        psig = boltz_refund.musig_sign(secnonce, CLAIM_SK, session)
        return {"pubNonce": server_pubnonce.hex(), "partialSignature": psig.hex()}


class FakeBroadcast:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def __call__(self, tx_hex):
        self.sent.append(tx_hex)
        if self.error:
            raise self.error
        return bytes(wally.tx_get_txid(_parse(tx_hex)))[::-1].hex()


def _refund(client, broadcast, *, tip_height=TIMEOUT - 100, lockup_hex=None, **overrides):
    kwargs = dict(
        swap_id="swap123",
        refund_private_key=REFUND_SK.hex(),
        claim_public_key=_pub(CLAIM_SK).hex(),
        blinding_key=LOCKUP_BLINDING_SK.hex(),
        payment_hash=PAYMENT_HASH.hex(),
        timeout_block_height=TIMEOUT,
        lockup_tx_hex=lockup_hex or _lockup_hex(),
        destination_address=_destination(),
        network="mainnet",
        client=client,
        tip_height=tip_height,
        broadcast=broadcast,
        expected_amount=LOCKUP_VALUE,
    )
    kwargs.update(overrides)
    return boltz_refund.refund_submarine_swap(**kwargs)


class TestUnblindConfidentialLockup:
    """Positive unblind of a lockup blinded with a known key."""

    @pytest.mark.parametrize("network", ["mainnet", "testnet"])
    def test_unblinds_the_swap_output(self, network):
        tree = _local_tree()
        lockup_hex = _lockup_hex(network, tree)
        utxo = find_and_unblind_lockup(
            lockup_hex, tree, LOCKUP_BLINDING_SK, network, expected_amount=LOCKUP_VALUE
        )
        assert utxo.vout == LOCKUP_VOUT
        assert utxo.value == LOCKUP_VALUE
        assert utxo.asset[::-1].hex() == LBTC_DISPLAY_HEX[network]
        assert utxo.abf == LOCKUP_ABF
        assert utxo.vbf == LOCKUP_VBF
        assert utxo.scriptpubkey == tree.scriptpubkey
        assert utxo.txid == bytes(wally.tx_get_txid(_parse(lockup_hex)))
        assert (utxo.asset_commitment, utxo.value_commitment) == _lockup_prevout(
            lockup_hex
        )[1:]

    def test_rejects_the_asset_in_display_byte_order(self):
        """Pins the internal byte order of LBTC_ASSET_ID against the display hex."""
        tree = _local_tree()
        lockup_hex = _lockup_hex(
            tree=tree, asset_internal=bytes.fromhex(LBTC_DISPLAY_HEX["mainnet"])
        )
        with pytest.raises(RefundError, match="not L-BTC"):
            find_and_unblind_lockup(lockup_hex, tree, LOCKUP_BLINDING_SK, "mainnet")

    def test_rejects_the_other_networks_asset(self):
        tree = _local_tree()
        with pytest.raises(RefundError, match="not L-BTC"):
            find_and_unblind_lockup(
                _lockup_hex("testnet", tree), tree, LOCKUP_BLINDING_SK, "mainnet"
            )

    def test_wrong_blinding_key_fails_loudly(self):
        tree = _local_tree()
        with pytest.raises(RefundError, match="Could not unblind"):
            find_and_unblind_lockup(
                _lockup_hex(tree=tree), tree, bytes.fromhex("45" * 32), "mainnet"
            )

    def test_amount_mismatch_is_refused(self):
        tree = _local_tree()
        with pytest.raises(RefundError, match="refusing to build a refund"):
            find_and_unblind_lockup(
                _lockup_hex(tree=tree), tree, LOCKUP_BLINDING_SK, "mainnet",
                expected_amount=LOCKUP_VALUE + 1,
            )


class TestSighashOnConfidentialRefund:
    """The TestSighash fixture has no proofs or nonces; a real blinded refund does."""

    def test_keypath_matches_wally_with_rangeproofs_and_nonces(self):
        tree = _local_tree()
        lockup_hex = _lockup_hex(tree=tree)
        utxo = find_and_unblind_lockup(lockup_hex, tree, LOCKUP_BLINDING_SK, "mainnet")
        tx = build_refund_transaction(utxo, _destination(), 20, 0, "mainnet")
        assert wally.tx_get_output_rangeproof_len(tx, 0) > 0
        assert wally.tx_get_output_surjectionproof_len(tx, 0) > 0
        args = (
            tx, 0, [utxo.scriptpubkey], [utxo.asset_commitment],
            [utxo.value_commitment], GENESIS_BLOCK_HASH["mainnet"],
        )
        assert elements_taproot_sighash(*args) == keypath_sighash_wally(*args)


class TestRefundSubmarineSwap:
    """refund_submarine_swap end to end, with a fake provider and broadcaster."""

    def _assert_pays_destination(self, tx, result, lockup_hex):
        lockup_txid = bytes(wally.tx_get_txid(_parse(lockup_hex)))
        assert wally.tx_get_num_inputs(tx) == 1
        assert bytes(wally.tx_get_input_txhash(tx, 0)) == lockup_txid
        assert wally.tx_get_input_index(tx, 0) == LOCKUP_VOUT
        assert wally.tx_get_num_outputs(tx) == 2
        # Output 1 is Elements' explicit fee output: no script, unblinded value.
        assert not wally.tx_get_output_script_len(tx, 1)
        assert bytes(wally.tx_get_output_value(tx, 1)) == bytes(
            wally.tx_confidential_value_from_satoshi(result["fee"])
        )
        value, asset = _unblind_destination(tx)
        assert value == result["amount"] == LOCKUP_VALUE - result["fee"]
        assert asset[::-1].hex() == LBTC_DISPLAY_HEX["mainnet"]
        assert 1 <= result["fee"] <= MAX_REFUND_FEE_SATS

    def test_cooperative_refund(self):
        lockup_hex = _lockup_hex()
        client, broadcast = FakeBoltz(lockup_hex), FakeBroadcast()
        result = _refund(client, broadcast, lockup_hex=lockup_hex)

        assert result["refund_type"] == "cooperative", result.get("cooperative_error")
        assert "cooperative_error" not in result
        assert len(client.posted) == 1 and len(broadcast.sent) == 1
        tx = _parse(broadcast.sent[0])
        assert result["refund_txid"] == bytes(wally.tx_get_txid(tx))[::-1].hex()
        assert result["lockup_vout"] == LOCKUP_VOUT
        self._assert_pays_destination(tx, result, lockup_hex)

        # The provider co-signed exactly the tx that went out; only the witness differs.
        posted = _parse(client.posted[0])
        assert bytes(wally.tx_get_txid(posted)) == bytes(wally.tx_get_txid(tx))
        assert wally.tx_get_locktime(tx) == 0

        [signature] = _witness(tx)
        spk, asset, value = _lockup_prevout(lockup_hex)
        message = keypath_sighash_wally(
            tx, 0, [spk], [asset], [value], GENESIS_BLOCK_HASH["mainnet"]
        )
        tree = _local_tree()
        assert _xonly_verify(tree.output_key_xonly, signature, message)
        # The verifier is not vacuous.
        assert not _xonly_verify(tree.output_key_xonly, signature, bytes(32))

    @pytest.mark.parametrize("tip_height", [TIMEOUT, TIMEOUT + 50])
    def test_unilateral_after_cooperative_refusal(self, tip_height):
        lockup_hex = _lockup_hex()
        client = FakeBoltz(
            lockup_hex, refusal=RefundError("400: cooperative refunds are disabled")
        )
        broadcast = FakeBroadcast()
        result = _refund(
            client, broadcast, tip_height=tip_height, lockup_hex=lockup_hex, fee_rate=1.0
        )

        assert result["refund_type"] == "unilateral"
        assert "cooperative refunds are disabled" in result["cooperative_error"]
        assert len(broadcast.sent) == 1
        tx = _parse(broadcast.sent[0])
        self._assert_pays_destination(tx, result, lockup_hex)
        assert wally.tx_get_locktime(tx) == TIMEOUT
        assert wally.tx_get_input_sequence(tx, 0) == 0xFFFFFFFD

        tree = _local_tree()
        signature, leaf, control = _witness(tx)
        assert len(signature) == 64
        assert leaf == tree.refund_leaf
        assert control == tree.control_block()
        spk, asset, value = _lockup_prevout(lockup_hex)
        message = elements_taproot_sighash(
            tx, 0, [spk], [asset], [value],
            GENESIS_BLOCK_HASH["mainnet"], leaf_hash=tree.refund_leaf_hash,
        )
        assert _xonly_verify(_pub(REFUND_SK)[1:], signature, message)
        # Sized with the full script-path witness, not just the signature.
        assert result["fee"] >= -(-_discounted_vsize(tx) * 1.0 // 1)

    @pytest.mark.parametrize("tip_height", [TIMEOUT - 100, TIMEOUT + 100])
    def test_spent_lockup_is_reported_not_waited_on(self, tip_height):
        lockup_hex = _lockup_hex()
        client = FakeBoltz(
            lockup_hex, refusal=RefundError("400: no unspent lockup transaction found")
        )
        broadcast = FakeBroadcast()
        with pytest.raises(LockupSpentError, match="already spent"):
            _refund(client, broadcast, tip_height=tip_height, lockup_hex=lockup_hex)
        assert broadcast.sent == []

    def test_before_timeout_says_which_block_to_wait_for(self):
        lockup_hex = _lockup_hex()
        client = FakeBoltz(lockup_hex, refusal=RefundError("400: nope"))
        broadcast = FakeBroadcast()
        with pytest.raises(RefundError) as exc_info:
            _refund(client, broadcast, tip_height=TIMEOUT - 1, lockup_hex=lockup_hex)
        assert type(exc_info.value) is RefundError
        message = str(exc_info.value)
        assert f"block {TIMEOUT}" in message
        assert "1 blocks away" in message
        assert broadcast.sent == []

    @pytest.mark.parametrize(
        "fees,fees_error,expected_rate",
        [
            (None, RuntimeError("provider down"), 0.1),
            ({}, None, 0.1),
            ({"L-BTC": 0.3}, None, 0.3),
        ],
    )
    def test_fee_rate_source(self, monkeypatch, fees, fees_error, expected_rate):
        rates = []
        real_estimate = boltz_refund._estimate_fee

        def spy(tx, fee_rate):
            rates.append(fee_rate)
            return real_estimate(tx, fee_rate)

        monkeypatch.setattr(boltz_refund, "_estimate_fee", spy)
        lockup_hex = _lockup_hex()
        client = FakeBoltz(lockup_hex, fees=fees, fees_error=fees_error)
        result = _refund(client, FakeBroadcast(), lockup_hex=lockup_hex)

        assert client.fee_calls == 1
        assert rates == [expected_rate]
        assert result["refund_type"] == "cooperative", result.get("cooperative_error")
        assert 1 <= result["fee"] <= MAX_REFUND_FEE_SATS

    def test_explicit_fee_rate_skips_the_provider(self):
        lockup_hex = _lockup_hex()
        client = FakeBoltz(lockup_hex)
        low = _refund(client, FakeBroadcast(), lockup_hex=lockup_hex, fee_rate=0.1)
        high = _refund(client, FakeBroadcast(), lockup_hex=lockup_hex, fee_rate=1.0)
        assert client.fee_calls == 0
        assert high["fee"] > low["fee"]

    def test_dry_run_builds_a_signed_tx_without_broadcasting(self):
        lockup_hex = _lockup_hex()

        def broadcast(tx_hex):
            pytest.fail("dry_run must not broadcast")

        result = _refund(FakeBoltz(lockup_hex), broadcast, lockup_hex=lockup_hex, dry_run=True)
        assert result["dry_run"] is True
        assert "refund_txid" not in result
        tx = _parse(result["tx_hex"])
        self._assert_pays_destination(tx, result, lockup_hex)
        [signature] = _witness(tx)
        spk, asset, value = _lockup_prevout(lockup_hex)
        message = keypath_sighash_wally(
            tx, 0, [spk], [asset], [value], GENESIS_BLOCK_HASH["mainnet"]
        )
        assert _xonly_verify(_local_tree().output_key_xonly, signature, message)

    def test_broadcast_of_a_spent_lockup(self):
        lockup_hex = _lockup_hex()
        broadcast = FakeBroadcast(error=RuntimeError("bad-txns-inputs-missingorspent"))
        with pytest.raises(LockupSpentError, match="spent before this refund"):
            _refund(FakeBoltz(lockup_hex), broadcast, lockup_hex=lockup_hex)

    def test_other_broadcast_errors_propagate_unchanged(self):
        lockup_hex = _lockup_hex()
        error = RuntimeError("min relay fee not met")
        with pytest.raises(RuntimeError) as exc_info:
            _refund(FakeBoltz(lockup_hex), FakeBroadcast(error=error), lockup_hex=lockup_hex)
        assert exc_info.value is error

    def test_rejects_an_unsupported_network(self):
        with pytest.raises(RefundError, match="only supported"):
            _refund(FakeBoltz(_lockup_hex()), FakeBroadcast(), network="regtest")
