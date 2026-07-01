"""JAN3 / AQUA / Ankara backend integration.

Single home for everything that talks to JAN3's backend (AQUA's Ankara host,
``ANKARA_API_URL`` — the three names are the same backend):

    * **Lightning → L-BTC swaps** — ``AnkaraSwapInfo`` / ``AnkaraClient`` (Boltz
      orchestration), consumed by ``lightning.py``.
    * **AQUA-account auth** — ``JAN3Session`` / ``JAN3AuthClient`` /
      ``JAN3AccountManager``: the email-OTP → JWT login surfaced as the
      ``aqua_*`` tools, plus provisioning the WapuPay API key from the AQUA
      backend. ``wapupay.py`` consumes the auth surface; this module never
      imports ``wapupay`` (one-way dependency).
"""

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from typing import Any, Callable, Optional

# API URL with environment variable override
ANKARA_API_URL = os.environ.get("ANKARA_API_URL", "https://ankara.aquabtc.com")

# AQUA-account login (the `aqua_*` tools) authenticates against the same Ankara host
AUTH_BASE_URL = f"{ANKARA_API_URL.rstrip('/')}/api/v1/auth"
WAPUPAY_ACCOUNT_PATH = "/api/v1/wapupay/account/"

USER_AGENT = "agentic-aqua"
HTTP_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger(__name__)

# Server-side cap on addresses per ``POST /api/v1/auth/user/addresses/`` request
# (see ``UserLiquidAddressesUpsert.addresses.maxItems`` in the OpenAPI schema).
MAX_ADDRESSES_PER_REGISTRATION = 15


class SessionExpiredError(ValueError):
    """Access or refresh JWT was rejected by Ankara (HTTP 401);
    triggers token refresh and subclasses ValueError."""

# Bank-PII / secret fields that must never reach the logs. Shared by the JAN3
# auth surface and (via import) WapuPay's business client.
_SENSITIVE_LOG_FIELDS = frozenset(
    {
        "alias",
        "receiver_name",
        "refund_address",
        "access",
        "refresh",
        "authorization",
        "x-api-key",
        "api_key",
        "token",
    }
)


@dataclass
class AnkaraSwapInfo:
    """Holds all data for an active/completed Ankara Lightning swap."""

    swap_id: str
    boltz_swap_id: str
    invoice: str
    address: str
    amount: int
    wallet_name: str
    status: str  # "pending" | "settled" | "failed"
    created_at: str
    preimage: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AnkaraSwapInfo":
        return cls(**data)


class AnkaraClient:
    """HTTP client for Ankara backend API."""

    def __init__(self):
        self.base_url = ANKARA_API_URL

    def _api_request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Make HTTP request to Ankara API."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "agentic-aqua",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # Try to extract error message from response body
            detail = ""
            try:
                err_body = json.loads(e.read().decode())
                detail = err_body.get("error", err_body.get("message", ""))
            except Exception:
                pass
            msg = f"Ankara API error ({e.code} {method} {path})"
            if detail:
                msg += f": {detail}"
            raise RuntimeError(msg) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ankara API unreachable ({method} {path}): {e.reason}") from e

    def create_swap(self, amount: int, address: str) -> dict:
        """POST /api/v1/lightning/swaps/create/ - create a new swap."""
        return self._api_request(
            "POST",
            "/api/v1/lightning/swaps/create/",
            {
                "amount": amount,
                "address": address,
            },
        )

    def claim_swap(self, swap_id: str) -> dict:
        """POST /api/v1/lightning/swaps/{swap_id}/claim/ - claim a swap."""
        return self._api_request("POST", f"/api/v1/lightning/swaps/{swap_id}/claim/")

    def verify_swap(self, swap_id: str) -> dict:
        """GET /api/v1/lightning/lnurlp/verify/{swap_id} - verify swap status."""
        return self._api_request("GET", f"/api/v1/lightning/lnurlp/verify/{swap_id}")


# ---------------------------------------------------------------------------
# JAN3 / AQUA account auth — shared logging/PII helpers
# ---------------------------------------------------------------------------


