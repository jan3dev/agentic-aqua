"""Ankara backend integration: Lightning swaps + shared HTTP/PII/JWT helpers.

Talks to JAN3's backend (AQUA's Ankara host, ``ANKARA_API_URL`` — the three
names are the same backend):

    * **Lightning → L-BTC swaps** — ``AnkaraSwapInfo`` / ``AnkaraClient`` (Boltz
      orchestration), consumed by ``lightning.py``.
    * **Shared helpers** — PII/secret redaction (``_redact`` / ``_mask`` /
      ``_extract_error_message``) and JWT-expiry helpers (``_jwt_exp`` /
      ``_access_token_expired``) plus ``SessionExpiredError``, reused by the
      JAN3-account layer in ``jan3_accounts.py``.

JAN3/AQUA *account* management (login/verify/logout/session + WapuPay-key
provisioning, the ``jan3_*`` tools) lives in ``jan3_accounts.py``, which imports
the shared helpers from this module. The dependency is one-way: ``ankara.py``
never imports ``jan3_accounts`` or ``wapupay``.
"""

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Optional

# API URL with environment variable override
ANKARA_API_URL = os.environ.get("ANKARA_API_URL", "https://ankara.aquabtc.com")

USER_AGENT = "agentic-aqua"
HTTP_TIMEOUT_SECONDS = 30.0


class SessionExpiredError(ValueError):
    """Access or refresh JWT was rejected by Ankara (HTTP 401);
    triggers token refresh and subclasses ValueError."""

# Bank-PII / secret fields that must never reach the logs.
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

