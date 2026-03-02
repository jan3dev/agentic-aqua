"""Tests for all MCP tool functions exposed by the liquid wallet server."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import lwk
import pytest

from aqua_mcp.storage import Storage, WalletData
from aqua_mcp.tools import (
    _manager,
    get_manager,
    lw_address,
    lw_balance,
    lw_export_descriptor,
    lw_generate_mnemonic,
    lw_import_descriptor,
    lw_import_mnemonic,
    lw_list_wallets,
    lw_send,
    lw_send_asset,
    lw_transactions,
)
from aqua_mcp.wallet import WalletManager

# Test mnemonic (well-known, NOT real funds)
TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


@pytest.fixture(autouse=True)
def isolated_manager():
    """Replace the global manager with one using a temp directory for every test."""
    import aqua_mcp.tools as tools_module

    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Storage(Path(tmpdir))
        manager = WalletManager(storage=storage)
        tools_module._manager = manager
        yield manager
        tools_module._manager = None


# ---------------------------------------------------------------------------
# lw_generate_mnemonic  # Significance: 5 (Essential)
# ---------------------------------------------------------------------------


class TestGenerateMnemonic:
    def test_generates_12_word_mnemonic(self):
        """Default generation returns a 12-word mnemonic."""
        result = lw_generate_mnemonic()

        assert "mnemonic" in result
        assert result["words"] == 12
        words = result["mnemonic"].split()
        assert len(words) == 12

    def test_response_contains_warning(self):
        """Result includes a security warning about the mnemonic."""
        result = lw_generate_mnemonic()
        assert "warning" in result
        assert "securely" in result["warning"].lower()

    def test_generates_unique_mnemonics(self):
        """Two consecutive generations produce different mnemonics."""
        r1 = lw_generate_mnemonic()
        r2 = lw_generate_mnemonic()
        assert r1["mnemonic"] != r2["mnemonic"]


# ---------------------------------------------------------------------------
# lw_import_mnemonic  # Significance: 5 (Essential)
# ---------------------------------------------------------------------------


class TestImportMnemonic:
    def test_import_creates_wallet(self):
        """Importing a mnemonic returns wallet metadata and persists it."""
        result = lw_import_mnemonic(
            mnemonic=TEST_MNEMONIC,
            wallet_name="test_wallet",
            network="mainnet",
        )

        assert result["wallet_name"] == "test_wallet"
        assert result["network"] == "mainnet"
        assert result["watch_only"] is False
        assert result["descriptor"].startswith("ct(")

    def test_import_with_testnet(self):
        """Importing on testnet records the correct network."""
        result = lw_import_mnemonic(
            mnemonic=TEST_MNEMONIC,
            wallet_name="testnet_wallet",
            network="testnet",
        )
        assert result["network"] == "testnet"

    def test_import_with_passphrase_encrypts(self, isolated_manager):
        """When a passphrase is given the mnemonic is stored encrypted."""
        lw_import_mnemonic(
            mnemonic=TEST_MNEMONIC,
            wallet_name="enc_wallet",
            passphrase="s3cret",
        )

        wallet = isolated_manager.storage.load_wallet("enc_wallet")
        assert wallet.encrypted_mnemonic is not None
        assert wallet.encrypted_mnemonic != TEST_MNEMONIC

    def test_import_duplicate_raises(self):
        """Importing the same wallet name twice raises ValueError."""  # Significance: 4
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="dup")

        with pytest.raises(ValueError, match="already exists"):
            lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="dup")

    def test_import_invalid_mnemonic_raises(self):
        """An invalid mnemonic is rejected by lwk."""  # Significance: 4
        with pytest.raises(Exception):
            lw_import_mnemonic(mnemonic="not a valid mnemonic phrase", wallet_name="bad")


# ---------------------------------------------------------------------------
# lw_import_descriptor  # Significance: 5 (Essential)
# ---------------------------------------------------------------------------


class TestImportDescriptor:
    def _get_descriptor(self) -> str:
        """Helper: generate a valid descriptor from a mnemonic."""
        net = lwk.Network.mainnet()
        m = lwk.Mnemonic(TEST_MNEMONIC)
        signer = lwk.Signer(m, net)
        return str(signer.wpkh_slip77_descriptor())

    def test_import_creates_watch_only(self):
        """Importing a descriptor creates a watch-only wallet."""
        desc = self._get_descriptor()
        result = lw_import_descriptor(
            descriptor=desc,
            wallet_name="watch",
            network="mainnet",
        )

        assert result["wallet_name"] == "watch"
        assert result["watch_only"] is True
        assert result["network"] == "mainnet"

    def test_import_descriptor_duplicate_raises(self):
        """Duplicate wallet name raises."""  # Significance: 4
        desc = self._get_descriptor()
        lw_import_descriptor(descriptor=desc, wallet_name="dup_wo")

        with pytest.raises(ValueError, match="already exists"):
            lw_import_descriptor(descriptor=desc, wallet_name="dup_wo")


# ---------------------------------------------------------------------------
# lw_export_descriptor  # Significance: 4 (Important)
# ---------------------------------------------------------------------------


class TestExportDescriptor:
    def test_export_returns_descriptor(self):
        """Exporting a wallet's descriptor returns the original one."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="exp")
        result = lw_export_descriptor(wallet_name="exp")

        assert result["wallet_name"] == "exp"
        assert result["descriptor"].startswith("ct(")

    def test_export_roundtrip(self):
        """Exported descriptor matches what was stored at import time."""
        import_result = lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="rt")
        export_result = lw_export_descriptor(wallet_name="rt")

        assert import_result["descriptor"] == export_result["descriptor"]

    def test_export_nonexistent_raises(self):
        """Exporting from a non-existent wallet raises."""  # Significance: 4
        with pytest.raises(ValueError, match="not found"):
            lw_export_descriptor(wallet_name="ghost")


