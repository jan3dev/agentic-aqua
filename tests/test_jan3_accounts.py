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
    DEFAULT_LN_ADDRESS_POOL,
    MAX_ADDRESSES_PER_REGISTRATION,
    Jan3AccountsClient,
    Jan3AccountsManager,
    Jan3Session,
    _email_to_filename,
    _token_preview,
    _validate_email,
    _validate_ln_username,
)
from aqua.storage import Storage

# Real L-BTC mainnet policy asset id (any 64-hex placeholder is fine here).
LBTC_ASSET_ID = "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
VAULT_ADDR = (
    "lq1qqvxk052kf3qtkxmrakx50a9gc3smqad2ync54hzntjt980kfej9kkfe"
    "0247rp5h4yzmdftsahhw64uy8pzfe7cpg4fgykm7cv"
)
# Mirror the env-configured backend so persisted sessions (which record
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


def _capturing_route(mapping):
    """Like :func:`_route` but records every request URL (with query string).

    The returned side_effect exposes ``.calls`` — a list of ``req.full_url``
    strings — so a test can assert whether ``?override_fingerprint=true`` was
    (or was not) sent.
    """
    urls: list[str] = []
    base = _route(mapping)

    def side_effect(req, timeout=None):
        urls.append(req.full_url)
        return base(req, timeout=timeout)

    side_effect.calls = urls
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

    def test_verify_cues_ln_address_offer(self, storage):
        # The post-login UX: verify's return must cue the agent to offer the
        # Lightning Address opt-in (this is why the toggle is user-facing).
        mgr = _manager(storage)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route(
                {"/api/v1/auth/verify/": {"access": "A" * 40, "refresh": "R" * 40}}
            ),
        ):
            result = mgr.verify("me@example.com", "123456")
        assert "lightning address" in result["next_step"].lower()
        assert "jan3_enable_lightning_address" in result["next_step"]

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

    def test_list_sessions_skips_unreadable_file(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "a@example.com")
        # A stray unreadable/invalid .json in jan3_dir must not break listing
        # nor be surfaced as an account.
        (storage.jan3_dir / "garbage.json").write_text("{not valid json")
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


# ---------------------------------------------------------------------------
# LN username validation
# ---------------------------------------------------------------------------


class TestLnUsernameValidation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("alice", "alice"),
            ("ALICE", "alice"),
            ("bob.smith", "bob.smith"),
            ("user1234", "user1234"),
            ("  Trimmed  ", "trimmed"),
        ],
    )
    def test_valid_normalizes(self, raw, expected):
        assert _validate_ln_username(raw) == expected

    @pytest.mark.parametrize(
        "bad",
        ["abc", "", "a_b", "ab.cd.ef", "with space", "under_score", "x" * 65, "no-dash"],
    )
    def test_invalid_rejected(self, bad):
        with pytest.raises(ValueError, match="Invalid ln_username"):
            _validate_ln_username(bad)


# ---------------------------------------------------------------------------
# LN client HTTP methods (urlopen seam)
# ---------------------------------------------------------------------------


