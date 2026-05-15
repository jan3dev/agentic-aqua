"""Tests for Bitcoin (BDK) wallet and btc_* / unified_balance tools."""

import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import lwk
import pytest

from aqua.bitcoin import (
    BitcoinWalletManager,
    _derive_change_from_external,
    _extract_confirmation_height,
    _extract_xpub_metadata,
)
from aqua.storage import Storage, WalletData
from aqua.tools import (
    btc_address,
    btc_balance,
    btc_send,
    btc_transactions,
    get_btc_manager,
    get_manager,
    lw_import_mnemonic,
    unified_balance,
)

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


@pytest.fixture(autouse=True)
def isolated_managers():
    """Use temp directory and reset both manager singletons."""
    import gc

    import aqua.tools as tools_module

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        storage = Storage(Path(tmpdir))
        from aqua.wallet import WalletManager

        manager = WalletManager(storage=storage)
        btc_manager = BitcoinWalletManager(storage=storage)
        tools_module._manager = manager
        tools_module._btc_manager = btc_manager
        yield manager, btc_manager
        tools_module._manager = None
        tools_module._btc_manager = None
        # Release BDK SQLite handles so Windows can delete the tempdir.
        btc_manager._wallets.clear()
        btc_manager._persisters.clear()
        btc_manager._clients.clear()
        gc.collect()


# ---------------------------------------------------------------------------
# BitcoinWalletManager unit tests (with mocked BDK sync)
# ---------------------------------------------------------------------------