def _redact(payload: Any) -> Any:
    """Recursively redact sensitive keys for safe logging."""
    if isinstance(payload, dict):
        return {
            k: ("***" if k.lower() in _SENSITIVE_LOG_FIELDS else _redact(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_redact(v) for v in payload]
    return payload


def _scrub_text(s: str) -> str:
    """Mask emails and long digit sequences in text."""
    if not s:
        return s
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<redacted-email>", s)
    s = re.sub(r"\d{11,}", "<redacted>", s)
    return s


def _mask(secret: str) -> str:
    """Hide secret except first and last few chars; short secrets become ***."""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}…{secret[-4:]}"


def _extract_error_message(body: str) -> str:
    """Extract a scrubbed error message from an Ankara error body."""
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return _scrub_text(body[:200])
    if isinstance(parsed, dict):
        for key in ("error", "detail", "message"):
            val = parsed.get(key)
            if isinstance(val, str) and val:
                return _scrub_text(val[:200])
        # Redact nested validation errors before logging.

        details = parsed.get("details")
        if details:
            return _scrub_text(json.dumps(_redact(details))[:200])
        return _scrub_text(json.dumps(_redact(parsed))[:200])
    return _scrub_text(str(parsed)[:200])


# Treat an access token within this margin of its `exp` as already expired, to
# cover clock skew and the latency of the call it is about to authorize.
_JWT_EXP_SKEW_SECONDS = 30


def _jwt_exp(token: str) -> Optional[int]:
    """Best-effort decode of a JWT's ``exp`` claim (no signature check).
    Returns the timestamp, or ``None`` if the token is malformed or missing ``exp``."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (AttributeError, IndexError, ValueError, TypeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    try:
        return int(exp) if exp is not None else None
    except (ValueError, TypeError):
        return None


def _access_token_expired(token: str, *, now: Optional[float] = None) -> bool:
    """True if the access JWT is expired or unreadable (forces a live refresh)."""
    exp = _jwt_exp(token)
    if exp is None:
        return True
    current = now if now is not None else datetime.now(UTC).timestamp()
    return exp <= current + _JWT_EXP_SKEW_SECONDS


# ---------------------------------------------------------------------------
# JAN3 / AQUA account session + client + manager
# ---------------------------------------------------------------------------


@dataclass
class JAN3Session:
    """Persisted AQUA↔Ankara session (JWT pair)."""

    email: str
    access: str
    refresh: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "JAN3Session":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class JAN3AuthClient:
    """AQUA auth/account HTTP client."""

    def __init__(
        self,
        auth_base_url: Optional[str] = None,
        aqua_backend_url: Optional[str] = None,
    ) -> None:
        self.auth_base_url = (auth_base_url or AUTH_BASE_URL).rstrip("/")
        self.aqua_backend_url = (aqua_backend_url or ANKARA_API_URL).rstrip("/")

    def _api_request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict] = None,
        access_token: Optional[str] = None,
    ) -> Any:
        """HTTP request, parse JSON. Raises ValueError on error."""

        data = json.dumps(json_body).encode() if json_body is not None else None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode()
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"AQUA backend returned a non-JSON response "
                        f"({resp.status} {method})"
                    ) from e
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            detail = _extract_error_message(body)
            if e.code == 401:
                # Recoverable — raise the typed signal so the manager can refresh-then-retry.
                msg = "AQUA session token rejected (401)"
                if detail:
                    msg += f": {detail}"
                raise SessionExpiredError(msg) from e
            msg = f"AQUA backend request failed ({e.code} {method})"
            if detail:
                msg += f": {detail}"
            raise ValueError(msg) from e
        except urllib.error.URLError as e:
            raise ValueError(
                f"AQUA backend unreachable ({method}): {e.reason}"
            ) from e

    def login(self, email: str, language: str = "en") -> dict:
        """POST /auth/login/ — request an OTP email. Returns ``{message[, otp_code]}``."""
        return self._api_request(
            "POST",
            f"{self.auth_base_url}/login/",
            json_body={"email": email, "language": language},
        ) or {}

    def verify(self, email: str, otp_code: str) -> dict:
        """POST /auth/verify/ — exchange the OTP for ``{access, refresh}`` JWTs."""
        return self._api_request(
            "POST",
            f"{self.auth_base_url}/verify/",
            json_body={"email": email, "otp_code": otp_code},
        ) or {}

    def get_user(self, access_token: str) -> dict:
        """GET /auth/user/ — current user profile (email, ln_username, fingerprint, …)."""
        return self._api_request(
            "GET",
            f"{self.auth_base_url}/user/",
            access_token=access_token,
        ) or {}

    def ln_address_toggle(self, access_token: str, enabled: bool) -> dict:
        """POST /auth/user/ln-address-toggle/ — opt in/out of the LN-address feature."""
        return self._api_request(
            "POST",
            f"{self.auth_base_url}/user/ln-address-toggle/",
            json_body={"enabled": bool(enabled)},
            access_token=access_token,
        ) or {}

    def ln_username_available(self, username: str, access_token: Optional[str] = None) -> dict:
        """GET /auth/user/ln-username/{u}/is-available — availability check.

        Schema marks this anonymous, but the live deployment requires a JWT — so
        we forward ``access_token`` when one is available.
        """
        safe = urllib.parse.quote(username, safe="")
        return self._api_request(
            "GET",
            f"{self.auth_base_url}/user/ln-username/{safe}/is-available",
            access_token=access_token,
        ) or {}

    def register_addresses(
        self,
        access_token: str,
        fingerprint: str,
        addresses: list[str],
        override_fingerprint: bool = False,
    ) -> dict:
        """POST /auth/user/addresses/ — upload unused Liquid receive addresses.

        ``override_fingerprint=true`` (query string) makes the server re-bind the
        account to this wallet's fingerprint and also flips ``ln_address_toggled``
        back to true.
        """
        url = f"{self.auth_base_url}/user/addresses/"
        if override_fingerprint:
            url += "?override_fingerprint=true"
        return self._api_request(
            "POST",
            url,
            json_body={"fingerprint": fingerprint, "addresses": list(addresses)},
            access_token=access_token,
        ) or {}

    def refresh_token(self, refresh: str) -> dict:
        """POST /auth/refresh/ — exchange a refresh JWT for a fresh ``{access}``.
        May also return a rotated ``refresh``; raises ``SessionExpiredError`` on 401."""
        return self._api_request(
            "POST",
            f"{self.auth_base_url}/refresh/",
            json_body={"refresh": refresh},
        ) or {}

    def provision_wapupay_account(self, access_token: str) -> dict:
        """
        POST /api/v1/wapupay/account/ — creates a WapuPay user and returns its API key.
        Authenticates with AQUA JWT. Raises ValueError on error or unreachable backend.
        """

        url = f"{self.aqua_backend_url}{WAPUPAY_ACCOUNT_PATH}"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        }
        req = urllib.request.Request(url, data=None, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode()
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"AQUA backend returned a non-JSON response ({resp.status} POST)"
                    ) from e
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Recoverable — manager wraps this via _with_auth_retry and retries before re-login.
                raise SessionExpiredError(
                    "AQUA session invalid or expired — run aqua_login / aqua_verify again."
                ) from e
            if e.code == 403:
                raise ValueError(
                    "WapuPay is not enabled for your AQUA account."
                ) from e
            if e.code == 502:
                raise ValueError(
                    "WapuPay upstream error while provisioning your account — "
                    "try again shortly."
                ) from e
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            detail = _extract_error_message(body)
            msg = f"AQUA backend request failed ({e.code} POST)"
            if detail:
                msg += f": {detail}"
            raise ValueError(msg) from e
        except urllib.error.URLError as e:
            raise ValueError(f"AQUA backend unreachable (POST): {e.reason}") from e


class JAN3AccountManager:
    """
    Manages JAN3/AQUA account session: login, verify, logout, status, and key provisioning.
    Uses Storage for persistence.
    """

    def __init__(
        self,
        storage,
        wallet_manager_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.storage = storage
        self._client: Optional[JAN3AuthClient] = None
        # Lazy WalletManager accessor. Optional so tests can construct the
        # manager without wiring up wallet state.
        self._wallet_manager_factory = wallet_manager_factory

    @property
    def client(self) -> JAN3AuthClient:
        if self._client is None:
            self._client = JAN3AuthClient()
        return self._client

    def login(self, email: str, language: str = "en") -> dict:
        """Start login: Ankara emails an OTP. Does not persist anything yet."""
        if not email or "@" not in email:
            raise ValueError("A valid email is required to log in to your AQUA account")
        resp = self.client.login(email, language=language)
        out = {
            "email": email,
            "message": resp.get(
                "message", "An OTP code has been sent to your email."
            ),
            "next_step": "Call aqua_verify with the OTP code from your email.",
        }
        # Non-prod Ankara (EMAIL_BASED_OTP off) returns the code inline.
        if resp.get("otp_code"):
            out["otp_code"] = resp["otp_code"]
        return out

    def verify(self, email: str, otp_code: str) -> dict:
        """Verify the OTP and persist the resulting JWT session."""
        if not otp_code or not str(otp_code).strip():
            raise ValueError("otp_code is required")
        tokens = self.client.verify(email, str(otp_code).strip())
        access = tokens.get("access")
        refresh = tokens.get("refresh")
        if not access or not refresh:
            raise ValueError(
                "AQUA verify did not return tokens — check the email and OTP code."
            )
        session = JAN3Session(
            email=email,
            access=access,
            refresh=refresh,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.storage.save_jan3_session(session)
        return {
            "email": email,
            "logged_in": True,
            "message": "Logged in to your AQUA account via JAN3 Ankara.",
        }

    def logout(self) -> dict:
        """Forget the local AQUA session (does not revoke the token server-side)."""
        existed = self.storage.load_jan3_session() is not None
        self.storage.delete_jan3_session()
        return {"logged_out": existed}

    def session_status(self) -> dict:
        """Report AQUA session status without leaking secrets.

        Validates ``exp`` locally; refreshes via stored token if expired.
        Returns ``valid``: True=ok, False=re-login needed, None=network error."""
        session = self.storage.load_jan3_session()
        if not session:
            return {"logged_in": False}
        base = {
            "logged_in": True,
            "email": session.email,
            "created_at": session.created_at,
        }
        if not _access_token_expired(session.access):
            # Still valid — no network call, no token rotation.
            return {**base, "valid": True}
        # Access expired (or unreadable) — confirm the session can be renewed.
        try:
            self._refresh_session(session)
            return {**base, "valid": True}
        except SessionExpiredError:
            return {
                **base,
                "valid": False,
                "message": (
                    "AQUA session expired — run aqua_login then aqua_verify again."
                ),
            }
        except ValueError as e:
            # Network/backend failure — don't claim the session is invalid.
            return {**base, "valid": None, "message": f"Could not verify session: {e}"}

    def require_session(self) -> JAN3Session:
        """Return the stored AQUA session, or raise if the user is not logged in."""
        session = self.storage.load_jan3_session()
        if not session or not session.access:
            raise ValueError(
                "Not logged in to your AQUA account. Run aqua_login then aqua_verify."
            )
        return session

    def _opportunistic_top_up(self, access_token: str) -> None:
        """After refreshing a session, refill the LN-address pool in the background.
        
        Used as on_refresh with _with_auth_retry after a 401. 
        Errors are silently logged and never interrupt the caller.
        """
   
        if self._wallet_manager_factory is None:
            return
        try:
            wm = self._wallet_manager_factory()
            profile = self.client.get_user(access_token)
            self.ensure_ln_pool(wallet_manager=wm, wallet_name="default", profile=profile)
        except Exception as e:  # noqa: BLE001 — never break the caller's flow
            logger.debug("Opportunistic LN-address top-up skipped: %s", e)

    def get_user(self) -> dict:
        """Return the current user profile from /auth/user/."""
        return self._with_auth_retry(
            self.client.get_user, on_refresh=self._opportunistic_top_up
        )

    def ln_address_toggle(self, enabled: bool) -> dict:
        """Enable or disable the LN-address feature on the account."""
        return self._with_auth_retry(
            lambda access: self.client.ln_address_toggle(access, enabled),
            on_refresh=self._opportunistic_top_up,
        )

    def ln_username_available(self, username: str) -> dict:
        """Check whether an LN username is free to claim.

        The OpenAPI schema marks this anonymous, but the live AQUA deployment
        requires a JWT. If a local session exists we forward the token (with
        auto-refresh on 401); otherwise we fall back to an anonymous request so
        the schema-documented behavior still works against future deployments.
        """
        username = (username or "").strip()
        if not username:
            raise ValueError("ln_username is required")
        session = self.storage.load_jan3_session()
        if session and session.access:
            return self._with_auth_retry(
                lambda access: self.client.ln_username_available(username, access),
                on_refresh=self._opportunistic_top_up,
            )
        return self.client.ln_username_available(username)

    def register_ln_addresses(
        self,
        wallet_manager,
        wallet_name: str,
        count: Optional[int] = None,
        override_fingerprint: bool = False,
        password: Optional[str] = None,
        profile: Optional[dict] = None,
    ) -> dict:
        """Mint a batch of unused Liquid receive addresses and POST them to /auth/user/addresses/.

        Without a healthy pool of unused addresses on the server, inbound LN
        payments to ``<ln_username>@aquabtc.com`` cannot be delivered — the
        server hands out one address per invoice. Mirrors the AQUA app's
        ``LnAddressRegistrationNotifier``.

        Args:
            wallet_manager: WalletManager instance (passed in to avoid a circular import).
            wallet_name: Liquid wallet whose receive addresses get uploaded.
            count: number of addresses to register. If omitted, uses the server's
                ``new_addresses_needed`` from /auth/user/, falling back to 5
                (the app's boot constant). Capped at 15 (server limit per call).
            override_fingerprint: re-bind the account to this wallet's fingerprint.
                Server-side this also flips ``ln_address_toggled`` to true. Use
                only when intentionally rebinding (e.g. after a wallet restore).
            password: decrypts the wallet's mnemonic if it was encrypted at rest.
            profile: an already-fetched ``aqua_get_user`` profile dict. Callers
                that just fetched it can pass it in to avoid a second round trip.

        Returns:
            ``{registered_count, fingerprint, addresses}`` where ``addresses`` is
            the full active/unused pool the server now knows about.
        """
        # Register new unused Liquid receive addresses, optionally rebinding fingerprint
        user = profile if profile is not None else self._with_auth_retry(self.client.get_user)
        ln_username = user.get("ln_username")
        if not ln_username:
            raise ValueError(
                "This account has no LN username yet — run jan3_purchase_ln_username first."
            )
        if not user.get("ln_address_toggled") and not override_fingerprint:
            raise ValueError(
                "LN address is currently disabled — call aqua_ln_address_toggle(enabled=true) first, "
                "or pass override_fingerprint=true to re-enable it as part of this call."
            )

        server_fp = user.get("fingerprint")
        local_fp = wallet_manager.fingerprint(wallet_name, password=password)
        if (
            server_fp
            and server_fp != local_fp
            and not override_fingerprint
        ):
            raise ValueError(
                f"Wallet fingerprint mismatch: account is bound to {server_fp!r}, "
                f"this wallet is {local_fp!r}. Pass override_fingerprint=true to re-bind "
                "(this will also disable LN delivery for the previously-bound wallet)."
            )

        if count is None:
            needed = user.get("new_addresses_needed") or 0
            count = needed if needed > 0 else 5
        if count <= 0:
            raise ValueError("count must be positive")
        if count > MAX_ADDRESSES_PER_REGISTRATION:
            raise ValueError(
                f"count cannot exceed {MAX_ADDRESSES_PER_REGISTRATION} (server-side per-call limit)"
            )

        # ``reserve_addresses`` mints all ``count`` fresh addresses in one
        # batched load+save of the wallet record (vs. ``count`` individual
        # ``get_address(None)`` calls that each write to disk). The counter
        # advances past every minted index so no other flow (lw_address,
        # lightning_receive, …) collides with these.
        addresses = [a.address for a in wallet_manager.reserve_addresses(wallet_name, count)]

        resp = self._with_auth_retry(
            lambda access: self.client.register_addresses(
                access, local_fp, addresses, override_fingerprint=override_fingerprint
            )
        )
        return {
            "requested_count": len(addresses),
            "pool_size": len(resp.get("addresses", [])),
            "fingerprint": local_fp,
            "addresses": resp.get("addresses", []),
        }

    def ensure_ln_pool(
        self,
        wallet_manager,
        wallet_name: str = "default",
        password: Optional[str] = None,
        profile: Optional[dict] = None,
    ) -> dict:
        """Idempotent LN-address pool top-up.

        Reads the user's current profile and only POSTs new addresses if the
        server reports ``new_addresses_needed > 0``. No-ops + returns a status
        dict when conditions don't allow auto-refill (no LN username, toggle
        off, fingerprint bound to a different wallet). This is what the
        post-refresh hook calls, and what the ``aqua_ensure_ln_pool`` MCP tool
        exposes to agents.

        When ``profile`` is provided (e.g. by ``_opportunistic_top_up`` after a
        token refresh) it's reused instead of fetching one — saving a round
        trip on the auto-refill path.
        """
        def _skip(reason: str, **extra) -> dict:
            return {"refilled": False, "reason": reason, **extra}

        user = profile if profile is not None else self._with_auth_retry(self.client.get_user)
        ln_username = user.get("ln_username")
        if not ln_username:
            return _skip("no_ln_username", fingerprint=user.get("fingerprint"))
        if not user.get("ln_address_toggled"):
            return _skip("ln_address_disabled", fingerprint=user.get("fingerprint"))
        needed = user.get("new_addresses_needed") or 0
        if needed <= 0:
            return _skip(
                "pool_full", fingerprint=user.get("fingerprint"), new_addresses_needed=0
            )

        server_fp = user.get("fingerprint")
        local_fp = wallet_manager.fingerprint(wallet_name, password=password)
        if server_fp and server_fp != local_fp:
            return _skip(
                "fingerprint_mismatch",
                server_fingerprint=server_fp,
                local_fingerprint=local_fp,
            )

        result = self.register_ln_addresses(
            wallet_manager=wallet_manager,
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

    def _refresh_session(self, session: JAN3Session) -> JAN3Session:
        """Exchange the stored refresh token for a new access token and persist it.
        Rotation-aware; raises ``SessionExpiredError`` if no refresh is stored or rejected."""
        if not session.refresh:
            raise SessionExpiredError("No refresh token stored for this AQUA session.")
        tokens = self.client.refresh_token(session.refresh) or {}
        new_access = tokens.get("access")
        if not new_access or not str(new_access).strip():
            raise SessionExpiredError(
                "AQUA refresh did not return a new access token."
            )
        updated = JAN3Session(
            email=session.email,
            access=str(new_access).strip(),
            # ROTATE_REFRESH_TOKENS may hand back a fresh refresh token; if not,
            # the existing one stays valid.
            refresh=str(tokens.get("refresh") or session.refresh),
            created_at=session.created_at,
        )
        self.storage.save_jan3_session(updated)
        return updated

    def _with_auth_retry(self, call, on_refresh: Optional[Callable[[str], Any]] = None):
        """Call ``call(access_token)``; on ``SessionExpiredError`` refresh once and retry.
        Raises ``ValueError`` only when refresh itself fails or the retry is still rejected.

        ``on_refresh`` (if given) runs with the fresh access token *after* a
        successful refresh-and-retry — used to opportunistically top up the
        LN-address pool once the session has just been renewed."""
        session = self.require_session()
        try:
            return call(session.access)
        except SessionExpiredError:
            try:
                session = self._refresh_session(session)
            except SessionExpiredError as e:
                raise ValueError(
                    "AQUA session expired and could not be refreshed — "
                    "run aqua_login then aqua_verify again."
                ) from e
            try:
                result = call(session.access)
            except SessionExpiredError as e:
                raise ValueError(
                    "AQUA session still invalid after refresh — "
                    "run aqua_login then aqua_verify again."
                ) from e
            if on_refresh is not None:
                on_refresh(session.access)
            return result

    def provision_wapupay_token(self) -> str:
        """Provision a fresh WapuPay API key via the AQUA backend (requires prior login).
        Uses ``_with_auth_retry``; the backend invalidates any previous key on each call."""
        resp = self._with_auth_retry(
            lambda access: self.client.provision_wapupay_account(access)
        )
        token = (resp or {}).get("token")
        if not token or not str(token).strip():
            raise ValueError(
                "AQUA backend did not return a WapuPay API key (token missing)."
            )
        return str(token).strip()
