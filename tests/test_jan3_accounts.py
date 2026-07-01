"""Tests for the unified JAN3 / AQUA account stack (aqua.jan3_accounts).

The only HTTP seam is ``urllib.request.urlopen`` (patched here); the wallet
manager's ``craft_raw_tx`` is faked for the paid captchaless flow. Manager tests
drive the real client through urlopen so the 401→SessionExpiredError mapping,
the refresh-and-retry orchestration, and rotation-aware refresh are all exercised
end-to-end — never stubbed out.
"""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aqua.ankara import ANKARA_API_URL, SessionExpiredError
from aqua.jan3_accounts import (
    ASSET_TICKER_LBTC,
    Jan3AccountsClient,
    Jan3AccountsManager,
    Jan3Session,
    _email_to_filename,
    _token_preview,
    _validate_email,
)
from aqua.storage import Storage

# Real L-BTC mainnet policy asset id (any 64-hex placeholder is fine here).
LBTC_ASSET_ID = "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
VAULT_ADDR = (
    "lq1qqvxk052kf3qtkxmrakx50a9gc3smqad2ync54hzntjt980kfej9kkfe"
    "0247rp5h4yzmdftsahhw64uy8pzfe7cpg4fgykm7cv"
)
# Mirror the env-configured backend so the legacy-migration test (which records
# base_url=ANKARA_API_URL) and the manager agree on the host.
BASE_URL = ANKARA_API_URL


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Storage(Path(tmpdir))


def _fake_wallet_manager(raw_tx: str = "0200deadbeef") -> MagicMock:
    """Wallet manager stub for tests.

    ``craft_raw_tx`` returns the canned hex; ``_get_policy_asset`` returns
    the L-BTC policy id.
    """
    wm = MagicMock()
    wm.craft_raw_tx.return_value = raw_tx
    wm._get_policy_asset.return_value = LBTC_ASSET_ID
    wallet = MagicMock()
    wallet.network = "mainnet"
    wm.storage.load_wallet.return_value = wallet
    return wm


def _manager(storage, raw_tx: str = "0200deadbeef") -> Jan3AccountsManager:
    return Jan3AccountsManager(
        storage=storage,
        wallet_manager=_fake_wallet_manager(raw_tx),
        base_url=BASE_URL,
    )


def _mock_response(data, status=200):
    resp = MagicMock()
    if isinstance(data, (dict, list)):
        resp.read.return_value = json.dumps(data).encode()
    elif data is None:
        resp.read.return_value = b""
    else:
        resp.read.return_value = data
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code, body):
    return urllib.error.HTTPError(
        url="https://ankara.aquabtc.com/x", code=code, msg="err", hdrs=None,
        fp=io.BytesIO(body.encode() if isinstance(body, str) else body),
    )


def _jwt_with_exp(exp: int) -> str:
    """Build an unsigned JWT carrying the given ``exp`` claim (test fixture)."""

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg({'exp': exp})}.sig"


# A far-future / long-expired JWT pair for seeding sessions whose access token
# must (not) trigger ``_with_auth_retry``'s proactive refresh.
FUTURE_JWT = _jwt_with_exp(9_999_999_999)
EXPIRED_JWT = _jwt_with_exp(1_000_000_000)


class Seq(list):
    """Marker for a response *sequence* (consumed left-to-right per call).

    A bare list value is a JSON list response (e.g. the products endpoint); wrap
    it in ``Seq`` only when you want successive calls to the same path to return
    different things (e.g. a 401 then a success after refresh).
    """