class TestBitcoinWalletManager:
    def test_create_wallet_requires_existing_liquid_wallet(self, isolated_managers):
        """create_wallet raises if wallet does not exist in storage."""
        _, btc_manager = isolated_managers
        with pytest.raises(ValueError, match="not found"):
            btc_manager.create_wallet(TEST_MNEMONIC, "ghost", "mainnet")

    def test_create_wallet_stores_btc_descriptors(self, isolated_managers):
        """After unified import, wallet has btc_descriptor and btc_change_descriptor."""
        manager, _ = isolated_managers
        manager.import_mnemonic(TEST_MNEMONIC, "w", "mainnet")
        btc_manager = get_btc_manager()
        btc_manager.create_wallet(TEST_MNEMONIC, "w", "mainnet")
        w = manager.storage.load_wallet("w")
        assert w.btc_descriptor is not None
        assert w.btc_descriptor.startswith("wpkh(")
        assert w.btc_change_descriptor is not None
        assert w.btc_change_descriptor.startswith("wpkh(")

    def test_get_balance_after_create(self, isolated_managers):
        """btc_balance returns structure with balance_sats (mocked sync)."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="bal", network="mainnet")
        with patch.object(get_btc_manager(), "sync_wallet"):
            result = btc_balance(wallet_name="bal")
        assert result["wallet_name"] == "bal"
        assert "balance_sats" in result
        assert "balance_btc" in result
        assert isinstance(result["balance_sats"], int)

    def test_get_address_returns_bc1(self, isolated_managers):
        """btc_address returns a Bitcoin address (bc1 for mainnet)."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="addr", network="mainnet")
        with patch.object(get_btc_manager(), "sync_wallet"):
            result = btc_address(wallet_name="addr")
        assert "address" in result
        assert "index" in result
        # Mainnet SegWit is bc1...
        assert result["address"].startswith("bc1") or result["address"].startswith("tb1")

    def test_same_mnemonic_different_addresses_btc_vs_liquid(self, isolated_managers):
        """Same mnemonic produces different receive addresses for BTC vs Liquid."""
        from aqua.tools import lw_address

        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="diff", network="mainnet")
        with patch.object(get_btc_manager(), "sync_wallet"):
            btc_addr = btc_address(wallet_name="diff")
        lw_addr = lw_address(wallet_name="diff")
        assert btc_addr["address"] != lw_addr["address"]
        assert btc_addr["address"].startswith(("bc1", "tb1"))
        assert lw_addr["address"].startswith("lq1") or lw_addr["address"].startswith(("VJL", "VTp"))

    def test_btc_transactions_structure(self, isolated_managers):
        """btc_transactions returns wallet_name, transactions list, count."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="txs", network="mainnet")
        with patch.object(get_btc_manager(), "sync_wallet"):
            result = btc_transactions(wallet_name="txs", limit=5)
        assert result["wallet_name"] == "txs"
        assert "transactions" in result
        assert isinstance(result["transactions"], list)
        assert result["count"] == len(result["transactions"])

    def test_btc_send_watch_only_raises(self, isolated_managers):
        """Cannot btc_send from a watch-only (descriptor-only) wallet."""
        manager, _ = isolated_managers
        net = lwk.Network.mainnet()
        m = lwk.Mnemonic(TEST_MNEMONIC)
        signer = lwk.Signer(m, net)
        desc = str(signer.wpkh_slip77_descriptor())
        manager.import_descriptor(desc, "watch", "mainnet")
        with pytest.raises(ValueError, match="watch-only|no Bitcoin descriptors|not found"):
            btc_send(wallet_name="watch", address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh", amount=1000)

    def test_btc_send_zero_amount_raises(self, isolated_managers):
        """Sending zero satoshis raises ValueError."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="zero_btc", network="mainnet")
        with pytest.raises(ValueError, match="Amount must be positive"):
            btc_send(wallet_name="zero_btc", address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh", amount=0)

    def test_btc_send_negative_amount_raises(self, isolated_managers):
        """Sending negative satoshis raises ValueError."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="neg_btc", network="mainnet")
        with pytest.raises(ValueError, match="Amount must be positive"):
            btc_send(wallet_name="neg_btc", address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh", amount=-500)

    def test_btc_send_zero_fee_rate_raises(self, isolated_managers):
        """Fee rate of zero raises ValueError."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="zero_fee", network="mainnet")
        with pytest.raises(ValueError, match="Fee rate must be positive"):
            btc_send(
                wallet_name="zero_fee",
                address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
                amount=1000,
                fee_rate=0,
            )

    def test_btc_send_negative_fee_rate_raises(self, isolated_managers):
        """Negative fee rate raises ValueError."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="neg_fee", network="mainnet")
        with pytest.raises(ValueError, match="Fee rate must be positive"):
            btc_send(
                wallet_name="neg_fee",
                address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
                amount=1000,
                fee_rate=-5,
            )

    def test_get_wallet_with_signer_loads_existing_persisted_wallet(self, isolated_managers):
        """Signer wallet path should load existing DB instead of recreating it."""
        manager, btc_manager = isolated_managers
        manager.import_mnemonic(TEST_MNEMONIC, "signer", "mainnet")
        btc_manager.create_wallet(TEST_MNEMONIC, "signer", "mainnet")

        wallet, network = btc_manager._get_wallet_with_signer(
            "signer",
            TEST_MNEMONIC,
        )
        assert wallet is not None
        assert network == "mainnet"

        # A second load must not fail with DataAlreadyExists.
        wallet2, network2 = btc_manager._get_wallet_with_signer(
            "signer",
            TEST_MNEMONIC,
        )
        assert wallet2 is not None
        assert network2 == "mainnet"


class TestTransactionHeightExtraction:
    def test_extract_confirmation_height_from_chain_position(self):
        class BlockId:
            height = 123

        class ConfirmationBlockTime:
            block_id = BlockId()

        class ChainPosition:
            confirmation_block_time = ConfirmationBlockTime()

        class TxRecord:
            chain_position = ChainPosition()

        assert _extract_confirmation_height(TxRecord()) == 123

    def test_extract_confirmation_height_prefers_direct_height(self):
        class TxRecord:
            height = 456
            chain_position = None

        assert _extract_confirmation_height(TxRecord()) == 456


# ---------------------------------------------------------------------------
# Unified import and unified_balance
# ---------------------------------------------------------------------------


class TestUnifiedImportAndBalance:
    def test_unified_import_creates_btc_descriptors(self, isolated_managers):
        """lw_import_mnemonic returns btc_descriptor in response."""
        result = lw_import_mnemonic(
            mnemonic=TEST_MNEMONIC,
            wallet_name="unified",
            network="mainnet",
        )
        assert result["btc_descriptor"] is not None
        assert result["btc_descriptor"].startswith("wpkh(")

    def test_unified_balance_aggregates_both_networks(self, isolated_managers):
        """unified_balance returns bitcoin and liquid sections."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="u", network="mainnet")
        with patch.object(get_btc_manager(), "sync_wallet"), patch.object(
            get_manager(), "sync_wallet"
        ):
            result = unified_balance(wallet_name="u")
        assert result["wallet_name"] == "u"
        assert "bitcoin" in result
        assert "liquid" in result
        assert "balance_sats" in result["bitcoin"]
        assert "balance_btc" in result["bitcoin"]
        assert "balances" in result["liquid"]
        assert isinstance(result["liquid"]["balances"], list)

    def test_unified_balance_wallet_without_btc_descriptors(self, isolated_managers):
        """unified_balance still works for Liquid-only wallet (e.g. watch-only)."""
        manager, _ = isolated_managers
        net = lwk.Network.mainnet()
        m = lwk.Mnemonic(TEST_MNEMONIC)
        signer = lwk.Signer(m, net)
        desc = str(signer.wpkh_slip77_descriptor())
        manager.import_descriptor(desc, "liquid_only", "mainnet")
        with patch.object(manager, "sync_wallet"):
            result = unified_balance(wallet_name="liquid_only")
        assert result["wallet_name"] == "liquid_only"
        assert result["bitcoin"] is None
        assert "bitcoin_error" in result
        assert "liquid" in result


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class TestBitcoinToolRegistry:
    def test_btc_tools_registered(self):
        """btc_* and unified_balance are in TOOLS."""
        from aqua.tools import TOOLS

        assert "btc_balance" in TOOLS
        assert "btc_address" in TOOLS
        assert "btc_transactions" in TOOLS
        assert "btc_send" in TOOLS
        assert "unified_balance" in TOOLS

    def test_btc_tools_callable(self):
        """All btc_* and unified_balance are callable."""
        from aqua.tools import TOOLS

        for name in ("btc_balance", "btc_address", "btc_transactions", "btc_send", "unified_balance"):
            assert callable(TOOLS[name]), f"{name} is not callable"


# ---------------------------------------------------------------------------
# import_descriptor / export_descriptor (BIP84 watch-only)
# ---------------------------------------------------------------------------


def _seed_btc_descriptors(isolated_managers, network: str = "mainnet"):
    """Bootstrap canonical BIP84 ext + change descriptors via the create_wallet flow."""
    manager, btc_manager = isolated_managers
    seed_name = "_seed_btc"
    manager.import_mnemonic(TEST_MNEMONIC, seed_name, network)
    btc_manager.create_wallet(TEST_MNEMONIC, seed_name, network)
    w = manager.storage.load_wallet(seed_name)
    return w.btc_descriptor, w.btc_change_descriptor


def _strip_fp(descriptor: str) -> str:
    """Strip [fp/path] prefix and trailing #checksum so bdk re-checksums on parse."""
    no_fp = re.sub(r"\[[0-9a-fA-F]{8}/[^\]]+\]", "", descriptor)
    return re.sub(r"#[a-zA-Z0-9]+$", "", no_fp)


class TestBitcoinImportExportDescriptor:
    # --- import_descriptor ---

    def test_import_descriptor_creates_watch_only_wallet(self, isolated_managers):
        """Fresh wallet from descriptor: btc_descriptor stored, watch_only=True."""
        ext, chg = _seed_btc_descriptors(isolated_managers)
        _, btc = isolated_managers
        result = btc.import_descriptor(ext, "watch", "mainnet", chg)
        assert result.name == "watch"
        assert result.btc_descriptor.startswith("wpkh(")
        assert result.btc_change_descriptor.startswith("wpkh(")
        assert result.watch_only is True
        assert result.encrypted_mnemonic is None

    def test_import_descriptor_auto_derives_change(self, isolated_managers):
        """Omitting change_descriptor auto-replaces /0/* with /1/*."""
        ext, _ = _seed_btc_descriptors(isolated_managers)
        _, btc = isolated_managers
        result = btc.import_descriptor(ext, "auto_chg", "mainnet")
        assert "/1/*" in result.btc_change_descriptor
        assert "/0/*" in result.btc_descriptor

    def test_import_descriptor_explicit_change(self, isolated_managers):
        """Explicit change_descriptor is honored."""
        ext, chg = _seed_btc_descriptors(isolated_managers)
        _, btc = isolated_managers
        result = btc.import_descriptor(ext, "explicit_chg", "mainnet", chg)
        assert "/1/*" in result.btc_change_descriptor

    def test_import_descriptor_invalid_external_raises(self, isolated_managers):
        """bdk.Descriptor() failure surfaces as Exception."""
        _, btc = isolated_managers
        with pytest.raises(Exception):
            btc.import_descriptor("not a descriptor", "bad_ext", "mainnet")

    def test_import_descriptor_invalid_change_raises(self, isolated_managers):
        """Bad change_descriptor surfaces as Exception."""
        ext, _ = _seed_btc_descriptors(isolated_managers)
        _, btc = isolated_managers
        with pytest.raises(Exception):
            btc.import_descriptor(ext, "bad_chg", "mainnet", "not a descriptor")

    def test_import_descriptor_no_change_pattern_raises(self, isolated_managers):
        """No /0/* and no change_descriptor: ValueError mentioning auto-derive."""
        _, change = _seed_btc_descriptors(isolated_managers)
        _, btc = isolated_managers
        # change descriptor has /1/*, not /0/* — auto-derivation must fail
        with pytest.raises(ValueError, match="auto-derive"):
            btc.import_descriptor(change, "no_pat", "mainnet")

    def test_import_descriptor_existing_btc_raises(self, isolated_managers):
        """Wallet already has btc_descriptor: ValueError 'already has a Bitcoin descriptor'."""
        ext, _ = _seed_btc_descriptors(isolated_managers)
        _, btc = isolated_managers
        with pytest.raises(ValueError, match="already has a Bitcoin descriptor"):
            btc.import_descriptor(ext, "_seed_btc", "mainnet")

    def test_import_descriptor_adds_to_liquid_only_wallet(self, isolated_managers):
        """Liquid-only wallet gains BTC; Liquid descriptor untouched."""
        manager, btc = isolated_managers
        net = lwk.Network.mainnet()
        m = lwk.Mnemonic(TEST_MNEMONIC)
        signer = lwk.Signer(m, net)
        liquid_desc = str(signer.wpkh_slip77_descriptor())
        manager.import_descriptor(liquid_desc, "lq_only", "mainnet")
        ext, _ = _seed_btc_descriptors(isolated_managers)
        result = btc.import_descriptor(ext, "lq_only", "mainnet")
        assert result.descriptor == liquid_desc
        assert result.btc_descriptor is not None
        assert result.btc_change_descriptor is not None

    def test_import_descriptor_accepts_descriptor_without_fingerprint(self, isolated_managers):
        """Bare 'wpkh(xpub.../0/*)' (no fingerprint prefix) imports cleanly."""
        ext, _ = _seed_btc_descriptors(isolated_managers)
        bare = _strip_fp(ext)
        _, btc = isolated_managers
        result = btc.import_descriptor(bare, "bare", "mainnet")
        assert result.btc_descriptor is not None

    def test_import_descriptor_enables_btc_address(self, isolated_managers):
        """After import, btc_address() returns a bc1/tb1 address."""
        ext, chg = _seed_btc_descriptors(isolated_managers)
        _, btc = isolated_managers
        btc.import_descriptor(ext, "addr_test", "mainnet", chg)
        with patch.object(btc, "sync_wallet"):
            addr = btc.get_address("addr_test", index=0)
        assert addr.address.startswith(("bc1", "tb1"))

    def test_import_descriptor_blocks_btc_send(self, isolated_managers):
        """btc_send on imported watch-only raises 'watch-only'."""
        ext, _ = _seed_btc_descriptors(isolated_managers)
        _, btc = isolated_managers
        btc.import_descriptor(ext, "no_send", "mainnet")
        with pytest.raises(ValueError, match="watch-only"):
            btc.send(
                "no_send",
                "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
                1000,
            )

    # --- export_descriptor ---

    def test_export_descriptor_returns_all_fields(self, isolated_managers):
        """Returns external_descriptor, change_descriptor, xpub, fingerprint, derivation_path, network, wallet_name."""
        ext, chg = _seed_btc_descriptors(isolated_managers)
        _, btc = isolated_managers
        result = btc.export_descriptor("_seed_btc")
        assert result["wallet_name"] == "_seed_btc"
        assert result["network"] == "mainnet"
        assert result["external_descriptor"] == ext
        assert result["change_descriptor"] == chg
        assert "xpub" in result
        assert "fingerprint" in result
        assert "derivation_path" in result

    def test_export_descriptor_handles_descriptor_without_fingerprint(self, isolated_managers):
        """When the wallet was imported without [fp/path], fingerprint and derivation_path are None."""
        ext, _ = _seed_btc_descriptors(isolated_managers)
        bare = _strip_fp(ext)
        _, btc = isolated_managers
        btc.import_descriptor(bare, "bare_exp", "mainnet")
        result = btc.export_descriptor("bare_exp")
        assert result["fingerprint"] is None
        assert result["derivation_path"] is None

    def test_export_descriptor_no_btc_raises(self, isolated_managers):
        """Liquid-only wallet: ValueError 'has no Bitcoin descriptors'."""
        manager, btc = isolated_managers
        net = lwk.Network.mainnet()
        m = lwk.Mnemonic(TEST_MNEMONIC)
        signer = lwk.Signer(m, net)
        desc = str(signer.wpkh_slip77_descriptor())
        manager.import_descriptor(desc, "lq_export", "mainnet")
        with pytest.raises(ValueError, match="no Bitcoin descriptors"):
            btc.export_descriptor("lq_export")

    def test_export_descriptor_unknown_wallet_raises(self, isolated_managers):
        """Wallet not found: ValueError."""
        _, btc = isolated_managers
        with pytest.raises(ValueError, match="not found"):
            btc.export_descriptor("ghost")

    # --- round-trip ---

    def test_export_then_import_round_trip_same_address(self, isolated_managers):
        """Export from wallet 'A' -> import into wallet 'B' -> peek_address(0) matches."""
        ext, chg = _seed_btc_descriptors(isolated_managers)
        _, btc = isolated_managers
        with patch.object(btc, "sync_wallet"):
            addr_a = btc.get_address("_seed_btc", index=0)
        btc.import_descriptor(ext, "round_trip", "mainnet", chg)
        with patch.object(btc, "sync_wallet"):
            addr_b = btc.get_address("round_trip", index=0)
        assert addr_a.address == addr_b.address


# ---------------------------------------------------------------------------
# Pure helpers — no fixture needed
# ---------------------------------------------------------------------------


class TestDescriptorHelpers:
    def test_derive_change_from_external_basic(self):
        """_derive_change_from_external replaces last /0/* and drops the now-stale checksum."""
        result = _derive_change_from_external("wpkh([abcd1234/84'/0'/0']xpub.../0/*)#cs")
        assert result == "wpkh([abcd1234/84'/0'/0']xpub.../1/*)"

    def test_derive_change_from_external_without_checksum(self):
        """When the input has no checksum, the result has no checksum either."""
        result = _derive_change_from_external("wpkh([abcd1234/84'/0'/0']xpub.../0/*)")
        assert result == "wpkh([abcd1234/84'/0'/0']xpub.../1/*)"

    def test_derive_change_from_external_no_pattern_raises(self):
        """Missing /0/* raises ValueError."""
        with pytest.raises(ValueError, match=r"missing '/0/\*'"):
            _derive_change_from_external("wpkh(xpub.../*)")

    def test_extract_xpub_metadata_with_prefix(self):
        """Returns xpub, fingerprint, derivation_path for [fp/path]xpub form."""
        s = "wpkh([73c5da0a/84'/0'/0']xpub6CatWdiZiodmUeTDpfoo/0/*)#cs"
        meta = _extract_xpub_metadata(s)
        assert meta["xpub"] == "xpub6CatWdiZiodmUeTDpfoo"
        assert meta["fingerprint"] == "73c5da0a"
        assert meta["derivation_path"] == "84'/0'/0'"

    def test_extract_xpub_metadata_bare(self):
        """Returns xpub only for bare-xpub descriptor; fingerprint and path are None."""
        s = "wpkh(xpub6CatWdiZiodmUeTDp/0/*)"
        meta = _extract_xpub_metadata(s)
        assert meta["xpub"] == "xpub6CatWdiZiodmUeTDp"
        assert meta["fingerprint"] is None
        assert meta["derivation_path"] is None

    def test_extract_xpub_metadata_no_match(self):
        """Returns all-None dict for non-xpub descriptor strings."""
        meta = _extract_xpub_metadata("wpkh(privkey-format/0/*)")
        assert meta == {"xpub": None, "fingerprint": None, "derivation_path": None}
