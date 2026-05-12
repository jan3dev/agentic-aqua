"""Changelly integration for USDt cross-chain swaps via AQUA's backend proxy.

Talks to Changelly through AQUA's Ankara proxy at
`https://ankara.aquabtc.com/api/v1/changelly` so we don't have to manage a
Changelly API secret on the user's machine.

Scope (mirrors what AQUA Flutter exposes through Changelly): **USDt-Liquid
↔ USDt on the same 6 external chains we allow in SideShift** (Ethereum, Tron,
BSC, Solana, Polygon, TON). One leg of every swap MUST be `lusdt` (USDt-Liquid);
the other MUST be one of those 6 external USDt variants. L-BTC, BTC, and
arbitrary altcoins are intentionally excluded — for those use SideSwap or
SideShift. The override env var `CHANGELLY_ALLOW_ALL_PAIRS=1` bypasses the
allowlist for testing or power use.

Endpoints used (REST/JSON, base `https://ankara.aquabtc.com/api/v1/changelly`):

  GET  /currencies                 — list of supported currencies
  POST /pairs                      — available pairs (filterable by from/to)
  POST /get-fix-rate-for-amount    — fixed-rate quote (commit to a rate)
  POST /quote                      — variable-rate quote
  POST /create-fix-transaction     — create a fixed order from a quote
  POST /create-transaction         — create a variable order
  GET  /status/{orderId}           — poll order status

Asset id conventions (Changelly's own format, distinct from SideShift's):

  lusdt        — USDt on Liquid
  usdt20       — USDt on Ethereum (ERC-20)
  usdtrx       — USDt on Tron (TRC-20)
  usdtbsc      — USDt on BSC
  usdtsol      — USDt on Solana
  usdtpolygon  — USDt on Polygon
  usdton       — USDt on TON

Status state machine (lowercase): `new`, `waiting`, `confirming`, `exchanging`,
`sending`, `finished` (success), `failed`, `refunded`, `hold`, `overdue`,
`expired`. Helpers `swap_is_final`, `swap_is_success`, `swap_is_failed`
abstract over the terminal-state grouping.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from .assets import lookup_asset_by_ticker

logger = logging.getLogger(__name__)


# Endpoint configuration — defaults to AQUA's Ankara proxy. Override for
# testing or local development by setting CHANGELLY_BASE_URL.
CHANGELLY_BASE_URL = os.environ.get(
    "CHANGELLY_BASE_URL", "https://ankara.aquabtc.com/api/v1/changelly"
)
USER_AGENT = "agentic-aqua"
HTTP_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Asset identifiers + curated allowlist
# ---------------------------------------------------------------------------

# Changelly's identifier for USDt on the Liquid Network. One leg of every
# swap we support is this one.
LIQUID_USDT_ID = "lusdt"

# The 6 external USDt variants we expose. Mirrors the non-Liquid USDt subset
# of SideShift's allowlist (ethereum, tron, bsc, solana, polygon, ton).
EXTERNAL_USDT_IDS = frozenset({
    "usdt20",      # Ethereum (ERC-20)
    "usdtrx",      # Tron (TRC-20)
    "usdtbsc",     # BSC
    "usdtsol",     # Solana
    "usdtpolygon", # Polygon
    "usdton",      # TON
})

# Curated pair allowlist: one leg must be lusdt, the other must be in
# EXTERNAL_USDT_IDS. Mirrors AQUA Flutter's `ChangellyAssetIds` set in
# `lib/features/changelly/models/changelly_models.dart` (intersected with
# our SideShift allowlist for consistency between the two integrations).
ALLOWED_PAIRS: frozenset[tuple[str, str]] = frozenset(
    [(LIQUID_USDT_ID, ext) for ext in EXTERNAL_USDT_IDS]
    + [(ext, LIQUID_USDT_ID) for ext in EXTERNAL_USDT_IDS]
)

# User-facing network-name → Changelly asset_id mapping. Used by the MCP
# tools / CLI so users say "tron" rather than "usdtrx" — same vocabulary as
# the SideShift surface.
NETWORK_TO_USDT_ID = {
    "liquid": "lusdt",
    "ethereum": "usdt20",
    "tron": "usdtrx",
    "bsc": "usdtbsc",
    "solana": "usdtsol",
    "polygon": "usdtpolygon",
    "ton": "usdton",
}
USDT_ID_TO_NETWORK = {v: k for k, v in NETWORK_TO_USDT_ID.items()}


def _allow_all_pairs() -> bool:
    """Read the override env var. Re-read on every check so tests can mutate it.

    Accepts `1`, `true`, `yes` (case-insensitive) as truthy.
    """
    return os.environ.get("CHANGELLY_ALLOW_ALL_PAIRS", "").strip().lower() in {
        "1", "true", "yes",
    }


def _check_pair_allowed(from_id: str, to_id: str) -> None:
    """Raise ValueError if the (from, to) pair isn't on the allowlist.

    Both legs combined must form a curated pair: exactly one leg is `lusdt`,
    the other is one of the 6 external USDt variants. The override env var
    bypasses the check entirely.
    """
    if _allow_all_pairs():
        return
    pair = (from_id.lower(), to_id.lower())
    if pair not in ALLOWED_PAIRS:
        allowed = ", ".join(sorted(EXTERNAL_USDT_IDS))
        raise ValueError(
            f"Changelly pair ({from_id} → {to_id}) is not in the curated "
            f"allowlist. One leg must be {LIQUID_USDT_ID!r} (USDt-Liquid); "
            f"the other must be one of: {allowed}. Set "
            f"CHANGELLY_ALLOW_ALL_PAIRS=1 to bypass."
        )


# Per-network address format patterns. Used to catch wrong-network addresses
# before Changelly accepts the order (which could result in lost funds).
_ADDRESS_PATTERNS: dict[str, re.Pattern[str]] = {
    "tron":     re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
    "ethereum": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "bsc":      re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "polygon":  re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "solana":   re.compile(r"^[1-9A-HJ-NP-Za-km-z]{43,44}$"),
    "ton":      re.compile(r"^[EU][Qq][0-9A-Za-z_\-]{46}$"),
}


def _validate_settle_address(network: str, address: str) -> None:
    """Raise ValueError if `address` doesn't match the expected format for `network`.

    Prevents sending to a wrong-network address — Changelly may accept it and
    the funds would be unrecoverable.
    """
    if not address or not address.strip():
        raise ValueError("settle_address cannot be empty")
    norm = network.lower()
    pattern = _ADDRESS_PATTERNS.get(norm)
    if pattern is None:
        # Drift guard: if someone adds a network to NETWORK_TO_USDT_ID
        # without a matching pattern here, address validation silently
        # becomes a no-op. Refuse rather than ship a footgun.
        # "liquid" is intentionally absent — Liquid addresses are produced
        # by the local wallet, not user-pasted.
        if norm in NETWORK_TO_USDT_ID and norm != "liquid":
            raise RuntimeError(
                f"_ADDRESS_PATTERNS is missing an entry for supported network "
                f"{norm!r}. Add the pattern alongside NETWORK_TO_USDT_ID."
            )
        return
    if not pattern.match(address):
        raise ValueError(
            f"settle_address {address!r} doesn't look like a valid {network} address. "
            f"Double-check the address and network before sending."
        )


def network_to_asset_id(network: str) -> str:
    """Translate a user-facing network name to Changelly's asset id.

    Args:
        network: Lowercase network name (e.g. "tron", "liquid", "ethereum").

    Returns:
        The Changelly asset id (e.g. "usdtrx", "lusdt", "usdt20").

    Raises:
        ValueError if the network is not a USDt-supporting network we recognise.
    """
    norm = network.lower()
    if norm not in NETWORK_TO_USDT_ID:
        allowed = ", ".join(sorted(NETWORK_TO_USDT_ID))
        raise ValueError(
            f"Unknown network for Changelly USDt swap: {network!r}. "
            f"Supported: {allowed}."
        )
    return NETWORK_TO_USDT_ID[norm]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ChangellyQuote:
    """A fixed-rate quote from `/get-fix-rate-for-amount`.

    Note: amounts are decimal strings (e.g. "100.0") to preserve precision.
    `expired_at` is a Unix-epoch seconds timestamp.
    """

    quote_id: str
    from_asset: str
    to_asset: str
    amount_from: str
    amount_to: str
    network_fee: str
    min_from: str
    max_from: str
    expired_at: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChangellySwap:
    """Persistent record of a Changelly cross-chain swap (send or receive)."""

    order_id: str
    swap_type: str  # "fixed" | "variable"
    direction: str  # "send" (we sign deposit) | "receive" (we provide settle address)
    from_asset: str  # Changelly asset id (e.g. "lusdt", "usdtrx")
    to_asset: str
    settle_address: str  # `payoutAddress` in Changelly terms
    deposit_address: str  # `payinAddress` in Changelly terms
    refund_address: Optional[str]
    wallet_name: Optional[str]
    status: str
    created_at: str
    expires_at: Optional[str] = None  # ISO timestamp; from `payTill` for fixed orders
    amount_from: Optional[str] = None
    amount_to: Optional[str] = None
    network_fee: Optional[str] = None
    quote_id: Optional[str] = None  # fixed orders only
    deposit_hash: Optional[str] = None  # txid we broadcast (send flow)
    track_url: Optional[str] = None
    last_checked_at: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChangellySwap":
        data = {**data}
        for f in (
            "expires_at", "amount_from", "amount_to", "network_fee", "quote_id",
            "deposit_hash", "track_url", "last_checked_at", "last_error",
        ):
            data.setdefault(f, None)
        return cls(**data)


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


class ChangellyClient:
    """HTTP client for AQUA's Changelly proxy.

    No auth required — AQUA's backend handles the Changelly API secret. We
    just speak JSON.
    """

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or CHANGELLY_BASE_URL).rstrip("/")

    def _api_request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err_body = json.loads(e.read().decode())
                if isinstance(err_body, dict):
                    detail = (
                        err_body.get("error")
                        or err_body.get("message")
                        or str(err_body)
                    )
            except Exception:
                pass
            msg = f"Changelly API error ({e.code} {method} {path})"
            if detail:
                msg += f": {detail}"
            raise RuntimeError(msg) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Changelly API unreachable ({method} {path}): {e.reason}"
            ) from e

    # -- Endpoints -----------------------------------------------------------

    def get_currencies(self) -> list[str]:
        """List supported currencies. Returns Changelly asset ids (e.g. "lusdt", "btc")."""
        resp = self._api_request("GET", "/currencies")
        if isinstance(resp, dict):
            return resp.get("result", []) or []
        return resp or []

    def get_pairs(
        self,
        from_asset: Optional[str] = None,
        to_asset: Optional[str] = None,
    ) -> list[dict]:
        """Fetch available pairs. Optionally filter by from / to asset id.

        Returns a list of `{from, to}` dicts.
        """
        body: dict[str, Any] = {}
        if from_asset:
            body["from"] = from_asset.lower()
        if to_asset:
            body["to"] = to_asset.lower()
        resp = self._api_request("POST", "/pairs", body=body)
        return resp or []

    def get_fix_rate_for_amount(
        self,
        from_asset: str,
        to_asset: str,
        amount_from: Optional[str] = None,
        amount_to: Optional[str] = None,
    ) -> dict:
        """Fixed-rate quote (~5-30 minute TTL). Returns `{id, result, expiredAt, ...}`.

        Provide exactly one of `amount_from` or `amount_to`. Amounts are
        decimal strings to preserve precision.
        """
        if (amount_from is None) == (amount_to is None):
            raise ValueError("exactly one of amount_from or amount_to must be provided")
        body: dict[str, Any] = {
            "from": from_asset.lower(),
            "to": to_asset.lower(),
        }
        if amount_from is not None:
            body["amountFrom"] = amount_from
        if amount_to is not None:
            body["amountTo"] = amount_to
        return self._api_request("POST", "/get-fix-rate-for-amount", body=body) or {}

    def get_variable_quote(
        self,
        from_asset: str,
        to_asset: str,
        amount_from: str,
    ) -> dict:
        """Variable-rate quote. Returns the first quote from the response list."""
        body = {
            "from": from_asset.lower(),
            "to": to_asset.lower(),
            "amountFrom": amount_from,
        }
        resp = self._api_request("POST", "/quote", body=body)
        if isinstance(resp, list):
            if not resp:
                raise RuntimeError("Changelly /quote returned no quotes")
            return resp[0]
        return resp or {}

    def create_fixed_transaction(
        self,
        from_asset: str,
        to_asset: str,
        rate_id: str,
        address: str,
        refund_address: str,
        amount_from: Optional[str] = None,
        amount_to: Optional[str] = None,
    ) -> dict:
        """Create a fixed-rate order. Returns full order incl. `payinAddress`, `payTill`."""
        body: dict[str, Any] = {
            "from": from_asset.lower(),
            "to": to_asset.lower(),
            "rateId": rate_id,
            "address": address,
            "refundAddress": refund_address,
        }
        if amount_from is not None:
            body["amountFrom"] = amount_from
        if amount_to is not None:
            body["amountTo"] = amount_to
        return self._api_request("POST", "/create-fix-transaction", body=body) or {}

    def create_variable_transaction(
        self,
        from_asset: str,
        to_asset: str,
        address: str,
        refund_address: Optional[str] = None,
        amount_from: Optional[str] = None,
        amount_to: Optional[str] = None,
    ) -> dict:
        """Create a variable-rate order. Returns full order incl. `payinAddress`."""
        body: dict[str, Any] = {
            "from": from_asset.lower(),
            "to": to_asset.lower(),
            "address": address,
        }
        if refund_address:
            body["refundAddress"] = refund_address
        if amount_from is not None:
            body["amountFrom"] = amount_from
        if amount_to is not None:
            body["amountTo"] = amount_to
        return self._api_request("POST", "/create-transaction", body=body) or {}

    def get_status(self, order_id: str) -> str:
        """Poll order status. Returns the bare status string (e.g. 'finished')."""
        resp = self._api_request("GET", f"/status/{order_id}")
        if isinstance(resp, str):
            return resp
        if isinstance(resp, dict):
            status = resp.get("status") or resp.get("result")
            if not status:
                raise RuntimeError(f"Changelly status response missing status/result field: {resp!r}")
            return status
        raise RuntimeError(f"Unexpected Changelly status response type {type(resp).__name__}: {resp!r}")


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

# Lowercase status strings per Changelly's `ChangellyOrderStatus` enum.
_FINAL_STATUSES = {"finished", "failed", "refunded", "expired", "overdue", "hold"}
_SUCCESS_STATUSES = {"finished"}
_FAILED_STATUSES = {"failed", "refunded", "expired", "overdue"}


def swap_is_final(status: str) -> bool:
    return status.lower() in _FINAL_STATUSES


def swap_is_success(status: str) -> bool:
    return status.lower() in _SUCCESS_STATUSES


def swap_is_failed(status: str) -> bool:
    return status.lower() in _FAILED_STATUSES


def changelly_track_url(order_id: str) -> str:
    """Public Changelly tracking URL for the given order id."""
    return f"https://changelly.com/track/{order_id}"


# ---------------------------------------------------------------------------
# High-level manager
# ---------------------------------------------------------------------------


# Liquid USDt asset id (hex) — what `WalletManager.send` expects when sending
# a non-L-BTC Liquid asset. Sourced from the canonical MAINNET_ASSETS registry.
LIQUID_USDT_HEX = lookup_asset_by_ticker("USDt").asset_id  # type: ignore[union-attr]


class ChangellyManager:
    """Orchestrates Changelly send / receive flows tied to AQUA's wallet manager.

    Two flows mirror SideShift:

    1. **Send** (`send_swap`): user holds USDt-Liquid and wants USDt on
       another chain. We get a fixed-rate quote, create a fixed order, and
       broadcast the deposit from the Liquid wallet.

    2. **Receive** (`receive_swap`): user wants USDt-Liquid in. We create
       a variable-rate order with the wallet's Liquid address as the settle
       target; the external sender pays the returned deposit address from
       any USDt-supporting chain.

    Both flows always set a refund address (the wallet's own Liquid address
    on send; user-supplied external address on receive — strongly
    recommended). Without one, a stuck order requires manual intervention
    via the Changelly web UI.
    """

    def __init__(self, storage, wallet_manager) -> None:
        self.storage = storage
        self.wallet_manager = wallet_manager
        self._client: Optional[ChangellyClient] = None

    @property
    def client(self) -> ChangellyClient:
        if self._client is None:
            self._client = ChangellyClient()
        return self._client

    # -- Read-only helpers ---------------------------------------------------

    def list_currencies(self) -> list[str]:
        return self.client.get_currencies()

    def fixed_quote(
        self,
        from_asset: str,
        to_asset: str,
        amount_from: Optional[str] = None,
        amount_to: Optional[str] = None,
    ) -> dict:
        # Allowlist-check the pair even on quote so we fail fast in the UI.
        _check_pair_allowed(from_asset, to_asset)
        return self.client.get_fix_rate_for_amount(
            from_asset, to_asset,
            amount_from=amount_from, amount_to=amount_to,
        )

    # -- Send flow (USDt-Liquid → USDt-on-external-chain) --------------------

    def send_swap(
        self,
        external_network: str,
        amount_from: str,
        settle_address: str,
        wallet_name: str = "default",
        password: Optional[str] = None,
        rate_id: Optional[str] = None,
    ) -> "ChangellySwap":
        """Send USDt-Liquid out via a Changelly fixed-rate order.

        Args:
            external_network: target USDt network (e.g. "tron", "ethereum").
            amount_from: USDt-Liquid to send (decimal string, e.g. "100").
            settle_address: external chain address to receive at.
            wallet_name: local Liquid wallet to sign with.
            password: mnemonic decryption password (if encrypted at rest).
            rate_id: rate id from a prior changelly_quote call. If provided,
                skips the internal quote fetch and uses this rate directly,
                preventing rate drift between quote and execution.

        Returns a persisted `ChangellySwap` with the broadcast `deposit_hash`
        and Changelly's order id.
        """
        from_asset = LIQUID_USDT_ID
        to_asset = network_to_asset_id(external_network)
        # Validate the agreed pair and destination address before any HTTP work.
        _check_pair_allowed(from_asset, to_asset)
        _validate_settle_address(external_network, settle_address)

        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        if wallet_data.watch_only:
            raise ValueError("Watch-only wallet cannot sign a Changelly deposit")
        if wallet_data.encrypted_mnemonic and self.storage.is_mnemonic_encrypted(
            wallet_data.encrypted_mnemonic
        ):
            if not password:
                raise ValueError("Password required to decrypt mnemonic")

        # Refund address: wallet's own Liquid address (USDt-Liquid is on Liquid).
        refund_address = self.wallet_manager.get_address(wallet_name).address

        # Step 1 — fixed-rate quote (skip if caller supplies a rate_id from a
        # prior changelly_quote call to avoid rate drift between preview and send)
        if rate_id is None:
            quote = self.client.get_fix_rate_for_amount(
                from_asset=from_asset,
                to_asset=to_asset,
                amount_from=amount_from,
            )
            rate_id = quote.get("id") or quote.get("rateId")
            if not rate_id:
                raise RuntimeError(f"Unexpected Changelly quote response: {quote!r}")

        # Step 2 — create fixed order
        order = self.client.create_fixed_transaction(
            from_asset=from_asset,
            to_asset=to_asset,
            rate_id=rate_id,
            address=settle_address,
            refund_address=refund_address,
            amount_from=amount_from,
        )
        order_id = order.get("id")
        deposit_address = order.get("payinAddress")
        if not order_id or not deposit_address:
            raise RuntimeError(f"Unexpected Changelly order response: {order!r}")

        swap = self._swap_from_response(
            order,
            swap_type="fixed",
            direction="send",
            wallet_name=wallet_name,
            refund_address=refund_address,
            quote_id=rate_id,
        )
        # Persist BEFORE broadcasting so a crash mid-broadcast is recoverable.
        self.storage.save_changelly_swap(swap)

        # Step 3 — broadcast the USDt-Liquid deposit. Changelly amounts are
        # human-readable decimals; our wallet expects integer sats. USDt-Liquid
        # uses 8 decimal places (same as L-BTC).
        deposit_sats = _decimal_to_sats(order.get("amountExpectedFrom") or amount_from)
        # Defence in depth: refuse to sign a zero/negative-value send even if
        # the Changelly response (or upstream wrapper) passed validation. The
        # tool-layer wrapper checks amount_from, but the manager is also
        # callable directly (tests, future callers) so re-check here.
        if deposit_sats <= 0:
            msg = f"Changelly returned non-positive deposit amount: {deposit_sats}"
            swap.last_error = msg
            swap.status = "failed"
            self.storage.save_changelly_swap(swap)
            raise RuntimeError(msg)
        try:
            txid = self.wallet_manager.send(
                wallet_name,
                deposit_address,
                deposit_sats,
                asset_id=LIQUID_USDT_HEX,
                password=password,
            )
        except Exception as e:
            swap.last_error = f"Deposit broadcast failed: {e}"
            swap.status = swap.status or "failed"
            self.storage.save_changelly_swap(swap)
            raise
        swap.deposit_hash = txid
        self.storage.save_changelly_swap(swap)
        return swap

    # -- Receive flow (USDt-on-external → USDt-Liquid) -----------------------

    def receive_swap(
        self,
        external_network: str,
        wallet_name: str = "default",
        external_refund_address: Optional[str] = None,
        amount_from: str = "",
    ) -> "ChangellySwap":
        """Receive USDt-Liquid via a Changelly variable-rate order.

        Args:
            external_network: source USDt network the external sender pays from.
            wallet_name: local Liquid wallet to receive into.
            external_refund_address: STRONGLY RECOMMENDED — sender's address
                on the source chain. Without it, a stuck order requires
                manual intervention via the Changelly web UI.
            amount_from: amount the external sender will deposit (decimal string,
                e.g. "50"). Required by the Ankara backend serializer.
        """
        from_asset = network_to_asset_id(external_network)
        to_asset = LIQUID_USDT_ID
        _check_pair_allowed(from_asset, to_asset)
        # Validate the user-supplied refund address against the source-chain
        # format before we ship it to Changelly. Pasting a Liquid address as a
        # Tron refund is exactly the footgun the docstring warns about.
        if external_refund_address:
            _validate_settle_address(external_network, external_refund_address)

        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        settle_address = self.wallet_manager.get_address(wallet_name).address

        order = self.client.create_variable_transaction(
            from_asset=from_asset,
            to_asset=to_asset,
            address=settle_address,
            refund_address=external_refund_address,
            amount_from=amount_from,
        )
        order_id = order.get("id")
        deposit_address = order.get("payinAddress")
        if not order_id or not deposit_address:
            raise RuntimeError(f"Unexpected Changelly order response: {order!r}")

        swap = self._swap_from_response(
            order,
            swap_type="variable",
            direction="receive",
            wallet_name=wallet_name,
            refund_address=external_refund_address,
        )
        self.storage.save_changelly_swap(swap)
        return swap

    # -- Status polling ------------------------------------------------------

    def status(self, order_id: str) -> dict:
        swap = self.storage.load_changelly_swap(order_id)
        if not swap:
            raise ValueError(f"Changelly swap not found: {order_id}")

        warning = None
        try:
            new_status = self.client.get_status(order_id)
            swap.status = (new_status or swap.status).lower()
            swap.last_checked_at = datetime.now(UTC).isoformat()
            self.storage.save_changelly_swap(swap)
        except Exception as e:
            warning = f"Could not refresh status: {e}"

        result = swap.to_dict()
        result["is_final"] = swap_is_final(swap.status)
        result["is_success"] = swap_is_success(swap.status)
        result["is_failed"] = swap_is_failed(swap.status)
        if warning:
            result["warning"] = warning
        return result

    # -- Helpers -------------------------------------------------------------

    def _swap_from_response(
        self,
        resp: dict,
        *,
        swap_type: str,
        direction: str,
        wallet_name: Optional[str],
        refund_address: Optional[str],
        quote_id: Optional[str] = None,
    ) -> "ChangellySwap":
        return ChangellySwap(
            order_id=resp["id"],
            swap_type=swap_type,
            direction=direction,
            from_asset=resp.get("currencyFrom", ""),
            to_asset=resp.get("currencyTo", ""),
            settle_address=resp.get("payoutAddress", ""),
            deposit_address=resp["payinAddress"],
            refund_address=refund_address,
            wallet_name=wallet_name,
            status=(resp.get("status") or "new").lower(),
            created_at=datetime.now(UTC).isoformat(),
            expires_at=resp.get("payTill"),
            amount_from=resp.get("amountExpectedFrom"),
            amount_to=resp.get("amountExpectedTo"),
            network_fee=resp.get("networkFee"),
            quote_id=quote_id,
            track_url=resp.get("trackUrl") or changelly_track_url(resp["id"]),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _decimal_to_sats(decimal_str: str | float | int) -> int:
    """Convert a Changelly human-readable amount (e.g. "100.0") to integer sats.

    USDt-Liquid uses 8 decimal places — same conversion as L-BTC sats.
    """
    d = Decimal(str(decimal_str))
    sats = (d * Decimal(100_000_000)).quantize(Decimal("1."), rounding=ROUND_HALF_UP)
    return int(sats)
