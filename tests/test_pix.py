"""Tests for the Ankara-backed PIX → DePix integration."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from aqua import tools as aqua_tools
from aqua.ankara import ANKARA_API_URL
from aqua.cli.eulen import eulen
from aqua.pix import EulenAPIError, EulenClient, PixManager, PixSwap, format_brl
from aqua.storage import Storage


def _response(data):
    response = MagicMock()
    response.read.return_value = json.dumps(data).encode()
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


class FakeJan3:
    def __init__(self, token: str = "jan3-access"):
        self.token = token

    def with_auth_retry(self, email, call):
        assert email == "person@example.com"
        return call(self.token)


class FakeWalletManager:
    def __init__(self, storage):
        self.storage = storage
        self.get_address = MagicMock(
            return_value=MagicMock(address="lq1qqexampleconfidentialaddress")
        )


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Storage(Path(tmpdir))


@pytest.fixture
def manager(storage):
    wallet = MagicMock(network="mainnet")
    storage.load_wallet = MagicMock(return_value=wallet)
    return PixManager(
        storage=storage,
        wallet_manager=FakeWalletManager(storage),
        jan3_manager=FakeJan3(),
        base_url=ANKARA_API_URL,
    )


def test_format_brl():
    assert format_brl(1) == "R$0,01"
    assert format_brl(123456) == "R$1.234,56"


def test_client_targets_ankara_and_uses_jan3_bearer():
    client = EulenClient(base_url=ANKARA_API_URL, access_token="access")
    response = _response({"session_id": "s", "status": "created"})
    with patch("urllib.request.urlopen", return_value=response) as urlopen:
        client.create_kyc_session()

    request = urlopen.call_args.args[0]
    assert request.full_url == f"{ANKARA_API_URL}/api/v1/eulen/kyc/session/"
    assert request.headers["Authorization"] == "Bearer access"
    assert json.loads(request.data) == {}
    assert "depix.eulen.app" not in request.full_url


def test_confirm_sends_only_opaque_session_id():
    client = EulenClient(base_url=ANKARA_API_URL, access_token="access")
    payload = {
        "session_id": "session-1",
        "session_status": "approved",
        "verification_status": "VERIFIED",
        "euid": "opaque",
    }
    with patch("urllib.request.urlopen", return_value=_response(payload)) as urlopen:
        client.confirm_kyc_session("session-1")

    request = urlopen.call_args.args[0]
    assert json.loads(request.data) == {"session_id": "session-1"}


def test_client_preserves_ankara_error_code():
    body = json.dumps(
        {
            "error_code": "EULEN_PROFILE_REQUIRED",
            "message": "Complete verification first.",
        }
    ).encode()
    error = urllib.error.HTTPError(
        "url", 400, "bad request", hdrs=None, fp=io.BytesIO(body)
    )
    client = EulenClient(base_url=ANKARA_API_URL, access_token="access")
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(EulenAPIError) as exc:
            client.create_deposit(5000, "lq1address")
    assert exc.value.code == "EULEN_PROFILE_REQUIRED"


def test_tool_translates_eulen_error_to_local_envelope(monkeypatch):
    manager = MagicMock()
    manager.create_kyc_session.side_effect = EulenAPIError(
        "EULEN_KYC_UNAVAILABLE",
        "KYC is temporarily unavailable.",
        status=503,
        details={"retry_after": 60},
    )
    monkeypatch.setattr(aqua_tools, "get_pix_manager", lambda: manager)

    assert aqua_tools.eulen_kyc_session("person@example.com") == {
        "error": {
            "code": "EULEN_KYC_UNAVAILABLE",
            "message": "KYC is temporarily unavailable.",
            "details": {"retry_after": 60},
        }
    }


def test_manager_kyc_flow(manager):
    responses = [
        _response({"session_id": "session-1", "status": "created", "operator_id": "op"}),
        _response(
            {
                "session_id": "session-1",
                "session_status": "approved",
                "verification_status": "VERIFIED",
                "euid": "opaque",
            }
        ),
    ]
    with patch("urllib.request.urlopen", side_effect=responses):
        started = manager.create_kyc_session("person@example.com")
        confirmed = manager.confirm_kyc_session("person@example.com", "session-1")

    assert started["operator_id"] == "op"
    assert "Hosted KYC" in started["next_step"]
    assert confirmed["session_status"] == "approved"
    assert "pix_receive" in confirmed["next_step"]


def test_create_deposit_uses_dynamic_fee_and_persists(manager, storage):
    responses = [
        _response(
            {
                "net_amount_brl_cents": 4901,
                "gross_amount_brl_cents": 5000,
                "extra_charges_brl_cents": 99,
            }
        ),
        _response(
            {
                "deposit_id": 42,
                "qr_copy_paste": "00020126PIX",
                "qr_image_url": "https://ankara.aquabtc.com/qr/42",
            }
        ),
    ]
    with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
        swap = manager.create_deposit("  Person@Example.com  ", 5000)

    assert swap.swap_id == "42"
    assert swap.account_email == "person@example.com"
    assert swap.fee_cents == 99
    assert swap.net_amount_cents == 4901
    assert storage.load_pix_swap("42") == swap
    deposit_request = urlopen.call_args_list[1].args[0]
    assert json.loads(deposit_request.data) == {
        "amount_brl_cents": 5000,
        "liquid_depix_address": "lq1qqexampleconfidentialaddress",
    }


def test_create_deposit_rejects_testnet_before_http(manager, storage):
    storage.load_wallet.return_value.network = "testnet"
    with patch("urllib.request.urlopen") as urlopen:
        with pytest.raises(ValueError, match="mainnet"):
            manager.create_deposit("person@example.com", 5000)
    urlopen.assert_not_called()


def test_status_fetches_filtered_ankara_record_and_updates_local_cache(manager, storage):
    swap = PixSwap(
        swap_id="42",
        amount_cents=5000,
        account_email="person@example.com",
        wallet_name="default",
        depix_address="lq1address",
        qr_copy_paste="pix",
        status="pending",
        network="mainnet",
        created_at="2026-09-09T00:00:00+00:00",
    )
    storage.save_pix_swap(swap)
    payload = {
        "count": 1,
        "deposits": [
            {
                "deposit_id": 42,
                "eulen_deposit_id": "eulen-42",
                "status": "depix_sent",
                "amount_brl_cents": 5000,
                "depix_address": "lq1address",
                "qr_copy_paste": "pix",
                "qr_image_url": "",
                "blockchain_tx_id": "ab" * 32,
                "created": "2026-09-09T00:00:00Z",
            }
        ],
    }
    with patch(
        "urllib.request.urlopen", return_value=_response(payload)
    ) as urlopen:
        result = manager.get_deposit_status("42", "person@example.com")

    assert urlopen.call_count == 1
    assert urlopen.call_args.args[0].full_url.endswith(
        "/api/v1/eulen/deposits/?deposit_id=42"
    )
    assert result["status"] == "depix_sent"
    assert result["blockchain_txid"] == "ab" * 32
    assert storage.load_pix_swap("42").status == "depix_sent"


def test_status_hydrates_filtered_deposit_on_local_miss(manager, storage):
    payload = {
        "count": 1,
        "deposits": [
            {
                "deposit_id": 42,
                "eulen_deposit_id": "eulen-42",
                "status": "depix_sent",
                "amount_brl_cents": 6000,
                "depix_address": "lq1address42",
                "qr_copy_paste": "pix42",
                "qr_image_url": "https://example.test/42",
                "blockchain_tx_id": "ab" * 32,
                "created": "2026-09-10T00:00:00Z",
            },
        ],
    }
    with patch(
        "urllib.request.urlopen", return_value=_response(payload)
    ) as urlopen:
        result = manager.get_deposit_status("42", "person@example.com")

    assert urlopen.call_count == 1
    assert urlopen.call_args.args[0].full_url.endswith(
        "/api/v1/eulen/deposits/?deposit_id=42"
    )
    assert result["swap_id"] == "42"
    assert result["eulen_deposit_id"] == "eulen-42"
    assert result["blockchain_txid"] == "ab" * 32
    assert storage.load_pix_swap("42").status == "depix_sent"


def test_list_deposits_forwards_filters_and_refreshes_cache(manager, storage):
    payload = {
        "count": 1,
        "deposits": [
            {
                "deposit_id": 43,
                "eulen_deposit_id": "eulen-43",
                "status": "pending",
                "amount_brl_cents": 7000,
                "depix_address": "lq1address43",
                "qr_copy_paste": "pix43",
                "qr_image_url": "",
                "blockchain_tx_id": "",
                "created": "2026-09-18T12:00:00Z",
            }
        ],
    }
    with patch(
        "urllib.request.urlopen", return_value=_response(payload)
    ) as urlopen:
        result = manager.list_deposits(
            "person@example.com",
            date_from="2026-09-14",
            date_to="2026-09-18",
            status="pending",
        )

    assert urlopen.call_args.args[0].full_url.endswith(
        "/api/v1/eulen/deposits/"
        "?date_from=2026-09-14&date_to=2026-09-18&status=pending"
    )
    assert result["count"] == 1
    assert result["deposits"][0]["created_at"] == "2026-09-18T12:00:00Z"
    assert storage.load_pix_swap("43").amount_cents == 7000


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"date_from": "18-09-2026"}, "YYYY-MM-DD"),
        (
            {"date_from": "2026-09-18", "date_to": "2026-09-17"},
            "date_to must be",
        ),
        ({"status": "unknown"}, "Unknown PIX deposit status"),
        ({"deposit_id": 0}, "positive integer"),
    ],
)
def test_list_deposits_rejects_invalid_filters_before_http(manager, kwargs, message):
    with patch("urllib.request.urlopen") as urlopen:
        with pytest.raises(ValueError, match=message):
            manager.list_deposits("person@example.com", **kwargs)
    urlopen.assert_not_called()


def test_status_local_miss_fails_when_ankara_does_not_contain_id(manager):
    with patch(
        "urllib.request.urlopen",
        return_value=_response({"count": 0, "deposits": []}),
    ) as urlopen:
        with pytest.raises(ValueError, match="not found in Ankara"):
            manager.get_deposit_status("999", "person@example.com")
    assert urlopen.call_args.args[0].full_url.endswith(
        "/api/v1/eulen/deposits/?deposit_id=999"
    )


def test_status_rejects_non_numeric_id_before_http(manager):
    with patch("urllib.request.urlopen") as urlopen:
        with pytest.raises(ValueError, match="not an Ankara deposit id"):
            manager.get_deposit_status("eulen-42", "person@example.com")
    urlopen.assert_not_called()


def test_status_rejects_mismatched_email_without_http(manager, storage):
    storage.save_pix_swap(
        PixSwap(
            swap_id="42",
            amount_cents=5000,
            account_email="other@example.com",
            wallet_name="default",
            depix_address="lq1address",
            qr_copy_paste="pix",
            status="pending",
            network="mainnet",
            created_at="2026-09-09T00:00:00+00:00",
        )
    )
    with patch("urllib.request.urlopen") as urlopen:
        with pytest.raises(ValueError, match="different JAN3 account"):
            manager.get_deposit_status("42", "person@example.com")
    urlopen.assert_not_called()


def test_pix_storage_rejects_traversal_and_uses_private_permissions(storage):
    with pytest.raises(ValueError, match="Invalid swap ID"):
        storage.load_pix_swap("../secret")
    swap = PixSwap(
        swap_id="7",
        amount_cents=100,
        account_email="person@example.com",
        wallet_name="default",
        depix_address="lq1address",
        qr_copy_paste="pix",
        status="pending",
        network="mainnet",
        created_at="2026-09-09T00:00:00+00:00",
    )
    storage.save_pix_swap(swap)
    if os.name != "nt":
        assert stat.S_IMODE((storage.pix_swaps_dir / "7.json").stat().st_mode) == 0o600


def test_cli_kyc_session_calls_tool(monkeypatch):
    monkeypatch.setattr(
        "aqua.cli.eulen.eulen_kyc_session",
        lambda email: {"email": email, "status": "created"},
    )
    result = CliRunner().invoke(
        eulen,
        ["kyc-session", "--email", "person@example.com"],
        obj=SimpleNamespace(fmt=None),
    )
    assert result.exit_code == 0
    assert "created" in result.output.lower()


def test_cli_exits_nonzero_for_pix_error_envelope(monkeypatch):
    monkeypatch.setattr(
        "aqua.cli.eulen.eulen_kyc_session",
        lambda email: {
            "error": {
                "code": "EULEN_KYC_UNAVAILABLE",
                "message": "KYC is temporarily unavailable.",
            }
        },
    )
    result = CliRunner().invoke(
        eulen,
        ["kyc-session", "--email", "person@example.com"],
        obj=SimpleNamespace(fmt=None),
    )
    assert result.exit_code == 1
    assert "EULEN_KYC_UNAVAILABLE" in result.output


def test_cli_status_requires_and_passes_email(monkeypatch):
    called = {}

    def fake_status(swap_id, email):
        called.update(swap_id=swap_id, email=email)
        return {"swap_id": swap_id, "status": "pending"}

    monkeypatch.setattr("aqua.cli.eulen.pix_status", fake_status)
    runner = CliRunner()
    missing = runner.invoke(
        eulen,
        ["status", "--swap-id", "42"],
        obj=SimpleNamespace(fmt=None),
    )
    result = runner.invoke(
        eulen,
        [
            "status",
            "--swap-id",
            "42",
            "--email",
            "person@example.com",
        ],
        obj=SimpleNamespace(fmt=None),
    )

    assert missing.exit_code == 2
    assert result.exit_code == 0
    assert called == {"swap_id": "42", "email": "person@example.com"}


def test_cli_list_passes_date_and_status_filters(monkeypatch):
    called = {}

    def fake_list(**kwargs):
        called.update(kwargs)
        return {"count": 0, "deposits": []}

    monkeypatch.setattr("aqua.cli.eulen.pix_list", fake_list)
    result = CliRunner().invoke(
        eulen,
        [
            "list",
            "--email",
            "person@example.com",
            "--date-from",
            "2026-09-14",
            "--date-to",
            "2026-09-18",
            "--status",
            "depix_sent",
        ],
        obj=SimpleNamespace(fmt=None),
    )

    assert result.exit_code == 0
    assert called == {
        "email": "person@example.com",
        "deposit_id": None,
        "date_from": "2026-09-14",
        "date_to": "2026-09-18",
        "status": "depix_sent",
    }