class TestLnClientHttp:
    @patch("urllib.request.urlopen")
    def test_get_user_sends_bearer(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            {"email": "me@x.com", "ln_username": "alice"}
        )
        client = Jan3AccountsClient(base_url=BASE_URL, access_token="jwt.acc")
        out = client.get_user()
        assert out["ln_username"] == "alice"
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith("/api/v1/auth/user/")
        assert req.get_method() == "GET"
        assert req.get_header("Authorization") == "Bearer jwt.acc"

    @patch("urllib.request.urlopen")
    def test_ln_address_toggle_posts_enabled(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"ln_address_toggled": True})
        client = Jan3AccountsClient(base_url=BASE_URL, access_token="jwt")
        client.ln_address_toggle(True)
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith("/api/v1/auth/user/ln-address-toggle/")
        assert json.loads(req.data.decode()) == {"enabled": True}

    @patch("urllib.request.urlopen")
    def test_ln_username_available_quotes_and_forwards_token(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"available": True})
        client = Jan3AccountsClient(base_url=BASE_URL, access_token="jwt")
        client.ln_username_available("alice.bob")
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith(
            "/api/v1/auth/user/ln-username/alice.bob/is-available"
        )
        assert req.get_header("Authorization") == "Bearer jwt"

    @patch("urllib.request.urlopen")
    def test_ln_username_available_anonymous_without_token(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"available": True})
        client = Jan3AccountsClient(base_url=BASE_URL)  # no access token
        client.ln_username_available("alice")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") is None

    @patch("urllib.request.urlopen")
    def test_register_addresses_override_adds_query(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"addresses": ["a", "b"]})
        client = Jan3AccountsClient(base_url=BASE_URL, access_token="jwt")
        client.register_addresses("fp1234", ["a", "b"], override_fingerprint=True)
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith("/api/v1/auth/user/addresses/?override_fingerprint=true")
        assert json.loads(req.data.decode()) == {
            "fingerprint": "fp1234",
            "addresses": ["a", "b"],
        }

    @patch("urllib.request.urlopen")
    def test_register_addresses_no_override_no_query(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"addresses": []})
        client = Jan3AccountsClient(base_url=BASE_URL, access_token="jwt")
        client.register_addresses("fp", [])
        req = mock_urlopen.call_args[0][0]
        assert "override_fingerprint" not in req.full_url

    @patch("urllib.request.urlopen")
    def test_create_payment_request_posts_body(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            {"payment_id": "p1", "address": VAULT_ADDR, "amount_base_units": 500}
        )
        client = Jan3AccountsClient(base_url=BASE_URL, access_token="jwt")
        client.create_ln_username_payment_request("L-BTC", "alice")
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith(
            "/api/v1/liquid-wallet/payment-request/ln-username/"
        )
        assert json.loads(req.data.decode()) == {"asset": "L-BTC", "ln_username": "alice"}

    @patch("urllib.request.urlopen")
    def test_submit_raw_tx_posts_body(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"status": "PENDING"})
        client = Jan3AccountsClient(base_url=BASE_URL, access_token="jwt")
        client.submit_raw_tx("p1", "deadbeef")
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith("/api/v1/liquid-wallet/payment/submit-raw-tx/")
        assert json.loads(req.data.decode()) == {"payment_id": "p1", "raw_tx": "deadbeef"}


# ---------------------------------------------------------------------------
# Manager: LN-address pool (get_user auto-heal + toggle + register/ensure)
# ---------------------------------------------------------------------------


def _ln_manager(storage, *, fingerprint="abcd1234", raw_tx="0200deadbeef"):
    """Manager wired to a wallet stub with controllable fingerprint + addresses."""
    wm = _fake_wallet_manager(raw_tx)
    wm.fingerprint.return_value = fingerprint
    wm.reserve_addresses.side_effect = lambda name, count: [
        MagicMock(address=f"lq1addr{i}") for i in range(count)
    ]
    mgr = Jan3AccountsManager(
        storage=storage, wallet_manager=wm, base_url=BASE_URL
    )
    return mgr, wm


