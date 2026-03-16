"""Tests for Ankara Lightning receive integration (Layers 1-7)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, UTC

import pytest

from aqua_mcp.ankara import AnkaraClient, AnkaraSwapInfo
from aqua_mcp.storage import Storage
from aqua_mcp.wallet import WalletManager
import urllib.error


TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)

VALID_INVOICE = "lnbc500u1ptest_valid_bolt11_invoice_data"

MOCK_ANKARA_CREATE_RESPONSE = {
    "swap_id": "ankara_test_123",
    "boltz_swap_id": "boltz_abc_456",
    "invoice": VALID_INVOICE,
}

MOCK_ANKARA_VERIFY_RESPONSE_PENDING = {
    "settled": False,
}

MOCK_ANKARA_VERIFY_RESPONSE_SETTLED = {
    "settled": True,
    "preimage": "aa" * 32,
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


@pytest.fixture
def test_wallet(isolated_manager):
    """Create a test wallet with balance."""
    isolated_manager.import_mnemonic(TEST_MNEMONIC, "default", "testnet")
    return isolated_manager.load_wallet("default")


class TestAnkaraSwapInfo:
    """Tests for AnkaraSwapInfo dataclass."""

    def test_to_dict(self):
        """Convert AnkaraSwapInfo to dict."""
        swap = AnkaraSwapInfo(
            swap_id="test_swap",
            boltz_swap_id="boltz_123",
            invoice="lnbc...",
            address="lq1test",
            amount=100000,
            wallet_name="default",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
        )
        result = swap.to_dict()
        assert result["swap_id"] == "test_swap"
        assert result["status"] == "pending"

    def test_from_dict(self):
        """Create AnkaraSwapInfo from dict."""
        data = {
            "swap_id": "test_swap",
            "boltz_swap_id": "boltz_123",
            "invoice": "lnbc...",
            "address": "lq1test",
            "amount": 100000,
            "wallet_name": "default",
            "status": "pending",
            "created_at": "2026-03-01T00:00:00+00:00",
            "preimage": None,
        }
        swap = AnkaraSwapInfo.from_dict(data)
        assert swap.swap_id == "test_swap"
        assert swap.wallet_name == "default"

    def test_with_preimage(self):
        """AnkaraSwapInfo with preimage."""
        swap = AnkaraSwapInfo(
            swap_id="test",
            boltz_swap_id="bolt",
            invoice="lnbc",
            address="lq1",
            amount=1000,
            wallet_name="w",
            status="settled",
            created_at="2026-01-01T00:00:00+00:00",
            preimage="aa" * 32,
        )
        assert swap.preimage == "aa" * 32
        assert swap.to_dict()["preimage"] == "aa" * 32


class TestAnkaraClientHTTP:
    """Tests for AnkaraClient HTTP communication."""

    def test_create_swap_success(self):
        """POST /api/v1/lightning/swaps/create/ succeeds."""
        client = AnkaraClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(MOCK_ANKARA_CREATE_RESPONSE)
            result = client.create_swap(100000, "lq1test")
            assert result["swap_id"] == "ankara_test_123"
            assert result["invoice"] == VALID_INVOICE

    def test_create_swap_http_error(self):
        """POST /api/v1/lightning/swaps/create/ handles HTTP error."""
        client = AnkaraClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_error = urllib.error.HTTPError(
                "url", 400, "Bad Request", {}, MagicMock()
            )
            mock_error.read = MagicMock(
                return_value=json.dumps({"error": "Invalid amount"}).encode()
            )
            mock_urlopen.side_effect = mock_error
            with pytest.raises(RuntimeError) as exc:
                client.create_swap(0, "lq1test")
            assert "Invalid amount" in str(exc.value)

    def test_claim_swap_success(self):
        """POST /api/v1/lightning/swaps/{swap_id}/claim/ succeeds."""
        client = AnkaraClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response({"status": "claimed"})
            result = client.claim_swap("test_swap_123")
            assert result["status"] == "claimed"

    def test_verify_swap_success(self):
        """GET /api/v1/lightning/lnurlp/verify/{swap_id} succeeds."""
        client = AnkaraClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(
                MOCK_ANKARA_VERIFY_RESPONSE_SETTLED
            )
            result = client.verify_swap("test_swap_123")
            assert result["settled"] is True
            assert result["preimage"] == "aa" * 32

    def test_api_request_url_error(self):
        """_api_request handles URLError."""
        client = AnkaraClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
            with pytest.raises(RuntimeError) as exc:
                client.create_swap(100000, "lq1test")
            assert "unreachable" in str(exc.value)


class TestAnkaraStoragePersistence:
    """Tests for Ankara swap storage operations."""

    @pytest.fixture
    def storage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Storage(Path(tmpdir))

    def test_save_ankara_swap(self, storage):
        """save_ankara_swap writes to disk."""
        swap = AnkaraSwapInfo(
            swap_id="test_123",
            boltz_swap_id="boltz_456",
            invoice="lnbc...",
            address="lq1test",
            amount=100000,
            wallet_name="default",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
        )
        storage.save_ankara_swap(swap)
        assert (storage.ankara_swaps_dir / "test_123.json").exists()

    def test_load_ankara_swap(self, storage):
        """load_ankara_swap reads from disk."""
        swap = AnkaraSwapInfo(
            swap_id="test_123",
            boltz_swap_id="boltz_456",
            invoice="lnbc...",
            address="lq1test",
            amount=100000,
            wallet_name="default",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
        )
        storage.save_ankara_swap(swap)
        loaded = storage.load_ankara_swap("test_123")
        assert loaded is not None
        assert loaded.swap_id == "test_123"
        assert loaded.wallet_name == "default"

    def test_load_ankara_swap_not_found(self, storage):
        """load_ankara_swap returns None for missing swap."""
        result = storage.load_ankara_swap("nonexistent")
        assert result is None

    def test_list_ankara_swaps(self, storage):
        """list_ankara_swaps returns all swap IDs."""
        swap1 = AnkaraSwapInfo(
            swap_id="swap_1",
            boltz_swap_id="bolt_1",
            invoice="lnbc1",
            address="lq1a",
            amount=1000,
            wallet_name="w1",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
        )
        swap2 = AnkaraSwapInfo(
            swap_id="swap_2",
            boltz_swap_id="bolt_2",
            invoice="lnbc2",
            address="lq1b",
            amount=2000,
            wallet_name="w2",
            status="settled",
            created_at="2026-03-02T00:00:00+00:00",
        )
        storage.save_ankara_swap(swap1)
        storage.save_ankara_swap(swap2)
        swaps = storage.list_ankara_swaps()
        assert len(swaps) == 2
        assert "swap_1" in swaps
        assert "swap_2" in swaps

    def test_swap_file_permissions(self, storage):
        """Ankara swap files have restricted permissions (0o600)."""
        swap = AnkaraSwapInfo(
            swap_id="test_123",
            boltz_swap_id="boltz_456",
            invoice="lnbc...",
            address="lq1test",
            amount=100000,
            wallet_name="default",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
        )
        storage.save_ankara_swap(swap)
        path = storage.ankara_swaps_dir / "test_123.json"
        assert path.exists()
        import os
        stat_info = os.stat(path)
        mode = stat_info.st_mode & 0o777
        assert mode == 0o600


