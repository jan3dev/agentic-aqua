"""WapuPay direct-fiat payments (Argentine ARS payouts funded with Liquid USDT).

A WapuPay **direct-fiat** order lets a user pay an Argentine bank account
(by alias / CBU / CVU) in **ARS**, funding the payout with **USDT on Liquid**.
WapuPay's API is called **directly** (``https://be-prod.wapu.app`` by default;
override with ``WAPUPAY_BASE_URL`` for staging, e.g. ``be-stage.wapu.app``).
Each call carries WapuPay's own ``X-API-Key`` (read lazily from the
``WAPUPAY_API_KEY`` env var); the funding rail is pinned to Liquid USDT.
WapuPay is the source of truth; we keep only a lightweight local order record
for CLI / MCP recovery and tracking.

Two independent auth surfaces (see CLAUDE.md):

    * **WapuPay API key** — every order/transaction call sends ``X-API-Key``.
      WapuPay treats ``X-API-Key`` and ``Authorization: Bearer`` as mutually
      exclusive (sending both → 400), so WapuPay calls send **only** the key
      and never a Bearer token. ``exchange_rates`` is a public endpoint and
      sends no auth header at all.
    * **AQUA account login** — ``login``/``verify`` are an *AQUA-account*
      email-OTP against Ankara (``{ANKARA}/api/v1/auth/{login,verify}/`` → JWT),
      surfaced as the ``aqua_*`` tools. This session is **decoupled** from the
      WapuPay calls above (they need ``WAPUPAY_API_KEY``, not a login).

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

from .ankara import (
    HTTP_TIMEOUT_SECONDS,
    USER_AGENT,
    _extract_error_message,
    _mask,
    _redact,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint configuration
# ---------------------------------------------------------------------------

# WapuPay's own API host — called directly. Override for
# staging / local development with WAPUPAY_BASE_URL (e.g. be-stage.wapu.app).
WAPUPAY_BASE_URL = os.environ.get(
    "WAPUPAY_BASE_URL", "https://be-prod.wapu.app"
).rstrip("/")

# WapuPay API key, read lazily per call (see WapuPayManager._require_api_key)
WAPUPAY_API_KEY_ENV = "WAPUPAY_API_KEY"

# Fixed funding rail (v1: Liquid USDT only). So we send them explicitly and never offer a choice.
FUNDING_METHOD_USDT = "USDT"
FUNDING_NETWORK_LIQUID = "LIQUID"

# Fiat side is always Argentine pesos
CURRENCY_PAYMENT_ARS = "ARS"
CURRENCY_TAKEN_USDT = "USDT"

# WapuPay direct-fiat transfer types the user can choose between.
TRANSFER_TYPES = ("fiat_transfer", "fast_fiat_transfer")

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


def _normalize_ars_amount(amount_ars: str | int | float | Decimal) -> Decimal:
    """Validate ``amount_ars`` as a positive, whole-peso fiat Decimal.

    ARS is fiat, not satoshis — kept as a Decimal at the wire boundary, never a
    float internally. WapuPay direct-fiat transfers are whole pesos
    (``min_payment_amount_ars`` is 10000), so a non-integral amount is rejected.
    Raises ``ValueError`` on non-positive / non-integral / unparseable input.
    """
    try:
        d = Decimal(str(amount_ars))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"Invalid amount_ars: {amount_ars!r}") from e
    # Reject non-finite amounts (NaN or Infinity) before validating.
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


def usdt_to_base_units(amount_usdt: str | int | float | Decimal) -> int:
    """Convert a USDT-on-Liquid decimal amount to integer base units.

    L-USDt has precision 8 (8 decimal places), so 1 USDT = 100_000_000 base
    units — the same scale as L-BTC satoshis, but these are USDT units, not
    bitcoin sats. Kept deliberately distinct from WapuPay's wire
    ``funding_amount_sat`` (real BTC/Lightning satoshis) so the two are never
    conflated.
    """
    d = Decimal(str(amount_usdt))
    units = (d * Decimal(100_000_000)).quantize(Decimal("1."), rounding=ROUND_HALF_UP)
    return int(units)


def _to_decimal(value: str | int | float | Decimal) -> Decimal:
    """Convert a numeric value to Decimal, avoiding float drift."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


