"""Tests for JAN3 Accounts integration (login + purchases).

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
    _validate_ln_username,
)
from aqua.lnurl import resolve_lightning_address
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

    def test_validate_ln_username_lowercases(self):
        assert _validate_ln_username("AliceBob") == "alicebob"

    @pytest.mark.parametrize("ok", ["abcd", "ab.cd", "alice.bob", "u" * 64])
    def test_validate_ln_username_accepts(self, ok):
        assert _validate_ln_username(ok) == ok.lower()

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "abc",  # too short (<4)
            "u" * 65,  # too long
            "Has Space",
            "a..b",  # two dots
            "a-b",  # hyphen not allowed
            "..",
        ],
    )
    def test_validate_ln_username_rejects(self, bad):
        with pytest.raises(ValueError, match="Invalid ln_username"):
            _validate_ln_username(bad)

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

    @patch("urllib.request.urlopen")
    def test_authed_request_injects_bearer(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"payment_id": "p1"})
        client = Jan3AccountsClient(
            base_url="https://test.aquabtc.com", access_token="tok-xyz"
        )
        client.create_ln_username_payment_request(asset="L-BTC", ln_username="alice")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer tok-xyz"

    def test_authed_request_without_token_raises(self):
        client = Jan3AccountsClient(base_url="https://test.aquabtc.com")
        with pytest.raises(ValueError, match="requires an access_token"):
            client.create_ln_username_payment_request("L-BTC", "alice")


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


# ---------------------------------------------------------------------------
# Purchase flow
# ---------------------------------------------------------------------------


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


class TestPurchaseLnUsername:
    PR_RESPONSE = {
        "payment_id": "11111111-1111-1111-1111-111111111111",
        "status": "PENDING",
        "product_type": "LN_USERNAME_UPDATE",
        "asset_ticker": "L-BTC",
        "amount": "0.00001000",
        "amount_base_units": 1000,
        "address": VAULT_ADDR,
        "expires_at": "2026-06-16T01:00:00+00:00",
        "product_details": {"ln_username": "alicebob"},
    }
    SUBMIT_RESPONSE = {**PR_RESPONSE, "status": "ACCEPTED"}

    def _raw_hex(self):
        # A real signed Liquid tx is far too large to inline; use a known-good
        # short hex that lwk.Transaction will accept. The purchase flow only
        # calls lwk.Transaction(...).txid() — mock that instead.
        return "DEADBEEF" * 16

    def test_happy_path(self, storage):
        mgr = _manager(storage, raw_tx=self._raw_hex())
        _seed_session(mgr, "me@example.com")

        fake_tx = MagicMock()
        fake_tx.txid.return_value = "computed-txid"

        with (
            patch.object(
                Jan3AccountsClient,
                "create_ln_username_payment_request",
                return_value=self.PR_RESPONSE,
            ),
            patch.object(
                Jan3AccountsClient,
                "submit_raw_tx",
                return_value=self.SUBMIT_RESPONSE,
            ) as mock_submit,
            patch("aqua.jan3_accounts.lwk.Transaction", return_value=fake_tx),
        ):
            result = mgr.purchase_ln_username(
                email="me@example.com",
                ln_username="alicebob",
                wallet_name="default",
            )

        assert result["payment_id"] == self.PR_RESPONSE["payment_id"]
        assert result["status"] == "ACCEPTED"
        assert result["txid"] == "computed-txid"
        assert result["ln_username"] == "alicebob"
        assert result["amount_sats"] == 1000
        mock_submit.assert_called_once()
        kwargs = mock_submit.call_args.kwargs
        assert kwargs["payment_id"] == self.PR_RESPONSE["payment_id"]
        assert kwargs["raw_tx"] == self._raw_hex()
        # Wallet was asked to craft a tx for the exact amount/address.
        mgr.wallet_manager.craft_raw_tx.assert_called_once_with(
            wallet_name="default",
            address=VAULT_ADDR,
            amount=1000,
            asset_id=LBTC_ASSET_ID,
            password=None,
        )

    def test_requires_session(self, storage):
        mgr = _manager(storage)
        with pytest.raises(ValueError, match="No JAN3 session"):
            mgr.purchase_ln_username(email="ghost@example.com", ln_username="alicebob")

    def test_rejects_invalid_username_before_network(self, storage):
        mgr = _manager(storage)
        _seed_session(mgr, "me@example.com")
        with patch.object(Jan3AccountsClient, "create_ln_username_payment_request") as mock_create:
            with pytest.raises(ValueError, match="Invalid ln_username"):
                mgr.purchase_ln_username(email="me@example.com", ln_username="bad..username")
        mock_create.assert_not_called()

    def test_refreshes_access_token_on_401(self, storage):
        """First call gets 401 → refresh → second call succeeds."""
        mgr = _manager(storage, raw_tx=self._raw_hex())
        _seed_session(mgr, "me@example.com")

        fake_tx = MagicMock()
        fake_tx.txid.return_value = "txid-after-refresh"

        # Sequence: create_ln_username_payment_request succeeds on first try
        # (no 401), then submit_raw_tx returns 401 once, then succeeds after
        # refresh. (The 401 retry wraps each call independently.)
        submit_responses = [
            Jan3UnauthorizedError("expired"),
            self.SUBMIT_RESPONSE,
        ]

        def submit_side_effect(*a, **k):
            resp = submit_responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp

        with (
            patch.object(
                Jan3AccountsClient,
                "create_ln_username_payment_request",
                return_value=self.PR_RESPONSE,
            ),
            patch.object(
                Jan3AccountsClient,
                "submit_raw_tx",
                side_effect=submit_side_effect,
            ),
            patch.object(
                Jan3AccountsClient,
                "refresh_access_token",
                return_value="new-access-token",
            ) as mock_refresh,
            patch("aqua.jan3_accounts.lwk.Transaction", return_value=fake_tx),
        ):
            result = mgr.purchase_ln_username(
                email="me@example.com", ln_username="alicebob"
            )

        assert result["status"] == "ACCEPTED"
        mock_refresh.assert_called_once_with("refresh-token-xyz")
        # New token persisted on disk.
        reloaded = mgr.load_session("me@example.com")
        assert reloaded.access_token == "new-access-token"
        assert reloaded.refreshed_at is not None

    def test_refreshes_access_token_when_first_authed_call_401s(self, storage):
        """The refresh-retry wrapper must kick in for the *first* authed
        call too, not just submit. Earlier coverage only exercised the
        second call — this fills the gap."""
        mgr = _manager(storage, raw_tx=self._raw_hex())
        _seed_session(mgr, "me@example.com")

        create_responses = [
            Jan3UnauthorizedError("expired"),
            self.PR_RESPONSE,
        ]

        def create_side_effect(*a, **k):
            resp = create_responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp

        fake_tx = MagicMock()
        fake_tx.txid.return_value = "txid"

        with (
            patch.object(
                Jan3AccountsClient,
                "create_ln_username_payment_request",
                side_effect=create_side_effect,
            ),
            patch.object(
                Jan3AccountsClient,
                "submit_raw_tx",
                return_value=self.SUBMIT_RESPONSE,
            ),
            patch.object(
                Jan3AccountsClient,
                "refresh_access_token",
                return_value="new-access-token",
            ) as mock_refresh,
            patch("aqua.jan3_accounts.lwk.Transaction", return_value=fake_tx),
        ):
            result = mgr.purchase_ln_username(
                email="me@example.com", ln_username="alicebob"
            )

        assert result["status"] == "ACCEPTED"
        mock_refresh.assert_called_once_with("refresh-token-xyz")
        reloaded = mgr.load_session("me@example.com")
        assert reloaded.access_token == "new-access-token"

    def test_post_refresh_401_wipes_session(self, storage):
        """Submit 401 → refresh OK → submit 401 again → session deleted.

        Server-side revocation (account banned, key rotated, etc.) gives
        us a successful refresh but the next authed call still 401s. The
        local session is poisoned — wipe it so the user is forced to
        re-login rather than retrying forever."""
        mgr = _manager(storage, raw_tx=self._raw_hex())
        _seed_session(mgr, "me@example.com")

        with (
            patch.object(
                Jan3AccountsClient,
                "create_ln_username_payment_request",
                return_value=self.PR_RESPONSE,
            ),
            patch.object(
                Jan3AccountsClient,
                "submit_raw_tx",
                side_effect=Jan3UnauthorizedError("server says no"),
            ),
            patch.object(
                Jan3AccountsClient,
                "refresh_access_token",
                return_value="new-access-token",
            ),
            patch("aqua.jan3_accounts.lwk.Transaction", return_value=MagicMock()),
        ):
            with pytest.raises(Jan3UnauthorizedError):
                mgr.purchase_ln_username(
                    email="me@example.com", ln_username="alicebob"
                )

        assert mgr.load_session("me@example.com") is None

    def test_double_401_wipes_session(self, storage):
        """Submit 401 → refresh 401 → session deleted, error raised."""
        mgr = _manager(storage, raw_tx=self._raw_hex())
        _seed_session(mgr, "me@example.com")

        with (
            patch.object(
                Jan3AccountsClient,
                "create_ln_username_payment_request",
                return_value=self.PR_RESPONSE,
            ),
            patch.object(
                Jan3AccountsClient,
                "submit_raw_tx",
                side_effect=Jan3UnauthorizedError("expired"),
            ),
            patch.object(
                Jan3AccountsClient,
                "refresh_access_token",
                side_effect=Jan3UnauthorizedError("refresh also expired"),
            ),
            patch("aqua.jan3_accounts.lwk.Transaction", return_value=MagicMock()),
        ):
            with pytest.raises(Jan3UnauthorizedError):
                mgr.purchase_ln_username(email="me@example.com", ln_username="alicebob")

        assert mgr.load_session("me@example.com") is None


# ---------------------------------------------------------------------------
# Refresh-token flow (issue #39): expired access token → refresh → retry
# ---------------------------------------------------------------------------


class TestRefreshFlow:
    """End-to-end verification of the access-token refresh contract.

    Documents that the implementation:
      1. Persists BOTH access_token and refresh_token after login.
      2. Detects access-token expiry (HTTP 401) on any authed call.
      3. Calls /api/v1/auth/refresh/ with the STORED refresh_token
         (and no Authorization header — the bearer has just expired).
      4. Persists the new access_token while preserving refresh_token.
      5. Retries the original authed call with the new bearer.
    """

    @patch("urllib.request.urlopen")
    def test_refresh_endpoint_wire_shape(self, mock_urlopen):
        """The refresh call hits /api/v1/auth/refresh/ with ``{"refresh": …}``
        and no Authorization header (the access token has just expired,
        so presenting it would be useless or worse)."""
        mock_urlopen.return_value = _mock_response({"access": "new-tok"})
        client = Jan3AccountsClient(
            base_url="https://test.aquabtc.com",
            access_token="expired-tok",
        )

        assert client.refresh_access_token("the-refresh-token") == "new-tok"

        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.full_url.endswith("/api/v1/auth/refresh/")
        assert req.get_header("Authorization") is None
        assert json.loads(req.data.decode()) == {"refresh": "the-refresh-token"}

    @patch("urllib.request.urlopen")
    def test_refresh_endpoint_missing_access_field_raises(self, mock_urlopen):
        """A 200 OK with no ``access`` field is a server bug — surface it
        as a clear RuntimeError rather than silently storing ``None``."""
        mock_urlopen.return_value = _mock_response({})
        client = Jan3AccountsClient(base_url="https://test.aquabtc.com")
        with pytest.raises(RuntimeError, match="no access token"):
            client.refresh_access_token("the-refresh-token")

    def test_full_round_trip_on_access_token_expiry(self, storage):
        """Login → persist BOTH tokens → 401 on authed call → refresh using
        the stored refresh_token → persist new access_token (refresh_token
        unchanged) → retry call uses the new bearer."""
        mgr = _manager(storage, raw_tx="hex-tx")

        # (1) Login persists access + refresh together on disk.
        with patch.object(
            Jan3AccountsClient,
            "verify_otp",
            return_value={
                "access": "old-access",
                "refresh": "the-refresh-token",
            },
        ):
            mgr.complete_login(email="me@example.com", otp_code="123456")

        before = mgr.load_session("me@example.com")
        assert before.access_token == "old-access"
        assert before.refresh_token == "the-refresh-token"
        assert before.refreshed_at is None

        # Capture the access_token each submit_raw_tx call sees so we can
        # prove the retry used the NEW token (not the stale one).
        tokens_seen: list[str] = []
        submit_calls = {"n": 0}

        def submit_side_effect(client_self, *args, **kwargs):
            tokens_seen.append(client_self.access_token)
            submit_calls["n"] += 1
            if submit_calls["n"] == 1:
                raise Jan3UnauthorizedError("token expired")
            return TestPurchaseLnUsername.SUBMIT_RESPONSE

        fake_tx = MagicMock()
        fake_tx.txid.return_value = "tx-after-refresh"

        with (
            patch.object(
                Jan3AccountsClient,
                "create_ln_username_payment_request",
                return_value=TestPurchaseLnUsername.PR_RESPONSE,
            ),
            patch.object(
                Jan3AccountsClient,
                "submit_raw_tx",
                autospec=True,
                side_effect=submit_side_effect,
            ),
            patch.object(
                Jan3AccountsClient,
                "refresh_access_token",
                return_value="new-access",
            ) as mock_refresh,
            patch("aqua.jan3_accounts.lwk.Transaction", return_value=fake_tx),
        ):
            result = mgr.purchase_ln_username(
                email="me@example.com", ln_username="alicebob"
            )

        # (2/3) Refresh used the STORED refresh_token — not invented, not blank.
        mock_refresh.assert_called_once_with("the-refresh-token")

        # (5) First submit got the old token (and 401'd); the retry got the
        # new token (and succeeded). Proves the retry rebuilt its client.
        assert tokens_seen == ["old-access", "new-access"]
        assert result["status"] == "ACCEPTED"

        # (4) On-disk session after the refresh:
        #   • access_token replaced with the new value
        #   • refresh_token PRESERVED — the server's /auth/refresh/ contract
        #     returns only {access}; we must not clobber refresh locally
        #   • refreshed_at populated
        after = mgr.load_session("me@example.com")
        assert after.access_token == "new-access"
        assert after.refresh_token == "the-refresh-token"
        assert after.refreshed_at is not None


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


# ---------------------------------------------------------------------------
# Purchase + LNURL resolution linkage
# ---------------------------------------------------------------------------


def _lnurl_response(data: dict, final_url: str | None = None) -> MagicMock:
    """urlopen response mock for LNURL-leg patches.

    Distinct from ``_mock_response`` above because ``aqua.lnurl._http_get_json``
    calls ``resp.geturl()`` to enforce the post-redirect https guard.
    """
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.geturl.return_value = final_url or "https://example.com/x"
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _payreq(
    *,
    callback: str = "https://test.aqua.net/lnurlp/cb",
    min_msat: int = 1_000,
    max_msat: int = 100_000_000_000,
    metadata: str = '[["text/plain","pay alice"]]',
) -> dict:
    return {
        "tag": "payRequest",
        "callback": callback,
        "minSendable": min_msat,
        "maxSendable": max_msat,
        "metadata": metadata,
    }


class TestPurchaseThenResolve:
    """End-to-end (mocked) linkage: buy a JAN3 LN username and resolve the
    resulting ``<username>@<jan3-domain>`` Lightning Address via aqua.lnurl.

    Proves our code wires the two halves together — that the bare
    ``ln_username`` returned by ``purchase_ln_username`` composes cleanly
    into a ``.well-known/lnurlp/`` lookup that yields a payable BOLT11.
    The real-backend version of this check lives in
    ``tests/smoke/test_jan3_lightning.py``.
    """

    LN_USERNAME = "alicebob"
    LN_DOMAIN = "test.aqua.net"
    AMOUNT_SATS = 100
    INVOICE = "lnbc1u1ptest_jan3_invoice"

    PR_RESPONSE = {
        "payment_id": "11111111-1111-1111-1111-111111111111",
        "status": "PENDING",
        "product_type": "LN_USERNAME_UPDATE",
        "asset_ticker": "L-BTC",
        "amount_base_units": 1000,
        "address": VAULT_ADDR,
        "expires_at": "2026-06-16T01:00:00+00:00",
    }
    SUBMIT_RESPONSE = {**PR_RESPONSE, "status": "ACCEPTED"}

    def _purchase(self, mgr: Jan3AccountsManager) -> dict:
        """Drive ``purchase_ln_username`` with all HTTP + signing mocked."""
        fake_tx = MagicMock()
        fake_tx.txid.return_value = "computed-txid"
        with (
            patch.object(
                Jan3AccountsClient,
                "create_ln_username_payment_request",
                return_value=self.PR_RESPONSE,
            ),
            patch.object(
                Jan3AccountsClient,
                "submit_raw_tx",
                return_value=self.SUBMIT_RESPONSE,
            ),
            patch("aqua.jan3_accounts.lwk.Transaction", return_value=fake_tx),
        ):
            return mgr.purchase_ln_username(
                email="me@example.com",
                ln_username=self.LN_USERNAME,
                wallet_name="default",
            )

    def test_purchase_then_resolve_returns_invoice(self, storage):
        """Happy path: buy username → resolve <user>@<domain> → BOLT11."""
        mgr = _manager(storage, raw_tx="DEADBEEF" * 16)
        _seed_session(mgr, "me@example.com")

        purchase = self._purchase(mgr)
        assert purchase["ln_username"] == self.LN_USERNAME
        address = f"{purchase['ln_username']}@{self.LN_DOMAIN}"

        with (
            patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen,
            patch(
                "aqua.lnurl.decode_bolt11_amount_sats",
                return_value=self.AMOUNT_SATS,
            ),
        ):
            mock_urlopen.side_effect = [
                _lnurl_response(
                    _payreq(),
                    final_url=(
                        f"https://{self.LN_DOMAIN}/.well-known/lnurlp/"
                        f"{self.LN_USERNAME}"
                    ),
                ),
                _lnurl_response({"pr": self.INVOICE}),
            ]
            invoice = resolve_lightning_address(address, self.AMOUNT_SATS)

        assert invoice == self.INVOICE
        first_req = mock_urlopen.call_args_list[0].args[0]
        assert first_req.full_url == (
            f"https://{self.LN_DOMAIN}/.well-known/lnurlp/{self.LN_USERNAME}"
        )
        second_req = mock_urlopen.call_args_list[1].args[0]
        assert f"amount={self.AMOUNT_SATS * 1000}" in second_req.full_url

    def test_resolve_amount_outside_bounds_raises(self, storage):
        """Purchase succeeds, but payRequest declares bounds that exclude
        the requested amount — surface as ValueError from aqua.lnurl."""
        mgr = _manager(storage, raw_tx="DEADBEEF" * 16)
        _seed_session(mgr, "me@example.com")

        purchase = self._purchase(mgr)
        address = f"{purchase['ln_username']}@{self.LN_DOMAIN}"

        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            # min 1000 sats, max 10000 sats; we'll request 100 sats.
            mock_urlopen.return_value = _lnurl_response(
                _payreq(min_msat=1_000_000, max_msat=10_000_000),
                final_url=(
                    f"https://{self.LN_DOMAIN}/.well-known/lnurlp/"
                    f"{self.LN_USERNAME}"
                ),
            )
            with pytest.raises(ValueError, match="outside Lightning Address bounds"):
                resolve_lightning_address(address, self.AMOUNT_SATS)

    def test_resolve_amount_mismatched_invoice_raises(self, storage):
        """Purchase succeeds, callback returns a BOLT11 encoding a different
        amount than we asked for — surface as ValueError from aqua.lnurl."""
        mgr = _manager(storage, raw_tx="DEADBEEF" * 16)
        _seed_session(mgr, "me@example.com")

        purchase = self._purchase(mgr)
        address = f"{purchase['ln_username']}@{self.LN_DOMAIN}"

        with (
            patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen,
            patch("aqua.lnurl.decode_bolt11_amount_sats", return_value=50),
        ):
            mock_urlopen.side_effect = [
                _lnurl_response(
                    _payreq(),
                    final_url=(
                        f"https://{self.LN_DOMAIN}/.well-known/lnurlp/"
                        f"{self.LN_USERNAME}"
                    ),
                ),
                _lnurl_response({"pr": self.INVOICE}),
            ]
            with pytest.raises(ValueError, match="does not match"):
                resolve_lightning_address(address, self.AMOUNT_SATS)
