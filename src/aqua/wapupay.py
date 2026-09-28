"""WapuPay direct-fiat payments (Argentine ARS payouts funded on Liquid).

A WapuPay **direct-fiat** order lets a user pay an Argentine bank account
(by alias / CBU / CVU) in **ARS**, funding the payout with **USDT or L-BTC on Liquid**.
WapuPay's API is called **directly** (``https://be-prod.wapu.app`` by default;
override with ``WAPUPAY_BASE_URL`` for staging, e.g. ``be-stage.wapu.app``).
Each call carries WapuPay's own ``X-API-Key`` (read lazily from the
``WAPUPAY_API_KEY`` env var); the payout is funded from a Liquid address,
with either USDT (default) or L-BTC as the funding rail.
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
                     →  pay the returned Liquid address (lw_send_asset)
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
from .assets import LBTC_ASSET_ID, USDT_LIQUID_ASSET_ID

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

# Funding rail: the payout is always settled from a Liquid address, but the
# caller may fund it with either USDT or L-BTC. Both are sent explicitly on the
# wire. WapuPay returns the sat figures ONLY for the L-BTC rail (USDT is priced
# purely in USDT terms), so downstream code branches on funding_currency when
# telling the user exactly what to send. The send amount is total_amount_sats;
# funding_amount_sat is the pre-fee payout and is kept for the record only.
FUNDING_METHOD_USDT = "USDT"
FUNDING_METHOD_LBTC = "LBTC"
FUNDING_METHODS = (FUNDING_METHOD_USDT, FUNDING_METHOD_LBTC)
FUNDING_NETWORK_LIQUID = "LIQUID"

# A known Liquid policy asset pins the rail. Used ONLY when WapuPay omits
# funding_currency (thin cross-device records / legacy files): asset_id is the
# field lw_send_asset actually spends by, so it is the one unambiguous rail
# signal in a funding response. An unknown asset stays un-inferred — the
# denomination branches must then refuse to name a send amount.
_RAIL_BY_ASSET_ID = {
    LBTC_ASSET_ID: FUNDING_METHOD_LBTC,
    USDT_LIQUID_ASSET_ID: FUNDING_METHOD_USDT,
}
_ASSET_ID_BY_RAIL = {rail: asset for asset, rail in _RAIL_BY_ASSET_ID.items()}

# Fiat side is always Argentine pesos
CURRENCY_PAYMENT_ARS = "ARS"
CURRENCY_TAKEN_USDT = "USDT"

# WapuPay direct-fiat transfer types the user can choose between.
TRANSFER_TYPES = ("fiat_transfer", "fast_fiat_transfer")

# Canonical user-facing explanation of what WapuPay is. Single source of truth:
# reused by the MCP resource (aqua://docs/wapupay) and the CLI `wapupay about`
# command so the agent can answer "what is WapuPay? / what can I do with it?"
# with consistent wording across surfaces.
WAPUPAY_ABOUT = """\
# What is WapuPay?

WapuPay lets you pay an Argentine bank account in **pesos (ARS)**, funding the
payout with **USDT or L-BTC on the Liquid network**.

**It is NOT an exchange.** WapuPay is an *automated peer-to-peer (P2P) platform*:
it finds a trusted P2P payer who settles the payment in Argentine pesos on your
behalf — think of it as an "Uber for P2P". You send USDT or L-BTC on Liquid; a matched
payer pushes the pesos to the recipient's bank account.

WapuPay operates as an escrow:
Wapu can hold USDT during a transaction to ensure the exchange proceeds safely for
both parties, preventing assets from being lost in the process.

## What you can do

- Check the USDT/ARS exchange rate (`wapupay_exchange_rates` / `aqua wapupay rates`).
- Preview the cost of a payment without committing (`wapupay_quote` / `aqua wapupay quote`).
- Create an order and get a Liquid funding address (`wapupay_create_order` /
  `aqua wapupay create-order`); pay that address and WapuPay orchestrates the operation with a P2P payer that settles the ARS.