class TestGetUserAutoPool:
    def test_auto_refills_when_active(self, storage):
        mgr, wm = _ln_manager(storage, fingerprint="abcd1234")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        profile = {
            "email": "me@example.com",
            "ln_username": "alice",
            "ln_address_toggled": True,
            "fingerprint": "abcd1234",
            "new_addresses_needed": 3,
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({
                "/addresses/": {"addresses": ["a", "b", "c"]},
                "/user/": profile,
            }),
        ):
            out = mgr.get_user("me@example.com")
        assert out["ln_username"] == "alice"
        assert out["ln_address_pool"]["refilled"] is True
        assert out["ln_address_pool"]["requested_count"] == 3
        wm.reserve_addresses.assert_called_once_with("default", 3)

    def test_skips_when_toggle_off(self, storage):
        mgr, wm = _ln_manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        profile = {
            "ln_username": "alice",
            "ln_address_toggled": False,
            "fingerprint": "abcd1234",
            "new_addresses_needed": 5,
        }
        with patch(
            "urllib.request.urlopen", side_effect=_route({"/user/": profile})
        ):
            out = mgr.get_user("me@example.com")
        assert out["ln_address_pool"]["reason"] == "ln_address_disabled"
        wm.reserve_addresses.assert_not_called()

    def test_pool_full_no_register(self, storage):
        mgr, wm = _ln_manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        profile = {
            "ln_username": "alice",
            "ln_address_toggled": True,
            "fingerprint": "abcd1234",
            "new_addresses_needed": 0,
        }
        with patch(
            "urllib.request.urlopen", side_effect=_route({"/user/": profile})
        ):
            out = mgr.get_user("me@example.com")
        assert out["ln_address_pool"]["reason"] == "pool_full"
        wm.reserve_addresses.assert_not_called()

    def test_topup_backend_failure_never_breaks_read(self, storage):
        # A REAL failure mode: the local reserve succeeds but the backend
        # register POST fails (transient 500). It must be swallowed into a skip,
        # not raised — the profile read still returns. (The previous version of
        # this test injected a "password required" error from fingerprint(),
        # which the real code cannot produce for an encrypted hot wallet:
        # address derivation needs only the descriptor, not the mnemonic —
        # see test_wallet.py::TestEncryptedWalletNoPassword.)
        mgr, wm = _ln_manager(storage, fingerprint="abcd1234")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        profile = {
            "ln_username": "alice",
            "ln_address_toggled": True,
            "fingerprint": "abcd1234",
            "new_addresses_needed": 3,
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({
                "/user/": profile,
                "/addresses/": _http_error(500, "backend down"),
            }),
        ):
            out = mgr.get_user("me@example.com")
        # Profile still returned; the topup failure is reported, not raised.
        assert out["ln_username"] == "alice"
        assert out["ln_address_pool"]["refilled"] is False
        assert out["ln_address_pool"]["reason"] == "auto_topup_unavailable"
        # The burn-before-POST ordering means reserve was attempted first.
        wm.reserve_addresses.assert_called_once_with("default", 3)


class TestLnAddressToggleManager:
    def test_enable_populates_pool(self, storage):
        mgr, wm = _ln_manager(storage, fingerprint="abcd1234")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        profile = {
            "ln_username": "alice",
            "ln_address_toggled": True,
            "fingerprint": "abcd1234",
            "new_addresses_needed": 2,
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({
                "/ln-address-toggle/": {"ln_address_toggled": True},
                "/addresses/": {"addresses": ["a", "b"]},
                "/user/": profile,
            }),
        ):
            out = mgr.ln_address_toggle("me@example.com", True)
        assert out["enabled"] is True
        assert out["ln_address_pool"]["refilled"] is True
        wm.reserve_addresses.assert_called_once_with("default", 2)

    def test_disable_does_not_touch_pool(self, storage):
        mgr, wm = _ln_manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/ln-address-toggle/": {"ln_address_toggled": False}}),
        ):
            out = mgr.ln_address_toggle("me@example.com", False)
        assert out["enabled"] is False
        assert "ln_address_pool" not in out
        wm.reserve_addresses.assert_not_called()

    def test_enable_without_username_reports_skip(self, storage):
        mgr, wm = _ln_manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        profile = {"ln_username": None, "ln_address_toggled": True, "fingerprint": "fp"}
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({
                "/ln-address-toggle/": {"ok": True},
                "/user/": profile,
            }),
        ):
            out = mgr.ln_address_toggle("me@example.com", True)
        assert out["ln_address_pool"]["reason"] == "no_ln_username"
        wm.reserve_addresses.assert_not_called()


