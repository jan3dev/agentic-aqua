"""JAN3 / AQUA account management (the ``jan3_*`` tools).

Single home for everything that manages a JAN3/AQUA account against the Ankara
backend (``ANKARA_API_URL`` — env-overridable, staging via
``https://test.aquabtc.com``). Sessions are **multi-account**: one JWT pair per
email, persisted at ``~/.aqua/jan3/{email}.json`` (0o600).

Two login flows, both ending at the same ``/api/v1/auth/verify/`` endpoint:

  * **Free email-OTP (default)** — ``login`` → ``POST /api/v1/auth/login/`` emails
    an OTP; ``verify`` exchanges it for ``{access, refresh}``.
  * **Paid captchaless (fallback)** — ``request_login`` crafts a signed L-BTC tx
    funding AQUA's vault for the CAPTCHALESS_LOGIN price, POSTs it to
    ``POST /api/v2/auth/login/`` (server broadcasts it, flips ``captcha_exempt``,
    emails the OTP); ``verify`` then completes it.

The WapuPay API-key provisioning call (``POST /api/v1/wapupay/account/``) also
lives here and works with a session from *either* flow — it only needs the JWT.

Shared HTTP/PII/JWT helpers (``_redact`` / ``_mask`` / ``_extract_error_message``
/ ``_jwt_exp`` / ``_access_token_expired`` / ``SessionExpiredError``) are imported
from ``ankara.py``; the dependency is one-way (``ankara`` never imports this).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import lwk

from .ankara import (
    ANKARA_API_URL,
    HTTP_TIMEOUT_SECONDS,
    USER_AGENT,
    SessionExpiredError,
    _access_token_expired,
    _extract_error_message,
)
from .assets import resolve_liquid_asset_id
from .storage import Storage
from .wallet import WalletManager

logger = logging.getLogger(__name__)


# Auth/account endpoints (same Ankara host as Lightning).
AUTH_BASE_PATH_V1 = "/api/v1/auth"
AUTH_BASE_PATH_V2 = "/api/v2/auth"
WAPUPAY_ACCOUNT_PATH = "/api/v1/wapupay/account/"

PRODUCT_TYPE_CAPTCHALESS_LOGIN = "CAPTCHALESS_LOGIN"

ASSET_TICKER_LBTC = "L-BTC"
ASSET_TICKER_USDT = "USDt"

# LN-username / LN-address endpoints (same Ankara host).
LN_USERNAME_PAYMENT_REQUEST_PATH = "/api/v1/liquid-wallet/payment-request/ln-username/"
SUBMIT_RAW_TX_PATH = "/api/v1/liquid-wallet/payment/submit-raw-tx/"

# Server-side per-call cap on address registration, and the app's default pool
# size to register when the server doesn't report how many are needed.
MAX_ADDRESSES_PER_REGISTRATION = 15
DEFAULT_LN_ADDRESS_POOL = 5

# Minimal email syntax check — the server validates properly; this just
# catches obvious mistakes before we make a network call.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Mirrors aqua-ankara's common.constants.LN_USERNAME_REGEX so we fail fast
# before a network call: lowercase alphanumerics with at most one dot.
LN_USERNAME_REGEX = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)?$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _token_preview(token: str) -> str:
    """Return a short, log-safe preview of a token (``abcd…wxyz``).

    Tokens shorter than 12 chars are fully redacted rather than half-shown:
    a 4-char token printed as ``ab…cd`` reveals the entire secret.
    """
    if not token:
        return ""
    if len(token) < 12:
        return "…"
    return f"{token[:4]}…{token[-4:]}"


def _validate_email(email: str) -> str:
    email = (email or "").strip()
    if not email or not _EMAIL_RE.match(email):
        raise ValueError(f"Invalid email address: {email!r}")
    return email.lower()


def _validate_ln_username(username: str) -> str:
    username = (username or "").strip().lower()
    if not (4 <= len(username) <= 64) or not LN_USERNAME_REGEX.match(username):
        raise ValueError(
            f"Invalid ln_username {username!r}: must be 4–64 chars, "
            "lowercase letters and digits, with at most one dot."
        )
    return username


def _email_to_filename(email: str) -> str:
    """Convert an email to a safe filename component.

    JSON files are named after the email but we percent-encode any character
    outside ``[a-z0-9@._-]`` so the filesystem never sees ``..``/``/`` etc.
    ``@`` is intentionally kept verbatim — it's safe on every supported OS
    and makes the filenames human-readable.
    """
    return re.sub(r"[^a-z0-9@._-]", lambda m: f"%{ord(m.group(0)):02x}", email)


@dataclass
class Jan3Session:
    """Persistent record of a JAN3 account login session (one per email).

    ``captcha_exempt`` is True for sessions created via the paid captchaless
    flow; False for the free email-OTP flow. It's informational only — it does
    not affect authentication.
    """

    email: str
    base_url: str
    access_token: str
    refresh_token: str
    created_at: str
    refreshed_at: Optional[str] = None
    captcha_exempt: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Jan3Session":
        # Tolerant load: drop unknown keys, backfill optional fields.
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        data.setdefault("refreshed_at", None)
        data.setdefault("captcha_exempt", False)
        return cls(**data)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class Jan3AccountsClient:
    """HTTP client for the AQUA Ankara account API.

    Stateless except for the optional bearer ``access_token``. Raises
    :class:`SessionExpiredError` on HTTP 401 (recoverable — the manager
    refreshes and retries) and ``ValueError`` on any other backend failure.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or ANKARA_API_URL).rstrip("/")
        self.access_token = access_token

    def _api_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        query: Optional[dict] = None,
        auth: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            cleaned = {k: v for k, v in query.items() if v is not None}
            if cleaned:
                url += "?" + urllib.parse.urlencode(cleaned)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.access_token:
                raise ValueError("Authenticated request requires an access_token")
            headers["Authorization"] = f"Bearer {self.access_token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode()
            except Exception:
                pass
            detail = _extract_error_message(body_text)
            if e.code == 401:
                # Recoverable — manager refreshes the token and retries once.
                msg = f"AQUA session token rejected (401 {method} {path})"
                if detail:
                    msg += f": {detail}"
                raise SessionExpiredError(msg) from e
            if e.code == 403:
                raise ValueError(
                    f"AQUA backend forbade the request (403 {method} {path})"
                    + (f": {detail}" if detail else "")
                ) from e
            if e.code == 502:
                raise ValueError(
                    f"AQUA upstream error (502 {method} {path}) — try again shortly."
                ) from e
            msg = f"AQUA API error ({e.code} {method} {path})"
            if detail:
                msg += f": {detail}"
            raise ValueError(msg) from e
        except urllib.error.URLError as e:
            raise ValueError(f"AQUA API unreachable ({method} {path}): {e.reason}") from e

    # ─── public (no-auth) endpoints ────────────────────────────────────

    def get_vault_payment_address(self) -> str:
        result = self._api_request(
            "GET", "/api/v1/liquid-wallet/payment/receive-address/"
        )
        address = (result or {}).get("address")
        if not address:
            raise ValueError(
                "AQUA payment receive-address endpoint returned no address"
            )
        return address

    def get_product_price(self, product_type: str) -> dict:
        """GET /products/?product_type=... — returns the single matching row."""
        result = self._api_request(
            "GET",
            "/api/v1/liquid-wallet/products/",
            query={"product_type": product_type},
        )
        rows = result if isinstance(result, list) else []
        for row in rows:
            if row.get("product_type") == product_type:
                return row
        raise ValueError(
            f"AQUA products endpoint returned no entry for {product_type!r}"
        )

    # ─── login / auth endpoints ────────────────────────────────────────

    def login_free(self, email: str, language: str = "en") -> dict:
        """POST /api/v1/auth/login/ — free email-OTP. Returns ``{message[, otp_code]}``."""
        return self._api_request(
            "POST",
            f"{AUTH_BASE_PATH_V1}/login/",
            body={"email": email, "language": language},
        ) or {}

    def login_captchaless(
        self,
        email: str,
        language: str = "en",
        *,
        raw_tx: Optional[str] = None,
        payment_address: Optional[str] = None,
        captcha_token: Optional[str] = None,
    ) -> dict:
        """POST /api/v2/auth/login/ — paid captchaless bypass (raw_tx + payment_address)."""
        challenge: dict[str, str] = {}
        if raw_tx is not None:
            challenge["raw_tx"] = raw_tx
        if payment_address is not None:
            challenge["payment_address"] = payment_address
        if captcha_token is not None:
            challenge["captcha_token"] = captcha_token
        body: dict[str, Any] = {"email": email, "language": language}
        if challenge:
            body["login_challenge"] = challenge
        return self._api_request("POST", f"{AUTH_BASE_PATH_V2}/login/", body=body) or {}

    def verify_otp(
        self,
        email: str,
        otp_code: str,
        fingerprint: Optional[str] = None,
    ) -> dict:
        """POST /api/v1/auth/verify/ — exchange the OTP for ``{access, refresh}``."""
        body: dict[str, Any] = {"email": email, "otp_code": otp_code}
        if fingerprint is not None:
            body["fingerprint"] = fingerprint
        return self._api_request("POST", f"{AUTH_BASE_PATH_V1}/verify/", body=body) or {}

    def refresh_access_token(self, refresh_token: str) -> dict:
        """POST /api/v1/auth/refresh/ — returns ``{access[, refresh]}`` (rotation-aware).

        Raises :class:`SessionExpiredError` (via 401) if the refresh token is
        itself rejected.
        """
        return self._api_request(
            "POST", f"{AUTH_BASE_PATH_V1}/refresh/", body={"refresh": refresh_token}
        ) or {}

    def provision_wapupay_account(self, access_token: str) -> dict:
        """POST /api/v1/wapupay/account/ — create a WapuPay user, return its API key.

        Authenticates with the AQUA JWT (no X-API-Key — this hits AQUA/Ankara).
        Raises :class:`SessionExpiredError` on 401 so the manager can refresh.
        """
        client = Jan3AccountsClient(base_url=self.base_url, access_token=access_token)
        return client._api_request("POST", WAPUPAY_ACCOUNT_PATH, auth=True) or {}

    # ─── LN-address / LN-username (authenticated) ──────────────────────

    def get_user(self) -> dict:
        """GET /api/v1/auth/user/ — the account profile (email, ln_username,
        fingerprint, ln_address_toggled, new_addresses_needed, …)."""
        return self._api_request("GET", f"{AUTH_BASE_PATH_V1}/user/", auth=True) or {}

    def ln_address_toggle(self, enabled: bool) -> dict:
        """POST /api/v1/auth/user/ln-address-toggle/ — opt in/out of LN-address delivery."""
        return self._api_request(
            "POST",
            f"{AUTH_BASE_PATH_V1}/user/ln-address-toggle/",
            body={"enabled": bool(enabled)},
            auth=True,
        ) or {}

    def ln_username_available(self, username: str) -> dict:
        """GET /api/v1/auth/user/ln-username/{u}/is-available — availability check.

        The OpenAPI schema marks this anonymous, but the live deployment requires
        a JWT — so we forward the bearer token whenever the client has one.
        """
        safe = urllib.parse.quote(username, safe="")
        return self._api_request(
            "GET",
            f"{AUTH_BASE_PATH_V1}/user/ln-username/{safe}/is-available",
            auth=bool(self.access_token),
        ) or {}

    def register_addresses(
        self,
        fingerprint: str,
        addresses: list[str],
        override_fingerprint: bool = False,
    ) -> dict:
        """POST /api/v1/auth/user/addresses/ — upload unused Liquid receive addresses.

        ``override_fingerprint=true`` (query string) re-binds the account to this
        wallet's fingerprint and flips ``ln_address_toggled`` back to true.
        """
        return self._api_request(
            "POST",
            f"{AUTH_BASE_PATH_V1}/user/addresses/",
            body={"fingerprint": fingerprint, "addresses": list(addresses)},
            query={"override_fingerprint": "true"} if override_fingerprint else None,
            auth=True,
        ) or {}

    def create_ln_username_payment_request(self, asset: str, ln_username: str) -> dict:
        """POST …/payment-request/ln-username/ — create an LN_USERNAME_UPDATE order."""
        return self._api_request(
            "POST",
            LN_USERNAME_PAYMENT_REQUEST_PATH,
            body={"asset": asset, "ln_username": ln_username},
            auth=True,
        ) or {}

    def submit_raw_tx(self, payment_id: str, raw_tx: str) -> dict:
        """POST …/payment/submit-raw-tx/ — submit the signed funding tx for an order."""
        return self._api_request(
            "POST",
            SUBMIT_RAW_TX_PATH,
            body={"payment_id": payment_id, "raw_tx": raw_tx},
            auth=True,
        ) or {}