# ---------------------------------------------------------------------------
# lw_list_wallets  # Significance: 4 (Important)
# ---------------------------------------------------------------------------


class TestListWallets:
    def test_empty_initially(self):
        """No wallets listed before any import."""
        result = lw_list_wallets()
        assert result["wallets"] == []
        assert result["count"] == 0

    def test_lists_imported_wallets(self):
        """All imported wallets appear in the listing."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="w1")

        net = lwk.Network.mainnet()
        m = lwk.Mnemonic(TEST_MNEMONIC)
        signer = lwk.Signer(m, net)
        desc = str(signer.wpkh_slip77_descriptor())
        lw_import_descriptor(descriptor=desc, wallet_name="w2")

        result = lw_list_wallets()
        assert set(result["wallets"]) == {"w1", "w2"}
        assert result["count"] == 2


# ---------------------------------------------------------------------------
# lw_address  # Significance: 5 (Essential)
# ---------------------------------------------------------------------------


class TestAddress:
    def test_generates_address(self):
        """Generates a valid Liquid address with an index."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="addr_test")
        result = lw_address(wallet_name="addr_test")

        assert "address" in result
        assert "index" in result
        assert isinstance(result["index"], int)
        assert len(result["address"]) > 0

    def test_address_is_confidential(self):
        """Generated addresses should be confidential (lq1 or VJL prefix)."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="conf_test")
        result = lw_address(wallet_name="conf_test")
        addr = result["address"]
        assert addr.startswith("lq1") or addr.startswith("VJL") or addr.startswith("VTp")

    def test_specific_index(self):
        """Requesting a specific index returns that index."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="idx_test")
        result = lw_address(wallet_name="idx_test", index=0)
        assert result["index"] == 0

    def test_same_index_same_address(self):
        """Same index always produces the same address."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="det_test")
        r1 = lw_address(wallet_name="det_test", index=0)
        r2 = lw_address(wallet_name="det_test", index=0)
        assert r1["address"] == r2["address"]

    def test_different_index_different_address(self):
        """Different indexes produce different addresses."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="diff_test")
        r0 = lw_address(wallet_name="diff_test", index=0)
        r1 = lw_address(wallet_name="diff_test", index=1)
        assert r0["address"] != r1["address"]

    def test_nonexistent_wallet_raises(self):
        """Address request for non-existent wallet raises."""  # Significance: 4
        with pytest.raises(ValueError, match="not found"):
            lw_address(wallet_name="nope")