# Money/rate fields are Decimals in memory and serialized as strings.
_MONEY_FIELDS = (
    "exchange_rate",
    "fee_amount_usdt",
    "funding_amount_usdt",
    "total_amount_usdt",
)


# A WapuPay tentative id is a canonical UUID.
_TENTATIVE_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _validate_tentative_id(tentative_id: str) -> str:
    """Validate and normalize a tentative id, ensuring it is a UUID before building a URL."""
    tid = (tentative_id or "").strip()
    if not tid:
        raise ValueError("tentative_id is required")
    if not _TENTATIVE_ID_RE.fullmatch(tid):
        raise ValueError(f"Invalid tentative_id (expected a UUID): {tentative_id!r}")
    return tid


def validate_liquid_refund_address(address: str) -> str:
    """Validate and normalize a Liquid mainnet refund address for USDT refunds.

    Accepts all valid mainnet address formats (confidential, unconfidential, and legacy).
    Ensures the address is Liquid mainnet by parsing and checking its network.
    """
    import lwk

    addr = (address or "").strip()
    try:
        parsed = lwk.Address(addr)
    except Exception as e:
        raise ValueError(
            f"Invalid Liquid refund_address {address!r}: not a valid Liquid address."
        ) from e
    if not parsed.network().is_mainnet():
        raise ValueError(
            f"refund_address {address!r} is not a Liquid mainnet address. "
            "WapuPay refunds USDT on Liquid mainnet — use an lq1…/ex1… address."
        )
    return addr


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class WapuPayApiKey:
    """Locally-persisted WapuPay API key provisioned via the AQUA backend.

    Stored 0o600 — it authorizes WapuPay calls directly and is never
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
    # USDT amounts / exchange rate: Decimal in memory, serialized as strings.
    exchange_rate: Optional[Decimal] = None
    fee_amount_usdt: Optional[Decimal] = None
    funding_amount_usdt: Optional[Decimal] = None
    # Amount in satoshis for Lightning/BTC funding only; None otherwise.
    funding_amount_sat: Optional[int] = None
    total_amount_usdt: Optional[Decimal] = None
    # Integer USDT amount (precision-8) to send on Liquid; derived from total_amount_usdt.
    total_funding_amount_base_units: Optional[int] = None
    address_destination: Optional[str] = None
    asset_id: Optional[str] = None
    expires_at: Optional[str] = None
    funding_expires_at: Optional[str] = None
    refund_address: Optional[str] = None
    funding_transaction_id: Optional[str] = None
    executed_transaction_id: Optional[str] = None
    wallet_name: Optional[str] = None
    last_checked_at: Optional[str] = None
    last_error: Optional[str] = None

    def __post_init__(self) -> None:
        # Ensure money/rate fields are always Decimals internally.
        for fld in _MONEY_FIELDS:
            value = getattr(self, fld)
            if value is not None and not isinstance(value, Decimal):
                setattr(self, fld, _to_decimal(value))

    def _derive_base_units(self, *, only_if_missing: bool = False) -> None:
        """Set total_funding_amount_base_units from total_amount_usdt,
        unless it is already set (if only_if_missing is True).
        Ensures the correct integer USDT amount (precision-8) for Liquid funding."""
   
        if only_if_missing and self.total_funding_amount_base_units is not None:
            return
        if self.total_amount_usdt is not None:
            self.total_funding_amount_base_units = usdt_to_base_units(self.total_amount_usdt)

    def to_dict(self) -> dict:
        data = asdict(self) 
        for fld in _MONEY_FIELDS:
            if data.get(fld) is not None:
                data[fld] = str(data[fld])
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "WapuPayOrder":
        data = dict(data)
        # This cleans up data from older records predating the rename.
        network = (data.get("funding_network") or "").upper()
        if network in ("", FUNDING_NETWORK_LIQUID):
            data.pop("funding_amount_sat", None)
        known = {f.name for f in fields(cls)}
        # __post_init__ coerces money to Decimal; back-fill the send amount for
        # legacy records that predate total_funding_amount_base_units.
        obj = cls(**{k: v for k, v in data.items() if k in known})
        obj._derive_base_units(only_if_missing=True)
        return obj

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
            "funding_transaction_id": "funding_transaction_id",
            "executed_transaction_id": "executed_transaction_id",
        }
        for attr, key in mapping.items():
            if key in resp and resp[key] is not None:
                value = resp[key]
                # Money/rate stays Decimal internally — coerce at the wire seam.
                if attr in _MONEY_FIELDS:
                    value = _to_decimal(value)
                setattr(self, attr, value)
        # Always recalculate integer USDT base units (precision-8) for Liquid from 
        # total_amount_usdt to avoid stale values; distinct from funding_amount_sat (BTC).
 
        self._derive_base_units()
        # Ensure funding_amount_sat remains an integer per WapuPay spec.
        if isinstance(self.funding_amount_sat, float):
            self.funding_amount_sat = int(self.funding_amount_sat)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class WapuPayClient:
    """HTTP client for WapuPay's API.

    Uses ``_api_request`` for all network calls, always authenticating with ``X-API-Key`` (not Bearer).
    Raises ValueError on upstream errors with a clear message.
    """

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or WAPUPAY_BASE_URL).rstrip("/")

    @staticmethod
    def _api_key_headers(api_key: str) -> dict[str, str]:
        """Build WapuPay's API-key auth header.

        WapuPay rejects a request that carries both ``X-API-Key`` and a Bearer
        token, so this is the *only* auth header WapuPay calls ever send.
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
    """Wallet-aware orchestration of WapuPay direct-fiat orders.

    Order persistence goes through ``Storage``; the wallet manager resolves a
    Liquid refund address when one isn't supplied. AQUA-account auth (login /
    session) and the WapuPay-key provisioning call are delegated to the injected
    ``ankara.JAN3AccountManager`` (``self.jan3``).
    """

    def __init__(self, storage, wallet_manager, jan3_manager) -> None:
        self.storage = storage
        self.wallet_manager = wallet_manager
        self.jan3 = jan3_manager
        self._client: Optional[WapuPayClient] = None

    @property
    def client(self) -> WapuPayClient:
        if self._client is None:
            self._client = WapuPayClient()
        return self._client

    # -- Account provisioning ------------------------------------------------

    def provision_account(self) -> dict:
        """Provision a WapuPay API key via the AQUA backend and store it locally.

        Requires a prior AQUA login (``aqua_login`` → ``aqua_verify``): the call
        is authorized with that JWT. The returned key is persisted to
        ``~/.aqua/wapupay/api_key.json`` (0o600) so every ``wapupay_*``
        tool can use it without an env var — the raw key is never returned.

        The AQUA backend issues a fresh key on EVERY call and invalidates any key
        previously issued for the account (no grace period).
        """
        source, key = self._resolve_api_key()
        if source == "env":
            return {
                "already_configured": True,
                "source": "env",
                "key_preview": _mask(key),
                "message": (
                    f"A WapuPay API key is already set via {WAPUPAY_API_KEY_ENV} "
                    "env var (it takes precedence) — nothing to do."
                ),
            }
        if source == "stored":
            stored = self.storage.load_wapupay_api_key()
            return {
                "already_configured": True,
                "source": "stored",
                "key_preview": _mask(key),
                "created_at": stored.created_at if stored else None,
                "message": (
                    "A WapuPay API key is already provisioned and stored — all "
                    "WapuPay tools are ready."
                ),
            }

        # Session lookup + the authenticated AQUA-backend call live in the JAN3
        # account manager; WapuPay only resolves/stores the resulting key.
        token = self.jan3.provision_wapupay_token()
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

    def _resolve_api_key(self) -> tuple[Optional[str], Optional[str]]:
        """Resolve the WapuPay API key — the single source of truth for the order.

        Read lazily on every WapuPay call (so a key change takes effect without
        a restart and tests can monkeypatch it). Returns ``(source, key)``:

        1. ``("env", key)`` — ``WAPUPAY_API_KEY`` env var is set; explicit config wins.
        2. ``("stored", key)`` — the key provisioned via ``wapupay_provision_account``
           and persisted under ``~/.aqua/wapupay/api_key.json``.
        3. ``(None, None)`` — nothing configured.
        """
        env_key = os.environ.get(WAPUPAY_API_KEY_ENV)
        if env_key:
            return "env", env_key
        record = self.storage.load_wapupay_api_key()
        if record and record.token:
            return "stored", record.token
        return None, None

    def _require_api_key(self) -> str:
        """Return WapuPay's API key, or raise.

        WapuPay endpoints are authorized by this key (env var first,
        then the stored provisioned key — see ``_resolve_api_key``), not by the
        AQUA login session; a 401 from WapuPay means the key is missing or
        invalid. No silent fallback (CLAUDE.md "No lies rules").
        """
        _source, key = self._resolve_api_key()
        if key:
            return key
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
        ``asset_id`` (USDT), ``funding_amount_usdt`` / ``total_amount_usdt`` /
        ``total_funding_amount_base_units`` and ``funding_expires_at``. The
        caller pays the TOTAL with ``lw_send_asset`` — this method never
        broadcasts.
        """
        # Read the API key up front — before any network call or persistence —
        # so a missing key fails fast and never leaves a half-created order.
        key = self._require_api_key()
        self._validate_type(transfer_type)
        if not alias or not alias.strip():
            raise ValueError("alias (recipient bank alias / CBU / CVU) is required")
        refund = (
            validate_liquid_refund_address(refund_address)
            if refund_address and refund_address.strip()
            else None
        )
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
        if refund:
            body["refund_address"] = refund

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
        # Validate the id with the SAME UUID rule fund_order / order_status use,
        # so we never persist an order that is later un-pollable / un-fundable
        # (storage's looser SWAP_ID_PATTERN would otherwise accept a non-UUID).
        tentative_id = _validate_tentative_id(tentative_id)

        order = WapuPayOrder(
            tentative_id=tentative_id,
            status=created.get("status", "CREATED"),
            type=transfer_type,
            amount_ars=str(d),
            alias=alias.strip(),
            created_at=datetime.now(UTC).isoformat(),
            receiver_name=(receiver_name.strip() if receiver_name else None),
            refund_address=refund,
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
        if order.address_destination and order.total_funding_amount_base_units is not None:
            fee_display = order.fee_amount_usdt if order.fee_amount_usdt is not None else 0
            result["pay_instructions"] = (
                f"Send exactly {order.total_amount_usdt} USDT "
                f"({order.total_funding_amount_base_units} base units) on Liquid "
                f"to {order.address_destination} using lw_send_asset "
                f"(asset_id={order.asset_id}). This total already includes "
                f"WapuPay's {fee_display} USDT fee — send the full "
                f"amount or WapuPay won't settle. WapuPay then pays "
                f"{order.amount_ars} ARS to {order.alias}."
            )
        elif order.address_destination:
            # Thin record (e.g. order created on another device): the funding
            # response carries no total_amount_usdt, so the exact total isn't
            # known locally. Don't fabricate a "None" amount (No-lies rule) —
            # point the user at order-status to fetch the real total first.
            result["pay_instructions"] = (
                f"Funding address ready ({order.address_destination}, "
                f"asset_id={order.asset_id}), but the exact USDT total to send "
                f"is not available locally yet. Call wapupay_order_status with "
                f"tentative_id={order.tentative_id} to fetch total_amount_usdt, "
                f"then pay that exact amount with lw_send_asset."
            )
        return result