def _route(mapping):
    """Build a urlopen side_effect that dispatches on the request path.

    ``mapping`` maps a path suffix to a value (returned as a JSON response), an
    Exception (raised), or a ``Seq`` consumed left-to-right (response sequence).
    """

    def side_effect(req, timeout=None):
        path = req.full_url.split("?", 1)[0]
        for suffix, val in mapping.items():
            if path.endswith(suffix):
                if isinstance(val, Seq):
                    val = val.pop(0)
                if isinstance(val, Exception):
                    raise val
                return _mock_response(val)
        raise AssertionError(f"unexpected request to {path}")

    return side_effect


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestPureHelpers:
    def test_validate_email_lowercases(self):
        assert _validate_email("ME@Example.com") == "me@example.com"

    @pytest.mark.parametrize("bad", ["", "not-an-email", "a@b", "a@.com", "@b.com"])
    def test_validate_email_rejects(self, bad):
        with pytest.raises(ValueError, match="Invalid email"):
            _validate_email(bad)

    def test_token_preview_short_is_fully_redacted(self):
        # A 4-char preview of a 4-char token reveals the whole secret.
        # Below the threshold we fully redact instead.
        assert _token_preview("abc") == "…"
        assert _token_preview("abcdefghijk") == "…"  # 11 chars, still redacted

    def test_token_preview_long(self):
        token = "abcd" + "x" * 20 + "wxyz"
        assert _token_preview(token).startswith("abcd…")
        assert _token_preview(token).endswith("…wxyz")

    def test_token_preview_empty(self):
        assert _token_preview("") == ""

    def test_email_to_filename_safe(self):
        assert _email_to_filename("me@example.com") == "me@example.com"

    def test_email_to_filename_escapes_unsafe(self):
        # `/` would let an attacker write outside jan3_accounts_dir; it must
        # be percent-encoded. (`..` *within* a name is fine — pathlib treats
        # the result as a single file component, not a directory traversal.)
        encoded = _email_to_filename("a..b/c@d.com")
        assert "/" not in encoded
        assert "\\" not in encoded
        assert "%2f" in encoded  # the slash was percent-encoded


# ---------------------------------------------------------------------------
# Jan3Session round-trip
# ---------------------------------------------------------------------------


class TestSessionRoundtrip:
    def test_to_from_dict(self):
        s = Jan3Session(
            email="me@example.com",
            base_url=BASE_URL,
            access_token="A" * 40,
            refresh_token="R" * 40,
            created_at="2026-06-16T00:00:00+00:00",
            captcha_exempt=True,
        )
        assert Jan3Session.from_dict(s.to_dict()) == s

    def test_from_dict_backfills_optional_fields(self):
        s = Jan3Session.from_dict({
            "email": "x@y.com",
            "base_url": BASE_URL,
            "access_token": "a",
            "refresh_token": "r",
            "created_at": "now",
        })
        assert s.refreshed_at is None
        assert s.captcha_exempt is False

    def test_from_dict_drops_unknown_keys(self):
        s = Jan3Session.from_dict({
            "email": "x@y.com",
            "base_url": BASE_URL,
            "access_token": "a",
            "refresh_token": "r",
            "created_at": "now",
            "bogus_key_from_other_branch": "ignored",
        })
        assert s.email == "x@y.com"


# ---------------------------------------------------------------------------
# Jan3AccountsClient HTTP layer (urlopen seam)
# ---------------------------------------------------------------------------


