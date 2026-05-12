"""Tests for the Pix → DePix integration (Eulen API)."""

import json
import re
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aqua.pix import (
    EULEN_API_TOKEN_ENV,
    EulenClient,
    PixManager,
    PixSwap,
    format_brl,
)
from aqua.storage import Storage
from aqua.wallet import WalletManager

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)

MOCK_DEPOSIT_RESPONSE = {
    "id": "eulen_deposit_abc",
    "qrCopyPaste": "00020126580014br.gov.bcb.pix0136example-pix-copy-paste-string6304ABCD",
    "qrImageUrl": "https://depix.eulen.app/qr/eulen_deposit_abc.png",
    "expiration": "2026-05-08T23:59:59Z",
    "async": False,
}

MOCK_STATUS_PENDING = {
    "status": "pending",
    "valueInCents": 5000,
    "expiration": "2026-05-08T23:59:59Z",
}

MOCK_STATUS_SETTLED = {
    "status": "depix_sent",
    "valueInCents": 5000,
    "payerName": "FULANO DE TAL",
    "blockchainTxID": "deadbeef" * 8,
    "expiration": "2026-05-08T23:59:59Z",
}


def _mock_response(data, status=200):
    """Create a mock urllib response (context manager) that returns JSON."""
    resp = MagicMock()
    if isinstance(data, dict):
        resp.read.return_value = json.dumps(data).encode()
    else:
        resp.read.return_value = data
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture(autouse=True)
def eulen_token(monkeypatch):
    """Default to a present token; tests that need it absent call delenv themselves."""
    monkeypatch.setenv(EULEN_API_TOKEN_ENV, "test-token-xyz")


@pytest.fixture
def isolated_managers():
    """Storage + WalletManager + PixManager backed by a temp dir."""
    import aqua.tools as tools_module

    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Storage(Path(tmpdir))
        wm = WalletManager(storage=storage)
        tools_module._manager = wm
        tools_module._btc_manager = None
        tools_module._lightning_manager = None
        tools_module._pix_manager = None
        yield storage, wm
        tools_module._manager = None
        tools_module._btc_manager = None
        tools_module._lightning_manager = None
        tools_module._pix_manager = None


@pytest.fixture
def test_wallet(isolated_managers):
    storage, wm = isolated_managers
    # Pix → DePix is mainnet-only. The fixture mirrors that constraint.
    wm.import_mnemonic(TEST_MNEMONIC, "default", "mainnet")
    return storage, wm


@pytest.fixture
def testnet_wallet(isolated_managers):
    storage, wm = isolated_managers
    wm.import_mnemonic(TEST_MNEMONIC, "default", "testnet")
    return storage, wm


# ---------------------------------------------------------------------------
# PixSwap dataclass
# ---------------------------------------------------------------------------


class TestPixSwap:
    def test_to_dict_round_trip(self):
        swap = PixSwap(
            swap_id="dep_1",
            amount_cents=5000,
            wallet_name="default",
            depix_address="lq1test",
            qr_copy_paste="00020126...",
            status="pending",
            network="mainnet",
            created_at="2026-05-08T00:00:00+00:00",
        )
        data = swap.to_dict()
        round_tripped = PixSwap.from_dict(data)
        assert round_tripped == swap

    def test_from_dict_back_compat_missing_optional_fields(self):
        """A persisted record missing newer optional fields still loads."""
        data = {
            "swap_id": "dep_1",
            "amount_cents": 5000,
            "wallet_name": "default",
            "depix_address": "lq1test",
            "qr_copy_paste": "00020126...",
            "status": "pending",
            "network": "mainnet",
            "created_at": "2026-05-08T00:00:00+00:00",
        }
        swap = PixSwap.from_dict(data)
        assert swap.qr_image_url is None
        assert swap.blockchain_txid is None
        assert swap.payer_name is None
        assert swap.expiration is None


# ---------------------------------------------------------------------------
# Helper formatting
# ---------------------------------------------------------------------------