# ---------------------------------------------------------------------------
# lw_balance  # Significance: 5 (Essential)
# ---------------------------------------------------------------------------


class TestBalance:
    def test_balance_response_structure(self, isolated_manager):
        """Balance returns the expected response structure."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="bal_test")

        with patch.object(isolated_manager, "sync_wallet"):
            result = lw_balance(wallet_name="bal_test")

        assert result["wallet_name"] == "bal_test"
        assert "balances" in result
        assert isinstance(result["balances"], list)

    def test_balance_labels_lbtc(self, isolated_manager):
        """The policy asset is labeled 'L-BTC'."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="label_test")

        policy_asset = str(lwk.Network.mainnet().policy_asset())
        mock_balance = {policy_asset: 5000}

        wollet = isolated_manager._get_wollet("label_test")
        with (
            patch.object(isolated_manager, "sync_wallet"),
            patch.object(wollet, "balance", return_value=mock_balance),
        ):
            result = lw_balance(wallet_name="label_test")

        lbtc = [b for b in result["balances"] if b["ticker"] == "L-BTC"]
        assert len(lbtc) == 1
        assert lbtc[0]["amount_sats"] == 5000

    def test_balance_non_lbtc_asset_truncated_name(self, isolated_manager):
        """Non-L-BTC assets get a truncated name (first 8 chars + '...')."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="trunc_test")

        fake_asset = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        mock_balance = {fake_asset: 100}

        wollet = isolated_manager._get_wollet("trunc_test")
        with (
            patch.object(isolated_manager, "sync_wallet"),
            patch.object(wollet, "balance", return_value=mock_balance),
        ):
            result = lw_balance(wallet_name="trunc_test")

        asset_entry = result["balances"][0]
        assert asset_entry["asset_name"] == "abcdef12..."
        assert asset_entry["amount_sats"] == 100

    def test_balance_nonexistent_wallet_raises(self):
        """Balance for non-existent wallet raises."""  # Significance: 4
        with pytest.raises(ValueError, match="not found"):
            lw_balance(wallet_name="ghost")


# ---------------------------------------------------------------------------
# lw_transactions  # Significance: 4 (Important)
# ---------------------------------------------------------------------------


class TestTransactions:
    def test_transactions_response_structure(self, isolated_manager):
        """Transactions response has the expected keys."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="tx_test")

        wollet = isolated_manager._get_wollet("tx_test")
        with (
            patch.object(isolated_manager, "sync_wallet"),
            patch.object(wollet, "transactions", return_value=[]),
        ):
            result = lw_transactions(wallet_name="tx_test")

        assert result["wallet_name"] == "tx_test"
        assert result["transactions"] == []
        assert result["count"] == 0

    def test_transactions_with_mock_data(self, isolated_manager):
        """Transaction list is properly serialized."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="txdata_test")

        policy_asset = str(lwk.Network.mainnet().policy_asset())

        mock_tx = MagicMock()
        mock_tx.txid.return_value = "abc123"
        mock_tx.height.return_value = 100
        mock_tx.timestamp.return_value = 1700000000
        mock_tx.balance.return_value = {policy_asset: -500}
        mock_tx.fee.return_value = 250

        wollet = isolated_manager._get_wollet("txdata_test")
        with (
            patch.object(isolated_manager, "sync_wallet"),
            patch.object(wollet, "transactions", return_value=[mock_tx]),
        ):
            result = lw_transactions(wallet_name="txdata_test")

        assert result["count"] == 1
        tx = result["transactions"][0]
        assert tx["txid"] == "abc123"
        assert tx["height"] == 100
        assert tx["timestamp"] == 1700000000
        assert tx["fee"] == 250
        assert "L-BTC" in tx["balance"]
        assert tx["balance"]["L-BTC"]["asset_id"] == policy_asset

    def test_transactions_respects_limit(self, isolated_manager):
        """Only `limit` transactions are returned."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="lim_test")

        policy_asset = str(lwk.Network.mainnet().policy_asset())

        def make_mock_tx(txid):
            tx = MagicMock()
            tx.txid.return_value = txid
            tx.height.return_value = 1
            tx.timestamp.return_value = 1
            tx.balance.return_value = {}
            tx.fee.return_value = 0
            return tx

        mock_txs = [make_mock_tx(f"tx{i}") for i in range(5)]

        wollet = isolated_manager._get_wollet("lim_test")
        with (
            patch.object(isolated_manager, "sync_wallet"),
            patch.object(wollet, "transactions", return_value=mock_txs),
        ):
            result = lw_transactions(wallet_name="lim_test", limit=2)

        assert result["count"] == 2

    def test_transactions_nonexistent_wallet_raises(self):
        """Transactions for non-existent wallet raises."""  # Significance: 4
        with pytest.raises(ValueError, match="not found"):
            lw_transactions(wallet_name="ghost")


