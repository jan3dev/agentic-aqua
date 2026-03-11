"""Tests for Lightning (Boltz submarine swap) tools (Layers 5, 6, 7)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from aqua_mcp.boltz import SwapInfo
from aqua_mcp.storage import Storage
from aqua_mcp.tools import (
    TOOLS,
    lbtc_pay_lightning_invoice,
    lbtc_swap_lightning_status,
)
from aqua_mcp.wallet import Balance, WalletManager


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)

MOCK_SUBMARINE_PAIRS = {
    "L-BTC": {
        "BTC": {
            "rate": 1.0,
            "fees": {"percentage": 0.1, "minerFees": 19},
            "limits": {"maximal": 25000000, "minimal": 1000, "maximalZeroConf": 500000},
        }
    }
}

MOCK_SWAP_RESPONSE = {
    "id": "test_swap_123",
    "address": "lq1qqexampleaddress",
    "expectedAmount": 50069,
    "claimPublicKey": "03" + "ab" * 32,
    "swapTree": {
        "claimLeaf": {"version": 192, "output": "a914..."},
        "refundLeaf": {"version": 192, "output": "b914..."},
    },
    "timeoutBlockHeight": 2500000,
}

MOCK_CLAIM_DETAILS = {
    "preimage": "aa" * 32,
    "transactionHash": "bb" * 32,
    "pubNonce": "cc" * 33,
}

VALID_INVOICE = "lnbc500u1ptest_valid_bolt11_invoice_data"

# L-BTC asset ID on mainnet
LBTC_ASSET_ID = (
    "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_manager():
    """Replace the global manager with one using a temp directory."""
    import aqua_mcp.tools as tools_module

    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Storage(Path(tmpdir))
        manager = WalletManager(storage=storage)
        tools_module._manager = manager
        tools_module._btc_manager = None
        yield manager
        tools_module._manager = None
        tools_module._btc_manager = None


def _mock_balance(amount_sats: int) -> list[Balance]:
    """Create a mock L-BTC balance list."""
    return [
        Balance(
            asset_id=LBTC_ASSET_ID,
            asset_name="Liquid Bitcoin",
            ticker="L-BTC",
            amount=amount_sats,
            precision=8,
        )
    ]


def _save_test_swap(storage: Storage, **overrides) -> SwapInfo:
    """Save a test swap to disk and return it."""
    defaults = {
        "swap_id": "test_swap_123",
        "address": "lq1qqexampleaddress",
        "expected_amount": 50069,
        "claim_public_key": "03" + "ab" * 32,
        "swap_tree": {"claimLeaf": {}, "refundLeaf": {}},
        "timeout_block_height": 2500000,
        "refund_private_key": "aa" * 32,
        "refund_public_key": "03" + "cc" * 32,
        "invoice": VALID_INVOICE,
        "status": "swap.created",
        "network": "mainnet",
        "created_at": "2026-03-05T12:00:00",
        "lockup_txid": "dd" * 32,
    }
    defaults.update(overrides)
    swap = SwapInfo(**defaults)
    storage.save_swap(swap)
    return swap


# ===========================================================================
# Layer 5: Tool lbtc_pay_lightning_invoice
# ===========================================================================


class TestPayLightningInvoice:
    """Integration tests for lbtc_pay_lightning_invoice tool."""

    @patch("aqua_mcp.tools.BoltzClient")
    @patch("aqua_mcp.tools.generate_keypair", return_value=("aa" * 32, "03" + "cc" * 32))
    def test_pay_lightning_invoice_happy_path(
        self, mock_keypair, MockBoltz, isolated_manager
    ):
        """Full happy path - validate, create swap, send L-BTC, persist."""
        mock_client = MockBoltz.return_value
        mock_client.get_submarine_pairs.return_value = MOCK_SUBMARINE_PAIRS
        mock_client.create_submarine_swap.return_value = MOCK_SWAP_RESPONSE

        with patch.object(
            isolated_manager, "get_balance", return_value=_mock_balance(100000)
        ), patch.object(
            isolated_manager, "send", return_value="ff" * 32
        ):
            # Import a wallet first
            isolated_manager.import_mnemonic(TEST_MNEMONIC, "default", "mainnet")
            result = lbtc_pay_lightning_invoice(
                invoice=VALID_INVOICE, wallet_name="default"
            )

        assert result["swap_id"] == "test_swap_123"
        assert "lockup_txid" in result
        assert "status" in result
        assert "expected_amount" in result

    def test_pay_lightning_invoice_invalid_invoice_format(self):
        """Invoice not starting with lnbc is rejected."""
        with pytest.raises(ValueError, match="invoice"):
            lbtc_pay_lightning_invoice(invoice="invalid_invoice_here")

    def test_pay_lightning_invoice_empty_invoice_raises(self):
        """Empty invoice is rejected."""
        with pytest.raises(ValueError):
            lbtc_pay_lightning_invoice(invoice="")

    @patch("aqua_mcp.tools.BoltzClient")
    @patch("aqua_mcp.tools.generate_keypair", return_value=("aa" * 32, "03" + "cc" * 32))
    def test_pay_lightning_invoice_insufficient_balance(
        self, mock_keypair, MockBoltz, isolated_manager
    ):
        """Insufficient balance during send raises ValueError."""
        mock_client = MockBoltz.return_value
        mock_client.get_submarine_pairs.return_value = MOCK_SUBMARINE_PAIRS
        mock_client.create_submarine_swap.return_value = MOCK_SWAP_RESPONSE

        isolated_manager.import_mnemonic(TEST_MNEMONIC, "default", "mainnet")
        with patch.object(
            isolated_manager, "send",
            side_effect=ValueError("Insufficient L-BTC balance"),
        ):
            with pytest.raises(ValueError, match="[Ii]nsufficient|[Bb]alance"):
                lbtc_pay_lightning_invoice(
                    invoice=VALID_INVOICE, wallet_name="default"
                )

    def test_pay_lightning_invoice_watch_only_wallet_raises(self, isolated_manager):
        """Watch-only wallet cannot pay."""
        # Import as watch-only (descriptor only, no mnemonic)
        isolated_manager.import_descriptor(
            "ct(slip77(ab),elwpkh(xpub))", "watchonly", "mainnet"
        )
        with pytest.raises(ValueError, match="[Ww]atch.only"):
            lbtc_pay_lightning_invoice(
                invoice=VALID_INVOICE, wallet_name="watchonly"
            )

    def test_pay_lightning_invoice_passphrase_required(self, isolated_manager):
        """Encrypted wallet without passphrase raises error."""
        isolated_manager.import_mnemonic(
            TEST_MNEMONIC, "encrypted", "mainnet", passphrase="Secret-pass-123"
        )
        with pytest.raises(ValueError, match="[Pp]assphrase"):
            lbtc_pay_lightning_invoice(
                invoice=VALID_INVOICE, wallet_name="encrypted"
            )

    @patch("aqua_mcp.tools.BoltzClient")
    @patch("aqua_mcp.tools.generate_keypair", return_value=("aa" * 32, "03" + "cc" * 32))
    def test_pay_lightning_invoice_boltz_api_error(
        self, mock_keypair, MockBoltz, isolated_manager
    ):
        """Boltz API error propagates with descriptive message."""
        mock_client = MockBoltz.return_value
        mock_client.get_submarine_pairs.return_value = MOCK_SUBMARINE_PAIRS
        mock_client.create_submarine_swap.side_effect = RuntimeError(
            "Boltz API error: 400 Bad Request"
        )

        with patch.object(
            isolated_manager, "get_balance", return_value=_mock_balance(100000)
        ):
            isolated_manager.import_mnemonic(TEST_MNEMONIC, "default", "mainnet")
            with pytest.raises(Exception, match="[Bb]oltz|API|error"):
                lbtc_pay_lightning_invoice(
                    invoice=VALID_INVOICE, wallet_name="default"
                )

    @patch("aqua_mcp.tools.BoltzClient")
    @patch("aqua_mcp.tools.generate_keypair", return_value=("aa" * 32, "03" + "cc" * 32))
    def test_pay_lightning_invoice_persists_swap_before_sending(
        self, mock_keypair, MockBoltz, isolated_manager
    ):
        """Swap is saved to disk BEFORE sending L-BTC (for recovery)."""
        mock_client = MockBoltz.return_value
        mock_client.get_submarine_pairs.return_value = MOCK_SUBMARINE_PAIRS
        mock_client.create_submarine_swap.return_value = MOCK_SWAP_RESPONSE

        save_calls = []
        original_save = isolated_manager.storage.save_swap

        def tracking_save(swap):
            save_calls.append(SwapInfo(**swap.to_dict()))  # snapshot
            return original_save(swap)

        send_called = []

        def tracking_send(*args, **kwargs):
            # At this point, save_swap should have been called at least once
            send_called.append(len(save_calls))
            return "ff" * 32

        with patch.object(
            isolated_manager, "get_balance", return_value=_mock_balance(100000)
        ), patch.object(
            isolated_manager.storage, "save_swap", side_effect=tracking_save
        ), patch.object(
            isolated_manager, "send", side_effect=tracking_send
        ):
            isolated_manager.import_mnemonic(TEST_MNEMONIC, "default", "mainnet")
            lbtc_pay_lightning_invoice(
                invoice=VALID_INVOICE, wallet_name="default"
            )

        # save_swap was called before send
        assert len(send_called) > 0
        assert send_called[0] >= 1, "save_swap must be called before send"
        # First save had lockup_txid = None
        assert save_calls[0].lockup_txid is None

    @patch("aqua_mcp.tools.BoltzClient")
    @patch("aqua_mcp.tools.generate_keypair", return_value=("aa" * 32, "03" + "cc" * 32))
    def test_pay_lightning_invoice_updates_swap_with_lockup_txid(
        self, mock_keypair, MockBoltz, isolated_manager
    ):
        """After sending L-BTC, the persisted swap has lockup_txid set."""
        mock_client = MockBoltz.return_value
        mock_client.get_submarine_pairs.return_value = MOCK_SUBMARINE_PAIRS
        mock_client.create_submarine_swap.return_value = MOCK_SWAP_RESPONSE

        with patch.object(
            isolated_manager, "get_balance", return_value=_mock_balance(100000)
        ), patch.object(
            isolated_manager, "send", return_value="ff" * 32
        ):
            isolated_manager.import_mnemonic(TEST_MNEMONIC, "default", "mainnet")
            result = lbtc_pay_lightning_invoice(
                invoice=VALID_INVOICE, wallet_name="default"
            )

        # Verify swap on disk has lockup_txid
        stored = isolated_manager.storage.load_swap(result["swap_id"])
        assert stored is not None
        assert stored.lockup_txid is not None

    def test_pay_lightning_invoice_amount_below_minimum_raises(self, isolated_manager):
        """Client-side validation rejects invoice below MIN_SWAP_AMOUNT_SATS."""
        # lnbc10n = 10 nano-BTC = 1 sat → below minimum (100 sats)
        tiny_invoice = "lnbc10n1ptest0000"
        isolated_manager.import_mnemonic(TEST_MNEMONIC, "default", "mainnet")
        with pytest.raises(ValueError, match="below the minimum"):
            lbtc_pay_lightning_invoice(invoice=tiny_invoice, wallet_name="default")

    def test_pay_lightning_invoice_amount_above_maximum_raises(self, isolated_manager):
        """Client-side validation rejects invoice above MAX_SWAP_AMOUNT_SATS."""
        # lnbc300m = 300 milli-BTC = 30,000,000 sats → above 25M maximum
        huge_invoice = "lnbc300m1ptest0000"
        isolated_manager.import_mnemonic(TEST_MNEMONIC, "default", "mainnet")
        with pytest.raises(ValueError, match="exceeds the maximum"):
            lbtc_pay_lightning_invoice(invoice=huge_invoice, wallet_name="default")

    def test_pay_lightning_invoice_wallet_not_found_raises(self):
        """Non-existent wallet raises ValueError."""
        with pytest.raises(ValueError, match="not found|[Nn]ot found"):
            lbtc_pay_lightning_invoice(
                invoice=VALID_INVOICE, wallet_name="nonexistent"
            )


# ===========================================================================
# Layer 6: Tool lbtc_swap_lightning_status
# ===========================================================================


class TestSwapLightningStatus:
    """Integration tests for lbtc_swap_lightning_status tool."""

    @patch("aqua_mcp.tools.BoltzClient")
    def test_swap_lightning_status_returns_current_status(
        self, MockBoltz, isolated_manager
    ):
        """Returns combined local + remote status."""
        _save_test_swap(isolated_manager.storage)
        mock_client = MockBoltz.return_value
        mock_client.get_swap_status.return_value = {"status": "transaction.mempool"}

        result = lbtc_swap_lightning_status(swap_id="test_swap_123")

        assert result["swap_id"] == "test_swap_123"
        assert result["status"] == "transaction.mempool"
        assert "lockup_txid" in result
        assert "timeout_block_height" in result

    @patch("aqua_mcp.tools.BoltzClient")
    def test_swap_lightning_status_claimed_fetches_preimage(
        self, MockBoltz, isolated_manager
    ):
        """When claimed, fetches claim details including preimage."""
        _save_test_swap(isolated_manager.storage)
        mock_client = MockBoltz.return_value
        mock_client.get_swap_status.return_value = {"status": "transaction.claimed"}
        mock_client.get_claim_details.return_value = MOCK_CLAIM_DETAILS

        result = lbtc_swap_lightning_status(swap_id="test_swap_123")

        assert result["status"] == "transaction.claimed"
        assert result.get("preimage") == "aa" * 32

    @patch("aqua_mcp.tools.BoltzClient")
    def test_swap_lightning_status_claim_pending_is_intermediate(
        self, MockBoltz, isolated_manager
    ):
        """6.2b: claim.pending is an intermediate state — no preimage, no refund info."""
        _save_test_swap(isolated_manager.storage)
        mock_client = MockBoltz.return_value
        mock_client.get_swap_status.return_value = {"status": "transaction.claim.pending"}

        result = lbtc_swap_lightning_status(swap_id="test_swap_123")

        assert result["status"] == "transaction.claim.pending"
        assert "preimage" not in result
        assert "refund_info" not in result

    @patch("aqua_mcp.tools.BoltzClient")
    def test_swap_lightning_status_failure_returns_refund_info(
        self, MockBoltz, isolated_manager
    ):
        """Failed swap returns refund info."""
        _save_test_swap(isolated_manager.storage)
        mock_client = MockBoltz.return_value
        mock_client.get_swap_status.return_value = {"status": "invoice.failedToPay"}

        result = lbtc_swap_lightning_status(swap_id="test_swap_123")

        assert result["status"] == "invoice.failedToPay"
        assert "refund_info" in result
        # Refund info should contain timeout block height
        refund = result["refund_info"]
        assert "2500000" in str(refund) or "timeout" in str(refund).lower()

    def test_swap_lightning_status_not_found_raises(self):
        """Unknown swap_id raises ValueError."""
        with pytest.raises(ValueError, match="not found|unknown"):
            lbtc_swap_lightning_status(swap_id="nonexistent_swap_id")

    @patch("aqua_mcp.tools.BoltzClient")
    def test_swap_lightning_status_updates_stored_swap(
        self, MockBoltz, isolated_manager
    ):
        """After querying, stored swap is updated with new status."""
        _save_test_swap(isolated_manager.storage, status="swap.created")
        mock_client = MockBoltz.return_value
        mock_client.get_swap_status.return_value = {"status": "transaction.mempool"}

        lbtc_swap_lightning_status(swap_id="test_swap_123")

        stored = isolated_manager.storage.load_swap("test_swap_123")
        assert stored.status == "transaction.mempool"

    @patch("aqua_mcp.tools.BoltzClient")
    def test_swap_lightning_status_boltz_api_error_returns_local_data(
        self, MockBoltz, isolated_manager
    ):
        """If Boltz API fails, return local data with warning."""
        _save_test_swap(isolated_manager.storage, status="transaction.mempool")
        mock_client = MockBoltz.return_value
        mock_client.get_swap_status.side_effect = RuntimeError("API unreachable")

        result = lbtc_swap_lightning_status(swap_id="test_swap_123")

        assert result["swap_id"] == "test_swap_123"
        assert result["status"] == "transaction.mempool"  # local status
        assert "warning" in result


# ===========================================================================
# Layer 7: Tool registry
# ===========================================================================


class TestLightningToolRegistry:
    """Tests for Lightning tool registration in TOOLS dict."""

    def test_lightning_tools_registered_in_tools_dict(self):
        """Both new tools are in TOOLS."""
        assert "lbtc_pay_lightning_invoice" in TOOLS
        assert "lbtc_swap_lightning_status" in TOOLS

    def test_lightning_tools_are_callable(self):
        """Both tools are callable."""
        assert callable(TOOLS["lbtc_pay_lightning_invoice"])
        assert callable(TOOLS["lbtc_swap_lightning_status"])
