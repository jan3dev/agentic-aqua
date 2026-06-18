"""WapuPay direct-fiat payments (Argentine ARS payouts funded with Liquid USDT).

A WapuPay **direct-fiat** order lets a user pay an Argentine bank account
(by alias / CBU / CVU) in **ARS**, funding the payout with **USDT on Liquid**.
WapuPay's API is called **directly** (``https://be-prod.wapu.app`` by default;
override with ``WAPUPAY_BASE_URL`` for staging, e.g. ``be-stage.wapu.app``).
Each business call carries WapuPay's own ``X-API-Key`` (read lazily from the
``WAPUPAY_API_KEY`` env var); the funding rail is pinned to Liquid USDT.
WapuPay is the source of truth; we keep only a lightweight local order record
for CLI / MCP recovery and tracking.

Two independent auth surfaces (see CLAUDE.md):

    * **WapuPay API key** — every order/transaction call sends ``X-API-Key``.
      WapuPay treats ``X-API-Key`` and ``Authorization: Bearer`` as mutually
      exclusive (sending both → 400), so business calls send **only** the key
      and never a Bearer token. ``exchange_rates`` is a public endpoint and
      sends no auth header at all.
    * **AQUA account login** — ``login``/``verify`` are an *AQUA-account*
      email-OTP against Ankara (``{ANKARA}/api/v1/auth/{login,verify}/`` → JWT),
      surfaced as the ``aqua_*`` tools. This session is **decoupled** from the
      WapuPay business calls above (they need ``WAPUPAY_API_KEY``, not a login).

Direct-fiat flow (the "order"):

    quote (preview)  →  create_order (create-tentative + issue-funding)
                     →  pay the returned Liquid USDT address (lw_send_asset)
                     →  WapuPay settles ARS to the bank account.

Tentative status machine: ``CREATED → FUNDING_ISSUED → EXECUTED`` with terminals
``EXPIRED``, ``SETTLED_TO_BALANCE`` (USDT credited to WapuPay balance, payout not
made), and ``FAILED``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint configuration
# ---------------------------------------------------------------------------

# WapuPay's own API host — called directly (no Ankara proxy). Override for
# staging / local development with WAPUPAY_BASE_URL (e.g. be-stage.wapu.app).
WAPUPAY_BASE_URL = os.environ.get(
    "WAPUPAY_BASE_URL", "https://be-prod.wapu.app"
).rstrip("/")
# AQUA-account login (the `aqua_*` tools) still authenticates against Ankara —
# the same host as the Lightning integration. Override with ANKARA_API_URL.
ANKARA_API_URL = os.environ.get("ANKARA_API_URL", "https://ankara.aquabtc.com").rstrip(
    "/"
)
AUTH_BASE_URL = f"{ANKARA_API_URL}/api/v1/auth"
# AQUA/Ankara backend host for the WapuPay sub-user provisioning endpoint
# (POST /api/v1/wapupay/account/). Same backend that issues the AQUA JWT, so it
# defaults to ANKARA_API_URL — set AQUA_BACKEND_API_URL only to point provisioning
# at a different host (staging https://test.aquabtc.com, prod ankara.aquabtc.com).
AQUA_BACKEND_API_URL = os.environ.get("AQUA_BACKEND_API_URL", ANKARA_API_URL).rstrip("/")
# Provisioning endpoint path. Keep the trailing slash: Ankara is DRF and
# APPEND_SLASH would 301 a slashless POST and drop the body.
WAPUPAY_ACCOUNT_PATH = "/api/v1/wapupay/account/"

# WapuPay API key, read lazily per call (see WapuPayManager._require_api_key) so
# tests can monkeypatch it and a missing key surfaces a clear error — mirrors
# pix.py's EULEN_API_TOKEN_ENV.
WAPUPAY_API_KEY_ENV = "WAPUPAY_API_KEY"

USER_AGENT = "agentic-aqua"
HTTP_TIMEOUT_SECONDS = 30.0

# Fixed funding rail (v1: Liquid USDT only). Ankara pins these and rejects any
# other rail with a 400, so we send them explicitly and never offer a choice.
FUNDING_METHOD_USDT = "USDT"
FUNDING_NETWORK_LIQUID = "LIQUID"

# Fiat side is always Argentine pesos in v1.
CURRENCY_PAYMENT_ARS = "ARS"
CURRENCY_TAKEN_USDT = "USDT"

# WapuPay direct-fiat transfer types the user can choose between.
TRANSFER_TYPES = ("fiat_transfer", "fast_fiat_transfer")

# Bank-PII / secret fields that must never reach the logs (mirrors Ankara's
# WAPUPAY_SENSITIVE_FIELDS plus the WapuPay API key and the AQUA-login token).
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

# Tentative status groupings.
_FINAL_STATUSES = {"EXECUTED", "EXPIRED", "SETTLED_TO_BALANCE", "FAILED"}
_SUCCESS_STATUSES = {"EXECUTED"}
_FAILED_STATUSES = {"EXPIRED", "FAILED"}


def order_is_final(status: str) -> bool:
    return (status or "").upper() in _FINAL_STATUSES


def order_is_success(status: str) -> bool:
    return (status or "").upper() in _SUCCESS_STATUSES


def order_is_failed(status: str) -> bool:
    return (status or "").upper() in _FAILED_STATUSES


# ---------------------------------------------------------------------------
# Helpers
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
    """Mask structured PII embedded in free text before it reaches a log/envelope.

    Bank PII must never be logged, and key-based ``_redact`` cannot see PII inside
    a *string value* (e.g. an upstream error like ``"Invalid alias: 0001234..."``).
    This masks the unambiguous structured identifiers — email addresses and long
    digit runs (CBU/CVU are 22 digits; account/phone/DNI). Realistic ARS amounts
    are well under 11 digits (KYC caps payouts at a few million pesos), so the
    digit mask won't touch a legitimate amount.
    """
    if not s:
        return s
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<redacted-email>", s)
    s = re.sub(r"\d{11,}", "<redacted>", s)
    return s


def _mask(secret: str) -> str:
    """Mask a secret for display: keep the first/last few chars, hide the middle.

    The raw key must never appear in tool output or logs; this gives the user
    enough to confirm which key is configured without revealing it. Short
    strings collapse to ``***`` so we never leak a meaningful fraction.
    """
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}…{secret[-4:]}"


def _extract_error_message(body: str) -> str:
    """Pull a human-readable, PII-scrubbed message out of an Ankara/WapuPay error body.

    Ankara DRF errors use ``detail`` / ``message`` / ``error_code``; WapuPay
    envelopes use ``error`` / ``message``. The result flows into a logged
    exception message and the client error envelope, so every return path is
    scrubbed: dict dumps are ``_redact``-ed (key-based) and all strings pass
    through ``_scrub_text`` (value-based). Falls back to the raw body.
    """
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
        # DRF validation errors nest under "details" or field keys — redact keys
        # (alias/receiver_name/...) before dumping, then scrub residual free text.
        details = parsed.get("details")
        if details:
            return _scrub_text(json.dumps(_redact(details))[:200])
        return _scrub_text(json.dumps(_redact(parsed))[:200])
    return _scrub_text(str(parsed)[:200])


def _normalize_ars_amount(amount_ars: str | int | float | Decimal) -> Decimal:
    """Validate ``amount_ars`` as a positive, whole-peso fiat Decimal.

    ARS is fiat, not satoshis — kept as a Decimal at the wire boundary, never a
    float internally. WapuPay direct-fiat transfers are whole pesos
    (``min_payment_amount_ars`` is 10000), so a non-integral amount is rejected
    rather than silently floated onto the wire (no-float-at-boundary invariant).
    Raises ``ValueError`` on non-positive / non-integral / unparseable input.
    """
    try:
        d = Decimal(str(amount_ars))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"Invalid amount_ars: {amount_ars!r}") from e
    # Reject NaN / ±Infinity BEFORE the comparison below (a money validator must
    # never accept a non-finite amount, and `NaN <= 0` would raise InvalidOperation).
    if not d.is_finite():
        raise ValueError(f"amount_ars must be a finite number, got {amount_ars!r}")
    if d <= 0:
        raise ValueError("amount_ars must be positive")
    if d != d.to_integral_value():
        raise ValueError(
            f"amount_ars must be a whole number of pesos, got {amount_ars!r}"
        )
    return d


def _ars_for_wire(d: Decimal) -> int:
    """Render a whole-peso ARS Decimal as an integer JSON number (never a float)."""
    return int(d)


def usdt_to_sats(amount_usdt: str | int | float | Decimal) -> int:
    """Convert a USDT (Liquid) decimal amount to integer satoshis.

    USDT-Liquid uses 8 decimal places — same conversion as L-BTC sats.
    """
    d = Decimal(str(amount_usdt))
    sats = (d * Decimal(100_000_000)).quantize(Decimal("1."), rounding=ROUND_HALF_UP)
    return int(sats)


# A WapuPay tentative id is a canonical UUID (matches Ankara's proxy allowlist,
# which is stricter than storage's SWAP_ID_PATTERN).
_TENTATIVE_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _validate_tentative_id(tentative_id: str) -> str:
    """Validate + normalize a user-supplied tentative id BEFORE it builds a URL.

    Fails fast locally (no network call, no malformed path) on a bad id, and
    matches the UUID shape Ankara's allowlist requires.
    """
    tid = (tentative_id or "").strip()
    if not tid:
        raise ValueError("tentative_id is required")
    if not _TENTATIVE_ID_RE.fullmatch(tid):
        raise ValueError(f"Invalid tentative_id (expected a UUID): {tentative_id!r}")
    return tid


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class WapuPaySession:
    """Persisted AQUA↔Ankara session (JWT pair). Stored 0o600 — holds a
    money-authorizing bearer token; never logged."""

    email: str
    access: str
    refresh: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WapuPaySession":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class WapuPayApiKey:
    """Locally-persisted WapuPay API key provisioned via the AQUA backend.

    Stored 0o600 — it authorizes WapuPay business calls directly and is never
    logged (``token`` is in ``_SENSITIVE_LOG_FIELDS``). Decoupled from the AQUA
    login session: logging out does NOT delete it."""

    token: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WapuPayApiKey":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class WapuPayOrder:
    """Lightweight local record of a WapuPay direct-fiat order.

    WapuPay is the source of truth; this record exists only so the CLI / MCP can
    list, track, and recover orders. It carries bank PII (``alias`` / CBU,
    ``receiver_name``) — persisted 0o600 and never logged.
    """

    tentative_id: str
    status: str
    type: str
    amount_ars: str  # original decimal string (fidelity)
    alias: str
    created_at: str
    receiver_name: Optional[str] = None
    funding_currency: Optional[str] = None
    funding_network: Optional[str] = None
    exchange_rate: Optional[float] = None
    fee_amount_usdt: Optional[float] = None
    funding_amount_usdt: Optional[float] = None
    funding_amount_sat: Optional[int] = None
    total_amount_usdt: Optional[float] = None
    address_destination: Optional[str] = None
    asset_id: Optional[str] = None
    expires_at: Optional[str] = None
    funding_expires_at: Optional[str] = None
    refund_address: Optional[str] = None
    external_reference: Optional[str] = None
    funding_transaction_id: Optional[str] = None
    executed_transaction_id: Optional[str] = None
    wallet_name: Optional[str] = None
    last_checked_at: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WapuPayOrder":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def apply_tentative(self, resp: dict) -> None:
        """Merge a tentative / funding response from WapuPay into this record.

        Only overwrites fields present in the response, so a status poll that
        omits funding fields doesn't wipe a previously-issued funding address.
        """
        if not isinstance(resp, dict):
            raise ValueError(
                f"WapuPay returned an unexpected (non-object) response: {type(resp).__name__}"
            )
        mapping = {
            "status": "status",
            "funding_currency": "funding_currency",
            "funding_network": "funding_network",
            "exchange_rate": "exchange_rate",
            "fee_amount_usdt": "fee_amount_usdt",
            "funding_amount_usdt": "funding_amount_usdt",
            "funding_amount_sat": "funding_amount_sat",
            "total_amount_usdt": "total_amount_usdt",
            "address_destination": "address_destination",
            "asset_id": "asset_id",
            "expires_at": "expires_at",
            "funding_expires_at": "funding_expires_at",
            "refund_address": "refund_address",
            "external_reference": "external_reference",
            "funding_transaction_id": "funding_transaction_id",
            "executed_transaction_id": "executed_transaction_id",
        }
        for attr, key in mapping.items():
            if key in resp and resp[key] is not None:
                setattr(self, attr, resp[key])
        # Derive sats from the USDT funding amount when WapuPay omits it (it
        # always does on create). Re-derive whenever a response carries a new
        # USDT amount without a sat, so a re-funding can't leave a stale sat
        # that mis-states the amount to send via lw_send_asset.
        usdt_in_resp = resp.get("funding_amount_usdt") is not None
        sat_in_resp = resp.get("funding_amount_sat") is not None
        if self.funding_amount_usdt is not None and (
            self.funding_amount_sat is None or (usdt_in_resp and not sat_in_resp)
        ):
            self.funding_amount_sat = usdt_to_sats(self.funding_amount_usdt)
        # Integer-satoshis invariant: if WapuPay sent funding_amount_sat as a
        # float (against its own integer spec), coerce so callers / lw_send_asset
        # never receive a float amount.
        if isinstance(self.funding_amount_sat, float):
            self.funding_amount_sat = int(self.funding_amount_sat)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class WapuPayClient:
    """HTTP client for WapuPay's direct API + AQUA-account (Ankara) auth.

    All network I/O funnels through the single ``_api_request`` seam so tests
    patch one method (see ``tests/AGENTS.md``). Business calls send WapuPay's
    ``X-API-Key`` (never a Bearer token — the two are mutually exclusive).
    Upstream errors are surfaced as ``ValueError`` with a human message (a 401
    means the API key is missing/invalid); no fallback fake-success values
    (CLAUDE.md "No lies rules").
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_base_url: Optional[str] = None,
        aqua_backend_url: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or WAPUPAY_BASE_URL).rstrip("/")
        self.auth_base_url = (auth_base_url or AUTH_BASE_URL).rstrip("/")
        self.aqua_backend_url = (aqua_backend_url or AQUA_BACKEND_API_URL).rstrip("/")

    @staticmethod
    def _api_key_headers(api_key: str) -> dict[str, str]:
        """Build WapuPay's API-key auth header.

        WapuPay rejects a request that carries both ``X-API-Key`` and a Bearer
        token, so this is the *only* auth header business calls ever send.
        """
        return {"X-API-Key": api_key}

    def _api_request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict] = None,
        api_key: Optional[str] = None,
    ) -> Any:
        """Perform one HTTP request and return parsed JSON (or ``{}`` if empty).

        Sends ``X-API-Key`` when ``api_key`` is given (never an ``Authorization``
        header — WapuPay forbids carrying both).

        Raises:
            ValueError: on any non-2xx (a 401 means the API key is missing or
                invalid), a non-JSON body, or if the host is unreachable.
        """
        data = json.dumps(json_body).encode() if json_body is not None else None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if api_key:
            headers.update(self._api_key_headers(api_key))

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
                        f"WapuPay returned a non-JSON response "
                        f"({resp.status} {method})"
                    ) from e
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            detail = _extract_error_message(body)
            msg = f"WapuPay request failed ({e.code} {method})"
            if detail:
                msg += f": {detail}"
            raise ValueError(msg) from e
        except urllib.error.URLError as e:
            raise ValueError(
                f"WapuPay / Ankara unreachable ({method}): {e.reason}"
            ) from e

    # -- Ankara auth ---------------------------------------------------------

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
        """POST /api/v1/wapupay/account/ — provision a WapuPay sub-user & return its key.

        This hits the AQUA/Ankara backend (NOT WapuPay directly), authenticated
        with the AQUA JWT — so it sends ``Authorization: Bearer`` and never an
        ``X-API-Key``. The backend returns ``{"token": "<wapupay api key>"}`` and
        rotates the key on every successful call. Self-contained (does not reuse
        the WapuPay-flavoured ``_api_request`` seam) so the error messages and
        Bearer auth are correct for the AQUA backend; all network I/O still goes
        through ``urllib.request.urlopen`` (the single patch point in tests).

        Raises:
            ValueError: on any non-2xx (401 = bad/expired AQUA JWT, 403 =
                wapupay_b2b feature off, 502 = WapuPay upstream), a non-JSON
                body, or an unreachable host. No fake-success fallback.
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
            # Code-specific, actionable messages. For 401 we replace (not append)
            # the upstream DRF detail — "credentials were not provided" is noise.
            if e.code == 401:
                raise ValueError(
                    "AQUA session invalid or expired — run aqua_login / aqua_verify again."
                ) from e
            if e.code == 403:
                raise ValueError(
                    "WapuPay B2B is not enabled for your AQUA account "
                    "(feature 'wapupay_b2b'; contact AQUA ops)."
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

    # -- WapuPay direct API --------------------------------------------------

    def _proxy(
        self,
        method: str,
        subpath: str,
        *,
        api_key: Optional[str] = None,
        json_body: Optional[dict] = None,
        query: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}/{subpath.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return self._api_request(method, url, json_body=json_body, api_key=api_key)

    def exchange_rates(self) -> dict:
        """GET exchange_rates — current rate pairs (public; no API key)."""
        return self._proxy("GET", "exchange_rates") or {}

    def tentative_amount(self, body: dict, *, api_key: str) -> dict:
        """POST transactions/tentative-amount — cost preview (currencies forced server-side)."""
        return self._proxy(
            "POST", "transactions/tentative-amount", api_key=api_key, json_body=body
        ) or {}

    def create_tentative(self, body: dict, *, api_key: str) -> dict:
        """POST transactions/direct-fiat/tentatives — create + freeze the quote."""
        return self._proxy(
            "POST", "transactions/direct-fiat/tentatives", api_key=api_key, json_body=body
        ) or {}

    def issue_funding(self, tentative_id: str, *, api_key: str) -> dict:
        """POST …/tentatives/{uuid}/funding — issue Liquid USDT funding instructions."""
        return self._proxy(
            "POST",
            f"transactions/direct-fiat/tentatives/{tentative_id}/funding",
            api_key=api_key,
        ) or {}

    def get_tentative(self, tentative_id: str, *, api_key: str) -> dict:
        """GET …/tentatives/{uuid} — tentative status."""
        return self._proxy(
            "GET", f"transactions/direct-fiat/tentatives/{tentative_id}", api_key=api_key
        ) or {}

    def my_transactions(self, *, api_key: str) -> Any:
        """GET transactions/my_transactions — list scoped to the WapuPay account/key."""
        return self._proxy("GET", "transactions/my_transactions", api_key=api_key) or {}

    def get_transaction(self, tx_id: str, *, api_key: str) -> dict:
        """GET transactions/{id} — a single transaction (uuid or numeric)."""
        return self._proxy("GET", f"transactions/{tx_id}", api_key=api_key) or {}

    def spending_limit(self, *, api_key: str) -> dict:
        """GET users/spending_limit — monthly KYC limit (USDT)."""
        return self._proxy("GET", "users/spending_limit", api_key=api_key) or {}


# ---------------------------------------------------------------------------
# High-level manager
# ---------------------------------------------------------------------------


class WapuPayManager:
    """Wallet-aware orchestration of WapuPay auth + direct-fiat orders.

    Session and order persistence go through ``Storage``; the wallet manager is
    used only to resolve a Liquid refund address when one isn't supplied.
    """

    def __init__(self, storage, wallet_manager) -> None:
        self.storage = storage
        self.wallet_manager = wallet_manager
        self._client: Optional[WapuPayClient] = None

    @property
    def client(self) -> WapuPayClient:
        if self._client is None:
            self._client = WapuPayClient()
        return self._client

    # -- Auth ----------------------------------------------------------------

    def login(self, email: str, language: str = "en") -> dict:
        """Start login: Ankara emails an OTP. Does not persist anything yet."""
        if not email or "@" not in email:
            raise ValueError("A valid email is required to log in to WapuPay")
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
                "WapuPay verify did not return tokens — check the email and OTP code."
            )
        session = WapuPaySession(
            email=email,
            access=access,
            refresh=refresh,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.storage.save_wapupay_session(session)
        return {
            "email": email,
            "logged_in": True,
            "message": "Logged in to your AQUA account via JAN3 Ankara.",
        }

    def logout(self) -> dict:
        """Forget the local AQUA session (does not revoke the token server-side)."""
        existed = self.storage.load_wapupay_session() is not None
        self.storage.delete_wapupay_session()
        return {"logged_out": existed}

    def session_status(self) -> dict:
        """Report whether a local AQUA session exists (no secrets returned)."""
        session = self.storage.load_wapupay_session()
        if not session:
            return {"logged_in": False}
        return {
            "logged_in": True,
            "email": session.email,
            "created_at": session.created_at,
        }

    def provision_account(self) -> dict:
        """Provision a WapuPay API key via the AQUA backend and store it locally.

        Requires a prior AQUA login (``aqua_login`` → ``aqua_verify``): the call
        is authorized with that JWT. The returned key is persisted to
        ``~/.aqua/wapupay/api_key.json`` (0o600) so every ``wapupay_*`` business
        tool can use it without an env var — the raw key is never returned.

        The AQUA backend issues a fresh key on EVERY call and invalidates any key
        previously issued for the account (no grace period).
        """
        env_key = os.environ.get(WAPUPAY_API_KEY_ENV)
        if env_key:
            return {
                "already_configured": True,
                "source": "env",
                "key_preview": _mask(env_key),
                "message": (
                    f"A WapuPay API key is already set via {WAPUPAY_API_KEY_ENV} "
                    "(it takes precedence) — nothing to do."
                ),
            }
        stored = self.storage.load_wapupay_api_key()
        if stored and stored.token:
            return {
                "already_configured": True,
                "source": "stored",
                "key_preview": _mask(stored.token),
                "created_at": stored.created_at,
                "message": (
                    "A WapuPay API key is already provisioned and stored — all "
                    "WapuPay tools are ready."
                ),
            }

        session = self.storage.load_wapupay_session()
        if not session or not session.access:
            raise ValueError(
                "Not logged in to your AQUA account. Run aqua_login then aqua_verify "
                "before provisioning a WapuPay API key."
            )
        resp = self.client.provision_wapupay_account(session.access)
        token = (resp or {}).get("token")
        if not token or not str(token).strip():
            raise ValueError(
                "AQUA backend did not return a WapuPay API key (token missing)."
            )
        token = str(token).strip()
        record = WapuPayApiKey(token=token, created_at=datetime.now(UTC).isoformat())
        self.storage.save_wapupay_api_key(record)
        return {
            "provisioned": True,
            "source": "stored",
            "key_preview": _mask(token),
            "created_at": record.created_at,
            "message": (
                "WapuPay API key provisioned and stored locally — all WapuPay tools "
                "are now ready."
            ),
            "warning": (
                "AQUA's backend issues a fresh WapuPay API key on every call and "
                "invalidates any key previously issued for this account — any earlier "
                "WapuPay key no longer works."
            ),
        }

    def _require_api_key(self) -> str:
        """Return WapuPay's API key, or raise.

        Resolution order, read lazily on every business call (so a key change
        takes effect without a restart and tests can monkeypatch it):

        1. ``WAPUPAY_API_KEY`` env var — explicit config wins.
        2. The key provisioned via ``wapupay_provision_account`` and persisted
           under ``~/.aqua/wapupay/api_key.json``.

        WapuPay business endpoints are authorized by this key, not by the AQUA
        login session; a 401 from WapuPay means the key is missing or invalid.
        No silent fallback (CLAUDE.md "No lies rules").
        """
        key = os.environ.get(WAPUPAY_API_KEY_ENV)
        if key:
            return key
        record = self.storage.load_wapupay_api_key()
        if record and record.token:
            return record.token
        raise ValueError(
            f"WapuPay API key not configured. Set {WAPUPAY_API_KEY_ENV} in your "
            "environment, or run wapupay_provision_account (after aqua_login) to "
            "provision and store one."
        )

    # -- Read-only -----------------------------------------------------------

    def exchange_rates(self) -> dict:
        # Public endpoint — no API key required.
        return self.client.exchange_rates()

    def quote(self, amount_ars, transfer_type: str, alias: Optional[str] = None) -> dict:
        """Preview the USDT cost / fee / rate for a hypothetical ARS payment.

        Surfaces ``valid_cbu_alias`` so a bad alias/CBU is caught before any
        order is created. Currencies are forced to ARS/USDT server-side.
        """
        key = self._require_api_key()
        self._validate_type(transfer_type)
        d = _normalize_ars_amount(amount_ars)
        body: dict[str, Any] = {
            "amount": _ars_for_wire(d),
            "type": transfer_type,
            "currency_payment": CURRENCY_PAYMENT_ARS,
            "currency_taken": CURRENCY_TAKEN_USDT,
        }
        if alias and alias.strip():
            body["alias"] = alias.strip()
        return self.client.tentative_amount(body, api_key=key)

    def transactions(self) -> Any:
        return self.client.my_transactions(api_key=self._require_api_key())

    def transaction(self, tx_id: str) -> dict:
        if not tx_id or not str(tx_id).strip():
            raise ValueError("transaction id is required")
        key = self._require_api_key()
        return self.client.get_transaction(str(tx_id).strip(), api_key=key)

    def spending_limit(self) -> dict:
        result = self.client.spending_limit(api_key=self._require_api_key())
        if "kyc_tier" in result:
            result["tier"] = result.pop("kyc_tier")
        return result

    # -- Order lifecycle -----------------------------------------------------

    def create_order(
        self,
        amount_ars,
        alias: str,
        transfer_type: str,
        receiver_name: Optional[str] = None,
        refund_address: Optional[str] = None,
        external_reference: Optional[str] = None,
        wallet_name: str = "default",
    ) -> dict:
        """Create a direct-fiat order and issue Liquid USDT funding instructions.

        Two upstream steps, run back-to-back: create-tentative (freezes the
        quote) then issue-funding (returns the Liquid address). The local order
        is persisted the instant create succeeds — **before** funding — so a
        funding failure leaves a recoverable ``CREATED`` order rather than an
        orphan. On funding failure we return the order flagged ``funded=False``
        with the error (no silent fake-success); recover with ``fund_order``.

        Returns the order record including ``address_destination`` (Liquid),
        ``asset_id`` (USDT), ``funding_amount_usdt`` / ``funding_amount_sat`` and
        ``funding_expires_at``. The caller pays it with ``lw_send_asset`` — this
        method never broadcasts.
        """
        # Read the API key up front — before any network call or persistence —
        # so a missing key fails fast and never leaves a half-created order.
        key = self._require_api_key()
        self._validate_type(transfer_type)
        if not alias or not alias.strip():
            raise ValueError("alias (recipient bank alias / CBU / CVU) is required")
        d = _normalize_ars_amount(amount_ars)

        body: dict[str, Any] = {
            "amount_ars": _ars_for_wire(d),
            "type": transfer_type,
            "alias": alias.strip(),
            "funding_method": FUNDING_METHOD_USDT,
            "network": FUNDING_NETWORK_LIQUID,
        }
        if receiver_name and receiver_name.strip():
            body["receiver_name"] = receiver_name.strip()
        if refund_address and refund_address.strip():
            body["refund_address"] = refund_address.strip()
        if external_reference and external_reference.strip():
            body["external_reference"] = external_reference.strip()

        created = self.client.create_tentative(body, api_key=key)
        if not isinstance(created, dict):
            raise ValueError(
                f"WapuPay returned an unexpected create response: {type(created).__name__}"
            )
        tentative_id = created.get("tentative_id")
        if not tentative_id:
            raise ValueError(
                f"WapuPay did not return a tentative_id on create: {_redact(created)!r}"
            )

        order = WapuPayOrder(
            tentative_id=tentative_id,
            status=created.get("status", "CREATED"),
            type=transfer_type,
            amount_ars=str(d),
            alias=alias.strip(),
            created_at=datetime.now(UTC).isoformat(),
            receiver_name=(receiver_name.strip() if receiver_name else None),
            refund_address=(refund_address.strip() if refund_address else None),
            external_reference=(external_reference.strip() if external_reference else None),
            wallet_name=wallet_name,
        )
        order.apply_tentative(created)
        # Persist BEFORE funding — a crash/failure mid-funding stays recoverable.
        self.storage.save_wapupay_order(order)

        try:
            funding = self.client.issue_funding(tentative_id, api_key=key)
        except Exception as e:
            order.last_error = f"Funding not issued: {e}"
            self.storage.save_wapupay_order(order)
            result = order.to_dict()
            result["funded"] = False
            result["next_step"] = (
                "Order created but funding was not issued. Call wapupay_fund_order "
                f"with tentative_id={tentative_id} to get the Liquid address."
            )
            return result

        order.apply_tentative(funding)
        order.last_error = None
        self.storage.save_wapupay_order(order)
        return self._funded_result(order)

    def fund_order(self, tentative_id: str) -> dict:
        """Issue (or re-issue) funding instructions for an existing order."""
        # Validate the id BEFORE it reaches URL construction / the network.
        tentative_id = _validate_tentative_id(tentative_id)
        funding = self.client.issue_funding(tentative_id, api_key=self._require_api_key())

        order = self.storage.load_wapupay_order(tentative_id)
        if order is None:
            # Order created elsewhere (e.g. another device); start a thin record.
            order = WapuPayOrder(
                tentative_id=tentative_id,
                status=funding.get("status", "FUNDING_ISSUED"),
                type=funding.get("type", ""),
                amount_ars="",
                alias="",
                created_at=datetime.now(UTC).isoformat(),
            )
        order.apply_tentative(funding)
        order.last_error = None
        self.storage.save_wapupay_order(order)
        return self._funded_result(order)

    def order_status(self, tentative_id: str) -> dict:
        """Re-read the tentative from WapuPay and persist it (source of truth)."""
        tentative_id = _validate_tentative_id(tentative_id)
        # A missing API key is a config error — surface it directly rather than
        # masking it as a transient "could not refresh status" warning below.
        key = self._require_api_key()

        order = self.storage.load_wapupay_order(tentative_id)
        warning = None
        try:
            latest = self.client.get_tentative(tentative_id, api_key=key)
            if order is None:
                order = WapuPayOrder(
                    tentative_id=tentative_id,
                    status=latest.get("status", ""),
                    type=latest.get("type", ""),
                    amount_ars="",
                    alias="",
                    created_at=datetime.now(UTC).isoformat(),
                )
            order.apply_tentative(latest)
            order.last_checked_at = datetime.now(UTC).isoformat()
            self.storage.save_wapupay_order(order)
        except Exception as e:
            if order is None:
                raise
            warning = f"Could not refresh status: {e}"

        result = order.to_dict()
        result["is_final"] = order_is_final(order.status)
        result["is_success"] = order_is_success(order.status)
        result["is_failed"] = order_is_failed(order.status)
        if warning:
            result["warning"] = warning
        return result

    def list_orders(self) -> list[dict]:
        """Return all locally-persisted orders (most recent first).

        These are recovery records, so a single corrupt/partial file is skipped
        (with a warning) rather than aborting the whole listing.
        """
        orders = []
        for tid in self.storage.list_wapupay_orders():
            try:
                order = self.storage.load_wapupay_order(tid)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                logger.warning("Skipping unreadable WapuPay order file: %s", tid)
                continue
            if order is not None:
                orders.append(order)
        orders.sort(key=lambda o: o.created_at or "", reverse=True)
        return [o.to_dict() for o in orders]

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _validate_type(transfer_type: str) -> None:
        if transfer_type not in TRANSFER_TYPES:
            raise ValueError(
                f"type must be one of {TRANSFER_TYPES}, got {transfer_type!r}"
            )

    @staticmethod
    def _funded_result(order: "WapuPayOrder") -> dict:
        result = order.to_dict()
        result["funded"] = bool(order.address_destination)
        if order.address_destination:
            result["pay_instructions"] = (
                f"Send {order.funding_amount_usdt} USDT "
                f"({order.funding_amount_sat} sats) on Liquid to "
                f"{order.address_destination} using lw_send_asset "
                f"(asset_id={order.asset_id}). WapuPay then pays "
                f"{order.amount_ars} ARS to {order.alias}."
            )
        return result