class TestRegisterLnAddresses:
    def test_raises_without_username(self, storage):
        mgr, _ = _ln_manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        with pytest.raises(ValueError, match="no LN username"):
            mgr.register_ln_addresses(
                "me@example.com",
                profile={"ln_username": None, "ln_address_toggled": True},
            )

    def test_fingerprint_mismatch_raises(self, storage):
        mgr, _ = _ln_manager(storage, fingerprint="local999")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        with pytest.raises(ValueError, match="fingerprint mismatch"):
            mgr.register_ln_addresses(
                "me@example.com",
                profile={
                    "ln_username": "alice",
                    "ln_address_toggled": True,
                    "fingerprint": "server111",
                    "new_addresses_needed": 2,
                },
            )

    def test_caps_count_at_max(self, storage):
        mgr, _ = _ln_manager(storage, fingerprint="fp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        with pytest.raises(ValueError, match="cannot exceed"):
            mgr.register_ln_addresses(
                "me@example.com",
                count=MAX_ADDRESSES_PER_REGISTRATION + 1,
                profile={
                    "ln_username": "alice",
                    "ln_address_toggled": True,
                    "fingerprint": "fp",
                },
            )

    def test_default_count_when_none_needed(self, storage):
        mgr, wm = _ln_manager(storage, fingerprint="fp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/addresses/": {"addresses": ["x"] * 5}}),
        ):
            out = mgr.register_ln_addresses(
                "me@example.com",
                profile={
                    "ln_username": "alice",
                    "ln_address_toggled": True,
                    "fingerprint": "fp",
                    "new_addresses_needed": 0,
                },
            )
        wm.reserve_addresses.assert_called_once_with("default", DEFAULT_LN_ADDRESS_POOL)
        assert out["requested_count"] == DEFAULT_LN_ADDRESS_POOL


# ---------------------------------------------------------------------------
# Manager: rebind_wallet (self-serve override_fingerprint)
# ---------------------------------------------------------------------------