class TestFormatBRL:
    @pytest.mark.parametrize(
        "cents,expected",
        [
            (100, "R$1,00"),
            (5000, "R$50,00"),
            (12345, "R$123,45"),
            (123456, "R$1.234,56"),
            (10000000, "R$100.000,00"),
            (1, "R$0,01"),
        ],
    )
    def test_format_brl(self, cents, expected):
        assert format_brl(cents) == expected


# ---------------------------------------------------------------------------
# EulenClient HTTP behavior
# ---------------------------------------------------------------------------


class TestEulenClient:
    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.delenv(EULEN_API_TOKEN_ENV, raising=False)
        with pytest.raises(RuntimeError) as exc:
            EulenClient()
        assert EULEN_API_TOKEN_ENV in str(exc.value)

    def test_create_deposit_sends_bearer_and_nonce(self):
        client = EulenClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(MOCK_DEPOSIT_RESPONSE)
            result = client.create_deposit(5000, "lq1test")

            assert result["id"] == "eulen_deposit_abc"

            # Inspect the Request object that was passed in
            called_req = mock_urlopen.call_args.args[0]
            assert called_req.method == "POST"
            assert called_req.full_url.endswith("/deposit")
            assert called_req.headers["Authorization"] == "Bearer test-token-xyz"
            nonce = called_req.headers["X-nonce"]  # urllib lowercases the second char
            # nonce should be a uuid4 hex (32 chars, lowercase hex)
            assert re.fullmatch(r"[0-9a-f]{32}", nonce)
            body = json.loads(called_req.data.decode())
            assert body == {"amountInCents": 5000, "depixAddress": "lq1test"}

    def test_create_deposit_http_error_includes_detail(self):
        client = EulenClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            err = urllib.error.HTTPError("url", 400, "Bad Request", {}, MagicMock())
            err.read = MagicMock(
                return_value=json.dumps({"error": "invalid amount"}).encode()
            )
            mock_urlopen.side_effect = err
            with pytest.raises(RuntimeError) as exc:
                client.create_deposit(0, "lq1test")
            assert "invalid amount" in str(exc.value)
            assert "400" in str(exc.value)

    def test_create_deposit_http_error_empty_body_omits_literal_braces(self):
        # An HTTP error with an empty (or unrecognised-shape) JSON body must
        # not surface "{}" or "{'foo': 'bar'}" as a fake "error detail" —
        # that reads like an AQUA bug rather than a provider error.
        client = EulenClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            err = urllib.error.HTTPError("url", 500, "Server Error", {}, MagicMock())
            err.read = MagicMock(return_value=b"{}")
            mock_urlopen.side_effect = err
            with pytest.raises(RuntimeError) as exc:
                client.create_deposit(5000, "lq1test")
            message = str(exc.value)
            assert "500" in message
            assert "{}" not in message
            assert "{'" not in message

    def test_get_deposit_status_builds_query(self):
        client = EulenClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(MOCK_STATUS_PENDING)
            result = client.get_deposit_status("eulen_deposit_abc")

            assert result["status"] == "pending"
            called_req = mock_urlopen.call_args.args[0]
            assert called_req.method == "GET"
            assert "id=eulen_deposit_abc" in called_req.full_url

    def test_url_error_is_wrapped(self):
        client = EulenClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")
            with pytest.raises(RuntimeError) as exc:
                client.get_deposit_status("dep_x")
            assert "unreachable" in str(exc.value)


# ---------------------------------------------------------------------------
# PixManager — orchestration
# ---------------------------------------------------------------------------


