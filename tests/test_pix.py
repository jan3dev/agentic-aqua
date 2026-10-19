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


def _swap(**overrides) -> PixSwap:
    fields = {
        "swap_id": "42",
        "amount_cents": 5000,
        "account_email": "person@example.com",
        "wallet_name": "default",
        "depix_address": "lq1address",
        "qr_copy_paste": "pix",
        "status": "pending",
        "network": "mainnet",
        "created_at": "2026-09-09T00:00:00+00:00",
    }
    fields.update(overrides)
    return PixSwap(**fields)


def test_status_response_unknown_status_uses_fallback():
    response = PixManager._status_response(_swap(status="settlement_scheduled"))
    assert response["status"] == "settlement_scheduled"
    assert response["deposit_id"] == 42
    assert response["message"] == "PIX deposit status is settlement_scheduled."


def test_status_response_rejects_non_numeric_swap_id():
    with pytest.raises(ValueError, match="not an Ankara deposit id") as exc:
        PixManager._status_response(_swap(swap_id="not-a-number"))
    assert "invalid literal" not in str(exc.value)


def test_format_brl():
    assert format_brl(1) == "R$0,01"
    assert format_brl(123456) == "R$1.234,56"


def test_client_targets_ankara_and_uses_jan3_bearer():
    client = EulenClient(base_url=ANKARA_API_URL, access_token="access")
    response = _response({"session_id": "s", "status": "created"})
    with patch("urllib.request.urlopen", return_value=response) as urlopen:
        client.create_kyc_session(["otp"])

    request = urlopen.call_args.args[0]
    assert request.full_url == f"{ANKARA_API_URL}/api/v1/eulen/kyc/session/"
    assert request.headers["Authorization"] == "Bearer access"
    assert json.loads(request.data) == {"verification_methods": ["otp"]}
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
        _response(
            {
                "session_id": "session-1",
                "status": "created",
                "verification_url": "https://verify.example/s1",
                "verification_methods": ["otp"],
            }
        ),
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

    assert started["verification_url"] == "https://verify.example/s1"
    assert started["verification_methods"] == ["otp"]
    assert "default browser" in started["next_step"]
    assert "euid" not in started
    assert confirmed["session_status"] == "approved"
    assert confirmed["verification_status"] == "VERIFIED"
    assert confirmed["is_final"] is True
    assert confirmed["is_success"] is True
    assert "euid" not in confirmed
    assert "pix_receive" in confirmed["next_step"]


def test_kyc_responses_drop_fields_the_agent_must_not_see(manager):
    responses = [
        _response(
            {
                "session_id": "session-1",
                "status": "created",
                "verification_url": "https://verify.example/s1",
                "verification_methods": ["otp"],
                "euid": "opaque",
                "cpf": "00000000000",
                "customer_document": "rg",
                "internal_provider_token": "secret",
            }
        ),
        _response(
            {
                "session_id": "session-1",
                "session_status": "approved",
                "verification_status": "VERIFIED",
                "euid": "opaque",
                "cpf": "00000000000",
                "internal_provider_token": "secret",
            }
        ),
    ]
    with patch("urllib.request.urlopen", side_effect=responses):
        started = manager.create_kyc_session("person@example.com")
        confirmed = manager.confirm_kyc_session("person@example.com", "session-1")

    leaked = {"euid", "cpf", "customer_document", "internal_provider_token"}
    assert leaked.isdisjoint(started)
    assert leaked.isdisjoint(confirmed)
    assert set(started) == {
        "session_id",
        "status",
        "verification_url",
        "verification_methods",
        "email",
        "next_step",
    }
    assert set(confirmed) == {
        "session_id",
        "session_status",
        "verification_status",
        "is_final",
        "is_success",
        "poll_after_seconds",
        "email",
        "next_step",
    }


def test_approved_without_verified_profile_asks_to_retry(manager):
    payload = {
        "session_id": "session-1",
        "session_status": "approved",
        "verification_status": "PENDING",
    }
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        confirmed = manager.confirm_kyc_session("person@example.com", "session-1")

    assert confirmed["verification_status"] == "PENDING"
    assert confirmed["is_final"] is False
    assert confirmed["is_success"] is False
    assert "Retry eulen_kyc_confirm" in confirmed["next_step"]
    assert "call pix_receive" not in confirmed["next_step"]