class TestRebindWallet:
    """`rebind_wallet` re-binds the account's Lightning Address to a different
    local wallet. Three states: first-bind (no server fp), no-op (server==local),
    destructive re-bind (server!=local). confirm=False previews without mutating;
    confirm=True executes with override_fingerprint=true.
    """

    def _profile(self, *, server_fp="serverfp", needed=2, ln_username="alice@aquabtc.com"):
        return {
            "email": "me@example.com",
            "ln_username": ln_username,
            "ln_address_toggled": True,
            "fingerprint": server_fp,
            "new_addresses_needed": needed,
        }

    # -- destructive re-bind (server_fp != local_fp) ------------------------

    def test_preview_does_not_mutate_or_send_override(self, storage):
        # confirm=False on a mismatch must PREVIEW only: no address minting,
        # no POST to /addresses/, and certainly no override query. (AC-3)
        mgr, wm = _ln_manager(storage, fingerprint="localfp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        route = _capturing_route({"/user/": self._profile(server_fp="serverfp")})
        with patch("urllib.request.urlopen", side_effect=route):
            out = mgr.rebind_wallet("me@example.com", "default", confirm=False)
        assert out["requires_confirmation"] is True
        assert out["rebound"] is False
        assert out["current_fingerprint"] == "serverfp"
        assert out["new_fingerprint"] == "localfp"
        assert "serverfp" in out["warning"] and "localfp" in out["warning"]
        wm.reserve_addresses.assert_not_called()
        assert not any("override_fingerprint" in u for u in route.calls)
        assert not any("/addresses/" in u for u in route.calls)

    def test_confirm_executes_and_sends_override(self, storage):
        # confirm=True on a mismatch must call register with override_fingerprint,
        # sending ?override_fingerprint=true to /addresses/. (AC-4, AC-5)
        mgr, wm = _ln_manager(storage, fingerprint="localfp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        route = _capturing_route({
            "/user/": self._profile(server_fp="serverfp", needed=2),
            "/addresses/": {"addresses": ["a", "b"]},
        })
        with patch("urllib.request.urlopen", side_effect=route):
            out = mgr.rebind_wallet("me@example.com", "default", confirm=True)
        assert out["rebound"] is True
        assert out["new_fingerprint"] == "localfp"
        assert out["pool_size"] == 2
        wm.reserve_addresses.assert_called_once_with("default", 2)
        assert any(
            u.endswith("/api/v1/auth/user/addresses/?override_fingerprint=true")
            for u in route.calls
        )

    def test_warning_names_ln_address_not_account_email(self, storage):
        # D6: the warning must surface the Lightning Address (ln_username), never
        # the JAN3 account login email.
        mgr, _ = _ln_manager(storage, fingerprint="localfp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        route = _capturing_route({
            "/user/": self._profile(server_fp="serverfp", ln_username="alice@aquabtc.com"),
        })
        with patch("urllib.request.urlopen", side_effect=route):
            out = mgr.rebind_wallet("me@example.com", "default", confirm=False)
        assert "alice@aquabtc.com" in out["warning"]
        assert "me@example.com" not in out["warning"]

    # -- first bind (server_fp empty) --------------------------------------

    def test_first_bind_preview_has_no_bogus_none(self, storage):
        # AC-10: no wallet bound yet -> first bind, not a destructive re-bind.
        # The warning must not render a "None" replaced-fingerprint.
        mgr, wm = _ln_manager(storage, fingerprint="localfp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        route = _capturing_route({"/user/": self._profile(server_fp=None)})
        with patch("urllib.request.urlopen", side_effect=route):
            out = mgr.rebind_wallet("me@example.com", "default", confirm=False)
        assert out["state"] == "first_bind"
        assert out["current_fingerprint"] is None
        assert out["new_fingerprint"] == "localfp"
        assert "None" not in out["warning"]
        wm.reserve_addresses.assert_not_called()
        assert not any("override_fingerprint" in u for u in route.calls)

    def test_first_bind_confirm_executes(self, storage):
        mgr, wm = _ln_manager(storage, fingerprint="localfp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        route = _capturing_route({
            "/user/": self._profile(server_fp=None, needed=3),
            "/addresses/": {"addresses": ["a", "b", "c"]},
        })
        with patch("urllib.request.urlopen", side_effect=route):
            out = mgr.rebind_wallet("me@example.com", "default", confirm=True)
        assert out["rebound"] is True
        assert any("override_fingerprint=true" in u for u in route.calls)

    # -- already bound (server_fp == local_fp) -----------------------------

    def test_already_bound_is_noop_no_override(self, storage):
        # AC-11: server fp already equals this wallet -> no-op, no confirmation,
        # never sends a spurious destructive override.
        mgr, wm = _ln_manager(storage, fingerprint="samefp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        route = _capturing_route({"/user/": self._profile(server_fp="samefp", needed=0)})
        with patch("urllib.request.urlopen", side_effect=route):
            out = mgr.rebind_wallet("me@example.com", "default", confirm=False)
        assert out["already_bound"] is True
        assert out["requires_confirmation"] is False
        assert out["rebound"] is False
        assert not any("override_fingerprint" in u for u in route.calls)

    # -- guards -------------------------------------------------------------

    def test_no_username_raises(self, storage):
        mgr, _ = _ln_manager(storage, fingerprint="localfp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/user/": {"ln_username": None, "fingerprint": "serverfp"}}),
        ):
            with pytest.raises(ValueError, match="no LN username"):
                mgr.rebind_wallet("me@example.com", "default", confirm=True)

    def test_mismatch_skip_points_to_rebind_tool(self, storage):
        # AC-8: the auto-path skip message must now point at the new self-serve
        # tool instead of saying re-binding is unavailable.
        mgr, _ = _ln_manager(storage, fingerprint="localfp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        out = mgr.ensure_ln_pool(
            "me@example.com",
            profile={
                "ln_username": "alice@aquabtc.com",
                "ln_address_toggled": True,
                "fingerprint": "serverfp",
                "new_addresses_needed": 3,
            },
        )
        assert out["reason"] == "fingerprint_mismatch"
        assert "jan3_rebind_wallet" in out["message"]
        assert "serverfp" in out["message"] and "localfp" in out["message"]


