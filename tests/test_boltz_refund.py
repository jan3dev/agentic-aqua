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

from aqua.boltz_refund import (
    GENESIS_BLOCK_HASH,
    LEAF_VERSION_LIQUID,
    MAX_REFUND_FEE_SATS,
    LockupSpentError,
    LockupUtxo,
    RefundError,
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

    The two implementations differ only in the tapleaf version used to hash the
    leaf (Elements 0xc4 vs the 0xc0 wally hardcodes). Feeding this module wally's
    version makes them directly comparable, so a match proves every other part of
    the extension — spend type, leaf hash placement, key version, codeseparator.
    `TestSwapTree` pins the 0xc4 half against a real on-chain scriptPubKey.
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
        # At the nominal draft fee it gets past the guard and only fails later,
        # in blinding, which this stub's placeholder factors cannot satisfy.
        with pytest.raises(ValueError) as exc_info:
            build_refund_transaction(
                self._utxo(value=121), CONFIDENTIAL_ADDRESS, 1, 0, "mainnet"
            )
        assert "does not cover" not in str(exc_info.value)