def _http_error(status, body, headers=None):
    from email.message import Message

    hdrs = Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    return urllib.error.HTTPError(
        "url", status, "error", hdrs=hdrs, fp=io.BytesIO(json.dumps(body).encode())
    )


def test_pix_session_sends_amount_and_wallet_address(manager):
    payload = {
        "session_id": "session-1",
        "status": "created",
        "verification_url": "https://verify.example/s1",
        "verification_methods": ["pix", "otp"],
    }
    with patch("urllib.request.urlopen", return_value=_response(payload)) as urlopen:
        started = manager.create_kyc_session(
            "person@example.com",
            verification_methods=["pix", "otp"],
            amount_cents=1000,
        )

    assert json.loads(urlopen.call_args.args[0].data) == {
        "verification_methods": ["pix", "otp"],
        "amount_brl_cents": 1000,
        "destination_wallet": "lq1qqexampleconfidentialaddress",
    }
    assert started["destination_wallet"] == "lq1qqexampleconfidentialaddress"
    assert started["amount_brl"] == "R$10,00"


def test_pix_session_retry_reuses_destination_address(manager, storage):
    payload = {
        "session_id": "session-1",
        "status": "created",
        "verification_url": "https://verify.example/s1",
    }
    with patch(
        "urllib.request.urlopen",
        side_effect=[_response(payload), _response(payload)],
    ) as urlopen:
        for _ in range(2):
            manager.create_kyc_session(
                "person@example.com", verification_methods=["pix"], amount_cents=1000
            )

    assert manager.wallet_manager.get_address.call_count == 1
    bodies = [json.loads(call.args[0].data) for call in urlopen.call_args_list]
    assert bodies[0] == bodies[1]
    if os.name != "nt":
        path = storage.base_dir / "pix_kyc_addresses.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pix_session_expired_reservation_mints_new_address(manager, storage):
    storage.save_pix_kyc_addresses(
        {
            "person@example.com|default": {
                "address": "lq1old",
                "reserved_at": "2020-01-01T00:00:00+00:00",
            }
        }
    )
    payload = {
        "session_id": "session-1",
        "status": "created",
        "verification_url": "https://verify.example/s1",
    }
    with patch("urllib.request.urlopen", return_value=_response(payload)) as urlopen:
        manager.create_kyc_session(
            "person@example.com", verification_methods=["pix"], amount_cents=1000
        )

    body = json.loads(urlopen.call_args.args[0].data)
    assert body["destination_wallet"] == "lq1qqexampleconfidentialaddress"


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"verification_methods": []}, "non-empty"),
        ({"verification_methods": ["sms"]}, "Unknown verification method"),
        ({"verification_methods": ["pix"]}, "amount_cents is required"),
        ({"verification_methods": ["pix"], "amount_cents": 0}, "amount_cents is required"),
        (
            {"verification_methods": ["pix"], "amount_cents": 10_000_001},
            "at most",
        ),
        ({"verification_methods": ["otp"], "amount_cents": 1000}, "only accepted"),
    ],
)
def test_kyc_session_rejects_invalid_parameters(manager, kwargs, match):
    with patch("urllib.request.urlopen") as urlopen:
        with pytest.raises(ValueError, match=match):
            manager.create_kyc_session("person@example.com", **kwargs)
    urlopen.assert_not_called()
    manager.wallet_manager.get_address.assert_not_called()


def test_kyc_session_already_verified_is_reported(manager):
    error = _http_error(
        409,
        {"error_code": "EULEN_KYC_ALREADY_VERIFIED", "message": "Already verified."},
    )
    with patch("urllib.request.urlopen", side_effect=error):
        result = manager.create_kyc_session("person@example.com")

    assert result["status"] == "already_verified"
    assert "pix_receive" in result["next_step"]


def test_kyc_session_missing_verification_url_raises(manager):
    payload = {"session_id": "session-1", "status": "created"}
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        with pytest.raises(ValueError, match="missing required fields"):
            manager.create_kyc_session("person@example.com")


