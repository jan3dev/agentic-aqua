"""WapuPay direct-fiat payments via JAN3's AQUA Ankara backend proxy.

A WapuPay **direct-fiat** order lets an AQUA user pay an Argentine bank account
(by alias / CBU / CVU) in **ARS**, funding the payout with **USDT on Liquid**.
Everything is routed through the Ankara backend's thin catch-all proxy at
``{ANKARA}/api/v1/wapupay/<subpath>`` — never WapuPay directly. Ankara
authenticates the AQUA user (JWT), injects the business ``X-API-Key`` and the
per-user ``X-Wapu-User-Id`` sub-user, and pins the funding rail to Liquid USDT.
WapuPay is the source of truth; we keep only a lightweight local order record
for CLI / MCP recovery and tracking.

Auth (Ankara email-OTP → JWT):

    1. ``POST {ANKARA}/api/v1/auth/login/``  {email, language}  → OTP emailed.
    2. ``POST {ANKARA}/api/v1/auth/verify/`` {email, otp_code} → {access, refresh}.
    3. ``POST {ANKARA}/api/v1/auth/refresh/`` {refresh}         → {access}.

The Ankara wapupay view is ``IsAuthenticated`` and accepts the access token as
an ``Authorization: Bearer`` header (primary) or the ``access_token`` cookie
(fallback). We use the Bearer header — isolated in ``WapuPayClient._auth_headers``
so the cookie form is a one-line swap. Every subpath (even ``exchange_rates``)
requires a valid AQUA session; on a 401 we refresh once and retry.

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
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint configuration
# ---------------------------------------------------------------------------

# Reuse the same Ankara host as the Lightning integration. Override for staging
# / local development with ANKARA_API_URL (e.g. a be-stage Ankara instance).
ANKARA_API_URL = os.environ.get("ANKARA_API_URL", "https://ankara.aquabtc.com").rstrip(
    "/"
)
# The wapupay proxy base and the auth base both live under the same Ankara host.
WAPUPAY_BASE_URL = os.environ.get(
    "WAPUPAY_BASE_URL", f"{ANKARA_API_URL}/api/v1/wapupay"
).rstrip("/")
AUTH_BASE_URL = f"{ANKARA_API_URL}/api/v1/auth"

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
# WAPUPAY_SENSITIVE_FIELDS plus the bearer token).
_SENSITIVE_LOG_FIELDS = frozenset(
    {"alias", "receiver_name", "refund_address", "access", "refresh", "authorization"}
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
# Errors
# ---------------------------------------------------------------------------


class WapuPayAuthError(Exception):
    """Ankara rejected our AQUA JWT (HTTP 401).

    Internal control-flow signal: the manager catches it to refresh the token
    and retry once. It never escapes the manager — a persistent auth failure is
    re-raised as a plain ``ValueError`` telling the user to log in again.
    """


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


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class WapuPayClient:
    """HTTP client for AQUA's Ankara-proxied WapuPay surface + Ankara auth.

    All network I/O funnels through the single ``_api_request`` seam so tests
    patch one method (see ``tests/AGENTS.md``). Upstream errors are surfaced as
    ``ValueError`` with a human message; a 401 raises ``WapuPayAuthError`` so the
    manager can refresh + retry. No fallback fake-success values (CLAUDE.md
    "No lies rules").
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_base_url: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or WAPUPAY_BASE_URL).rstrip("/")
        self.auth_base_url = (auth_base_url or AUTH_BASE_URL).rstrip("/")

    @staticmethod
    def _auth_headers(access: str) -> dict[str, str]:
        """Build the AQUA-session auth header.

        Ankara's CookieJWTAuthentication accepts either form; we use the Bearer
        header. To fall back to the cookie form, return
        ``{"Cookie": f"access_token={access}"}`` here instead — single swap.
        """
        return {"Authorization": f"Bearer {access}"}

    def _api_request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict] = None,
        access: Optional[str] = None,
    ) -> Any:
        """Perform one HTTP request and return parsed JSON (or ``{}`` if empty).

        Raises:
            WapuPayAuthError: on HTTP 401 (expired/invalid AQUA token).
            ValueError: on any other non-2xx, or if Ankara is unreachable.
        """
        data = json.dumps(json_body).encode() if json_body is not None else None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if access:
            headers.update(self._auth_headers(access))

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            if e.code == 401:
                # Our AQUA token is stale — let the manager refresh + retry.
                raise WapuPayAuthError("AQUA session not accepted (401)") from e
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

    def refresh(self, refresh_token: str) -> dict:
        """POST /auth/refresh/ — rotate the access token. Returns ``{access}``."""
        return self._api_request(
            "POST",
            f"{self.auth_base_url}/refresh/",
            json_body={"refresh": refresh_token},
        ) or {}

    # -- WapuPay proxy (delegated through Ankara) ----------------------------

    def _proxy(
        self,
        method: str,
        subpath: str,
        access: str,
        *,
        json_body: Optional[dict] = None,
        query: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}/{subpath.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return self._api_request(method, url, json_body=json_body, access=access)

    def exchange_rates(self, access: str) -> dict:
        """GET exchange_rates — current rate pairs (still requires an AQUA session)."""
        return self._proxy("GET", "exchange_rates", access) or {}

    def tentative_amount(self, access: str, body: dict) -> dict:
        """POST transactions/tentative-amount — cost preview (currencies forced by Ankara)."""
        return self._proxy(
            "POST", "transactions/tentative-amount", access, json_body=body
        ) or {}

    def create_tentative(self, access: str, body: dict) -> dict:
        """POST transactions/direct-fiat/tentatives — create + freeze the quote."""
        return self._proxy(
            "POST", "transactions/direct-fiat/tentatives", access, json_body=body
        ) or {}

    def issue_funding(self, access: str, tentative_id: str) -> dict:
        """POST …/tentatives/{uuid}/funding — issue Liquid USDT funding instructions."""
        return self._proxy(
            "POST",
            f"transactions/direct-fiat/tentatives/{tentative_id}/funding",
            access,
        ) or {}

    def get_tentative(self, access: str, tentative_id: str) -> dict:
        """GET …/tentatives/{uuid} — tentative status."""
        return self._proxy(
            "GET", f"transactions/direct-fiat/tentatives/{tentative_id}", access
        ) or {}

    def my_transactions(self, access: str) -> Any:
        """GET transactions/my_transactions — list scoped to the sub-user."""
        return self._proxy("GET", "transactions/my_transactions", access) or {}

    def get_transaction(self, access: str, tx_id: str) -> dict:
        """GET transactions/{id} — a single transaction (uuid or numeric)."""
        return self._proxy("GET", f"transactions/{tx_id}", access) or {}

    def spending_limit(self, access: str) -> dict:
        """GET users/spending_limit — monthly KYC limit (USDT)."""
        return self._proxy("GET", "users/spending_limit", access) or {}


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
            "next_step": "Call wapupay_verify with the OTP code from your email.",
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
            "message": "Logged in to WapuPay via JAN3 Ankara.",
        }

    def logout(self) -> dict:
        """Forget the local session (does not revoke the token server-side)."""
        existed = self.storage.load_wapupay_session() is not None
        self.storage.delete_wapupay_session()
        return {"logged_out": existed}

    def session_status(self) -> dict:
        """Report whether a local WapuPay session exists (no secrets returned)."""
        session = self.storage.load_wapupay_session()
        if not session:
            return {"logged_in": False}
        return {
            "logged_in": True,
            "email": session.email,
            "created_at": session.created_at,
        }

    def _with_auth(self, fn: Callable[[str], Any]) -> Any:
        """Run ``fn(access)`` with a valid session, refreshing once on a 401."""
        session = self.storage.load_wapupay_session()
        if not session:
            raise ValueError(
                "Not logged in to WapuPay. Run `aqua wapupay login` (or wapupay_login) first."
            )
        try:
            return fn(session.access)
        except WapuPayAuthError:
            pass  # fall through to refresh + retry

        try:
            refreshed = self.client.refresh(session.refresh)
        except Exception as e:
            self.storage.delete_wapupay_session()
            raise ValueError(
                "WapuPay session expired and could not be refreshed. Please log in again."
            ) from e

        access = refreshed.get("access")
        if not access:
            self.storage.delete_wapupay_session()
            raise ValueError(
                "WapuPay token refresh returned no access token. Please log in again."
            )
        session.access = access
        self.storage.save_wapupay_session(session)

        try:
            return fn(access)
        except WapuPayAuthError as e:
            self.storage.delete_wapupay_session()
            raise ValueError(
                "WapuPay session expired. Please log in again."
            ) from e

    # -- Read-only -----------------------------------------------------------

    def exchange_rates(self) -> dict:
        return self._with_auth(lambda a: self.client.exchange_rates(a))

    def quote(self, amount_ars, transfer_type: str, alias: Optional[str] = None) -> dict:
        """Preview the USDT cost / fee / rate for a hypothetical ARS payment.

        Surfaces ``valid_cbu_alias`` so a bad alias/CBU is caught before any
        order is created. Currencies are forced to ARS/USDT by Ankara.
        """
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
        return self._with_auth(lambda a: self.client.tentative_amount(a, body))

    def transactions(self) -> Any:
        return self._with_auth(lambda a: self.client.my_transactions(a))

    def transaction(self, tx_id: str) -> dict:
        if not tx_id or not str(tx_id).strip():
            raise ValueError("transaction id is required")
        return self._with_auth(lambda a: self.client.get_transaction(a, str(tx_id).strip()))

    def spending_limit(self) -> dict:
        return self._with_auth(lambda a: self.client.spending_limit(a))

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

        created = self._with_auth(lambda a: self.client.create_tentative(a, body))
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
            funding = self._with_auth(lambda a: self.client.issue_funding(a, tentative_id))
        except Exception as e:
            order.last_error = f"Funding not issued: {e}"
            self.storage.save_wapupay_order(order)
            result = order.to_dict()
            result["funded"] = False
            # If the failure purged the session (persistent 401 / failed refresh),
            # an immediate fund_order would just fail with "not logged in" — tell
            # the user to re-authenticate first.
            if self.storage.load_wapupay_session() is None:
                result["next_step"] = (
                    "Order created, but the WapuPay session expired before funding. "
                    "Log in again (wapupay_login then wapupay_verify), then call "
                    f"wapupay_fund_order with tentative_id={tentative_id}."
                )
            else:
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
        funding = self._with_auth(lambda a: self.client.issue_funding(a, tentative_id))

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

        order = self.storage.load_wapupay_order(tentative_id)
        warning = None
        try:
            latest = self._with_auth(lambda a: self.client.get_tentative(a, tentative_id))
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
