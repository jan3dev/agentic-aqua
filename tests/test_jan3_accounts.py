"""Tests for JAN3 Accounts paid captchaless login.

Patches ``Jan3AccountsClient`` methods on the class so the manager-level
tests never touch the network; the wallet manager is faked too since
``craft_raw_tx`` is exercised in test_tools.py.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aqua.jan3_accounts import (
    AQUA_ANKARA_API_URL,
    ASSET_TICKER_LBTC,
    Jan3AccountsClient,
    Jan3AccountsManager,
    Jan3Session,
    Jan3UnauthorizedError,
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
        base_url=AQUA_ANKARA_API_URL,
    )


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


# ---------------------------------------------------------------------------
# Jan3Session round-trip
# ---------------------------------------------------------------------------


class TestSessionRoundtrip:
    def test_to_from_dict(self):
        s = Jan3Session(
            email="me@example.com",
            base_url="https://ankara.aquabtc.com",
            access_token="A" * 40,
            refresh_token="R" * 40,
            created_at="2026-06-16T00:00:00+00:00",
            captcha_exempt=True,
        )
        s2 = Jan3Session.from_dict(s.to_dict())
        assert s2 == s

    def test_from_dict_backfills_optional_fields(self):
        """Legacy files without refreshed_at / captcha_exempt should load."""
        s = Jan3Session.from_dict({
            "email": "x@y.com",
            "base_url": "https://ankara.aquabtc.com",
            "access_token": "a",
            "refresh_token": "r",
            "created_at": "now",
        })
        assert s.refreshed_at is None
        assert s.captcha_exempt is False


# ---------------------------------------------------------------------------
# Jan3AccountsClient HTTP layer
# ---------------------------------------------------------------------------


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


class TestHttpClient:
    @patch("urllib.request.urlopen")
    def test_get_vault_payment_address(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"address": VAULT_ADDR})
        client = Jan3AccountsClient(base_url="https://ankara.aquabtc.com")
        assert client.get_vault_payment_address() == VAULT_ADDR
        # The path is the documented one.
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith("/api/v1/liquid-wallet/payment/receive-address/")

    @patch("urllib.request.urlopen")
    def test_get_vault_payment_address_empty_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"address": ""})
        client = Jan3AccountsClient(base_url="https://ankara.aquabtc.com")
        with pytest.raises(RuntimeError, match="no address"):
            client.get_vault_payment_address()

    @patch("urllib.request.urlopen")
    def test_get_product_price_selects_matching_row(self, mock_urlopen):
        rows = [
            {
                "product_type": "OTHER",
                "lbtc_sats_price": 1,
                "usdt_base_units_price": 1,
                "usdt_display_price": "1",
            },
            {
                "product_type": "CAPTCHALESS_LOGIN",
                "lbtc_sats_price": 100,
                "usdt_base_units_price": 200,
                "usdt_display_price": "0.10",
            },
        ]
        mock_urlopen.return_value = _mock_response(rows)
        client = Jan3AccountsClient(base_url="https://ankara.aquabtc.com")
        row = client.get_product_price("CAPTCHALESS_LOGIN")
        assert row["lbtc_sats_price"] == 100

    @patch("urllib.request.urlopen")
    def test_get_product_price_missing_row_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response([])
        client = Jan3AccountsClient(base_url="https://ankara.aquabtc.com")
        with pytest.raises(RuntimeError, match="no entry for"):
            client.get_product_price("LN_USERNAME_UPDATE")

    @patch("urllib.request.urlopen")
    def test_login_v2_body_carries_login_challenge(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"message": "ok"})
        client = Jan3AccountsClient(base_url="https://ankara.aquabtc.com")
        result = client.login_v2(
            email="me@example.com",
            language="en",
            raw_tx="DEADBEEF",
            payment_address=VAULT_ADDR,
        )
        assert result == {"message": "ok"}
        # Verify request body shape.
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["email"] == "me@example.com"
        assert body["language"] == "en"
        assert body["login_challenge"] == {
            "raw_tx": "DEADBEEF",
            "payment_address": VAULT_ADDR,
        }
        assert req.full_url.endswith("/api/v2/auth/login/")

    @patch("urllib.request.urlopen")
    def test_login_v2_without_challenge_omits_field(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"message": "ok"})
        client = Jan3AccountsClient(base_url="https://ankara.aquabtc.com")
        client.login_v2(email="me@example.com")
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert "login_challenge" not in body

    @patch("urllib.request.urlopen")
    def test_401_raises_unauthorized(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="https://ankara.aquabtc.com/api/v1/auth/verify/",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"message": "bad otp"}).encode()),
        )
        mock_urlopen.side_effect = err
        client = Jan3AccountsClient(base_url="https://ankara.aquabtc.com")
        with pytest.raises(Jan3UnauthorizedError):
            client.verify_otp("me@example.com", "000000")

    @patch("urllib.request.urlopen")
    def test_other_http_error_raises_runtime(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="x", code=400, msg="Bad",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"message": "CAPTCHA_REQUIRED"}).encode()),
        )
        mock_urlopen.side_effect = err
        client = Jan3AccountsClient(base_url="https://ankara.aquabtc.com")
        with pytest.raises(RuntimeError, match="CAPTCHA_REQUIRED"):
            client.login_v2(email="me@example.com")


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


class TestRequestLogin:
    def test_happy_path(self, storage):
        mgr = _manager(storage, raw_tx="hex-tx")
        with (
            patch.object(Jan3AccountsClient, "get_vault_payment_address", return_value=VAULT_ADDR),
            patch.object(
                Jan3AccountsClient,
                "get_product_price",
                return_value={"lbtc_sats_price": 100},
            ),
            patch.object(
                Jan3AccountsClient,
                "login_v2",
                return_value={"message": "OTP sent"},
            ) as mock_login,
        ):
            result = mgr.request_login(
                email="ME@Example.com",
                wallet_name="default",
                password=None,
                language="en",
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
        mock_login.assert_called_once_with(
            email="me@example.com",
            language="en",
            raw_tx="hex-tx",
            payment_address=VAULT_ADDR,
        )
        # No session is persisted yet — complete_login does that.
        assert mgr.load_session("me@example.com") is None

    def test_blocked_when_disabled(self, storage):
        """A 403 CAPTCHALESS_LOGIN_DISABLED from the server surfaces as RuntimeError."""
        mgr = _manager(storage)
        with (
            patch.object(Jan3AccountsClient, "get_vault_payment_address", return_value=VAULT_ADDR),
            patch.object(
                Jan3AccountsClient,
                "get_product_price",
                return_value={"lbtc_sats_price": 100},
            ),
            patch.object(
                Jan3AccountsClient,
                "login_v2",
                side_effect=RuntimeError(
                    "AQUA API error (403 POST /api/v2/auth/login/): CAPTCHALESS_LOGIN_DISABLED"
                ),
            ),
        ):
            with pytest.raises(RuntimeError, match="CAPTCHALESS_LOGIN_DISABLED"):
                mgr.request_login(email="me@example.com")

    def test_rejects_invalid_email(self, storage):
        mgr = _manager(storage)
        with pytest.raises(ValueError, match="Invalid email"):
            mgr.request_login(email="not-an-email")

    def test_rejects_non_positive_price(self, storage):
        """If the server seeded the price at zero, we refuse to craft a zero-amount tx."""
        mgr = _manager(storage)
        with (
            patch.object(Jan3AccountsClient, "get_vault_payment_address", return_value=VAULT_ADDR),
            patch.object(
                Jan3AccountsClient,
                "get_product_price",
                return_value={"lbtc_sats_price": 0},
            ),
        ):
            with pytest.raises(RuntimeError, match="non-positive"):
                mgr.request_login(email="me@example.com")


class TestCompleteLogin:
    def test_persists_session(self, storage):
        mgr = _manager(storage)
        with patch.object(
            Jan3AccountsClient,
            "verify_otp",
            return_value={"access": "A" * 40, "refresh": "R" * 40},
        ):
            result = mgr.complete_login(email="me@example.com", otp_code="123456")

        assert result["captcha_exempt"] is True
        assert result["access_token_preview"].startswith("AAAA")
        # The refresh token is the long-lived secret — its preview must
        # never appear in tool output (would land in MCP transcripts).
        assert "refresh_token_preview" not in result

        loaded = mgr.load_session("me@example.com")
        assert loaded is not None
        assert loaded.access_token == "A" * 40
        assert loaded.refresh_token == "R" * 40
        assert loaded.captcha_exempt is True

        # File permissions: stash secrets at 0o600 on POSIX. (Skip on Windows
        # where chmod is largely a no-op — the atomic-write helper attempts
        # it best-effort.)
        if os.name == "posix":
            path = mgr._session_path("me@example.com")
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600

    def test_propagates_invalid_otp(self, storage):
        mgr = _manager(storage)
        with patch.object(
            Jan3AccountsClient,
            "verify_otp",
            side_effect=Jan3UnauthorizedError("bad OTP"),
        ):
            with pytest.raises(Jan3UnauthorizedError):
                mgr.complete_login(email="me@example.com", otp_code="000000")
        assert mgr.load_session("me@example.com") is None

    def test_rejects_blank_otp(self, storage):
        mgr = _manager(storage)
        with pytest.raises(ValueError, match="otp_code is required"):
            mgr.complete_login(email="me@example.com", otp_code="   ")

    def test_rejects_missing_tokens(self, storage):
        mgr = _manager(storage)
        with patch.object(
            Jan3AccountsClient,
            "verify_otp",
            return_value={"access": "only-access"},
        ):
            with pytest.raises(RuntimeError, match="both access and refresh"):
                mgr.complete_login(email="me@example.com", otp_code="123456")


def _seed_session(mgr: Jan3AccountsManager, email: str) -> Jan3Session:
    session = Jan3Session(
        email=email,
        base_url=mgr.base_url,
        access_token="initial-access",
        refresh_token="refresh-token-xyz",
        created_at="2026-06-16T00:00:00+00:00",
        captcha_exempt=True,
    )
    mgr.save_session(session)
    return session


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


class TestSessionManagement:
    def test_logout_deletes_file(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com")
        assert mgr.load_session("me@example.com") is not None

        assert mgr.delete_session("me@example.com") is True
        assert mgr.load_session("me@example.com") is None

    def test_logout_idempotent(self, storage):
        mgr = _manager(storage)
        assert mgr.delete_session("ghost@example.com") is False

    def test_list_sessions(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "a@example.com")
        _seed_session(mgr, "b@example.com")
        emails = {s.email for s in mgr.list_sessions()}
        assert emails == {"a@example.com", "b@example.com"}

    def test_session_file_is_under_jan3_accounts_dir(self, storage):
        mgr = _manager(storage)
        path = mgr._session_path("me@example.com")
        assert path.parent == storage.jan3_accounts_dir

    def test_load_session_tolerates_corrupted_file(self, storage):
        """A corrupted on-disk session shouldn't break every JAN3 tool —
        treat it as missing so the caller can prompt a re-login."""
        mgr = _manager(storage)
        path = mgr._session_path("me@example.com")
        path.write_text("{not valid json")
        assert mgr.load_session("me@example.com") is None

    def test_load_session_tolerates_missing_required_fields(self, storage):
        """A file that's valid JSON but missing required dataclass fields
        (e.g., refresh_token) should also be treated as missing."""
        mgr = _manager(storage)
        path = mgr._session_path("me@example.com")
        path.write_text('{"email": "me@example.com"}')
        assert mgr.load_session("me@example.com") is None