@pytest.mark.parametrize(
    "status, is_final, is_success, poll_after, hint",
    [
        ("created", False, False, 15, "15-20 seconds"),
        ("under_review", False, False, None, "NOT an approval"),
        ("rejected", True, False, None, "new session"),
        ("expired", True, False, None, "new eulen_kyc_session"),
        ("not_found", True, False, None, "new eulen_kyc_session"),
        ("identity_conflict", True, False, None, "support"),
    ],
)
def test_confirm_maps_every_session_status(
    manager, status, is_final, is_success, poll_after, hint
):
    payload = {
        "session_id": "session-1",
        "session_status": status,
        "verification_status": None,
        "euid": None,
    }
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        confirmed = manager.confirm_kyc_session("person@example.com", "session-1")

    assert confirmed["is_final"] is is_final
    assert confirmed["is_success"] is is_success
    assert confirmed["poll_after_seconds"] == poll_after
    assert hint in confirmed["next_step"]
    assert "call pix_receive" not in confirmed["next_step"]
    assert "verification_status" not in confirmed


def test_confirm_unknown_status_raises(manager):
    payload = {"session_id": "session-1", "session_status": "weird"}
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        with pytest.raises(ValueError, match="unknown session_status"):
            manager.confirm_kyc_session("person@example.com", "session-1")


def test_retry_after_reaches_tool_envelope(manager, monkeypatch):
    error = _http_error(
        429,
        {"error_code": "NOVIUZ_RATE_LIMITED", "message": "Slow down."},
        headers={"Retry-After": "42"},
    )
    monkeypatch.setattr(aqua_tools, "get_pix_manager", lambda: manager)
    with patch("urllib.request.urlopen", side_effect=error):
        result = aqua_tools.eulen_kyc_confirm("person@example.com", "session-1")

    assert result == {
        "error": {
            "code": "NOVIUZ_RATE_LIMITED",
            "message": "Slow down.",
            "retry_after_seconds": 42,
        }
    }


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


def test_list_keeps_local_payout_fields_when_ankara_omits_them(manager, storage):
    storage.save_pix_swap(
        PixSwap(
            swap_id="43",
            amount_cents=7000,
            account_email="person@example.com",
            wallet_name="default",
            depix_address="lq1address43",
            qr_copy_paste="pix43",
            status="pending",
            network="mainnet",
            created_at="2026-09-18T12:00:00Z",
        )
    )
    payload = {
        "count": 1,
        "deposits": [
            {
                "deposit_id": 43,
                "status": "depix_sent",
                "amount_brl_cents": 7000,
                "depix_address": None,
                "qr_copy_paste": "",
            }
        ],
    }
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        result = manager.list_deposits("person@example.com")

    saved = storage.load_pix_swap("43")
    assert result["deposits"][0]["depix_address"] == "lq1address43"
    assert saved.qr_copy_paste == "pix43"
    assert saved.status == "depix_sent"


def test_list_rejects_row_without_payout_fields_when_nothing_is_local(manager, storage):
    payload = {
        "count": 1,
        "deposits": [
            {
                "deposit_id": 44,
                "status": "pending",
                "amount_brl_cents": 100,
            }
        ],
    }
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        with pytest.raises(ValueError, match="missing depix_address"):
            manager.list_deposits("person@example.com")
    assert storage.load_pix_swap("44") is None


def test_list_rejects_non_positive_amount(manager, storage):
    payload = {
        "count": 1,
        "deposits": [
            {
                "deposit_id": 45,
                "status": "pending",
                "amount_brl_cents": -1,
                "depix_address": "lq1address",
                "qr_copy_paste": "pix",
            }
        ],
    }
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        with pytest.raises(ValueError, match="invalid deposit fields"):
            manager.list_deposits("person@example.com")
    assert storage.load_pix_swap("45") is None


def test_list_does_not_persist_earlier_rows_when_a_later_row_is_invalid(
    manager, storage
):
    payload = {
        "count": 2,
        "deposits": [
            {
                "deposit_id": 46,
                "status": "pending",
                "amount_brl_cents": 100,
                "depix_address": "lq1address",
                "qr_copy_paste": "pix46",
            },
            {
                "deposit_id": 47,
                "status": "pending",
                "amount_brl_cents": -1,
                "depix_address": "lq1address",
                "qr_copy_paste": "pix47",
            },
        ],
    }
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        with pytest.raises(ValueError, match="invalid deposit fields"):
            manager.list_deposits("person@example.com")
    assert storage.load_pix_swap("46") is None
    assert storage.load_pix_swap("47") is None


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