class TestHttpClient:
    @patch("urllib.request.urlopen")
    def test_get_vault_payment_address(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"address": VAULT_ADDR})
        client = Jan3AccountsClient(base_url=BASE_URL)
        assert client.get_vault_payment_address() == VAULT_ADDR
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith("/api/v1/liquid-wallet/payment/receive-address/")

    @patch("urllib.request.urlopen")
    def test_get_vault_payment_address_empty_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"address": ""})
        client = Jan3AccountsClient(base_url=BASE_URL)
        with pytest.raises(ValueError, match="no address"):
            client.get_vault_payment_address()

    @patch("urllib.request.urlopen")
    def test_get_product_price_selects_matching_row(self, mock_urlopen):
        rows = [
            {"product_type": "OTHER", "lbtc_sats_price": 1},
            {"product_type": "CAPTCHALESS_LOGIN", "lbtc_sats_price": 100},
        ]
        mock_urlopen.return_value = _mock_response(rows)
        client = Jan3AccountsClient(base_url=BASE_URL)
        row = client.get_product_price("CAPTCHALESS_LOGIN")
        assert row["lbtc_sats_price"] == 100
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith(
            "/api/v1/liquid-wallet/products/?product_type=CAPTCHALESS_LOGIN"
        )

    @patch("urllib.request.urlopen")
    def test_get_product_price_missing_row_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response([])
        client = Jan3AccountsClient(base_url=BASE_URL)
        with pytest.raises(ValueError, match="no entry for"):
            client.get_product_price("CAPTCHALESS_LOGIN")

    @patch("urllib.request.urlopen")
    def test_login_free_posts_email_and_language(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"message": "ok"})
        client = Jan3AccountsClient(base_url=BASE_URL)
        assert client.login_free("me@example.com", language="es") == {"message": "ok"}
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body == {"email": "me@example.com", "language": "es"}
        assert req.full_url.endswith("/api/v1/auth/login/")

    @patch("urllib.request.urlopen")
    def test_login_captchaless_carries_challenge(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"message": "ok"})
        client = Jan3AccountsClient(base_url=BASE_URL)
        result = client.login_captchaless(
            email="me@example.com",
            language="en",
            raw_tx="DEADBEEF",
            payment_address=VAULT_ADDR,
        )
        assert result == {"message": "ok"}
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["email"] == "me@example.com"
        assert body["login_challenge"] == {
            "raw_tx": "DEADBEEF",
            "payment_address": VAULT_ADDR,
        }
        assert req.full_url.endswith("/api/v2/auth/login/")

    @patch("urllib.request.urlopen")
    def test_login_captchaless_without_challenge_omits_field(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"message": "ok"})
        client = Jan3AccountsClient(base_url=BASE_URL)
        client.login_captchaless(email="me@example.com")
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert "login_challenge" not in body

    @patch("urllib.request.urlopen")
    def test_verify_otp_posts_code(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"access": "a", "refresh": "r"})
        client = Jan3AccountsClient(base_url=BASE_URL)
        out = client.verify_otp("me@example.com", "123456", fingerprint="fp")
        assert out == {"access": "a", "refresh": "r"}
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body == {
            "email": "me@example.com",
            "otp_code": "123456",
            "fingerprint": "fp",
        }
        assert req.full_url.endswith("/api/v1/auth/verify/")

    @patch("urllib.request.urlopen")
    def test_refresh_access_token_returns_dict_rotation_aware(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            {"access": "new.acc", "refresh": "rot.ref"}
        )
        client = Jan3AccountsClient(base_url=BASE_URL)
        out = client.refresh_access_token("the.refresh.tok")
        assert out == {"access": "new.acc", "refresh": "rot.ref"}
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.full_url.endswith("/api/v1/auth/refresh/")
        assert req.get_header("Authorization") is None
        assert json.loads(req.data.decode()) == {"refresh": "the.refresh.tok"}

    @patch("urllib.request.urlopen")
    def test_401_raises_session_expired(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(401, json.dumps({"message": "bad otp"}))
        client = Jan3AccountsClient(base_url=BASE_URL)
        with pytest.raises(SessionExpiredError):
            client.verify_otp("me@example.com", "000000")

    @patch("urllib.request.urlopen")
    def test_other_http_error_raises_valueerror_with_detail(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(
            400, json.dumps({"message": "CAPTCHA_REQUIRED"})
        )
        client = Jan3AccountsClient(base_url=BASE_URL)
        with pytest.raises(ValueError, match="CAPTCHA_REQUIRED"):
            client.login_captchaless(email="me@example.com")

    @patch("urllib.request.urlopen")
    def test_url_error_raises_valueerror(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("conn refused")
        client = Jan3AccountsClient(base_url=BASE_URL)
        with pytest.raises(ValueError, match="unreachable"):
            client.login_free("me@example.com")

    @patch("urllib.request.urlopen")
    def test_provision_wapupay_account_sends_bearer(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"token": "provisioned-key"})
        client = Jan3AccountsClient(base_url=BASE_URL)
        out = client.provision_wapupay_account("jwt.access.token")
        assert out == {"token": "provisioned-key"}
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith("/api/v1/wapupay/account/")
        assert req.get_method() == "POST"
        assert req.get_header("Authorization") == "Bearer jwt.access.token"
        assert req.get_header("X-api-key") is None

    @patch("urllib.request.urlopen")
    def test_provision_wapupay_account_401_is_session_expired(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(401, json.dumps({"detail": "expired"}))
        client = Jan3AccountsClient(base_url=BASE_URL)
        with pytest.raises(SessionExpiredError):
            client.provision_wapupay_account("expired")


# ---------------------------------------------------------------------------
# Manager: free email-OTP login
# ---------------------------------------------------------------------------


class TestFreeLogin:
    def test_login_emails_otp_and_persists_nothing(self, storage):
        mgr = _manager(storage)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/auth/login/": {"message": "sent"}}),
        ):
            out = mgr.login("ME@Example.com", language="es")
        assert out["email"] == "me@example.com"
        assert out["message"] == "sent"
        assert "next_step" in out
        assert mgr.load_session("me@example.com") is None

    def test_login_echoes_dev_otp_code(self, storage):
        mgr = _manager(storage)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route(
                {"/api/v1/auth/login/": {"message": "sent", "otp_code": "424242"}}
            ),
        ):
            out = mgr.login("me@example.com")
        assert out["otp_code"] == "424242"

    def test_login_rejects_bad_email(self, storage):
        mgr = _manager(storage)
        with pytest.raises(ValueError, match="Invalid email"):
            mgr.login("not-an-email")


# ---------------------------------------------------------------------------
# Manager: paid captchaless login (request_login)
# ---------------------------------------------------------------------------


class TestRequestLogin:
    def test_happy_path_crafts_tx_and_dispatches(self, storage):
        mgr = _manager(storage, raw_tx="hex-tx")
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({
                "/api/v1/liquid-wallet/payment/receive-address/": {"address": VAULT_ADDR},
                "/api/v1/liquid-wallet/products/": [
                    {"product_type": "CAPTCHALESS_LOGIN", "lbtc_sats_price": 100}
                ],
                "/api/v2/auth/login/": {"message": "OTP sent"},
            }),
        ):
            result = mgr.request_login(
                email="ME@Example.com", wallet_name="default", password=None
            )
        assert result["email"] == "me@example.com"
        assert result["payment_address"] == VAULT_ADDR
        assert result["amount_sats"] == 100
        assert result["asset_ticker"] == ASSET_TICKER_LBTC
        mgr.wallet_manager.craft_raw_tx.assert_called_once_with(
            wallet_name="default",
            address=VAULT_ADDR,
            amount=100,
            asset_id=LBTC_ASSET_ID,
            password=None,
        )
        # No session persisted yet — verify() does that.
        assert mgr.load_session("me@example.com") is None

    def test_blocked_when_disabled_surfaces_valueerror(self, storage):
        mgr = _manager(storage)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({
                "/api/v1/liquid-wallet/payment/receive-address/": {"address": VAULT_ADDR},
                "/api/v1/liquid-wallet/products/": [
                    {"product_type": "CAPTCHALESS_LOGIN", "lbtc_sats_price": 100}
                ],
                "/api/v2/auth/login/": _http_error(
                    403, json.dumps({"detail": "CAPTCHALESS_LOGIN_DISABLED"})
                ),
            }),
        ):
            with pytest.raises(ValueError, match="CAPTCHALESS_LOGIN_DISABLED"):
                mgr.request_login(email="me@example.com")

    def test_rejects_invalid_email(self, storage):
        mgr = _manager(storage)
        with pytest.raises(ValueError, match="Invalid email"):
            mgr.request_login(email="not-an-email")

    def test_rejects_non_positive_price(self, storage):
        mgr = _manager(storage)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({
                "/api/v1/liquid-wallet/payment/receive-address/": {"address": VAULT_ADDR},
                "/api/v1/liquid-wallet/products/": [
                    {"product_type": "CAPTCHALESS_LOGIN", "lbtc_sats_price": 0}
                ],
            }),
        ):
            with pytest.raises(ValueError, match="non-positive"):
                mgr.request_login(email="me@example.com")


