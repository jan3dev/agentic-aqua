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


class _AuthExpired(Exception):
    """Signals the access token was rejected (401). Caught by the manager to retry with a refreshed token."""

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
            if e.code == 401 and access_token:
                raise _AuthExpired() from e
            detail = _extract_error_message(body)
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

    def refresh_access(self, refresh_token: str) -> str:
        """POST /auth/refresh/ — exchange the refresh JWT for a new access JWT."""
        resp = self._api_request(
            "POST",
            f"{self.auth_base_url}/refresh/",
            json_body={"refresh": refresh_token},
        ) or {}
        access = resp.get("access")
        if not access:
            raise ValueError("AQUA refresh did not return a new access token.")
        return access

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
                raise ValueError(
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
        """Report whether a local AQUA session exists (no secrets returned)."""
        session = self.storage.load_jan3_session()
        if not session:
            return {"logged_in": False}
        return {
            "logged_in": True,
            "email": session.email,
            "created_at": session.created_at,
        }

    def require_session(self) -> JAN3Session:
        """Return the stored AQUA session, or raise if the user is not logged in."""
        session = self.storage.load_jan3_session()
        if not session or not session.access:
            raise ValueError(
                "Not logged in to your AQUA account. Run aqua_login then aqua_verify."
            )
        return session

    def _authed_call(self, fn: Callable[[str], Any]) -> Any:
        """Run ``fn(access_token)``; on 401, refresh the access token once and retry.

        Both the refresh and the original error surfaces are mapped to the
        "session invalid or expired" message so callers see a single recovery
        hint regardless of which leg failed.

        After a successful refresh, we opportunistically top up the LN-address
        pool — refresh is the natural "we just touched the session" checkpoint,
        and the AQUA app's ``LnAddressRegistrationNotifier`` re-runs on the
        equivalent provider rebuild. The top-up never breaks the caller's call:
        any error is swallowed and logged at DEBUG.
        """
        session = self.require_session()
        try:
            return fn(session.access)
        except _AuthExpired:
            try:
                new_access = self.client.refresh_access(session.refresh)
            except ValueError as refresh_err:
                raise ValueError(
                    "AQUA session expired and refresh failed — run aqua_login / aqua_verify again."
                ) from refresh_err
            session.access = new_access
            self.storage.save_jan3_session(session)
            try:
                result = fn(new_access)
            except _AuthExpired as second_401:
                raise ValueError(
                    "AQUA session invalid after refresh — run aqua_login / aqua_verify again."
                ) from second_401
            self._opportunistic_top_up(new_access)
            return result

    def _opportunistic_top_up(self, access_token: str) -> None:
        """Fire-and-forget LN-address pool refill after a successful token refresh.

        Fetches the user profile once with the just-refreshed token and threads
        it through ``ensure_ln_pool`` → ``register_ln_addresses`` so the whole
        opportunistic-top-up path costs at most a single ``get_user`` round trip.
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
        return self._authed_call(self.client.get_user)

    def ln_address_toggle(self, enabled: bool) -> dict:
        """Enable or disable the LN-address feature on the account."""
        return self._authed_call(lambda access: self.client.ln_address_toggle(access, enabled))

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
            return self._authed_call(
                lambda access: self.client.ln_username_available(username, access)
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
        user = profile if profile is not None else self.get_user()
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

        resp = self._authed_call(
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
        user = profile if profile is not None else self.get_user()
        ln_username = user.get("ln_username")
        if not ln_username:
            return {
                "refilled": False,
                "reason": "no_ln_username",
                "fingerprint": user.get("fingerprint"),
            }
        if not user.get("ln_address_toggled"):
            return {
                "refilled": False,
                "reason": "ln_address_disabled",
                "fingerprint": user.get("fingerprint"),
            }
        needed = user.get("new_addresses_needed") or 0
        if needed <= 0:
            return {
                "refilled": False,
                "reason": "pool_full",
                "fingerprint": user.get("fingerprint"),
                "new_addresses_needed": 0,
            }

        server_fp = user.get("fingerprint")
        local_fp = wallet_manager.fingerprint(wallet_name, password=password)
        if server_fp and server_fp != local_fp:
            return {
                "refilled": False,
                "reason": "fingerprint_mismatch",
                "server_fingerprint": server_fp,
                "local_fingerprint": local_fp,
            }

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

    def provision_wapupay_token(self) -> str:
        """Provision a fresh WapuPay API key from the AQUA backend and return it.

        Requires a prior AQUA login (``aqua_login`` → ``aqua_verify``): the call
        is authorized with that JWT. The AQUA backend issues a fresh key on EVERY
        call and invalidates any key previously issued for the account.
        """
        session = self.require_session()
        resp = self.client.provision_wapupay_account(session.access)
        token = (resp or {}).get("token")
        if not token or not str(token).strip():
            raise ValueError(
                "AQUA backend did not return a WapuPay API key (token missing)."
            )
        return str(token).strip()
