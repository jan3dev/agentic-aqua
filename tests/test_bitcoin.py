"""Tests for Bitcoin (BDK) wallet and btc_* / unified_balance tools."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import lwk
import pytest

from aqua_mcp.bitcoin import BitcoinWalletManager, _extract_confirmation_height
from aqua_mcp.storage import Storage, WalletData
from aqua_mcp.tools import (
    btc_address,
    btc_balance,
    btc_send,
    btc_transactions,
    get_btc_manager,
    lw_import_mnemonic,
    unified_balance,
)

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


@pytest.fixture(autouse=True)
def isolated_managers():
    """Use temp directory and reset both manager singletons."""
    import aqua_mcp.tools as tools_module

    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Storage(Path(tmpdir))
        from aqua_mcp.wallet import WalletManager

        manager = WalletManager(storage=storage)
        btc_manager = BitcoinWalletManager(storage=storage)
        tools_module._manager = manager
        tools_module._btc_manager = btc_manager
        yield manager, btc_manager
        tools_module._manager = None
        tools_module._btc_manager = None


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
        from aqua_mcp.tools import lw_address

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
            None,
        )
        assert wallet is not None
        assert network == "mainnet"

        # A second load must not fail with DataAlreadyExists.
        wallet2, network2 = btc_manager._get_wallet_with_signer(
            "signer",
            TEST_MNEMONIC,
            None,
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
        with patch.object(get_btc_manager(), "sync_wallet"):
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
        from aqua_mcp.tools import TOOLS

        assert "btc_balance" in TOOLS
        assert "btc_address" in TOOLS
        assert "btc_transactions" in TOOLS
        assert "btc_send" in TOOLS
        assert "unified_balance" in TOOLS

    def test_btc_tools_callable(self):
        """All btc_* and unified_balance are callable."""
        from aqua_mcp.tools import TOOLS

        for name in ("btc_balance", "btc_address", "btc_transactions", "btc_send", "unified_balance"):
            assert callable(TOOLS[name]), f"{name} is not callable"