# ---------------------------------------------------------------------------
# Manager: verify (shared by both flows) — persists per-email session
# ---------------------------------------------------------------------------


class TestVerify:
    def test_persists_session_free_flow(self, storage):
        mgr = _manager(storage)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route(
                {"/api/v1/auth/verify/": {"access": "A" * 40, "refresh": "R" * 40}}
            ),
        ):
            result = mgr.verify("me@example.com", "123456", captcha_exempt=False)

        assert result["logged_in"] is True
        assert result["captcha_exempt"] is False
        assert result["access_token_preview"].startswith("AAAA")
        # The refresh token's preview must never appear in tool output.
        assert "refresh_token_preview" not in result

        loaded = mgr.load_session("me@example.com")
        assert loaded is not None
        assert loaded.access_token == "A" * 40
        assert loaded.refresh_token == "R" * 40
        assert loaded.captcha_exempt is False

        if os.name == "posix":
            mode = stat.S_IMODE(mgr._session_path("me@example.com").stat().st_mode)
            assert mode == 0o600

    def test_persists_session_captchaless_flow_sets_flag(self, storage):
        mgr = _manager(storage)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route(
                {"/api/v1/auth/verify/": {"access": "A" * 40, "refresh": "R" * 40}}
            ),
        ):
            result = mgr.verify("me@example.com", "123456", captcha_exempt=True)
        assert result["captcha_exempt"] is True
        assert mgr.load_session("me@example.com").captcha_exempt is True

    def test_propagates_invalid_otp_and_saves_nothing(self, storage):
        mgr = _manager(storage)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route(
                {"/api/v1/auth/verify/": _http_error(401, json.dumps({"message": "bad"}))}
            ),
        ):
            with pytest.raises(SessionExpiredError):
                mgr.verify("me@example.com", "000000")
        assert mgr.load_session("me@example.com") is None

    def test_rejects_blank_otp(self, storage):
        mgr = _manager(storage)
        with pytest.raises(ValueError, match="otp_code is required"):
            mgr.verify("me@example.com", "   ")

    def test_rejects_missing_tokens(self, storage):
        mgr = _manager(storage)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/auth/verify/": {"access": "only-access"}}),
        ):
            with pytest.raises(ValueError, match="did not return tokens"):
                mgr.verify("me@example.com", "123456")
        assert mgr.load_session("me@example.com") is None