class TestPixManagerCreateDeposit:
    def test_rejects_non_int_amount(self, test_wallet):
        storage, wm = test_wallet
        manager = PixManager(storage=storage, wallet_manager=wm)
        with pytest.raises(ValueError, match="integer"):
            manager.create_deposit(50.0, "default")  # type: ignore[arg-type]

    def test_rejects_bool_amount(self, test_wallet):
        storage, wm = test_wallet
        manager = PixManager(storage=storage, wallet_manager=wm)
        with pytest.raises(ValueError, match="integer"):
            manager.create_deposit(True, "default")  # type: ignore[arg-type]

    def test_rejects_below_minimum(self, test_wallet):
        storage, wm = test_wallet
        manager = PixManager(storage=storage, wallet_manager=wm)
        with pytest.raises(ValueError, match="below the minimum"):
            manager.create_deposit(50, "default")

    def test_unknown_wallet(self, isolated_managers):
        storage, wm = isolated_managers
        manager = PixManager(storage=storage, wallet_manager=wm)
        with pytest.raises(ValueError, match="not found"):
            manager.create_deposit(5000, "ghost")

    def test_missing_token_surfaces_clear_error(self, test_wallet, monkeypatch):
        storage, wm = test_wallet
        monkeypatch.delenv(EULEN_API_TOKEN_ENV, raising=False)
        manager = PixManager(storage=storage, wallet_manager=wm)
        with pytest.raises(RuntimeError) as exc:
            manager.create_deposit(5000, "default")
        assert EULEN_API_TOKEN_ENV in str(exc.value)

    def test_happy_path_persists_swap(self, test_wallet):
        storage, wm = test_wallet
        manager = PixManager(storage=storage, wallet_manager=wm)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(MOCK_DEPOSIT_RESPONSE)
            swap = manager.create_deposit(5000, "default")

        assert swap.swap_id == "eulen_deposit_abc"
        assert swap.amount_cents == 5000
        assert swap.qr_copy_paste.startswith("00020126")
        assert swap.qr_image_url == MOCK_DEPOSIT_RESPONSE["qrImageUrl"]
        assert swap.depix_address  # populated from wallet
        assert swap.status == "pending"
        # Confirm it was persisted
        loaded = storage.load_pix_swap(swap.swap_id)
        assert loaded is not None
        assert loaded.swap_id == swap.swap_id

        # Confirm the request body sent the wallet's address as depixAddress
        called_req = mock_urlopen.call_args.args[0]
        body = json.loads(called_req.data.decode())
        assert body["depixAddress"] == swap.depix_address

    def test_response_missing_required_fields_raises(self, test_wallet):
        storage, wm = test_wallet
        manager = PixManager(storage=storage, wallet_manager=wm)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response({"id": "x"})  # no qrCopyPaste
            with pytest.raises(RuntimeError, match="missing required fields"):
                manager.create_deposit(5000, "default")

    def test_rejects_testnet_wallet(self, testnet_wallet):
        # DePix is a mainnet-only Liquid asset. A testnet wallet must fail
        # before any HTTP call so the user doesn't pay a Pix charge that
        # cannot be settled.
        storage, wm = testnet_wallet
        manager = PixManager(storage=storage, wallet_manager=wm)
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(ValueError, match="mainnet"):
                manager.create_deposit(5000, "default")
            mock_urlopen.assert_not_called()


class TestPixManagerGetDepositStatus:
    def _seed_swap(self, storage, wm) -> PixSwap:
        manager = PixManager(storage=storage, wallet_manager=wm)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(MOCK_DEPOSIT_RESPONSE)
            return manager.create_deposit(5000, "default")

    def test_unknown_swap_id(self, test_wallet):
        storage, wm = test_wallet
        manager = PixManager(storage=storage, wallet_manager=wm)
        with pytest.raises(ValueError, match="not found"):
            manager.get_deposit_status("missing_id")

    def test_pending_to_settled_transition_persists(self, test_wallet):
        storage, wm = test_wallet
        seeded = self._seed_swap(storage, wm)

        manager = PixManager(storage=storage, wallet_manager=wm)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(MOCK_STATUS_SETTLED)
            result = manager.get_deposit_status(seeded.swap_id)

        assert result["status"] == "depix_sent"
        assert result["blockchain_txid"] == "deadbeef" * 8
        assert result["payer_name"] == "FULANO DE TAL"
        assert result["amount_brl"] == "R$50,00"

        reloaded = storage.load_pix_swap(seeded.swap_id)
        assert reloaded is not None
        assert reloaded.status == "depix_sent"
        assert reloaded.blockchain_txid == "deadbeef" * 8

    def test_warning_on_remote_failure(self, test_wallet):
        storage, wm = test_wallet
        seeded = self._seed_swap(storage, wm)

        manager = PixManager(storage=storage, wallet_manager=wm)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("offline")
            result = manager.get_deposit_status(seeded.swap_id)

        # Status preserved from local record; warning surfaced.
        assert result["status"] == "pending"
        assert "warning" in result
        assert "offline" in result["warning"]

    def test_programming_error_propagates(self, test_wallet):
        # Internal bugs (AttributeError, KeyError, etc.) must not be hidden
        # behind a "Could not fetch remote status" warning — they would
        # otherwise look like a transient Eulen issue.
        storage, wm = test_wallet
        seeded = self._seed_swap(storage, wm)

        manager = PixManager(storage=storage, wallet_manager=wm)
        with patch("aqua.pix.EulenClient") as mock_client_cls:
            mock_client_cls.return_value.get_deposit_status.side_effect = AttributeError("boom")
            with pytest.raises(AttributeError, match="boom"):
                manager.get_deposit_status(seeded.swap_id)