- Track your orders/transactions and check your monthly spending limit.
- Where can I send money? To a bank account, alias, CBU, CVU, MercadoPago, Wapu ID, or USDT address, depending on operational availability.
- What countries does it work in? WapuPay is designed for Argentina. Available methods, processing times, and fees may vary depending on the transaction.
- What is the spending limit? The spending limit varies by user and how long they have been operating on the platform — the more you transact, the higher your monthly limit grows. You can check your monthly limit with spending_limit. Currently, the starting limit for all new users is $1,000 USD per month.

## Transfer speed

- **fast_fiat_transfer** (default, higher fee): prioritized; completes in ~10 minutes
  to 1 hour during daytime. Not instant.
- **fiat_transfer** (standard, lower fee): takes 3 to 12 hours. Best when there is no
  rush, or when paying at night or on weekends — a payer picks up the transaction the
  next day anyway.

## After funding

After paying the Liquid funding address, WapuPay orchestrates the operation with a P2P payer that settles the ARS.
Check the status of the order often with order-status and take the executed_transaction_id to use it with `transaction --id`,
the executed_transaction contain the details of the fiat transfer and the fiat transfer receipt.

## What happens if the order fails?

If the order fails, you will receive the funds back to the Liquid address that you provided in the field refund_address after 24 hours.
If you need support, you can contact WapuPay support at wapupay.com
"""

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
    ``total_amount_sats`` (real bitcoin satoshis) so the two are never
    conflated.
    """
    try:
        d = Decimal(str(amount_usdt))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"Invalid USDT amount: {amount_usdt!r}") from e
    if not d.is_finite():
        raise ValueError(f"USDT amount must be a finite number, got {amount_usdt!r}")
    if d <= 0:
        raise ValueError(f"USDT amount must be positive, got {amount_usdt!r}")
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