# ---------------------------------------------------------------------------
# lw_send  # Significance: 5 (Essential)
# ---------------------------------------------------------------------------


class TestSend:
    DEST_ADDRESS = "lq1qqvxk052kf3qtkxmrakx50a9gc3smqad2ync54hzntjt980kfej9kkfe0247rp5h4yzmdftsahhw64uy8pzfe7cpg4fgykm7cv"

    def _setup_send_mocks(self, isolated_manager, wallet_name):
        """Helper: set up mocks for send tests, returns (mock_builder, mock_client)."""
        mock_builder = MagicMock()
        mock_pset = MagicMock()
        mock_signed = MagicMock()
        mock_tx = MagicMock()
        mock_client = MagicMock()
        mock_net = MagicMock()

        mock_net.tx_builder.return_value = mock_builder
        mock_builder.finish.return_value = mock_pset
        isolated_manager._signers[wallet_name].sign = MagicMock(return_value=mock_signed)
        mock_signed.finalize.return_value = mock_tx

        # Pre-populate wollet cache with a mock so _get_wollet doesn't call _get_network
        isolated_manager._wollets[wallet_name] = MagicMock()

        return mock_builder, mock_client, mock_net

    def test_send_success(self, isolated_manager):
        """Send returns txid on success."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="send_test")
        mock_builder, mock_client, mock_net = self._setup_send_mocks(isolated_manager, "send_test")
        mock_client.broadcast.return_value = "txid_abc123"

        with (
            patch.object(isolated_manager, "sync_wallet"),
            patch.object(isolated_manager, "_get_network", return_value=mock_net),
            patch.object(isolated_manager, "_get_client", return_value=mock_client),
        ):
            result = lw_send(
                wallet_name="send_test",
                address=self.DEST_ADDRESS,
                amount=1000,
            )

        assert result["txid"] == "txid_abc123"
        assert result["amount"] == 1000
        assert result["address"] == self.DEST_ADDRESS

    def test_send_builds_lbtc_recipient_with_lwk_address(self, isolated_manager):
        """Send converts string address to lwk.Address for add_lbtc_recipient."""  # Significance: 5
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="addr_conv")
        mock_builder, mock_client, mock_net = self._setup_send_mocks(isolated_manager, "addr_conv")
        mock_client.broadcast.return_value = "ok"

        with (
            patch.object(isolated_manager, "sync_wallet"),
            patch.object(isolated_manager, "_get_network", return_value=mock_net),
            patch.object(isolated_manager, "_get_client", return_value=mock_client),
        ):
            lw_send(wallet_name="addr_conv", address=self.DEST_ADDRESS, amount=500)

        call_args = mock_builder.add_lbtc_recipient.call_args
        address_arg = call_args[0][0]
        assert isinstance(address_arg, lwk.Address)

    def test_send_from_watch_only_raises(self, isolated_manager):
        """Cannot send from a watch-only wallet."""  # Significance: 5
        net = lwk.Network.mainnet()
        m = lwk.Mnemonic(TEST_MNEMONIC)
        signer = lwk.Signer(m, net)
        desc = str(signer.wpkh_slip77_descriptor())

        lw_import_descriptor(descriptor=desc, wallet_name="wo_send")

        with pytest.raises(ValueError, match="watch-only"):
            lw_send(wallet_name="wo_send", address=self.DEST_ADDRESS, amount=100)

    def test_send_nonexistent_wallet_raises(self):
        """Send from non-existent wallet raises."""  # Significance: 4
        with pytest.raises(ValueError, match="not found"):
            lw_send(wallet_name="ghost", address=self.DEST_ADDRESS, amount=100)

    def test_send_without_passphrase_when_encrypted_raises(self):
        """Encrypted wallet requires passphrase for send."""  # Significance: 4
        lw_import_mnemonic(
            mnemonic=TEST_MNEMONIC,
            wallet_name="enc_send",
            passphrase="pass123",
        )
        manager = get_manager()
        # Clear cached signer to simulate fresh load
        manager._signers.pop("enc_send", None)

        with pytest.raises(ValueError, match="[Pp]assphrase required"):
            lw_send(wallet_name="enc_send", address=self.DEST_ADDRESS, amount=100)


# ---------------------------------------------------------------------------
# lw_send_asset  # Significance: 5 (Essential)
# ---------------------------------------------------------------------------


class TestSendAsset:
    DEST_ADDRESS = "lq1qqvxk052kf3qtkxmrakx50a9gc3smqad2ync54hzntjt980kfej9kkfe0247rp5h4yzmdftsahhw64uy8pzfe7cpg4fgykm7cv"
    FAKE_ASSET_ID = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"

    def _setup_send_mocks(self, isolated_manager, wallet_name):
        """Helper: set up mocks for send asset tests."""
        mock_builder = MagicMock()
        mock_pset = MagicMock()
        mock_signed = MagicMock()
        mock_tx = MagicMock()
        mock_client = MagicMock()
        mock_net = MagicMock()

        mock_net.tx_builder.return_value = mock_builder
        mock_builder.finish.return_value = mock_pset
        isolated_manager._signers[wallet_name].sign = MagicMock(return_value=mock_signed)
        mock_signed.finalize.return_value = mock_tx
        isolated_manager._wollets[wallet_name] = MagicMock()

        return mock_builder, mock_client, mock_net

    def test_send_asset_success(self, isolated_manager):
        """Send asset returns txid on success."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="asset_send")
        mock_builder, mock_client, mock_net = self._setup_send_mocks(isolated_manager, "asset_send")
        mock_client.broadcast.return_value = "asset_txid_123"

        with (
            patch.object(isolated_manager, "sync_wallet"),
            patch.object(isolated_manager, "_get_network", return_value=mock_net),
            patch.object(isolated_manager, "_get_client", return_value=mock_client),
        ):
            result = lw_send_asset(
                wallet_name="asset_send",
                address=self.DEST_ADDRESS,
                amount=50000,
                asset_id=self.FAKE_ASSET_ID,
            )

        assert result["txid"] == "asset_txid_123"
        assert result["amount"] == 50000
        assert result["asset_id"] == self.FAKE_ASSET_ID
        assert result["address"] == self.DEST_ADDRESS

    def test_send_asset_calls_add_recipient(self, isolated_manager):
        """Send asset uses add_recipient (not add_lbtc_recipient)."""  # Significance: 5
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="asset_recv")
        mock_builder, mock_client, mock_net = self._setup_send_mocks(isolated_manager, "asset_recv")
        mock_client.broadcast.return_value = "ok"

        with (
            patch.object(isolated_manager, "sync_wallet"),
            patch.object(isolated_manager, "_get_network", return_value=mock_net),
            patch.object(isolated_manager, "_get_client", return_value=mock_client),
        ):
            lw_send_asset(
                wallet_name="asset_recv",
                address=self.DEST_ADDRESS,
                amount=100,
                asset_id=self.FAKE_ASSET_ID,
            )

        mock_builder.add_recipient.assert_called_once()
        mock_builder.add_lbtc_recipient.assert_not_called()

        call_args = mock_builder.add_recipient.call_args[0]
        assert isinstance(call_args[0], lwk.Address)
        assert call_args[1] == 100
        assert call_args[2] == self.FAKE_ASSET_ID

    def test_send_asset_from_watch_only_raises(self):
        """Cannot send asset from watch-only wallet."""  # Significance: 4
        net = lwk.Network.mainnet()
        m = lwk.Mnemonic(TEST_MNEMONIC)
        signer = lwk.Signer(m, net)
        desc = str(signer.wpkh_slip77_descriptor())

        lw_import_descriptor(descriptor=desc, wallet_name="wo_asset")

        with pytest.raises(ValueError, match="watch-only"):
            lw_send_asset(
                wallet_name="wo_asset",
                address=self.DEST_ADDRESS,
                amount=100,
                asset_id=self.FAKE_ASSET_ID,
            )