# ---------------------------------------------------------------------------
# Manager (login orchestration + multi-account persistence)
# ---------------------------------------------------------------------------


class Jan3AccountsManager:
    """High-level JAN3 account orchestration (multi-account).

    Owns per-email session persistence and both login flows. The underlying
    :class:`Jan3AccountsClient` is stateless; the manager builds one per call
    (or per retry) with the current bearer token.
    """

    def __init__(
        self,
        storage: Storage,
        wallet_manager: WalletManager,
        base_url: Optional[str] = None,
    ) -> None:
        self.storage = storage
        self.wallet_manager = wallet_manager
        self.base_url = (base_url or ANKARA_API_URL).rstrip("/")

    # ─── session persistence ───────────────────────────────────────────

    def _session_path(self, email: str) -> Path:
        email = _validate_email(email)
        return self.storage.jan3_dir / f"{_email_to_filename(email)}.json"

    def load_session(self, email: str) -> Optional[Jan3Session]:
        path = self._session_path(email)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return Jan3Session.from_dict(json.load(f))
        except (OSError, json.JSONDecodeError, TypeError):
            # Corrupted file shouldn't lock the user out of every JAN3 tool.
            logger.warning("Unreadable JAN3 session file: %s", path)
            return None

    def save_session(self, session: Jan3Session) -> None:
        path = self._session_path(session.email)
        self.storage._atomic_write_json(path, session.to_dict())

    def delete_session(self, email: str) -> bool:
        path = self._session_path(email)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list_sessions(self) -> list[Jan3Session]:
        sessions: list[Jan3Session] = []
        for path in self.storage.jan3_dir.glob("*.json"):
            try:
                with open(path) as f:
                    sessions.append(Jan3Session.from_dict(json.load(f)))
            except (OSError, json.JSONDecodeError, TypeError):
                logger.warning("Skipping unreadable JAN3 session file: %s", path)
        sessions.sort(key=lambda s: s.created_at, reverse=True)
        return sessions

    # ─── asset resolution ─────────────────────────────────────────────

    def _resolve_asset_id(self, wallet_name: str, asset_ticker: str) -> str:
        """Resolve an asset ticker (L-BTC / USDt) to a Liquid asset id."""
        wallet = self.wallet_manager.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet {wallet_name!r} not found")
        ticker = (asset_ticker or "").strip()
        if ticker.upper() in ("L-BTC", "LBTC", "BTC"):
            return self.wallet_manager._get_policy_asset(wallet.network)
        resolved = resolve_liquid_asset_id(
            ticker, "liquid", asset_network=wallet.network
        )
        if not resolved:
            raise ValueError(
                f"Cannot resolve asset_id for ticker {asset_ticker!r} on "
                f"{wallet.network}."
            )
        return resolved

    # ─── login flows ──────────────────────────────────────────────────

    def login(self, email: str, language: str = "en") -> dict:
        """Free email-OTP login (default): Ankara emails an OTP. Persists nothing yet."""
        email = _validate_email(email)
        client = Jan3AccountsClient(base_url=self.base_url)
        resp = client.login_free(email, language=language)
        out = {
            "email": email,
            "message": resp.get("message", "An OTP code has been sent to your email."),
            "otp_sent_to": email,
            "next_step": "Call jan3_verify with the OTP code from your email.",
        }
        # Non-prod Ankara (EMAIL_BASED_OTP off) returns the code inline.
        if resp.get("otp_code"):
            out["otp_code"] = resp["otp_code"]
        return out

    def request_login(
        self,
        email: str,
        wallet_name: str = "default",
        password: Optional[str] = None,
        language: str = "en",
    ) -> dict:
        """Paid captchaless login (fallback) step 1: pay the fee and dispatch the OTP.

        Fetches the vault payment address and the CAPTCHALESS_LOGIN price, crafts
        a signed L-BTC tx funding that address, and POSTs everything to
        /api/v2/auth/login/. The server broadcasts the tx, sets ``captcha_exempt``
        on the user, and emails the OTP. The session is only persisted by
        :meth:`verify` after the user supplies the OTP.
        """
        email = _validate_email(email)
        client = Jan3AccountsClient(base_url=self.base_url)

        payment_address = client.get_vault_payment_address()
        price = client.get_product_price(PRODUCT_TYPE_CAPTCHALESS_LOGIN)
        amount_sats = int(price.get("lbtc_sats_price") or 0)
        if amount_sats <= 0:
            raise ValueError(
                f"AQUA CAPTCHALESS_LOGIN price is non-positive ({amount_sats}); "
                "captchaless login is not configured server-side."
            )
        asset_id = self._resolve_asset_id(wallet_name, ASSET_TICKER_LBTC)

        raw_tx = self.wallet_manager.craft_raw_tx(
            wallet_name=wallet_name,
            address=payment_address,
            amount=amount_sats,
            asset_id=asset_id,
            password=password,
        )

        response = client.login_captchaless(
            email=email,
            language=language,
            raw_tx=raw_tx,
            payment_address=payment_address,
        )
        return {
            "email": email,
            "message": response.get("message", "OTP dispatched"),
            "payment_address": payment_address,
            "amount_sats": amount_sats,
            "asset_ticker": ASSET_TICKER_LBTC,
            "otp_sent_to": email,
            # When EMAIL_BASED_OTP is disabled (dev/test) the OTP is echoed back.
            "otp_code": response.get("otp_code"),
            "next_step": "Call jan3_login_complete with the OTP code from your email.",
        }

    def verify(
        self,
        email: str,
        otp_code: str,
        fingerprint: Optional[str] = None,
        captcha_exempt: bool = False,
    ) -> dict:
        """Exchange the OTP for JWT tokens and persist the per-email session.

        Shared by both login flows. ``captcha_exempt`` records which flow created
        the session (True for the paid captchaless flow) — informational only.
        """
        email = _validate_email(email)
        otp_code = (otp_code or "").strip()
        if not otp_code:
            raise ValueError("otp_code is required")

        client = Jan3AccountsClient(base_url=self.base_url)
        tokens = client.verify_otp(email, otp_code, fingerprint)
        access = tokens.get("access")
        refresh = tokens.get("refresh")
        if not access or not refresh:
            raise ValueError(
                "AQUA verify did not return tokens — check the email and OTP code."
            )

        session = Jan3Session(
            email=email,
            base_url=self.base_url,
            access_token=access,
            refresh_token=refresh,
            created_at=_now_iso(),
            captcha_exempt=captcha_exempt,
        )
        self.save_session(session)
        return {
            "email": email,
            "logged_in": True,
            "captcha_exempt": captcha_exempt,
            "message": (
                f"Session saved at {self._session_path(email)}. "
                "You can now make authenticated calls."
            ),
            "access_token_preview": _token_preview(access),
            # Cue the agent to offer the Lightning Address opt-in right after
            # login — this is the intended post-login UX. Enabling it stores a
            # batch of Liquid receive addresses so inbound Lightning payments are
            # delivered there.
            "next_step": (
                "Ask the user if they want to activate their Lightning Address. "
                "If yes, call jan3_enable_lightning_address(email, enabled=true) — this "
                "stores a batch of Liquid receive addresses so JAN3/AQUA can "
                "deliver inbound Lightning payments to them. A Lightning username "
                "(jan3_purchase_ln_username) is required first; run jan3_user_info "
                "to check current status."
            ),
        }

    def logout(self, email: str) -> dict:
        """Forget the local session for ``email`` (does not revoke server-side)."""
        deleted = self.delete_session(email)
        return {"email": email, "logged_out": deleted}

    # ─── session status / refresh-and-retry ────────────────────────────

    def require_session(self, email: str) -> Jan3Session:
        """Return the stored session for ``email``, or raise if not logged in."""
        session = self.load_session(email)
        if not session or not session.access_token:
            raise ValueError(
                f"Not logged in to JAN3 account {email!r}. Run jan3_login then "
                "jan3_verify (or jan3_login_start then jan3_login_complete)."
            )
        return session

    def session_status(self, email: str) -> dict:
        """Report session status without leaking secrets.

        Validates ``exp`` locally; refreshes via the stored token if expired.
        ``valid``: True=ok, False=re-login needed, None=network/backend error.
        """
        session = self.load_session(email)
        if not session:
            return {"email": email, "logged_in": False}
        base = {
            "email": session.email,
            "logged_in": True,
            "base_url": session.base_url,
            "created_at": session.created_at,
            "refreshed_at": session.refreshed_at,
            "captcha_exempt": session.captcha_exempt,
            "access_token_preview": _token_preview(session.access_token),
        }
        if not _access_token_expired(session.access_token):
            return {**base, "valid": True}
        try:
            self._refresh_session(session)
            return {**base, "valid": True}
        except SessionExpiredError:
            return {
                **base,
                "valid": False,
                "message": (
                    "JAN3 session expired — run jan3_login then jan3_verify "
                    "(or the captchaless flow) again."
                ),
            }
        except ValueError as e:
            # Network/backend failure — don't claim the session is invalid.
            return {**base, "valid": None, "message": f"Could not verify session: {e}"}

    def _refresh_session(self, session: Jan3Session) -> Jan3Session:
        """Mint a new access token (rotation-aware) and persist it.

        Mutates ``session`` in place and returns it. Deletes the stored session
        and raises :class:`SessionExpiredError` if the refresh token is rejected
        or missing — so a dead session never lingers on disk.
        """
        if not session.refresh_token:
            self.delete_session(session.email)
            raise SessionExpiredError("No refresh token stored for this JAN3 session.")
        client = Jan3AccountsClient(base_url=session.base_url)
        try:
            tokens = client.refresh_access_token(session.refresh_token) or {}
        except SessionExpiredError:
            self.delete_session(session.email)
            raise
        new_access = tokens.get("access")
        if not new_access or not str(new_access).strip():
            self.delete_session(session.email)
            raise SessionExpiredError("AQUA refresh did not return a new access token.")
        session.access_token = str(new_access).strip()
        # ROTATE_REFRESH_TOKENS may hand back a fresh refresh token; keep the
        # old one if the backend didn't rotate.
        if tokens.get("refresh"):
            session.refresh_token = str(tokens["refresh"])
        session.refreshed_at = _now_iso()
        self.save_session(session)
        return session

    def _with_auth_retry(self, email: str, call):
        """Run ``call(access_token)`` for ``email``; refresh+retry once on 401.

        Proactively refreshes if the stored access token is already expired. If
        the call still 401s after a refresh, the session is dead at the server —
        wipe the local copy and raise ``ValueError`` guiding a re-login.
        """
        session = self.require_session(email)
        if _access_token_expired(session.access_token):
            session = self._refresh_token_or_reraise(session)
        try:
            return call(session.access_token)
        except SessionExpiredError:
            session = self._refresh_token_or_reraise(session)
            try:
                return call(session.access_token)
            except SessionExpiredError as e:
                self.delete_session(email)
                raise ValueError(
                    "JAN3 session still invalid after refresh — run jan3_login "
                    "then jan3_verify (or the captchaless flow) again."
                ) from e

    def _refresh_token_or_reraise(self, session: Jan3Session) -> Jan3Session:
        """``_refresh_session`` but translate a dead-session signal to ValueError."""
        try:
            return self._refresh_session(session)
        except SessionExpiredError as e:
            raise ValueError(
                "JAN3 session expired and could not be refreshed — run jan3_login "
                "then jan3_verify (or the captchaless flow) again."
            ) from e

    def provision_wapupay_token(self, email: str) -> str:
        """Provision a fresh WapuPay API key via the AQUA backend for ``email``.

        Requires a prior login (either flow). Uses ``_with_auth_retry``; the
        backend invalidates any previous key on each call.
        """
        email = _validate_email(email)
        resp = self._with_auth_retry(
            email,
            lambda access: Jan3AccountsClient(
                base_url=self.base_url
            ).provision_wapupay_account(access),
        )
        token = (resp or {}).get("token")
        if not token or not str(token).strip():
            raise ValueError(
                "AQUA backend did not return a WapuPay API key (token missing)."
            )
        return str(token).strip()

    # ─── LN address / LN username ──────────────────────────────────────

    def _authed_client(self, access_token: str) -> Jan3AccountsClient:
        """Build a stateless client bound to ``access_token`` for one authed call."""
        return Jan3AccountsClient(base_url=self.base_url, access_token=access_token)

    def get_user(
        self,
        email: str,
        wallet_name: str = "default",
        password: Optional[str] = None,
    ) -> dict:
        """Fetch the AQUA account profile for ``email`` and keep the pool healthy.

        The returned dict is the backend profile (``ln_username``,
        ``ln_address_toggled``, ``fingerprint``, ``new_addresses_needed``, …).
        When the LN-address feature is active we opportunistically top up the
        unused-address pool — best-effort, never failing the profile read — and
        attach the outcome under ``ln_address_pool``. This is why there is no
        user-facing "ensure pool" tool: reading the account self-heals it.
        """
        email = _validate_email(email)
        profile = self._with_auth_retry(
            email, lambda access: self._authed_client(access).get_user()
        )
        result = dict(profile)
        result["ln_address_pool"] = self._auto_ensure_ln_pool(
            email, wallet_name=wallet_name, password=password, profile=profile
        )
        return result

    def ln_address_toggle(
        self,
        email: str,
        enabled: bool,
        wallet_name: str = "default",
        password: Optional[str] = None,
    ) -> dict:
        """Enable or disable the LN-address feature for ``email``.

        Enabling means AQUA will deliver inbound Lightning payments to the user's
        Lightning Address by handing out the Liquid receive addresses we register
        here. So on enable we immediately populate the pool (best-effort) and
        report the outcome under ``ln_address_pool`` — a ``no_ln_username`` skip
        just means the user still has to run ``jan3_purchase_ln_username``.
        """
        email = _validate_email(email)
        resp = self._with_auth_retry(
            email,
            lambda access: self._authed_client(access).ln_address_toggle(bool(enabled)),
        )
        out: dict[str, Any] = {"email": email, "enabled": bool(enabled), "result": resp}
        if enabled:
            out["ln_address_pool"] = self._auto_ensure_ln_pool(
                email, wallet_name=wallet_name, password=password, profile=None
            )
        return out

    def ln_username_available(self, email: str, username: str) -> dict:
        """Check whether an LN username is free to claim (authenticated).

        Call before ``purchase_ln_username`` to avoid paying L-BTC on a username
        that's already taken.
        """
        email = _validate_email(email)
        username = (username or "").strip().lower()
        if not username:
            raise ValueError("ln_username is required")
        return self._with_auth_retry(
            email,
            lambda access: self._authed_client(access).ln_username_available(username),
        )

    def register_ln_addresses(
        self,
        email: str,
        wallet_name: str = "default",
        count: Optional[int] = None,
        override_fingerprint: bool = False,
        password: Optional[str] = None,
        profile: Optional[dict] = None,
    ) -> dict:
        """Mint unused Liquid receive addresses and POST them to /auth/user/addresses/.

        Internal — NOT exposed as an MCP tool. The pool is managed automatically
        via :meth:`ensure_ln_pool` (called by :meth:`get_user` and on
        :meth:`ln_address_toggle` enable). Without a healthy pool the AQUA
        backend can't deliver inbound LN payments — it hands out one address per
        invoice. Raises on hard errors (no username, disabled, fingerprint
        mismatch); the auto path wraps those into skip dicts.
        """
        email = _validate_email(email)
        user = (
            profile
            if profile is not None
            else self._with_auth_retry(
                email, lambda access: self._authed_client(access).get_user()
            )
        )
        if not user.get("ln_username"):
            raise ValueError(
                "This account has no LN username yet — run "
                "jan3_purchase_ln_username first."
            )
        if not user.get("ln_address_toggled") and not override_fingerprint:
            raise ValueError(
                "LN address is currently disabled — call "
                "jan3_enable_lightning_address(enabled=true) first, or pass "
                "override_fingerprint=true to re-enable it as part of this call."
            )

        server_fp = user.get("fingerprint")
        local_fp = self.wallet_manager.fingerprint(wallet_name, password=password)
        if server_fp and server_fp != local_fp and not override_fingerprint:
            raise ValueError(
                f"Wallet fingerprint mismatch: account is bound to {server_fp!r}, "
                f"this wallet is {local_fp!r}. Pass override_fingerprint=true to "
                "re-bind (this also disables LN delivery for the previously-bound "
                "wallet)."
            )

        if count is None:
            needed = user.get("new_addresses_needed") or 0
            count = needed if needed > 0 else DEFAULT_LN_ADDRESS_POOL
        if count <= 0:
            raise ValueError("count must be positive")
        if count > MAX_ADDRESSES_PER_REGISTRATION:
            raise ValueError(
                f"count cannot exceed {MAX_ADDRESSES_PER_REGISTRATION} "
                "(server-side per-call limit)"
            )

        addresses = [
            a.address for a in self.wallet_manager.reserve_addresses(wallet_name, count)
        ]
        resp = self._with_auth_retry(
            email,
            lambda access: self._authed_client(access).register_addresses(
                local_fp, addresses, override_fingerprint=override_fingerprint
            ),
        )
        return {
            "requested_count": len(addresses),
            "pool_size": len(resp.get("addresses", [])),
            "fingerprint": local_fp,
            "addresses": resp.get("addresses", []),
        }

    def ensure_ln_pool(
        self,
        email: str,
        wallet_name: str = "default",
        password: Optional[str] = None,
        profile: Optional[dict] = None,
    ) -> dict:
        """Idempotent LN-address pool top-up (internal — NOT an MCP tool).

        Only POSTs new addresses when the server reports
        ``new_addresses_needed > 0``. Returns a ``{refilled: False, reason: …}``
        dict (never raises for policy reasons) when auto-refill doesn't apply:
        no LN username, toggle off, pool already full, or the local wallet's
        fingerprint doesn't match the account-bound one.
        """
        email = _validate_email(email)

        def _skip(reason: str, **extra) -> dict:
            return {"refilled": False, "reason": reason, **extra}

        user = (
            profile
            if profile is not None
            else self._with_auth_retry(
                email, lambda access: self._authed_client(access).get_user()
            )
        )
        if not user.get("ln_username"):
            return _skip("no_ln_username", fingerprint=user.get("fingerprint"))
        if not user.get("ln_address_toggled"):
            return _skip("ln_address_disabled", fingerprint=user.get("fingerprint"))
        needed = user.get("new_addresses_needed") or 0
        if needed <= 0:
            return _skip(
                "pool_full", fingerprint=user.get("fingerprint"), new_addresses_needed=0
            )

        server_fp = user.get("fingerprint")
        local_fp = self.wallet_manager.fingerprint(wallet_name, password=password)
        if server_fp and server_fp != local_fp:
            return _skip(
                "fingerprint_mismatch",
                server_fingerprint=server_fp,
                local_fingerprint=local_fp,
            )

        result = self.register_ln_addresses(
            email,
            wallet_name=wallet_name,
            count=min(needed, MAX_ADDRESSES_PER_REGISTRATION),
            password=password,
            profile=user,
        )
        return {
            "refilled": True,
            "requested_count": result["requested_count"],
            "pool_size": result["pool_size"],
            "fingerprint": result["fingerprint"],
        }

    def _auto_ensure_ln_pool(
        self,
        email: str,
        wallet_name: str,
        password: Optional[str],
        profile: Optional[dict] = None,
    ) -> dict:
        """Best-effort wrapper around :meth:`ensure_ln_pool` for the auto paths.

        Used by :meth:`get_user` and by :meth:`ln_address_toggle` on enable, so
        the pool self-heals without a dedicated user-facing tool. Never raises: a
        locked/encrypted wallet (no password), an offline backend, etc. are
        reported as a skip dict rather than failing the caller.
        """
        try:
            return self.ensure_ln_pool(
                email, wallet_name=wallet_name, password=password, profile=profile
            )
        except Exception as e:  # noqa: BLE001 — auto path must never break its caller
            logger.warning("Auto LN-address pool top-up skipped for %s: %s", email, e)
            return {
                "refilled": False,
                "reason": "auto_topup_unavailable",
                "detail": str(e),
            }

    def purchase_ln_username(
        self,
        email: str,
        ln_username: str,
        wallet_name: str = "default",
        password: Optional[str] = None,
        asset: str = ASSET_TICKER_LBTC,
    ) -> dict:
        """Buy / update the Lightning username for ``email`` (on-chain L-BTC payment).

        Creates an LN_USERNAME_UPDATE payment request, funds it with a signed
        L-BTC tx to AQUA's address, and submits the raw tx. On a 401 the access
        token is refreshed and the call retried once (via ``_with_auth_retry``).
        """
        email = _validate_email(email)
        ln_username = _validate_ln_username(ln_username)

        order = self._with_auth_retry(
            email,
            lambda access: self._authed_client(
                access
            ).create_ln_username_payment_request(asset, ln_username),
        )
        payment_id = order.get("payment_id")
        address = order.get("address")
        amount_sats = int(order.get("amount_base_units") or 0)
        asset_ticker = order.get("asset_ticker") or asset
        if not payment_id or not address or amount_sats <= 0:
            raise ValueError(
                "AQUA payment-request response is missing fields "
                f"(payment_id/address/amount): {order!r}"
            )

        asset_id = self._resolve_asset_id(wallet_name, asset_ticker)
        raw_tx = self.wallet_manager.craft_raw_tx(
            wallet_name=wallet_name,
            address=address,
            amount=amount_sats,
            asset_id=asset_id,
            password=password,
        )
        # The API response omits the txid; compute it locally from the finalized
        # raw tx so the caller has something to track on a block explorer.
        txid = str(lwk.Transaction(raw_tx).txid())

        result = self._with_auth_retry(
            email,
            lambda access: self._authed_client(access).submit_raw_tx(
                payment_id=payment_id, raw_tx=raw_tx
            ),
        )
        return {
            "payment_id": payment_id,
            "status": result.get("status"),
            "txid": txid,
            "ln_username": ln_username,
            "amount_sats": amount_sats,
            "asset_ticker": asset_ticker,
            "address": address,
            "message": (
                f"Submitted raw_tx for {ln_username!r}. Server reports "
                f"{result.get('status', 'UNKNOWN')}."
            ),
        }