# ---------------------------------------------------------------------------
# Manager: multi-account session persistence
# ---------------------------------------------------------------------------


def _seed_session(
    mgr, email, access="initial-access", refresh="refresh-token-xyz"
) -> Jan3Session:
    session = Jan3Session(
        email=email,
        base_url=mgr.base_url,
        access_token=access,
        refresh_token=refresh,
        created_at="2026-06-16T00:00:00+00:00",
        captcha_exempt=True,
    )
    mgr.save_session(session)
    return session


class TestSessionManagement:
    def test_save_load_roundtrip(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com")
        loaded = mgr.load_session("me@example.com")
        assert loaded is not None and loaded.email == "me@example.com"

    def test_logout_deletes_file(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com")
        assert mgr.delete_session("me@example.com") is True
        assert mgr.load_session("me@example.com") is None

    def test_logout_idempotent(self, storage):
        mgr = _manager(storage)
        assert mgr.delete_session("ghost@example.com") is False
        assert mgr.logout("ghost@example.com") == {
            "email": "ghost@example.com",
            "logged_out": False,
        }

    def test_list_sessions_multi_account(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "a@example.com")
        _seed_session(mgr, "b@example.com")
        emails = {s.email for s in mgr.list_sessions()}
        assert emails == {"a@example.com", "b@example.com"}

    def test_list_sessions_ignores_legacy_session_json(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "a@example.com")
        # A stray legacy single-session file must not be listed as an account.
        (storage.jan3_dir / "session.json").write_text("{}")
        emails = {s.email for s in mgr.list_sessions()}
        assert emails == {"a@example.com"}

    def test_session_file_is_under_jan3_dir(self, storage):
        mgr = _manager(storage)
        path = mgr._session_path("me@example.com")
        assert path.parent == storage.jan3_dir

    def test_load_session_tolerates_corrupted_file(self, storage):
        mgr = _manager(storage)
        path = mgr._session_path("me@example.com")
        path.write_text("{not valid json")
        assert mgr.load_session("me@example.com") is None


# ---------------------------------------------------------------------------
# Manager: session_status (local exp check + refresh-on-expiry)
# ---------------------------------------------------------------------------


class TestSessionStatus:
    def test_not_logged_in(self, storage):
        mgr = _manager(storage)
        assert mgr.session_status("ghost@example.com") == {
            "email": "ghost@example.com",
            "logged_in": False,
        }

    def test_valid_access_skips_network(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            st = mgr.session_status("me@example.com")
        assert st["logged_in"] is True and st["valid"] is True
        assert "access" not in st and "refresh" not in st  # no secrets leaked

    def test_expired_access_refreshes_and_reports_valid(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com", access=EXPIRED_JWT, refresh="good.ref")
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/auth/refresh/": {"access": FUTURE_JWT}}),
        ):
            st = mgr.session_status("me@example.com")
        assert st["valid"] is True
        # The rotated access token was persisted.
        assert mgr.load_session("me@example.com").access_token == FUTURE_JWT

    def test_refresh_rejected_reports_invalid(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com", access=EXPIRED_JWT, refresh="dead.ref")
        with patch(
            "urllib.request.urlopen",
            side_effect=_route(
                {"/api/v1/auth/refresh/": _http_error(401, "{}")}
            ),
        ):
            st = mgr.session_status("me@example.com")
        assert st["logged_in"] is True and st["valid"] is False
        assert "jan3_login" in st["message"]

    def test_network_error_reports_unknown(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com", access=EXPIRED_JWT, refresh="ref")
        with patch(
            "urllib.request.urlopen",
            side_effect=_route(
                {"/api/v1/auth/refresh/": urllib.error.URLError("unreachable")}
            ),
        ):
            st = mgr.session_status("me@example.com")
        assert st["valid"] is None
        assert "Could not verify" in st["message"]


# ---------------------------------------------------------------------------
# Manager: _refresh_session (rotation-aware) + _with_auth_retry
# ---------------------------------------------------------------------------


class TestRefreshSession:
    def test_keeps_old_refresh_when_not_rotated(self, storage):
        mgr = _manager(storage)
        session = _seed_session(
            mgr, "me@example.com", access="old.acc", refresh="keep.ref"
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/auth/refresh/": {"access": "new.acc"}}),
        ):
            updated = mgr._refresh_session(session)
        assert updated.access_token == "new.acc"
        assert updated.refresh_token == "keep.ref"
        reloaded = mgr.load_session("me@example.com")
        assert reloaded.access_token == "new.acc"
        assert reloaded.refreshed_at is not None

    def test_persists_rotated_refresh(self, storage):
        mgr = _manager(storage)
        session = _seed_session(
            mgr, "me@example.com", access="old.acc", refresh="old.ref"
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=_route(
                {"/api/v1/auth/refresh/": {"access": "rot.acc", "refresh": "rot.ref"}}
            ),
        ):
            mgr._refresh_session(session)
        reloaded = mgr.load_session("me@example.com")
        assert reloaded.access_token == "rot.acc" and reloaded.refresh_token == "rot.ref"

    def test_no_refresh_token_deletes_and_raises(self, storage):
        mgr = _manager(storage)
        session = _seed_session(mgr, "me@example.com", refresh="")
        with pytest.raises(SessionExpiredError):
            mgr._refresh_session(session)
        assert mgr.load_session("me@example.com") is None

    def test_empty_access_in_response_deletes_and_raises(self, storage):
        mgr = _manager(storage)
        session = _seed_session(mgr, "me@example.com", refresh="r")
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/auth/refresh/": {}}),
        ):
            with pytest.raises(SessionExpiredError):
                mgr._refresh_session(session)
        assert mgr.load_session("me@example.com") is None

    def test_refresh_401_deletes_session(self, storage):
        mgr = _manager(storage)
        session = _seed_session(mgr, "me@example.com", refresh="dead.ref")
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/auth/refresh/": _http_error(401, "{}")}),
        ):
            with pytest.raises(SessionExpiredError):
                mgr._refresh_session(session)
        assert mgr.load_session("me@example.com") is None


class TestWithAuthRetry:
    def test_first_call_success_no_refresh(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        seen = []

        def call(access):
            seen.append(access)
            return {"ok": True}

        with patch("urllib.request.urlopen", side_effect=AssertionError("no refresh")):
            assert mgr._with_auth_retry("me@example.com", call) == {"ok": True}
        assert seen == [FUTURE_JWT]

    def test_refreshes_and_retries_on_401(self, storage):
        mgr = _manager(storage)
        _seed_session(
            mgr, "me@example.com", access=FUTURE_JWT, refresh="good.ref"
        )
        seen = []
        calls = {"n": 0}

        def call(access):
            seen.append(access)
            calls["n"] += 1
            if calls["n"] == 1:
                raise SessionExpiredError("expired at server")
            return {"ok": True}

        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/auth/refresh/": {"access": "new.acc"}}),
        ):
            result = mgr._with_auth_retry("me@example.com", call)
        assert result == {"ok": True}
        assert seen == [FUTURE_JWT, "new.acc"]
        assert mgr.load_session("me@example.com").access_token == "new.acc"

    def test_proactive_refresh_when_access_expired(self, storage):
        # A locally-expired access token triggers a refresh BEFORE the call.
        mgr = _manager(storage)
        _seed_session(
            mgr, "me@example.com", access=EXPIRED_JWT, refresh="good.ref"
        )
        seen = []

        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/auth/refresh/": {"access": "fresh.acc"}}),
        ):
            mgr._with_auth_retry("me@example.com", lambda access: seen.append(access))
        # Call never saw the stale token — only the proactively-refreshed one.
        assert seen == ["fresh.acc"]

    def test_second_401_wipes_session_and_raises_valueerror(self, storage):
        mgr = _manager(storage)
        _seed_session(
            mgr, "me@example.com", access=FUTURE_JWT, refresh="good.ref"
        )

        def call(access):
            raise SessionExpiredError("server says no")

        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/auth/refresh/": {"access": "new.acc"}}),
        ):
            with pytest.raises(ValueError, match="still invalid after refresh"):
                mgr._with_auth_retry("me@example.com", call)
        assert mgr.load_session("me@example.com") is None

    def test_require_session_raises_when_not_logged_in(self, storage):
        mgr = _manager(storage)
        with pytest.raises(ValueError, match="Not logged in"):
            mgr.require_session("ghost@example.com")


# ---------------------------------------------------------------------------
# Manager: provision_wapupay_token (works from either login flow)
# ---------------------------------------------------------------------------


class TestProvisionWapupayToken:
    def test_happy_path_free_flow_session(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/wapupay/account/": {"token": "WapuKey_abc"}}),
        ):
            assert mgr.provision_wapupay_token("me@example.com") == "WapuKey_abc"

    def test_happy_path_captchaless_session(self, storage):
        mgr = _manager(storage)
        # captcha_exempt=True session (paid flow) provisions identically.
        s = _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        assert s.captcha_exempt is True
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/wapupay/account/": {"token": "WapuKey_xyz"}}),
        ):
            assert mgr.provision_wapupay_token("me@example.com") == "WapuKey_xyz"

    def test_refreshes_and_retries_on_401(self, storage):
        mgr = _manager(storage)
        _seed_session(
            mgr, "me@example.com", access=FUTURE_JWT, refresh="good.ref"
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({
                "/api/v1/wapupay/account/": Seq([
                    _http_error(401, json.dumps({"detail": "expired"})),
                    {"token": "WapuKey_after_refresh"},
                ]),
                "/api/v1/auth/refresh/": {"access": "fresh.acc", "refresh": "fresh.ref"},
            }),
        ):
            token = mgr.provision_wapupay_token("me@example.com")
        assert token == "WapuKey_after_refresh"
        assert mgr.load_session("me@example.com").access_token == "fresh.acc"

    def test_relogin_when_refresh_fails(self, storage):
        mgr = _manager(storage)
        _seed_session(
            mgr, "me@example.com", access=FUTURE_JWT, refresh="dead.ref"
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({
                "/api/v1/wapupay/account/": _http_error(401, "{}"),
                "/api/v1/auth/refresh/": _http_error(401, "{}"),
            }),
        ):
            with pytest.raises(ValueError, match="jan3_login"):
                mgr.provision_wapupay_token("me@example.com")

    def test_requires_login(self, storage):
        mgr = _manager(storage)
        with pytest.raises(ValueError, match="Not logged in"):
            mgr.provision_wapupay_token("me@example.com")

    def test_missing_token_raises(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/api/v1/wapupay/account/": {"not_token": "x"}}),
        ):
            with pytest.raises(ValueError, match="token missing"):
                mgr.provision_wapupay_token("me@example.com")