# ---------------------------------------------------------------------------
# WalletManager internal logic  # Significance: 4 (Important)
# ---------------------------------------------------------------------------


class TestWalletManagerInternals:
    def test_get_network_mainnet(self, isolated_manager):
        """_get_network returns mainnet correctly."""
        net = isolated_manager._get_network("mainnet")
        assert net.is_mainnet()

    def test_get_network_testnet(self, isolated_manager):
        """_get_network returns testnet correctly."""
        net = isolated_manager._get_network("testnet")
        assert not net.is_mainnet()

    def test_get_network_invalid_raises(self, isolated_manager):
        """Invalid network name raises ValueError."""  # Significance: 3
        with pytest.raises(ValueError, match="Unknown network"):
            isolated_manager._get_network("regtest")

    def test_import_caches_signer(self, isolated_manager):
        """After import, the signer is cached in memory."""
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="cache_test")
        assert "cache_test" in isolated_manager._signers

    def test_load_wallet_with_passphrase_restores_signer(self, isolated_manager):
        """Loading an encrypted wallet with passphrase restores the signer."""
        lw_import_mnemonic(
            mnemonic=TEST_MNEMONIC,
            wallet_name="restore_test",
            passphrase="mypass",
        )
        # Clear cache to simulate restart
        isolated_manager._signers.pop("restore_test", None)
        assert "restore_test" not in isolated_manager._signers

        isolated_manager.load_wallet("restore_test", passphrase="mypass")
        assert "restore_test" in isolated_manager._signers

    def test_send_no_mnemonic_no_passphrase_raises(self, isolated_manager):
        """Wallet without stored mnemonic and no passphrase cannot sign."""  # Significance: 4
        lw_import_mnemonic(mnemonic=TEST_MNEMONIC, wallet_name="no_sign")
        # Simulate: wallet saved without encrypted mnemonic but signer lost
        isolated_manager._signers.pop("no_sign", None)
        wallet = isolated_manager.storage.load_wallet("no_sign")
        assert wallet.encrypted_mnemonic is None

        with pytest.raises(ValueError, match="No mnemonic available"):
            isolated_manager.send(
                "no_sign",
                "lq1qqvxk052kf3qtkxmrakx50a9gc3smqad2ync54hzntjt980kfej9kkfe0247rp5h4yzmdftsahhw64uy8pzfe7cpg4fgykm7cv",
                100,
            )


# ---------------------------------------------------------------------------
# MCP server tool registry  # Significance: 3 (Good-to-have)
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_all_tools_registered(self):
        """All 11 MCP tools are in the TOOLS registry."""
        from aqua_mcp.tools import TOOLS

        expected = {
            "lw_generate_mnemonic",
            "lw_import_mnemonic",
            "lw_import_descriptor",
            "lw_export_descriptor",
            "lw_balance",
            "lw_address",
            "lw_transactions",
            "lw_send",
            "lw_send_asset",
            "lw_list_wallets",
            "lw_tx_status",
        }
        assert set(TOOLS.keys()) == expected

    def test_all_tools_are_callable(self):
        """Every registered tool is a callable."""
        from aqua_mcp.tools import TOOLS

        for name, fn in TOOLS.items():
            assert callable(fn), f"Tool {name} is not callable"