# ---------------------------------------------------------------------------
# Storage persistence
# ---------------------------------------------------------------------------


class TestPixStoragePersistence:
    @pytest.fixture
    def storage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Storage(Path(tmpdir))

    def test_save_and_load(self, storage):
        swap = PixSwap(
            swap_id="dep_1",
            amount_cents=5000,
            wallet_name="default",
            depix_address="lq1test",
            qr_copy_paste="00020126",
            status="pending",
            network="mainnet",
            created_at="2026-05-08T00:00:00+00:00",
        )
        storage.save_pix_swap(swap)
        assert (storage.pix_swaps_dir / "dep_1.json").exists()
        loaded = storage.load_pix_swap("dep_1")
        assert loaded is not None
        assert loaded.swap_id == "dep_1"

    def test_load_missing_returns_none(self, storage):
        assert storage.load_pix_swap("nonexistent") is None

    def test_list(self, storage):
        for sid in ("a_1", "b_2"):
            storage.save_pix_swap(
                PixSwap(
                    swap_id=sid,
                    amount_cents=100,
                    wallet_name="w",
                    depix_address="lq1",
                    qr_copy_paste="qr",
                    status="pending",
                    network="mainnet",
                    created_at="2026-05-08T00:00:00+00:00",
                )
            )
        listed = storage.list_pix_swaps()
        assert set(listed) == {"a_1", "b_2"}

    def test_invalid_swap_id_rejected(self, storage):
        with pytest.raises(ValueError, match="Invalid swap ID"):
            storage._pix_swap_path("../etc/passwd")

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX file permissions not enforced on Windows",
    )
    def test_file_permissions(self, storage):
        swap = PixSwap(
            swap_id="dep_perm",
            amount_cents=100,
            wallet_name="w",
            depix_address="lq1",
            qr_copy_paste="qr",
            status="pending",
            network="mainnet",
            created_at="2026-05-08T00:00:00+00:00",
        )
        storage.save_pix_swap(swap)
        path = storage.pix_swaps_dir / "dep_perm.json"
        import os

        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


class TestPixTools:
    def test_pix_receive_returns_qr_payload(self, test_wallet):
        from aqua.tools import pix_receive

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(MOCK_DEPOSIT_RESPONSE)
            result = pix_receive(amount_cents=5000)

        assert result["swap_id"] == "eulen_deposit_abc"
        assert result["qr_copy_paste"].startswith("00020126")
        assert result["qr_image_url"] == MOCK_DEPOSIT_RESPONSE["qrImageUrl"]
        assert result["amount_cents"] == 5000
        assert result["amount_brl"] == "R$50,00"
        assert "Copia e Cola" in result["message"]

    def test_pix_status_dispatches(self, test_wallet):
        from aqua.tools import pix_receive, pix_status

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(MOCK_DEPOSIT_RESPONSE)
            created = pix_receive(amount_cents=5000)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(MOCK_STATUS_SETTLED)
            status = pix_status(swap_id=created["swap_id"])

        assert status["status"] == "depix_sent"
        assert status["blockchain_txid"] == "deadbeef" * 8