class TestEnsureLnPool:
    def test_fingerprint_mismatch_skips(self, storage):
        mgr, wm = _ln_manager(storage, fingerprint="localfp")
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        out = mgr.ensure_ln_pool(
            "me@example.com",
            profile={
                "ln_username": "alice",
                "ln_address_toggled": True,
                "fingerprint": "serverfp",
                "new_addresses_needed": 3,
            },
        )
        assert out["refilled"] is False
        assert out["reason"] == "fingerprint_mismatch"
        assert out["server_fingerprint"] == "serverfp"
        assert out["local_fingerprint"] == "localfp"
        # The skip must carry actionable guidance naming both fingerprints
        # (the reason alone is a dead-end; there is no self-serve re-bind tool).
        assert "serverfp" in out["message"] and "localfp" in out["message"]
        wm.reserve_addresses.assert_not_called()

    def test_no_username_skips(self, storage):
        mgr, _ = _ln_manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        out = mgr.ensure_ln_pool("me@example.com", profile={"ln_username": None})
        assert out["reason"] == "no_ln_username"


# ---------------------------------------------------------------------------
# Manager: purchase LN username (on-chain L-BTC)
# ---------------------------------------------------------------------------


class TestPurchaseLnUsername:
    def test_happy_path(self, storage):
        mgr, wm = _ln_manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        fake_tx = MagicMock()
        fake_tx.txid.return_value = "computed-txid"
        order = {
            "payment_id": "pay1",
            "address": VAULT_ADDR,
            "amount_base_units": 777,
            "asset_ticker": "L-BTC",
            "amount": "0.00000777",
            "expires_at": "2026-07-03T18:13:55Z",
        }
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=_route({
                    "/payment-request/ln-username/": order,
                    "/payment/submit-raw-tx/": {"status": "PENDING"},
                }),
            ),
            patch("aqua.jan3_accounts.lwk.Transaction", return_value=fake_tx),
        ):
            out = mgr.purchase_ln_username("me@example.com", "alice", confirm=True)
        assert out["confirmed"] is True
        assert out["txid"] == "computed-txid"
        assert out["status"] == "PENDING"
        assert out["ln_username"] == "alice"
        assert out["amount_base_units"] == 777
        assert out["amount"] == "0.00000777"
        assert out["expires_at"] == "2026-07-03T18:13:55Z"
        assert out["display_amount"] == "777 Sats"
        wm.craft_raw_tx.assert_called_once()

    def test_dry_run_quote_does_not_sign(self, storage):
        mgr, wm = _ln_manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        order = {
            "payment_id": "pay1",
            "address": VAULT_ADDR,
            "amount_base_units": 8_000_000,
            "asset_ticker": "USDt",
            "amount": "0.08000000",
            "expires_at": "2026-07-03T18:13:55Z",
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/payment-request/ln-username/": order}),
        ):
            out = mgr.purchase_ln_username("me@example.com", "alice", asset="USDt")
        assert out["requires_confirmation"] is True
        assert out["confirmed"] is False
        assert out["amount_base_units"] == 8_000_000
        assert out["display_amount"] == "0.08 USDT"
        assert out["expires_at"] == "2026-07-03T18:13:55Z"
        assert "txid" not in out
        wm.craft_raw_tx.assert_not_called()

    def test_missing_fields_raises(self, storage):
        mgr, _ = _ln_manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        order = {"payment_id": None, "address": None, "amount_base_units": 0}
        with patch(
            "urllib.request.urlopen",
            side_effect=_route({"/payment-request/ln-username/": order}),
        ):
            with pytest.raises(ValueError, match="missing fields"):
                mgr.purchase_ln_username("me@example.com", "alice", confirm=True)

    def test_invalid_username_raises_before_network(self, storage):
        mgr, _ = _ln_manager(storage)
        _seed_session(mgr, "me@example.com", access=FUTURE_JWT)
        with pytest.raises(ValueError, match="Invalid ln_username"):
            mgr.purchase_ln_username("me@example.com", "bad..name")

