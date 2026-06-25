"""JAN3 Accounts integration: login + purchases.

Talks to the AQUA Ankara production backend at ``https://ankara.aquabtc.com``.
The base URL is hardcoded — staging is for internal use only and requires a
local code patch.

Surface (REST/JSON):

  GET  /api/v1/liquid-wallet/payment/receive-address/
       → {"address": "lq1..."} — public, no auth.

  GET  /api/v1/liquid-wallet/products/?product_type=...
       → [{"product_type","lbtc_sats_price","usdt_base_units_price",
            "usdt_display_price"}] — public.

  POST /api/v2/auth/login/
       Body: {"email","language","login_challenge":{"raw_tx",
                "payment_address","captcha_token"}}
       The paid bypass uses raw_tx + payment_address; server broadcasts
       the tx, flips UserProfile.captcha_exempt, and emails the OTP.

  POST /api/v1/auth/verify/      {email, otp_code, fingerprint?}
                                   → {access, refresh}
  POST /api/v1/auth/refresh/     {refresh} → {access}

  POST /api/v1/liquid-wallet/payment-request/ln-username/   (JWT)
       {asset, ln_username}
       → {payment_id, status, product_type, asset_ticker, amount,
          amount_base_units, address, expires_at, product_details}

  POST /api/v1/liquid-wallet/payment/submit-raw-tx/         (JWT)
       {payment_id, raw_tx}
       → same shape as create; status flips to "ACCEPTED" on success.
         txid is NOT in the response — compute locally from raw_tx.

Sessions persist at ~/.aqua/jan3_accounts/{email}.json (0o600). Only
``complete_login`` writes the session — ``request_login`` is a
non-mutating step that just dispatches the OTP email.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import lwk

from .assets import resolve_liquid_asset_id
from .storage import Storage
from .wallet import WalletManager

logger = logging.getLogger(__name__)


AQUA_ANKARA_API_URL = "https://ankara.aquabtc.com"
USER_AGENT = "agentic-aqua"
HTTP_TIMEOUT_SECONDS = 30.0

PRODUCT_TYPE_LN_USERNAME = "LN_USERNAME_UPDATE"
PRODUCT_TYPE_CAPTCHALESS_LOGIN = "CAPTCHALESS_LOGIN"

ASSET_TICKER_LBTC = "L-BTC"
ASSET_TICKER_USDT = "USDt"

# 4–64 chars, lowercase letters/digits, at most one dot. Mirrors
# common.constants.LN_USERNAME_REGEX in aqua-ankara so we fail fast
# instead of letting the server 400.
LN_USERNAME_REGEX = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)?$")

# Minimal email syntax check — the server validates properly; this just
# catches obvious mistakes before we make a network call.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Jan3UnauthorizedError(RuntimeError):
    """Raised when an authenticated request returns HTTP 401.

    The manager catches this once per call to attempt a refresh-and-retry.
    A second 401 (or a 401 on the refresh itself) deletes the local session
    and re-raises so the caller can prompt the user to log in again.
    """


@dataclass
class Jan3Session:
    """Persistent record of a JAN3 account login session.

    ``captcha_exempt`` is always True after a successful captchaless login.
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
        data = {**data}
        data.setdefault("refreshed_at", None)
        data.setdefault("captcha_exempt", False)
        return cls(**data)


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


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class Jan3AccountsClient:
    """HTTP client for the AQUA Ankara backend.

    Stateless except for the optional bearer ``access_token`` — pass a new
    token to retry after a refresh.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or AQUA_ANKARA_API_URL).rstrip("/")
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
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if auth:
            if not self.access_token:
                raise ValueError("Authenticated request requires an access_token")
            headers["Authorization"] = f"Bearer {self.access_token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err_body = json.loads(e.read().decode())
                detail = (
                    err_body.get("message")
                    or err_body.get("error_code")
                    or err_body.get("error")
                    or err_body.get("detail")
                    or ""
                )
                if isinstance(detail, dict):
                    detail = detail.get("message") or detail.get("code") or str(detail)
            except Exception:
                pass
            if e.code == 401:
                raise Jan3UnauthorizedError(
                    f"AQUA API unauthorized ({method} {path}): {detail or 'no detail'}"
                ) from e
            msg = f"AQUA API error ({e.code} {method} {path})"
            if detail:
                msg += f": {detail}"
            raise RuntimeError(msg) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"AQUA API unreachable ({method} {path}): {e.reason}"
            ) from e

    # ─── public endpoints ──────────────────────────────────────────────

    def get_vault_payment_address(self) -> str:
        result = self._api_request(
            "GET", "/api/v1/liquid-wallet/payment/receive-address/"
        )
        address = (result or {}).get("address")
        if not address:
            raise RuntimeError(
                "AQUA payment receive-address endpoint returned no address"
            )
        return address

    def get_product_price(self, product_type: str) -> dict:
        """GET /products/?product_type=... — returns the single matching row.

        Raises if the response is empty or doesn't include the requested
        product type (server-side default seed should always provide one).
        """
        result = self._api_request(
            "GET",
            "/api/v1/liquid-wallet/products/",
            query={"product_type": product_type},
        )
        rows = result if isinstance(result, list) else []
        for row in rows:
            if row.get("product_type") == product_type:
                return row
        raise RuntimeError(
            f"AQUA products endpoint returned no entry for {product_type!r}"
        )

    def login_v2(
        self,
        email: str,
        language: str = "en",
        *,
        raw_tx: Optional[str] = None,
        payment_address: Optional[str] = None,
        captcha_token: Optional[str] = None,
    ) -> dict:
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
        return self._api_request("POST", "/api/v2/auth/login/", body=body) or {}

    def verify_otp(
        self,
        email: str,
        otp_code: str,
        fingerprint: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {"email": email, "otp_code": otp_code}
        if fingerprint is not None:
            body["fingerprint"] = fingerprint
        return self._api_request("POST", "/api/v1/auth/verify/", body=body) or {}

    def refresh_access_token(self, refresh_token: str) -> str:
        result = self._api_request(
            "POST", "/api/v1/auth/refresh/", body={"refresh": refresh_token}
        ) or {}
        access = result.get("access")
        if not access:
            raise RuntimeError("AQUA refresh endpoint returned no access token")
        return access

    # ─── authenticated endpoints ───────────────────────────────────────

    def create_ln_username_payment_request(
        self, asset: str, ln_username: str
    ) -> dict:
        return self._api_request(
            "POST",
            "/api/v1/liquid-wallet/payment-request/ln-username/",
            body={"asset": asset, "ln_username": ln_username},
            auth=True,
        ) or {}

    def submit_raw_tx(self, payment_id: str, raw_tx: str) -> dict:
        return self._api_request(
            "POST",
            "/api/v1/liquid-wallet/payment/submit-raw-tx/",
            body={"payment_id": payment_id, "raw_tx": raw_tx},
            auth=True,
        ) or {}


# ---------------------------------------------------------------------------
# Manager (login + purchase orchestration)
# ---------------------------------------------------------------------------


class Jan3AccountsManager:
    """High-level JAN3 Accounts orchestration.

    Owns session persistence and the multi-step login + purchase flows.
    The underlying :class:`Jan3AccountsClient` is stateless; the manager
    builds one per call (or per retry) with the current bearer token.
    """

    def __init__(
        self,
        storage: Storage,
        wallet_manager: WalletManager,
        base_url: Optional[str] = None,
    ) -> None:
        self.storage = storage
        self.wallet_manager = wallet_manager
        self.base_url = (base_url or AQUA_ANKARA_API_URL).rstrip("/")

    # ─── session persistence ───────────────────────────────────────────

    def _session_path(self, email: str) -> Path:
        email = _validate_email(email)
        return self.storage.jan3_accounts_dir / f"{_email_to_filename(email)}.json"

    def load_session(self, email: str) -> Optional[Jan3Session]:
        path = self._session_path(email)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return Jan3Session.from_dict(json.load(f))
        except (OSError, json.JSONDecodeError, TypeError):
            # Corrupted file shouldn't pin the user out of every JAN3 tool.
            # Treat as missing and let the caller force a re-login.
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
        for path in self.storage.jan3_accounts_dir.glob("*.json"):
            try:
                with open(path) as f:
                    sessions.append(Jan3Session.from_dict(json.load(f)))
            except (OSError, json.JSONDecodeError, TypeError):
                # Skip corrupted files rather than failing the whole list.
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

    # ─── login flow ────────────────────────────────────────────────────

    def request_login(
        self,
        email: str,
        wallet_name: str = "default",
        password: Optional[str] = None,
        language: str = "en",
    ) -> dict:
        """Paid captchaless login step 1: dispatch the OTP email.

        Fetches the vault payment address and the CAPTCHALESS_LOGIN price
        from the AQUA backend, then crafts a signed L-BTC tx that funds
        that address and POSTs everything to /api/v2/auth/login/. The
        server broadcasts the tx, sets captcha_exempt on the user, and
        emails the OTP.

        Returns a dict describing what to do next — the actual session
        is only persisted by :meth:`complete_login` after the user
        supplies the OTP.
        """
        email = _validate_email(email)
        client = Jan3AccountsClient(base_url=self.base_url)

        payment_address = client.get_vault_payment_address()
        price = client.get_product_price(PRODUCT_TYPE_CAPTCHALESS_LOGIN)
        amount_sats = int(price.get("lbtc_sats_price") or 0)
        if amount_sats <= 0:
            raise RuntimeError(
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

        response = client.login_v2(
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
            # When the backend has EMAIL_BASED_OTP disabled (dev/test) it
            # echoes the OTP in the response — surface it so smoke tests
            # don't need email access.
            "otp_code": response.get("otp_code"),
            "next_step": "Call jan3_login_complete with the OTP code from your email.",
        }

    def complete_login(
        self,
        email: str,
        otp_code: str,
        fingerprint: Optional[str] = None,
    ) -> dict:
        """Exchange the OTP for JWT tokens and persist the session."""
        email = _validate_email(email)
        otp_code = (otp_code or "").strip()
        if not otp_code:
            raise ValueError("otp_code is required")

        client = Jan3AccountsClient(base_url=self.base_url)
        tokens = client.verify_otp(email, otp_code, fingerprint)
        access = tokens.get("access")
        refresh = tokens.get("refresh")
        if not access or not refresh:
            raise RuntimeError(
                "AQUA verify endpoint did not return both access and refresh tokens"
            )

        session = Jan3Session(
            email=email,
            base_url=self.base_url,
            access_token=access,
            refresh_token=refresh,
            created_at=_now_iso(),
            captcha_exempt=True,
        )
        self.save_session(session)
        return {
            "email": email,
            "captcha_exempt": True,
            "message": (
                f"Session saved at {self._session_path(email)}. "
                "You can now make authenticated purchases."
            ),
            "access_token_preview": _token_preview(access),
        }

    # ─── auth helpers ─────────────────────────────────────────────────

    def _require_session(self, email: str) -> Jan3Session:
        session = self.load_session(email)
        if not session:
            raise ValueError(
                f"No JAN3 session for {email!r}. "
                "Run jan3_login_start then jan3_login_complete first."
            )
        return session

    # ─── refresh-and-retry ────────────────────────────────────────────

    def _refresh_access_token(self, session: Jan3Session) -> Jan3Session:
        """Try to mint a new access token. Deletes the session on failure.

        Mutates ``session`` in place and returns it. Not safe under
        concurrent calls for the same email (last write wins) — fine for
        single-user CLI / MCP usage, would need a lock if invoked from
        parallel workers.
        """
        client = Jan3AccountsClient(base_url=session.base_url)
        try:
            new_access = client.refresh_access_token(session.refresh_token)
        except Jan3UnauthorizedError:
            # Refresh token is gone too — wipe the local session so the
            # user is forced to re-login rather than retrying forever.
            self.delete_session(session.email)
            raise
        session.access_token = new_access
        session.refreshed_at = _now_iso()
        self.save_session(session)
        return session

    def _with_refresh_retry(self, session: Jan3Session, call):
        """Run an authed API call; on 401, refresh access token and retry once.

        If the retry *also* 401s, the session is dead at the server (e.g.,
        user revoked, account banned) — wipe the local copy so we don't
        keep a poisoned bearer on disk. ``_refresh_access_token`` already
        handles the case where the refresh endpoint itself rejects us.

        ``session`` is mutated in place by ``_refresh_access_token``, so
        callers holding the same reference see the new ``access_token``
        after this returns.
        """
        client = Jan3AccountsClient(
            base_url=session.base_url, access_token=session.access_token
        )
        try:
            return call(client)
        except Jan3UnauthorizedError:
            session = self._refresh_access_token(session)
            client = Jan3AccountsClient(
                base_url=session.base_url, access_token=session.access_token
            )
            try:
                return call(client)
            except Jan3UnauthorizedError:
                self.delete_session(session.email)
                raise

    # ─── purchase flow ────────────────────────────────────────────────

    def purchase_ln_username(
        self,
        email: str,
        ln_username: str,
        wallet_name: str = "default",
        password: Optional[str] = None,
        asset: str = ASSET_TICKER_LBTC,
    ) -> dict:
        """Create + pay an LN_USERNAME_UPDATE payment request.

        On a 401 we refresh the access token once and retry once. A second
        401 (or a 401 on the refresh) deletes the local session and raises
        :class:`Jan3UnauthorizedError`.
        """
        email = _validate_email(email)
        ln_username = _validate_ln_username(ln_username)
        session = self._require_session(email)

        order = self._with_refresh_retry(
            session,
            lambda c: c.create_ln_username_payment_request(asset, ln_username),
        )

        payment_id = order.get("payment_id")
        address = order.get("address")
        amount_sats = int(order.get("amount_base_units") or 0)
        asset_ticker = order.get("asset_ticker") or asset
        if not payment_id or not address or amount_sats <= 0:
            raise RuntimeError(
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

        # The API response does not include the txid; compute it locally
        # from the finalized raw tx so the caller has something to track
        # on a block explorer.
        txid = str(lwk.Transaction(raw_tx).txid())

        result = self._with_refresh_retry(
            session,
            lambda c: c.submit_raw_tx(payment_id=payment_id, raw_tx=raw_tx),
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
