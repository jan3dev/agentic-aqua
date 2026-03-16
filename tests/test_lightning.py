"""Tests for Lightning abstraction layer (new unified interface)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, UTC

import pytest

from aqua_mcp.lightning import (
    LightningSwap,
    LightningManager,
)
from aqua_mcp.storage import Storage
from aqua_mcp.tools import (
    get_manager,
    get_lightning_manager,
    lightning_receive,
    lightning_send,
    lightning_transaction_status,
)
from aqua_mcp.wallet import WalletManager, Balance


TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)

VALID_INVOICE_MAINNET = "lnbc500u1ptest_valid_invoice"
VALID_INVOICE_TESTNET = "lntb500u1ptest_valid_invoice"

MOCK_ANKARA_CREATE_RESPONSE = {
    "swap_id": "ankara_uuid_123",
    "invoice": VALID_INVOICE_MAINNET,
}

MOCK_ANKARA_VERIFY_SETTLED = {
    "settled": True,
    "preimage": "aa" * 32,
}

MOCK_ANKARA_VERIFY_PENDING = {
    "settled": False,
}

MOCK_BOLTZ_SUBMARINE_PAIRS = {
    "L-BTC": {
        "BTC": {
            "rate": 1.0,
            "fees": {"percentage": 0.1, "minerFees": 19},
            "limits": {"maximal": 25000000, "minimal": 100},
        }
    }
}

MOCK_BOLTZ_SWAP_RESPONSE = {
    "id": "boltz_swap_123",
    "address": "lq1qqexampleaddress",
    "expectedAmount": 50069,
    "claimPublicKey": "03" + "ab" * 32,
    "swapTree": {
        "claimLeaf": {"version": 192, "output": "a914..."},
        "refundLeaf": {"version": 192, "output": "b914..."},
    },
    "timeoutBlockHeight": 2500000,
}


def _mock_response(data, status=200):
    """Create a mock urllib response (context manager)."""
    resp = MagicMock()
    if isinstance(data, dict):
        resp.read.return_value = json.dumps(data).encode()
    else:
        resp.read.return_value = data
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture(autouse=True)
def isolated_managers():
    """Replace global managers with temp storage."""
    import aqua_mcp.tools as tools_module

    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Storage(Path(tmpdir))
        manager = WalletManager(storage=storage)
        tools_module._manager = manager
        tools_module._btc_manager = None
        tools_module._lightning_manager = None
        yield manager
        tools_module._manager = None
        tools_module._btc_manager = None
        tools_module._lightning_manager = None


@pytest.fixture
def test_wallet(isolated_managers):
    """Create a test wallet with balance."""
    isolated_managers.import_mnemonic(TEST_MNEMONIC, "default", "mainnet")
    return isolated_managers.load_wallet("default")


class TestLightningSwap:
    """Tests for LightningSwap dataclass."""

    def test_to_dict_includes_all_fields(self):
        """to_dict() returns complete dict including internal fields."""
        swap = LightningSwap(
            swap_id="test_123",
            swap_type="receive",
            provider="ankara",
            invoice="lnbc...",
            amount=100000,
            wallet_name="default",
            status="pending",
            network="mainnet",
            created_at="2026-03-12T00:00:00+00:00",
            receive_address="lq1...",
            preimage="aa" * 32,
            refund_private_key="secret",
        )
        data = swap.to_dict()

        assert data["swap_id"] == "test_123"
        assert data["swap_type"] == "receive"
        assert data["provider"] == "ankara"
        assert data["receive_address"] == "lq1..."
        assert data["preimage"] == "aa" * 32
        assert data["refund_private_key"] == "secret"

    def test_from_dict_roundtrip(self):
        """from_dict() reconstructs from to_dict() output."""
        swap = LightningSwap(
            swap_id="test_123",
            swap_type="send",
            provider="boltz",
            invoice="lnbc...",
            amount=100000,
            wallet_name="default",
            status="processing",
            network="mainnet",
            created_at="2026-03-12T00:00:00+00:00",
            lockup_txid="abc123",
            timeout_block_height=2500000,
            refund_private_key="secret",
        )
        data = swap.to_dict()
        restored = LightningSwap.from_dict(data)

        assert restored.swap_id == swap.swap_id
        assert restored.lockup_txid == swap.lockup_txid
        assert restored.timeout_block_height == swap.timeout_block_height

    def test_from_dict_compat_missing_optional_fields(self):
        """from_dict() handles missing optional fields with backward compat."""
        data = {
            "swap_id": "test_123",
            "swap_type": "receive",
            "provider": "ankara",
            "invoice": "lnbc...",
            "amount": 100000,
            "wallet_name": "default",
            "status": "pending",
            "network": "mainnet",
            "created_at": "2026-03-12T00:00:00+00:00",
        }
        # Missing: receive_address, preimage, lockup_txid, claim_txid, refund_private_key, timeout_block_height
        swap = LightningSwap.from_dict(data)

        assert swap.receive_address is None
        assert swap.preimage is None
        assert swap.refund_private_key is None


class TestLightningManagerReceive:
    """Tests for LightningManager.create_receive_invoice()."""

    def test_receive_happy_path(self, test_wallet, isolated_managers):
        """Happy path: valid amount, wallet exists, creates and persists swap."""
        manager = get_lightning_manager()

        with patch("aqua_mcp.lightning.AnkaraClient") as mock_ankara:
            mock_client = MagicMock()
            mock_ankara.return_value = mock_client
            mock_client.create_swap.return_value = MOCK_ANKARA_CREATE_RESPONSE

            swap = manager.create_receive_invoice(100000, "default")

            assert swap.swap_id == "ankara_uuid_123"
            assert swap.swap_type == "receive"
            assert swap.provider == "ankara"
            assert swap.status == "pending"
            assert swap.amount == 100000
            assert swap.invoice == VALID_INVOICE_MAINNET
            assert swap.receive_address is not None

            loaded = isolated_managers.storage.load_lightning_swap(swap.swap_id)
            assert loaded is not None
            assert loaded.swap_id == swap.swap_id

    def test_receive_amount_too_low(self, test_wallet):
        """Amount below minimum raises ValueError."""
        manager = get_lightning_manager()

        with pytest.raises(ValueError, match="below minimum"):
            manager.create_receive_invoice(50, "default")

    def test_receive_amount_too_high(self, test_wallet):
        """Amount above maximum raises ValueError."""
        manager = get_lightning_manager()

        with pytest.raises(ValueError, match="exceeds maximum"):
            manager.create_receive_invoice(30_000_000, "default")

    def test_receive_wallet_not_found(self):
        """Non-existent wallet raises ValueError."""
        manager = get_lightning_manager()

        with pytest.raises(ValueError, match="not found"):
            manager.create_receive_invoice(100000, "nonexistent")

    def test_receive_watch_only_wallet(self, isolated_managers):
        """Watch-only wallet raises ValueError."""
        manager = get_lightning_manager()
        isolated_managers.import_descriptor(
            "ct(slip77(abcd),elwpkh([00000000]xpub...))",
            "watch_only",
            "mainnet",
        )

        with pytest.raises(ValueError, match="cannot receive"):
            manager.create_receive_invoice(100000, "watch_only")

    def test_receive_encrypted_wallet_no_passphrase(self, isolated_managers):
        """Encrypted wallet without passphrase raises ValueError."""
        manager = get_lightning_manager()
        isolated_managers.import_mnemonic(
            TEST_MNEMONIC, "encrypted", "mainnet", passphrase="test-pass"
        )

        with pytest.raises(ValueError, match="Passphrase required"):
            manager.create_receive_invoice(100000, "encrypted")

    def test_receive_api_error_propagates(self, test_wallet):
        """Ankara API error is wrapped and propagated."""
        manager = get_lightning_manager()

        with patch("aqua_mcp.lightning.AnkaraClient") as mock_ankara:
            mock_client = MagicMock()
            mock_ankara.return_value = mock_client
            mock_client.create_swap.side_effect = RuntimeError("API error")

            with pytest.raises(RuntimeError, match="Failed to create Ankara swap"):
                manager.create_receive_invoice(100000, "default")


class TestLightningManagerSend:
    """Tests for LightningManager.pay_invoice()."""

    @patch("aqua_mcp.lightning.generate_keypair")
    @patch.object(
        type(get_manager()),
        "get_balance",
        return_value=[
            Balance(
                asset_id="policy_asset",
                asset_name="L-BTC",
                ticker="L-BTC",
                amount=100000,
            )
        ],
    )
    @patch("aqua_mcp.lightning.decode_bolt11_amount_sats")
    @patch("aqua_mcp.lightning.BoltzClient")
    @patch("aqua_mcp.wallet.WalletManager.send")
    def test_send_happy_path(
        self,
        mock_send,
        mock_boltz,
        mock_decode,
        mock_get_balance,
        mock_keygen,
        test_wallet,
        isolated_managers,
    ):
        """Happy path: valid invoice, creates swap and sends L-BTC."""
        mock_send.return_value = "lockup_txid_123"
        mock_keygen.return_value = ("privkey", "pubkey")

        manager = get_lightning_manager()

        mock_boltz_client = MagicMock()
        mock_boltz.return_value = mock_boltz_client
        mock_boltz_client.get_submarine_pairs.return_value = MOCK_BOLTZ_SUBMARINE_PAIRS
        mock_boltz_client.create_submarine_swap.return_value = MOCK_BOLTZ_SWAP_RESPONSE
        mock_decode.return_value = 50000

        swap = manager.pay_invoice(VALID_INVOICE_MAINNET, "default")

        assert swap.swap_id == "boltz_swap_123"
        assert swap.swap_type == "send"
        assert swap.provider == "boltz"
        assert swap.status == "processing"
        assert swap.lockup_txid == "lockup_txid_123"
        assert swap.refund_private_key == "privkey"

        loaded = isolated_managers.storage.load_lightning_swap(swap.swap_id)
        assert loaded is not None

    def test_send_invalid_invoice_format(self, test_wallet):
        """Invalid invoice format raises ValueError."""
        manager = get_lightning_manager()

        with pytest.raises(ValueError, match="Invalid invoice"):
            manager.pay_invoice("invalid", "default")

    def test_send_wallet_not_found(self):
        """Non-existent wallet raises ValueError."""
        manager = get_lightning_manager()

        with pytest.raises(ValueError, match="not found"):
            manager.pay_invoice(VALID_INVOICE_MAINNET, "nonexistent")

    def test_send_watch_only_wallet(self, isolated_managers):
        """Watch-only wallet raises ValueError."""
        manager = get_lightning_manager()
        isolated_managers.import_descriptor(
            "ct(slip77(abcd),elwpkh([00000000]xpub...))",
            "watch_only",
            "mainnet",
        )

        with pytest.raises(ValueError, match="cannot sign"):
            manager.pay_invoice(VALID_INVOICE_MAINNET, "watch_only")

    def test_send_amount_validation(self, test_wallet):
        """Invoice amount outside limits raises ValueError."""
        manager = get_lightning_manager()

        with patch("aqua_mcp.lightning.decode_bolt11_amount_sats") as mock_decode:
            mock_decode.return_value = 50  # Below minimum

            with pytest.raises(ValueError, match="below minimum"):
                manager.pay_invoice(VALID_INVOICE_MAINNET, "default")

    @patch.object(
        type(get_manager()),
        "get_balance",
        return_value=[],  # No balance
    )
    @patch("aqua_mcp.lightning.decode_bolt11_amount_sats")
    def test_send_insufficient_balance(
        self, mock_decode, mock_get_balance, test_wallet
    ):
        """Insufficient L-BTC balance raises ValueError before Boltz call."""
        mock_decode.return_value = 100000
        manager = get_lightning_manager()

        with pytest.raises(ValueError, match="Insufficient L-BTC balance"):
            manager.pay_invoice(VALID_INVOICE_MAINNET, "default")

    @patch.object(
        type(get_manager()),
        "get_balance",
        return_value=[
            Balance(
                asset_id="policy_asset",
                asset_name="L-BTC",
                ticker="L-BTC",
                amount=100000,
            )
        ],
    )
    @patch("aqua_mcp.lightning.decode_bolt11_amount_sats")
    @patch("aqua_mcp.lightning.BoltzClient")
    def test_send_pair_not_available(
        self, mock_boltz, mock_decode, mock_get_balance, test_wallet
    ):
        """Boltz pair unavailable raises ValueError."""
        mock_boltz_client = MagicMock()
        mock_boltz.return_value = mock_boltz_client
        mock_boltz_client.get_submarine_pairs.return_value = {}
        mock_decode.return_value = 100000

        manager = get_lightning_manager()

        with pytest.raises(ValueError, match="pair not available"):
            manager.pay_invoice(VALID_INVOICE_MAINNET, "default")

    @patch("aqua_mcp.lightning.generate_keypair")
    @patch.object(
        type(get_manager()),
        "send",
        side_effect=None,  # Will be set in method
    )
    @patch.object(
        type(get_manager()),
        "get_balance",
        return_value=[
            Balance(
                asset_id="policy_asset",
                asset_name="L-BTC",
                ticker="L-BTC",
                amount=100000,
            )
        ],
    )
    @patch("aqua_mcp.lightning.decode_bolt11_amount_sats")
    @patch("aqua_mcp.lightning.BoltzClient")
    def test_send_persists_before_sending(
        self,
        mock_boltz,
        mock_decode,
        mock_get_balance,
        mock_send,
        mock_keygen,
        test_wallet,
        isolated_managers,
    ):
        """Swap is persisted to disk BEFORE sending L-BTC."""
        send_called = False

        def mock_send_impl(wallet, addr, amount, passphrase=None):
            nonlocal send_called
            send_called = True
            swaps = isolated_managers.storage.list_lightning_swaps()
            assert len(swaps) > 0
            return "txid"

        mock_send.side_effect = mock_send_impl
        mock_keygen.return_value = ("privkey", "pubkey")

        manager = get_lightning_manager()

        mock_boltz_client = MagicMock()
        mock_boltz.return_value = mock_boltz_client
        mock_boltz_client.get_submarine_pairs.return_value = MOCK_BOLTZ_SUBMARINE_PAIRS
        mock_boltz_client.create_submarine_swap.return_value = MOCK_BOLTZ_SWAP_RESPONSE
        mock_decode.return_value = 100000

        manager.pay_invoice(VALID_INVOICE_MAINNET, "default")

        assert send_called


class TestLightningManagerReceiveStatus:
    """Tests for LightningManager.get_receive_status()."""

    def test_status_pending_to_completed_auto_claim(self, test_wallet, isolated_managers):
        """Settled swap auto-claims and updates status."""
        manager = get_lightning_manager()

        with patch("aqua_mcp.lightning.AnkaraClient") as mock_ankara:
            mock_client = MagicMock()
            mock_ankara.return_value = mock_client
            mock_client.create_swap.return_value = MOCK_ANKARA_CREATE_RESPONSE

            swap = manager.create_receive_invoice(100000, "default")
            swap_id = swap.swap_id

        with patch("aqua_mcp.lightning.AnkaraClient") as mock_ankara:
            mock_client = MagicMock()
            mock_ankara.return_value = mock_client
            mock_client.verify_swap.return_value = MOCK_ANKARA_VERIFY_SETTLED
            mock_client.claim_swap.return_value = {}

            result = manager.get_receive_status(swap_id)

            assert result["status"] == "completed"
            assert result["preimage"] == "aa" * 32
            assert "claim_warning" not in result

            loaded = isolated_managers.storage.load_lightning_swap(swap_id)
            assert loaded.status == "completed"
            assert loaded.preimage == "aa" * 32

    def test_status_pending_no_settlement(self, test_wallet, isolated_managers):
        """Pending swap returns pending status."""
        manager = get_lightning_manager()

        with patch("aqua_mcp.lightning.AnkaraClient") as mock_ankara:
            mock_client = MagicMock()
            mock_ankara.return_value = mock_client
            mock_client.create_swap.return_value = MOCK_ANKARA_CREATE_RESPONSE
            swap = manager.create_receive_invoice(100000, "default")
            swap_id = swap.swap_id

            mock_client.verify_swap.return_value = MOCK_ANKARA_VERIFY_PENDING

            result = manager.get_receive_status(swap_id)

            assert result["status"] == "pending"
            assert "preimage" not in result

    def test_status_settled_but_claim_fails_gracefully(self, test_wallet, isolated_managers):
        """Claim failure adds warning."""
        manager = get_lightning_manager()

        with patch("aqua_mcp.lightning.AnkaraClient") as mock_ankara:
            mock_client = MagicMock()
            mock_ankara.return_value = mock_client
            mock_client.create_swap.return_value = MOCK_ANKARA_CREATE_RESPONSE
            swap = manager.create_receive_invoice(100000, "default")
            swap_id = swap.swap_id

            mock_client.verify_swap.return_value = MOCK_ANKARA_VERIFY_SETTLED
            mock_client.claim_swap.side_effect = RuntimeError("Claim API error")

            result = manager.get_receive_status(swap_id)

            assert "claim_warning" in result
            assert "Claim API error" in result["claim_warning"]

    def test_status_send_swap_raises(self, test_wallet, isolated_managers):
        """Querying status of a send swap raises ValueError."""
        manager = get_lightning_manager()

        swap = LightningSwap(
            swap_id="send_swap_123",
            swap_type="send",
            provider="boltz",
            invoice=VALID_INVOICE_MAINNET,
            amount=100000,
            wallet_name="default",
            status="processing",
            network="mainnet",
            created_at=datetime.now(UTC).isoformat(),
        )
        isolated_managers.storage.save_lightning_swap(swap)

        with pytest.raises(ValueError, match="send swap"):
            manager.get_receive_status("send_swap_123")

    def test_status_not_found_raises(self):
        """Non-existent swap raises ValueError."""
        manager = get_lightning_manager()

        with pytest.raises(ValueError, match="not found"):
            manager.get_receive_status("nonexistent_swap")

    def test_status_api_down_returns_local_with_warning(self, test_wallet, isolated_managers):
        """When API is down, returns local data with warning."""
        manager = get_lightning_manager()

        with patch("aqua_mcp.lightning.AnkaraClient") as mock_ankara:
            mock_client = MagicMock()
            mock_ankara.return_value = mock_client
            mock_client.create_swap.return_value = MOCK_ANKARA_CREATE_RESPONSE
            swap = manager.create_receive_invoice(100000, "default")
            swap_id = swap.swap_id

            # Simulate API down
            mock_client.verify_swap.side_effect = RuntimeError("Connection error")

            result = manager.get_receive_status(swap_id)

            assert result["status"] == "pending"
            assert "warning" in result
            assert "Connection error" in result["warning"]


class TestLightningTools:
    """Tests for lightning_receive, lightning_send, lightning_transaction_status tools."""

    def test_lightning_receive_tool(self, test_wallet):
        """lightning_receive tool delegates to manager."""
        with patch("aqua_mcp.tools.get_lightning_manager") as mock_get:
            mock_manager = MagicMock()
            mock_get.return_value = mock_manager
            mock_manager.create_receive_invoice.return_value = LightningSwap(
                swap_id="test_123",
                swap_type="receive",
                provider="ankara",
                invoice=VALID_INVOICE_MAINNET,
                amount=100000,
                wallet_name="default",
                status="pending",
                network="mainnet",
                created_at=datetime.now(UTC).isoformat(),
                receive_address="lq1...",
            )

            result = lightning_receive(100000, "default")

            assert result["swap_id"] == "test_123"
            assert result["amount"] == 100000
            assert "message" in result

    def test_lightning_send_tool(self, test_wallet):
        """lightning_send tool delegates to manager."""
        with patch("aqua_mcp.tools.get_lightning_manager") as mock_get:
            mock_manager = MagicMock()
            mock_get.return_value = mock_manager
            mock_manager.pay_invoice.return_value = LightningSwap(
                swap_id="boltz_123",
                swap_type="send",
                provider="boltz",
                invoice=VALID_INVOICE_MAINNET,
                amount=50069,
                wallet_name="default",
                status="processing",
                network="mainnet",
                created_at=datetime.now(UTC).isoformat(),
                lockup_txid="abc123",
            )

            result = lightning_send(VALID_INVOICE_MAINNET, "default")

            assert result["swap_id"] == "boltz_123"
            assert result["lockup_txid"] == "abc123"
            assert result["status"] == "processing"

    def test_lightning_transaction_status_tool(self):
        """lightning_transaction_status tool delegates to manager."""
        with patch("aqua_mcp.tools.get_lightning_manager") as mock_get:
            mock_manager = MagicMock()
            mock_get.return_value = mock_manager
            mock_manager.get_receive_status.return_value = {
                "swap_id": "test_123",
                "status": "completed",
                "amount": 100000,
                "wallet_name": "default",
                "invoice": VALID_INVOICE_MAINNET,
            }

            result = lightning_transaction_status("test_123")

            assert result["status"] == "completed"
