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
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from typing import Any, Optional

# API URL with environment variable override
ANKARA_API_URL = os.environ.get("ANKARA_API_URL", "https://ankara.aquabtc.com")

# AQUA-account login (the `aqua_*` tools) authenticates against the same Ankara host
AUTH_BASE_URL = f"{ANKARA_API_URL.rstrip('/')}/api/v1/auth"
WAPUPAY_ACCOUNT_PATH = "/api/v1/wapupay/account/"

USER_AGENT = "agentic-aqua"
HTTP_TIMEOUT_SECONDS = 30.0

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

    def __init__(self, storage) -> None:
        self.storage = storage
        self._client: Optional[JAN3AuthClient] = None

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