def test_status_canonicalizes_swap_id_before_storage(manager):
    with patch(
        "urllib.request.urlopen",
        return_value=_response({"count": 0, "deposits": []}),
    ) as urlopen:
        with pytest.raises(ValueError, match="not found in Ankara: 12") as exc:
            manager.get_deposit_status("+12", "person@example.com")
    assert "Invalid swap ID" not in str(exc.value)
    assert urlopen.call_args.args[0].full_url.endswith(
        "/api/v1/eulen/deposits/?deposit_id=12"
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
    called = {}

    def fake_session(**kwargs):
        called.update(kwargs)
        return {"email": kwargs["email"], "status": "created"}

    monkeypatch.setattr("aqua.cli.eulen.eulen_kyc_session", fake_session)
    result = CliRunner().invoke(
        eulen,
        ["kyc-session", "--email", "person@example.com"],
        obj=SimpleNamespace(fmt=None),
    )
    assert result.exit_code == 0
    assert "created" in result.output.lower()
    assert called == {
        "email": "person@example.com",
        "verification_methods": ["otp"],
        "amount_cents": None,
        "wallet_name": "default",
    }


def test_cli_kyc_session_passes_pix_options(monkeypatch):
    called = {}

    def fake_session(**kwargs):
        called.update(kwargs)
        return {"status": "created"}

    monkeypatch.setattr("aqua.cli.eulen.eulen_kyc_session", fake_session)
    result = CliRunner().invoke(
        eulen,
        [
            "kyc-session",
            "--email",
            "person@example.com",
            "--method",
            "pix",
            "--method",
            "otp",
            "--amount-cents",
            "1000",
            "--wallet-name",
            "savings",
        ],
        obj=SimpleNamespace(fmt=None),
    )
    assert result.exit_code == 0
    assert called == {
        "email": "person@example.com",
        "verification_methods": ["pix", "otp"],
        "amount_cents": 1000,
        "wallet_name": "savings",
    }


def test_cli_kyc_confirm_wait_polls_until_settled(monkeypatch):
    responses = iter(
        [
            {"session_status": "created", "poll_after_seconds": 15},
            {"error": {"code": "NOVIUZ_RATE_LIMITED", "message": "x", "retry_after_seconds": 7}},
            {"session_status": "approved", "verification_status": "VERIFIED"},
        ]
    )
    sleeps = []
    monkeypatch.setattr(
        "aqua.cli.eulen.eulen_kyc_confirm", lambda **kwargs: next(responses)
    )
    monkeypatch.setattr("aqua.cli.eulen.time.sleep", sleeps.append)
    result = CliRunner().invoke(
        eulen,
        ["kyc-confirm", "--email", "person@example.com", "--session-id", "s", "--wait"],
        obj=SimpleNamespace(fmt="json"),
    )
    assert result.exit_code == 0
    assert sleeps == [15, 7]
    assert '"approved"' in result.output


def test_cli_kyc_confirm_wait_stops_on_non_retryable_error(monkeypatch):
    calls = []

    def fake_confirm(**kwargs):
        calls.append(kwargs)
        return {"error": {"code": "EULEN_KYC_SESSION_NOT_FOUND", "message": "x"}}

    monkeypatch.setattr("aqua.cli.eulen.eulen_kyc_confirm", fake_confirm)
    monkeypatch.setattr("aqua.cli.eulen.time.sleep", lambda s: pytest.fail("slept"))
    result = CliRunner().invoke(
        eulen,
        ["kyc-confirm", "--email", "person@example.com", "--session-id", "s", "--wait"],
        obj=SimpleNamespace(fmt="json"),
    )
    assert result.exit_code == 1
    assert len(calls) == 1


def test_cli_exits_nonzero_for_pix_error_envelope(monkeypatch):
    monkeypatch.setattr(
        "aqua.cli.eulen.eulen_kyc_session",
        lambda **kwargs: {
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