_TENTATIVE_RESP_FIELDS = (
    "status",
    "funding_currency",
    "funding_network",
    "exchange_rate",
    "fee_amount_usdt",
    "funding_amount_usdt",
    "funding_amount_sat",
    "total_amount_sats",
    "total_amount_usdt",
    "address_destination",
    "asset_id",
    "expires_at",
    "refund_address",
    "funding_transaction_id",
    "executed_transaction_id",
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
            "WapuPay refunds on Liquid mainnet — use an lq1…/ex1… address."
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
    # RECORD ONLY — never the send amount. WapuPay returns funding_amount_sat as
    # the pre-fee payout, mirroring funding_amount_usdt; the amount to send is
    # total_amount_sats (mirroring total_amount_usdt). The two sat figures are
    # equal today, so reading this one would happen to work — until it doesn't.
    funding_amount_sat: Optional[int] = None
    # The exact L-BTC satoshis to send, fee included. Authoritative on the L-BTC rail.
    total_amount_sats: Optional[int] = None
    total_amount_usdt: Optional[Decimal] = None
    # Integer USDT amount (precision-8) to send on Liquid; derived from total_amount_usdt.
    total_funding_amount_base_units: Optional[int] = None
    address_destination: Optional[str] = None
    asset_id: Optional[str] = None
    expires_at: Optional[str] = None
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

    @property
    def is_lbtc(self) -> bool:
        """True when the payout is funded with L-BTC rather than USDT.

        Case-insensitive: this drives every money-denomination branch, and
        ``funding_currency`` can arrive either from the caller's request or from
        WapuPay's echo.
        """
        return (self.funding_currency or "").upper() == FUNDING_METHOD_LBTC

    def _derive_base_units(self, *, only_if_missing: bool = False) -> None:
        """Set total_funding_amount_base_units from total_amount_usdt: the exact
        integer USDT amount (precision-8) to send on the USDT rail.

        USDT-only. On the L-BTC rail the amount to send is total_amount_sats;
        this USDT-scale figure must NEVER be advertised alongside the L-BTC
        asset_id (a consumer pairing the two would send ~10^8x too much), so it
        is cleared rather than derived."""

        if self.is_lbtc:
            self.total_funding_amount_base_units = None
            return
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
        # Back-fill a missing rail from a known asset_id BEFORE the legacy
        # scrub below: a currency-less record with the L-BTC asset would
        # otherwise be treated as legacy USDT and lose its real sat amount.
        if not (data.get("funding_currency") or "").strip():
            inferred = _RAIL_BY_ASSET_ID.get(data.get("asset_id") or "")
            if inferred:
                data["funding_currency"] = inferred
        # Drop stale sat amounts from legacy records: the USDT-on-Liquid rail
        # never has real sats. The L-BTC-on-Liquid rail DOES (total_amount_sats
        # is the real amount to send), so it must survive a reload — key off
        # funding_currency, not just the network, to tell them apart. A
        # non-Liquid rail (e.g. Lightning) already short-circuits here.
        network = (data.get("funding_network") or "").upper()
        currency = (data.get("funding_currency") or "").upper()
        if network in ("", FUNDING_NETWORK_LIQUID) and currency in ("", FUNDING_METHOD_USDT):
            data.pop("funding_amount_sat", None)
            data.pop("total_amount_sats", None)
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
        for field in _TENTATIVE_RESP_FIELDS:
            if resp.get(field) is not None:
                value = resp[field]
                # Money/rate stays Decimal internally — coerce at the wire seam.
                if field in _MONEY_FIELDS:
                    value = _to_decimal(value)
                setattr(self, field, value)
        # A response that omits funding_currency (thin cross-device records)
        # must not default to USDT semantics: infer the rail from a known
        # asset_id before deriving any denomination-dependent amount.
        if not self.funding_currency:
            inferred = _RAIL_BY_ASSET_ID.get(self.asset_id or "")
            if inferred:
                self.funding_currency = inferred
        # Always recalculate integer USDT base units (precision-8) for Liquid from
        # total_amount_usdt to avoid stale values; distinct from funding_amount_sat (BTC).

        self._derive_base_units()
        # Sats are integers end-to-end (see CLAUDE.md invariant 1). total_amount_sats
        # is the L-BTC send amount, so anything but a positive whole number is a
        # contract violation, not something to coerce: rounding a fraction would
        # underpay, and a zero/negative/string value has no payable meaning. The
        # USDT rail already rejects non-positive totals (usdt_to_base_units) —
        # this keeps the L-BTC boundary equally strict.
        if self.total_amount_sats is not None:
            value = self.total_amount_sats
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(
                    f"WapuPay returned an invalid total_amount_sats: "
                    f"{self.total_amount_sats!r} (satoshis must be a positive "
                    f"whole number)"
                )
            self.total_amount_sats = value
        # funding_amount_sat is record-only; keep it an int for a clean round-trip.
        if isinstance(self.funding_amount_sat, float):
            self.funding_amount_sat = int(self.funding_amount_sat)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class WapuPayClient:
    """HTTP client for WapuPay's API.

    Uses ``_api_request`` for all network calls, always authenticating with
    ``X-API-Key`` (not Bearer). Raises ValueError on upstream errors with a
    clear message.
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
        """POST …/tentatives/{uuid}/funding — issue Liquid funding instructions."""
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
        quoted = urllib.parse.quote(str(tx_id), safe="")
        return self._proxy("GET", f"transactions/{quoted}", api_key=api_key) or {}

    def spending_limit(self, *, api_key: str) -> dict:
        """GET users/spending_limit — monthly KYC limit (USDT)."""
        return self._proxy("GET", "users/spending_limit", api_key=api_key) or {}


# ---------------------------------------------------------------------------
# High-level manager
# ---------------------------------------------------------------------------


class WapuPayManager:
    """Wallet-aware orchestration of WapuPay direct-fiat orders.

    Order persistence goes through ``Storage``; the wallet manager resolves a
    Liquid refund address when one isn't supplied. JAN3-account auth (login /
    session) and the WapuPay-key provisioning call are delegated to the injected
    ``jan3_accounts.Jan3AccountsManager`` (``self.jan3``).
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

    def provision_account(self, email: str) -> dict:
        """Provision a WapuPay API key via the AQUA backend and store it locally.

        Requires a prior JAN3 login for ``email`` (either flow — ``jan3_login`` →
        ``jan3_verify`` or ``jan3_login_start`` → ``jan3_login_complete``): the
        call is authorized with that account's JWT. The returned key is persisted
        to ``~/.aqua/wapupay/api_key.json`` (0o600) so every ``wapupay_*`` tool
        can use it without an env var — the raw key is never returned.

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
        token = self.jan3.provision_wapupay_token(email)
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
            "environment, or run wapupay_provision_account (after jan3_login) to "
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
        funding_method: str = FUNDING_METHOD_USDT,
    ) -> dict:
        """Create a direct-fiat order and issue Liquid funding instructions.

        Two upstream steps, run back-to-back: create-tentative (freezes the
        quote) then issue-funding (returns the Liquid address). The local order
        is persisted the instant create succeeds — **before** funding — so a
        funding failure leaves a recoverable ``CREATED`` order rather than an
        orphan. On funding failure we return the order flagged ``funded=False``
        with the error (no silent fake-success); recover with ``fund_order``.

        ``funding_method`` selects the rail used to fund the payout — ``"USDT"``
        (default) or ``"LBTC"`` — both settle from a Liquid address. WapuPay
        returns ``total_amount_sats`` (real sats to send) for the L-BTC rail;
        for USDT the amount to send is ``total_funding_amount_base_units``.

        Returns the order record including ``address_destination`` (Liquid),
        ``asset_id``, ``funding_amount_usdt`` / ``total_amount_usdt`` and
        ``expires_at`` — plus ``total_funding_amount_base_units`` (USDT
        rail) or ``total_amount_sats`` (L-BTC rail). The caller pays the amount
        named in ``pay_instructions`` with ``lw_send_asset`` — this method never
        broadcasts.
        """
        # Read the API key up front — before any network call or persistence —
        # so a missing key fails fast and never leaves a half-created order.
        key = self._require_api_key()
        self._validate_type(transfer_type)
        self._validate_funding_method(funding_method)
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
            "funding_method": funding_method,
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
            # The REQUESTED rail is authoritative and is recorded before any
            # response is merged. funding_currency drives every money-denomination
            # branch, so leaving it to WapuPay's optional echo would let a single
            # missing key re-denominate an L-BTC order in USDT terms.
            funding_currency=funding_method,
            funding_network=FUNDING_NETWORK_LIQUID,
        )
        order.apply_tentative(created)
        self._assert_rail(order, funding_method, funded=False)
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

        try:
            order.apply_tentative(funding)
        except ValueError as e:
            self._annotate_rejected_response(tentative_id, e)
            raise
        # Re-check after the SECOND merge: the funding response overwrites
        # funding_currency, so a rail that flips here would re-derive the other
        # rail's amounts while asset_id still points at the first one.
        self._assert_rail(order, funding_method, funded=True)
        order.last_error = None
        self.storage.save_wapupay_order(order)
        return self._funded_result(order)

    def _assert_rail(self, order: "WapuPayOrder", funding_method: str, *, funded: bool) -> None:
        """Refuse to continue if WapuPay's echo contradicts the expected rail.

        The rail selects the denomination of the amount the user is told to send
        (sats vs USDT base units) while ``asset_id`` selects the asset
        ``lw_send_asset`` actually spends. Both are checked: a flipped
        ``funding_currency`` re-denominates the amount (~10^8x overpay), and a
        flipped ``asset_id`` sends the right figure in the wrong asset. Either
        way this raises rather than re-deriving (CLAUDE.md invariant 5 — no
        silent fallback).
        """
        detail = None
        rail_flipped = False
        echoed = (order.funding_currency or "").upper()
        if echoed and echoed != funding_method:
            rail_flipped = True
            detail = (
                f"WapuPay echoed funding_currency={order.funding_currency!r} for a "
                f"funding_method={funding_method!r} order; refusing to continue. "
                f"The tentative exists upstream as {order.tentative_id}"
            )
        else:
            # Both rails settle in a Liquid policy asset whose id is a global
            # constant, so any other asset_id is an upstream contract violation.
            expected_asset = _ASSET_ID_BY_RAIL.get(funding_method)
            if expected_asset and order.asset_id and order.asset_id != expected_asset:
                detail = (
                    f"WapuPay returned asset_id={order.asset_id!r} for a "
                    f"funding_method={funding_method!r} order (expected "
                    f"{expected_asset}); refusing to continue. "
                    f"The tentative exists upstream as {order.tentative_id}"
                )
        if detail is None:
            return
        if funded:
            # Already persisted: record why it stalled so the local record isn't
            # a silent orphan, then refuse to hand back pay_instructions.
            order.last_error = detail
            if rail_flipped:
                # Restore the REQUESTED rail before saving. Clearing the derived
                # amount here would not stick — from_dict re-derives it on every
                # load — so the record must keep the requested denomination
                # instead of the flipped one. (asset_id keeps the echoed value;
                # last_error marks the record as not safe to pay.)
                order.funding_currency = funding_method
                order._derive_base_units()
            self.storage.save_wapupay_order(order)
            raise ValueError(f"{detail}; funding was issued but is NOT safe to pay.")
        raise ValueError(f"{detail} and will expire on its own; it was NOT funded.")

    def _assert_known_rail(self, order: "WapuPayOrder", expected_rail: str) -> None:
        """Run ``_assert_rail`` against the best-known rail after a re-merge.

        ``expected_rail`` is the rail stored BEFORE the merge (empty for thin
        records) — comparing against it catches a flip on the re-issue / poll
        paths. Without a stored rail, the merged/inferred one is used so the
        asset-consistency half of the check still runs. No rail at all (thin
        record, unknown asset): nothing to assert — ``_funded_result`` already
        refuses to name a send amount for an unknown rail.
        """
        rail = expected_rail if expected_rail in FUNDING_METHODS else (
            order.funding_currency or ""
        ).upper()
        if rail in FUNDING_METHODS:
            self._assert_rail(order, rail, funded=True)

    def _annotate_rejected_response(self, tentative_id: str, error: Exception) -> None:
        """Mark the persisted record with why a WapuPay response was rejected.

        Mirrors ``_assert_rail(funded=True)``: a raise after funding was issued
        must not leave the local record a silent orphan. The half-merged
        in-memory order is NOT saved — a rejected response must not leave its
        contract-violating values on disk — the clean stored record is
        annotated instead. No stored record (thin path): nothing to annotate.
        """
        stored = self.storage.load_wapupay_order(tentative_id)
        if stored is None:
            return
        stored.last_error = f"Funding response rejected: {error}"
        self.storage.save_wapupay_order(stored)

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
        # The stored rail is the one the user chose at create time; enforce it
        # against the re-issued echo the same way create_order does. Thin
        # records have no stored rail — the merged/inferred one still gets the
        # asset-consistency half of the check.
        expected_rail = (order.funding_currency or "").upper()
        try:
            order.apply_tentative(funding)
        except ValueError as e:
            self._annotate_rejected_response(tentative_id, e)
            raise
        self._assert_known_rail(order, expected_rail)
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
        latest = None
        # Only the NETWORK failure degrades to a warning (the last-known local
        # record is still useful). A response that violates the money contract
        # (rail flip, wrong asset, malformed amounts) must raise, not display.
        try:
            latest = self.client.get_tentative(tentative_id, api_key=key)
        except Exception as e:
            if order is None:
                raise
            warning = f"Could not refresh status: {e}"
        if latest is not None:
            if order is None:
                order = WapuPayOrder(
                    tentative_id=tentative_id,
                    status=latest.get("status", ""),
                    type=latest.get("type", ""),
                    amount_ars="",
                    alias="",
                    created_at=datetime.now(UTC).isoformat(),
                )
            expected_rail = (order.funding_currency or "").upper()
            order.apply_tentative(latest)
            self._assert_known_rail(order, expected_rail)
            order.last_checked_at = datetime.now(UTC).isoformat()
            self.storage.save_wapupay_order(order)

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
            except (OSError, ValueError, TypeError, InvalidOperation, json.JSONDecodeError):
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
    def _validate_funding_method(funding_method: str) -> None:
        if funding_method not in FUNDING_METHODS:
            raise ValueError(
                f"funding_method must be one of {FUNDING_METHODS}, got {funding_method!r}"
            )

    @staticmethod
    def _funded_result(order: "WapuPayOrder") -> dict:
        result = order.to_dict()
        result["funded"] = bool(order.address_destination)
        if not order.address_destination:
            return result

        expires_note = (
            f" Funding window: expires at {order.expires_at} UTC"
            f" (convert to the user's local timezone before displaying)."
            if order.expires_at
            else ""
        )
        payout_note = (
            f" WapuPay then pays {order.amount_ars} ARS to {order.alias}."
            if order.amount_ars and order.alias
            else " The ARS payout details (recipient and amount) are not "
            "stored locally for this order."
        )

        if order.is_lbtc and order.total_amount_sats is not None:
            # L-BTC rail: total_amount_sats is the fee-inclusive amount to send.
            # NOT funding_amount_sat — that is the pre-fee payout, the sat
            # analogue of funding_amount_usdt. Send sats, never USDT base units.
            result["pay_instructions"] = (
                f"Send exactly {order.total_amount_sats} sats of L-BTC on Liquid "
                f"to {order.address_destination} using lw_send_asset "
                f"(asset_id={order.asset_id}). This amount already includes "
                f"WapuPay's fee — send the full amount or WapuPay won't "
                f"settle.{payout_note}{expires_note}"
            )
        elif (
            (order.funding_currency or "").upper() == FUNDING_METHOD_USDT
            and order.total_funding_amount_base_units is not None
        ):
            fee_display = order.fee_amount_usdt if order.fee_amount_usdt is not None else 0
            result["pay_instructions"] = (
                f"Send exactly {order.total_amount_usdt} USDT "
                f"({order.total_funding_amount_base_units} base units) on Liquid "
                f"to {order.address_destination} using lw_send_asset "
                f"(asset_id={order.asset_id}). This total already includes "
                f"WapuPay's {fee_display} USDT fee — send the full "
                f"amount or WapuPay won't settle.{payout_note}{expires_note}"
            )
        elif (order.funding_currency or "").upper() in FUNDING_METHODS:
            # Thin record (e.g. order created on another device): the funding
            # response carries no total, so the exact amount isn't known locally.
            # Don't fabricate a "None" amount (No-lies rule) — point the user at
            # order-status to fetch the real total first. Name the field that is
            # DIRECTLY payable via lw_send_asset (integer sats / base units) per
            # rail: pointing an L-BTC payer at a USDT figure invites a ~10^8x
            # overpay, and pointing a USDT payer at the decimal total_amount_usdt
            # invites a ~10^8x underpay (lw_send_asset takes integer base units).
            missing = (
                "total_amount_sats" if order.is_lbtc
                else "total_funding_amount_base_units"
            )
            unit = "L-BTC satoshi" if order.is_lbtc else "integer USDT base-unit"
            result["pay_instructions"] = (
                f"Funding address ready ({order.address_destination}, "
                f"asset_id={order.asset_id}), but the exact {unit} amount to send "
                f"is not available locally yet. Call wapupay_order_status with "
                f"tentative_id={order.tentative_id} to fetch {missing}, "
                f"then pay that exact amount with lw_send_asset."
            )
        else:
            # Rail unknown: WapuPay omitted funding_currency and the asset_id is
            # not a known policy asset, so even the DENOMINATION of the amount
            # is unknown. Naming any figure here risks the ~10^8x sat/base-unit
            # mixup — refuse to instruct a send until a refresh supplies the rail.
            result["pay_instructions"] = (
                f"Funding address ready ({order.address_destination}), but the "
                f"funding rail (USDT vs L-BTC) and the exact amount to send are "
                f"not known locally. Call wapupay_order_status with "
                f"tentative_id={order.tentative_id} to fetch funding_currency "
                f"and the amount before paying anything."
            )
        return result
