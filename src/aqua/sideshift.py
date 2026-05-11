"""SideShift.ai integration for cross-chain swaps.

SideShift is a multi-chain swap service.

Two flows:

- **Fixed-rate** (`/v2/quotes` then `/v2/shifts/fixed`): commit to a quoted
  rate, send *exactly* `depositAmount` within the quote's TTL (~15 min), and
  the agreed `settleAmount` is delivered.
- **Variable-rate** (`/v2/shifts/variable`): get a deposit address, send any
  amount in `[depositMin, depositMax]`, rate set when deposit confirms.

When to use SideShift vs SideSwap:

- Both legs Liquid (or BTC↔L-BTC and you can wait) → SideSwap (trustless or
  Federation, lower fees).
- At least one leg is non-Liquid (USDt-Tron, ETH, USDt-on-Ethereum, LTC, etc.) →
  SideShift (custodial but covers everything else). Use `recommend_shift_or_swap`.

Endpoints used (all REST/JSON, base `https://sideshift.ai/api/v2`):

  GET  /v2/coins                  — supported coins+networks
  GET  /v2/permissions            — geo / availability check
  GET  /v2/pair/{from}/{to}       — rate, min, max for a pair
  POST /v2/quotes                 — fixed quote (15 min TTL)
  POST /v2/shifts/fixed           — create fixed shift from a quote
  POST /v2/shifts/variable        — create variable shift (no quote)
  GET  /v2/shifts/{id}            — shift status

Auth: anonymous, identified by `affiliateId` in request body (publicly visible
in any client; commission accrues to that affiliate's account). The affiliate
ID we ship with is `PVmPh4Mp3` — JAN3's AQUA wallet ID, also used by the
AQUA Flutter app.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional

from .assets import LBTC_ASSET_ID

logger = logging.getLogger(__name__)


# Public constants — same affiliate id AQUA Flutter ships with. Commission
# accrues to JAN3's SideShift account.
AFFILIATE_ID = "PVmPh4Mp3"
SIDESHIFT_BASE_URL = os.environ.get("SIDESHIFT_BASE_URL", "https://sideshift.ai/api/v2")
USER_AGENT = "agentic-aqua"
HTTP_TIMEOUT_SECONDS = 30.0

# SideShift's coin-network IDs use lowercase coin + lowercase network. The
# wallet's policy assets map as follows (see AQUA's `sideshift_ext.dart`):
SIDESHIFT_COIN_BTC_BITCOIN = ("btc", "bitcoin")
SIDESHIFT_COIN_BTC_LIQUID = ("btc", "liquid")  # SideShift calls L-BTC just "BTC" on the liquid network
SIDESHIFT_COIN_USDT_LIQUID = ("usdt", "liquid")

# Curated allowlist of (coin, network) pairs we expose for swaps. Mirrors
# AQUA Flutter's `SideshiftAsset` factories in
# `lib/features/sideshift/models/sideshift_assets.dart` — USDt routed across
# the major chains plus BTC mainchain. Both legs of a `sideshift_send` /
# `sideshift_receive` call must be in this set.
#
# This intentionally does NOT include `(btc, liquid)` (i.e. L-BTC) — for
# L-BTC ↔ external use SideSwap, or chain SideShift through USDt-Liquid
# (e.g. L-BTC → USDt-Liquid via SideSwap, then USDt-Liquid → USDt-Tron via
# SideShift). Override the allowlist for testing or power use by setting
# `SIDESHIFT_ALLOW_ALL_NETWORKS=1` in the environment.
ALLOWED_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("usdt", "ethereum"),
    ("usdt", "tron"),
    ("usdt", "bsc"),
    ("usdt", "solana"),
    ("usdt", "polygon"),
    ("usdt", "ton"),
    ("usdt", "liquid"),
    ("btc", "bitcoin"),
})


def _allow_all_networks() -> bool:
    """Read the override env var. Re-read on every check so tests can mutate it.

    Accepts `1`, `true`, `yes` (case-insensitive) as truthy.
    """
    return os.environ.get("SIDESHIFT_ALLOW_ALL_NETWORKS", "").strip().lower() in {
        "1", "true", "yes",
    }


def _check_pair_allowed(coin: str, network: str, side: str) -> None:
    """Raise ValueError if (coin, network) isn't on the curated allowlist.

    The override env var bypasses the check entirely.

    Args:
        coin / network: the pair to check (case-insensitive).
        side: "deposit" or "settle" — used only for the error message.
    """
    if _allow_all_networks():
        return
    pair = (coin.lower(), network.lower())
    if pair not in ALLOWED_PAIRS:
        allowed = ", ".join(f"{c}-{n}" for c, n in sorted(ALLOWED_PAIRS))
        raise ValueError(
            f"SideShift {side} pair {coin}-{network} is not in the curated "
            f"allowlist (matches AQUA Flutter's supported set): {allowed}. "
            "Set SIDESHIFT_ALLOW_ALL_NETWORKS=1 to bypass."
        )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SideShiftCoin:
    """A coin/network as returned from `/v2/coins`."""

    coin: str  # e.g. "USDT"
    name: str  # e.g. "Tether"
    networks: list[str]
    has_memo: bool = False
    fixed_only: bool = False
    variable_only: bool = False
    deposit_offline: bool = False
    settle_offline: bool = False
    token_details: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SideShiftPairInfo:
    """Pair info from `/v2/pair/{from}/{to}` — rate, min, max."""

    deposit_coin: str
    deposit_network: str
    settle_coin: str
    settle_network: str
    rate: str  # SideShift returns rates as strings to preserve precision
    min: str
    max: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SideShiftQuote:
    """A fixed-rate quote from `/v2/quotes`."""

    quote_id: str
    deposit_coin: str
    deposit_network: str
    settle_coin: str
    settle_network: str
    deposit_amount: str
    settle_amount: str
    rate: str
    affiliate_id: Optional[str]
    created_at: str  # ISO timestamp from server
    expires_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SideShiftShift:
    """Persistent record of a SideShift shift (fixed or variable, send or receive)."""

    shift_id: str
    shift_type: str  # "fixed" | "variable"
    direction: str  # "send" (we sign deposit) | "receive" (we provide settle address)
    deposit_coin: str
    deposit_network: str
    settle_coin: str
    settle_network: str
    settle_address: str
    deposit_address: str
    refund_address: Optional[str]
    wallet_name: Optional[str]
    status: str
    created_at: str
    expires_at: Optional[str] = None
    deposit_amount: Optional[str] = None  # set on fixed; None on variable until deposit confirms
    settle_amount: Optional[str] = None  # set on fixed; None on variable until rate is locked
    deposit_min: Optional[str] = None  # variable shifts only
    deposit_max: Optional[str] = None  # variable shifts only
    rate: Optional[str] = None
    quote_id: Optional[str] = None  # fixed shifts only
    deposit_memo: Optional[str] = None  # for memo-required networks (TON, BNB, etc.)
    deposit_hash: Optional[str] = None  # txid where the deposit landed
    settle_hash: Optional[str] = None  # txid where the settlement landed
    last_checked_at: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SideShiftShift":
        data = {**data}
        for f in (
            "expires_at", "deposit_amount", "settle_amount", "deposit_min",
            "deposit_max", "rate", "quote_id", "deposit_memo", "deposit_hash",
            "settle_hash", "last_checked_at", "last_error",
        ):
            data.setdefault(f, None)
        return cls(**data)


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


class SideShiftClient:
    """HTTP client for the SideShift v2 API.

    All POSTs include `affiliateId` in the body so commission accrues to our
    account. GET endpoints add `affiliateId` as a query parameter where
    applicable (`/v2/pair`).
    """

    def __init__(
        self,
        affiliate_id: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        """
        Args:
            affiliate_id: None falls back to the default (`PVmPh4Mp3`); pass
                an empty string to disable the affiliate id entirely (anonymous,
                no commission); pass any other string to override.
            base_url: Override the default API base.
        """
        if affiliate_id is None:
            affiliate_id = AFFILIATE_ID
        # Empty string explicitly disables; any other falsy value also disables.
        self.affiliate_id = affiliate_id or None
        self.base_url = (base_url or SIDESHIFT_BASE_URL).rstrip("/")

    def _api_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        query: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            # urllib will encode values; drop None values.
            cleaned = {k: v for k, v in query.items() if v is not None}
            if cleaned:
                url += "?" + urllib.parse.urlencode(cleaned)
        data = None
        if body is not None:
            payload = {**body}
            if self.affiliate_id and "affiliateId" not in payload:
                payload["affiliateId"] = self.affiliate_id
            data = json.dumps(payload).encode()
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
                # SideShift returns {"error": {"message": "..."}} on errors.
                err = err_body.get("error", {})
                if isinstance(err, dict):
                    detail = err.get("message") or err.get("code") or str(err)
                elif isinstance(err, str):
                    detail = err
                else:
                    detail = err_body.get("message", "")
            except Exception:
                pass
            msg = f"SideShift API error ({e.code} {method} {path})"
            if detail:
                msg += f": {detail}"
            raise RuntimeError(msg) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"SideShift API unreachable ({method} {path}): {e.reason}") from e

    # -- Endpoints -----------------------------------------------------------

    def get_coins(self) -> list[dict]:
        return self._api_request("GET", "/coins") or []

    def get_permissions(self) -> dict:
        return self._api_request("GET", "/permissions") or {}

    def get_pair(
        self,
        from_coin: str,
        from_network: str,
        to_coin: str,
        to_network: str,
        amount: Optional[str] = None,
    ) -> dict:
        from_id = f"{from_coin.lower()}-{from_network.lower()}"
        to_id = f"{to_coin.lower()}-{to_network.lower()}"
        query: dict[str, Any] = {}
        if self.affiliate_id:
            query["affiliateId"] = self.affiliate_id
        if amount is not None:
            query["amount"] = amount
        return self._api_request("GET", f"/pair/{from_id}/{to_id}", query=query) or {}

    def request_quote(
        self,
        deposit_coin: str,
        deposit_network: str,
        settle_coin: str,
        settle_network: str,
        deposit_amount: Optional[str] = None,
        settle_amount: Optional[str] = None,
    ) -> dict:
        if (deposit_amount is None) == (settle_amount is None):
            raise ValueError(
                "exactly one of deposit_amount or settle_amount must be provided"
            )
        body: dict[str, Any] = {
            "depositCoin": deposit_coin.upper(),
            "depositNetwork": deposit_network.lower(),
            "settleCoin": settle_coin.upper(),
            "settleNetwork": settle_network.lower(),
        }
        if deposit_amount is not None:
            body["depositAmount"] = deposit_amount
        if settle_amount is not None:
            body["settleAmount"] = settle_amount
        return self._api_request("POST", "/quotes", body=body) or {}

    def create_fixed_shift(
        self,
        quote_id: str,
        settle_address: str,
        refund_address: Optional[str] = None,
        settle_memo: Optional[str] = None,
        refund_memo: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "quoteId": quote_id,
            "settleAddress": settle_address,
        }
        if refund_address:
            body["refundAddress"] = refund_address
        if settle_memo:
            body["settleMemo"] = settle_memo
        if refund_memo:
            body["refundMemo"] = refund_memo
        if external_id:
            body["externalId"] = external_id
        return self._api_request("POST", "/shifts/fixed", body=body) or {}

    def create_variable_shift(
        self,
        deposit_coin: str,
        deposit_network: str,
        settle_coin: str,
        settle_network: str,
        settle_address: str,
        refund_address: Optional[str] = None,
        settle_memo: Optional[str] = None,
        refund_memo: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "depositCoin": deposit_coin.upper(),
            "depositNetwork": deposit_network.lower(),
            "settleCoin": settle_coin.upper(),
            "settleNetwork": settle_network.lower(),
            "settleAddress": settle_address,
        }
        if refund_address:
            body["refundAddress"] = refund_address
        if settle_memo:
            body["settleMemo"] = settle_memo
        if refund_memo:
            body["refundMemo"] = refund_memo
        if external_id:
            body["externalId"] = external_id
        return self._api_request("POST", "/shifts/variable", body=body) or {}

    def get_shift(self, shift_id: str) -> dict:
        return self._api_request("GET", f"/shifts/{shift_id}") or {}


# ---------------------------------------------------------------------------
# Coin/network resolution for the wallet's native chains
# ---------------------------------------------------------------------------


# What our wallet can sign for natively. Anything else is a (coin, network)
# string the user supplies for an external counterparty.
NATIVE_DEPOSIT_CHAINS = {
    "bitcoin",  # BDK
    "liquid",   # LWK
}


# ---------------------------------------------------------------------------
# Recommendation: SideSwap vs SideShift
# ---------------------------------------------------------------------------


def recommend_shift_or_swap(
    from_coin: str,
    from_network: str,
    to_coin: str,
    to_network: str,
) -> dict:
    """Decide whether SideSwap or SideShift is the better fit for a pair.

    Heuristic:
    - Both legs are in {bitcoin, liquid} → SideSwap (atomic Liquid swap or
      Liquid Federation peg; trustless or near-trustless, lower fees).
    - At least one leg is a non-{bitcoin, liquid} chain → SideShift
      (custodial; covers all the chains SideSwap doesn't).
    - L-BTC → BTC quickly (skip the federation wait): SideShift is also fine.

    Args:
        from_coin / from_network / to_coin / to_network: lowercase strings.

    Returns:
        {"recommendation": "sideswap" | "sideshift",
         "reason": <human-readable>,
         "from_coin", "from_network", "to_coin", "to_network"}
    """
    fnet, tnet = from_network.lower(), to_network.lower()
    fcoin, tcoin = from_coin.lower(), to_coin.lower()
    # Same (coin, network) on both sides isn't a swap — neither service quotes
    # it and a caller asking for one almost certainly has a bug. Surface the
    # error rather than silently steering them at sideswap.
    if (fcoin, fnet) == (tcoin, tnet):
        return {
            "recommendation": "none",
            "reason": (
                f"Same asset on the same network ({fcoin}-{fnet}) — there's "
                "nothing to swap. Re-check the from/to arguments."
            ),
            "from_coin": from_coin,
            "from_network": fnet,
            "to_coin": to_coin,
            "to_network": tnet,
        }
    if fnet in {"bitcoin", "liquid"} and tnet in {"bitcoin", "liquid"}:
        # Both legs are on networks SideSwap can handle natively.
        return {
            "recommendation": "sideswap",
            "reason": (
                "Both legs are on Bitcoin or Liquid. SideSwap offers atomic "
                "Liquid swaps and BTC↔L-BTC pegs — lower fees and no "
                "custodial risk. Use SideShift if you need a faster BTC ↔ "
                "L-BTC conversion than the Liquid Federation peg-in (102 "
                "BTC confs for large amounts) and accept the custodial "
                "trust trade-off."
            ),
            "from_coin": from_coin,
            "from_network": fnet,
            "to_coin": to_coin,
            "to_network": tnet,
        }
    return {
        "recommendation": "sideshift",
        "reason": (
            "At least one leg is on a network SideSwap doesn't cover "
            f"({fnet} → {tnet}). SideShift is custodial (trust the company, "
            "not on-chain) but covers 30+ chains including ETH, Tron, Solana, "
            "and USDt on every major network. Always supply a refund address."
        ),
        "from_coin": from_coin,
        "from_network": fnet,
        "to_coin": to_coin,
        "to_network": tnet,
    }


# ---------------------------------------------------------------------------
# Shift status mapping
# ---------------------------------------------------------------------------

# SideShift returns one of these statuses (lowercase). We surface them as-is
# but expose `is_final` and `is_success` helpers so callers don't have to
# memorise the state machine.
_FINAL_STATUSES = {"settled", "refunded", "expired"}
_SUCCESS_STATUSES = {"settled"}
_FAILED_STATUSES = {"refunded", "expired"}


def shift_is_final(status: str) -> bool:
    return status.lower() in _FINAL_STATUSES


def shift_is_success(status: str) -> bool:
    return status.lower() in _SUCCESS_STATUSES


def shift_is_failed(status: str) -> bool:
    return status.lower() in _FAILED_STATUSES


# ---------------------------------------------------------------------------
# High-level manager
# ---------------------------------------------------------------------------


class SideShiftManager:
    """Orchestrates SideShift send / receive flows tied to AQUA's wallet managers.

    Two flows:

    1. **Send** (`send_shift`): user has funds in their Liquid or BTC wallet
       and wants to convert to a non-Liquid asset (e.g. send USDt-Liquid to
       receive USDt-Tron at an external address). We:
         - Validate the deposit chain is one of {bitcoin, liquid}
         - Get a fixed-rate quote for the agreed amount
         - Create a fixed shift with the quote
         - Broadcast the deposit from our wallet (via `wallet.send` / `bitcoin.send` / `wallet.send_asset`)
         - Persist throughout

    2. **Receive** (`receive_shift`): user wants to receive Liquid or Bitcoin
       from any chain. We create a variable-rate shift with the user's wallet
       address as the settle address; the user (or external sender) sends
       to the returned `deposit_address` from any wallet on the deposit chain.

    Both flows always supply a refund address (the user's own wallet on the
    deposit chain when sending, or a user-provided external address when
    receiving). Without one, a stuck shift can't be unstuck without manually
    visiting the SideShift web UI.
    """

    def __init__(self, storage, wallet_manager, btc_wallet_manager) -> None:
        """
        Args:
            storage: Storage instance with sideshift_shift helpers.
            wallet_manager: WalletManager (Liquid/LWK).
            btc_wallet_manager: BitcoinWalletManager (BDK).
        """
        self.storage = storage
        self.wallet_manager = wallet_manager
        self.btc_wallet_manager = btc_wallet_manager
        self._client: Optional[SideShiftClient] = None

    @property
    def client(self) -> SideShiftClient:
        if self._client is None:
            self._client = SideShiftClient()
        return self._client

    # -- Read-only helpers ---------------------------------------------------

    def list_coins(self) -> list[dict]:
        return self.client.get_coins()

    def pair_info(
        self,
        from_coin: str,
        from_network: str,
        to_coin: str,
        to_network: str,
        amount: Optional[str] = None,
    ) -> dict:
        return self.client.get_pair(from_coin, from_network, to_coin, to_network, amount)

    def quote(
        self,
        deposit_coin: str,
        deposit_network: str,
        settle_coin: str,
        settle_network: str,
        deposit_amount: Optional[str] = None,
        settle_amount: Optional[str] = None,
    ) -> dict:
        return self.client.request_quote(
            deposit_coin, deposit_network, settle_coin, settle_network,
            deposit_amount=deposit_amount, settle_amount=settle_amount,
        )

    # -- Send flow -----------------------------------------------------------

    def send_shift(
        self,
        deposit_coin: str,
        deposit_network: str,
        settle_coin: str,
        settle_network: str,
        settle_address: str,
        deposit_amount: Optional[str] = None,
        settle_amount: Optional[str] = None,
        wallet_name: str = "default",
        password: Optional[str] = None,
        liquid_asset_id: Optional[str] = None,
        settle_memo: Optional[str] = None,
        refund_memo: Optional[str] = None,
        quote_id: Optional[str] = None,
    ) -> "SideShiftShift":
        """Send funds from our wallet via a fixed-rate shift.

        Args:
            deposit_coin / deposit_network: must be a chain we can sign on
                (bitcoin or liquid). Liquid assets identified by
                `(coin, network) = ("btc"|"usdt"|..., "liquid")`.
            settle_coin / settle_network: any chain SideShift supports.
            settle_address: where SideShift sends the converted asset
                (the user's external address).
            deposit_amount / settle_amount: provide exactly one. Strings to
                preserve precision (SideShift uses decimal strings).
            wallet_name: which local wallet to send from.
            password: mnemonic decryption password (if encrypted at rest).
            liquid_asset_id: hex asset id, required when the Liquid asset
                is not L-BTC (e.g. USDt-Liquid).
            settle_memo / refund_memo: required for memo networks
                (TON, BNB Beacon, etc.) on either side.
            quote_id: an existing fixed-rate quote id (from a prior
                `quote()` call). When provided, skip the internal
                `request_quote` call so the executed shift uses the same
                rate the caller just confirmed with the user. Without it,
                the manager fetches a fresh quote and the rate may differ
                slightly from a preview shown moments earlier.
        """
        deposit_network_l = deposit_network.lower()
        if deposit_network_l not in NATIVE_DEPOSIT_CHAINS:
            raise ValueError(
                f"Cannot sign on {deposit_network!r}; deposit_network must be one "
                f"of {sorted(NATIVE_DEPOSIT_CHAINS)} (use a wallet that holds the "
                f"deposit asset and sign externally if needed)."
            )
        # Allowlist check: both legs must be in AQUA's curated set, unless the
        # caller has set SIDESHIFT_ALLOW_ALL_NETWORKS.
        _check_pair_allowed(deposit_coin, deposit_network, side="deposit")
        _check_pair_allowed(settle_coin, settle_network, side="settle")

        # Guard against the silent-L-BTC footgun: when depositing a non-L-BTC
        # asset on Liquid (e.g. USDt-Liquid), the wallet's `send` method
        # defaults to L-BTC unless `liquid_asset_id` is set. Without this
        # check, a missing `liquid_asset_id` would broadcast L-BTC to the
        # SideShift deposit address — SideShift wouldn't credit the shift
        # and the funds would be stuck pending manual refund.
        if deposit_network_l == "liquid" and deposit_coin.lower() != "btc":
            if not liquid_asset_id or liquid_asset_id == LBTC_ASSET_ID:
                raise ValueError(
                    f"liquid_asset_id is required when depositing a non-L-BTC "
                    f"Liquid asset (deposit_coin={deposit_coin!r}) and must not "
                    "be the L-BTC policy asset id. Without it, the wallet "
                    "would send L-BTC to the deposit address."
                )

        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        if wallet_data.watch_only:
            raise ValueError("Watch-only wallet cannot sign a SideShift deposit")
        # Pre-validate the mnemonic decryption BEFORE creating the SideShift
        # order. Without this, a wrong password only surfaces at broadcast
        # time — leaving an orphan custodial order behind for every retry.
        if wallet_data.encrypted_mnemonic and self.storage.is_mnemonic_encrypted(
            wallet_data.encrypted_mnemonic
        ):
            if not password:
                raise ValueError("Password required to decrypt mnemonic")
            # retrieve_mnemonic raises on a bad password; let it propagate
            # before we contact SideShift.
            self.storage.retrieve_mnemonic(wallet_data.encrypted_mnemonic, password)

        # Refund address: same wallet, same network as the deposit. If the
        # shift fails for any reason, the funds come back to where they came
        # from (less the network fee on a refund tx).
        refund_address = self._wallet_address(deposit_network_l, wallet_name)

        if quote_id:
            # Reuse the caller-supplied quote so the shift executes at the
            # rate the user just confirmed in the preview. Skips a redundant
            # request_quote round-trip and removes the slippage window.
            shift_quote_id = quote_id
        else:
            quote = self.client.request_quote(
                deposit_coin, deposit_network, settle_coin, settle_network,
                deposit_amount=deposit_amount, settle_amount=settle_amount,
            )
            if not quote.get("id"):
                raise RuntimeError(f"Unexpected quote response: {quote!r}")
            shift_quote_id = quote["id"]

        shift_resp = self.client.create_fixed_shift(
            quote_id=shift_quote_id,
            settle_address=settle_address,
            refund_address=refund_address,
            settle_memo=settle_memo,
            refund_memo=refund_memo,
        )
        if not shift_resp.get("id") or not shift_resp.get("depositAddress"):
            raise RuntimeError(f"Unexpected shift response: {shift_resp!r}")

        shift = self._shift_from_response(
            shift_resp,
            shift_type="fixed",
            direction="send",
            wallet_name=wallet_name,
            refund_address=refund_address,
            quote_id=shift_quote_id,
        )
        # Persist BEFORE broadcasting the deposit. If the broadcast fails
        # we still have a record of the shift to refund or retry.
        self.storage.save_sideshift_shift(shift)

        # Broadcast the deposit. SideShift's depositAmount is in human-readable
        # decimal (e.g. "0.0005"); our wallet sends are in sats. Convert.
        deposit_sats = _decimal_to_sats_8dp(shift_resp["depositAmount"])
        try:
            txid = self._wallet_send(
                deposit_network_l,
                wallet_name=wallet_name,
                address=shift_resp["depositAddress"],
                amount_sats=deposit_sats,
                password=password,
                liquid_asset_id=liquid_asset_id,
            )
        except Exception as e:
            shift.last_error = f"Deposit broadcast failed: {e}"
            shift.status = "failed"
            self.storage.save_sideshift_shift(shift)
            raise
        shift.deposit_hash = txid
        # Status often stays "waiting" until SideShift sees the deposit on-chain,
        # which can take a confirmation. Don't override the server's status.
        self.storage.save_sideshift_shift(shift)
        return shift

    # -- Receive flow --------------------------------------------------------

    def receive_shift(
        self,
        deposit_coin: str,
        deposit_network: str,
        settle_coin: str,
        settle_network: str,
        wallet_name: str = "default",
        external_refund_address: Optional[str] = None,
        external_refund_memo: Optional[str] = None,
        settle_memo: Optional[str] = None,
    ) -> "SideShiftShift":
        """Receive funds into our wallet via a variable-rate shift.

        Args:
            deposit_coin / deposit_network: any chain SideShift supports
                (this is where the external sender pays from).
            settle_coin / settle_network: must be one of {bitcoin, liquid}
                (this is the chain we settle into).
            external_refund_address: where SideShift refunds if the deposit
                arrives wrong. Strongly recommended; without it a stuck shift
                requires manual web UI intervention. May be the deposit-side
                external sender's address, asked of the user.
        """
        settle_network_l = settle_network.lower()
        if settle_network_l not in NATIVE_DEPOSIT_CHAINS:
            raise ValueError(
                f"Cannot receive on {settle_network!r}; settle_network must be "
                f"one of {sorted(NATIVE_DEPOSIT_CHAINS)}."
            )
        # Allowlist check: both legs must be in AQUA's curated set, unless the
        # caller has set SIDESHIFT_ALLOW_ALL_NETWORKS.
        _check_pair_allowed(deposit_coin, deposit_network, side="deposit")
        _check_pair_allowed(settle_coin, settle_network, side="settle")

        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        # Receive doesn't need the mnemonic decrypted — we only need an address.

        settle_address = self._wallet_address(settle_network_l, wallet_name)

        shift_resp = self.client.create_variable_shift(
            deposit_coin=deposit_coin,
            deposit_network=deposit_network,
            settle_coin=settle_coin,
            settle_network=settle_network,
            settle_address=settle_address,
            refund_address=external_refund_address,
            settle_memo=settle_memo,
            refund_memo=external_refund_memo,
        )
        if not shift_resp.get("id") or not shift_resp.get("depositAddress"):
            raise RuntimeError(f"Unexpected shift response: {shift_resp!r}")

        shift = self._shift_from_response(
            shift_resp,
            shift_type="variable",
            direction="receive",
            wallet_name=wallet_name,
            refund_address=external_refund_address,
        )
        self.storage.save_sideshift_shift(shift)
        return shift

    # -- Status polling ------------------------------------------------------

    def status(self, shift_id: str) -> dict:
        shift = self.storage.load_sideshift_shift(shift_id)
        if not shift:
            raise ValueError(f"SideShift shift not found: {shift_id}")

        warning = None
        try:
            resp = self.client.get_shift(shift_id)
            shift.status = (resp.get("status") or shift.status).lower()
            if resp.get("depositHash"):
                shift.deposit_hash = resp["depositHash"]
            if resp.get("settleHash"):
                shift.settle_hash = resp["settleHash"]
            if resp.get("rate"):
                shift.rate = str(resp["rate"])
            if resp.get("depositAmount"):
                shift.deposit_amount = str(resp["depositAmount"])
            if resp.get("settleAmount"):
                shift.settle_amount = str(resp["settleAmount"])
            shift.last_checked_at = datetime.now(UTC).isoformat()
            self.storage.save_sideshift_shift(shift)
        except Exception as e:
            warning = f"Could not refresh status: {e}"

        result = shift.to_dict()
        result["is_final"] = shift_is_final(shift.status)
        result["is_success"] = shift_is_success(shift.status)
        result["is_failed"] = shift_is_failed(shift.status)
        if warning:
            result["warning"] = warning
        return result

    # -- Helpers -------------------------------------------------------------

    def _wallet_address(self, network: str, wallet_name: str) -> str:
        if network == "bitcoin":
            return self.btc_wallet_manager.get_address(wallet_name).address
        if network == "liquid":
            return self.wallet_manager.get_address(wallet_name).address
        raise ValueError(f"Unsupported network for wallet address lookup: {network!r}")

    def _wallet_send(
        self,
        network: str,
        *,
        wallet_name: str,
        address: str,
        amount_sats: int,
        password: Optional[str],
        liquid_asset_id: Optional[str],
    ) -> str:
        if network == "bitcoin":
            if liquid_asset_id is not None:
                raise ValueError("liquid_asset_id is not valid on Bitcoin sends")
            return self.btc_wallet_manager.send(
                wallet_name, address, amount_sats, password=password
            )
        if network == "liquid":
            if liquid_asset_id and liquid_asset_id != LBTC_ASSET_ID:
                return self.wallet_manager.send(
                    wallet_name, address, amount_sats,
                    asset_id=liquid_asset_id, password=password,
                )
            return self.wallet_manager.send(
                wallet_name, address, amount_sats, password=password
            )
        raise ValueError(f"Unsupported send network: {network!r}")

    def _shift_from_response(
        self,
        resp: dict,
        *,
        shift_type: str,
        direction: str,
        wallet_name: Optional[str],
        refund_address: Optional[str],
        quote_id: Optional[str] = None,
    ) -> "SideShiftShift":
        return SideShiftShift(
            shift_id=resp["id"],
            shift_type=shift_type,
            direction=direction,
            deposit_coin=resp.get("depositCoin", ""),
            deposit_network=resp.get("depositNetwork", ""),
            settle_coin=resp.get("settleCoin", ""),
            settle_network=resp.get("settleNetwork", ""),
            settle_address=resp.get("settleAddress", ""),
            deposit_address=resp["depositAddress"],
            refund_address=refund_address,
            wallet_name=wallet_name,
            status=(resp.get("status") or "waiting").lower(),
            created_at=datetime.now(UTC).isoformat(),
            expires_at=resp.get("expiresAt"),
            deposit_amount=str(resp.get("depositAmount")) if resp.get("depositAmount") is not None else None,
            settle_amount=str(resp.get("settleAmount")) if resp.get("settleAmount") is not None else None,
            deposit_min=str(resp.get("depositMin")) if resp.get("depositMin") is not None else None,
            deposit_max=str(resp.get("depositMax")) if resp.get("depositMax") is not None else None,
            rate=str(resp.get("rate")) if resp.get("rate") is not None else None,
            quote_id=quote_id,
            deposit_memo=resp.get("depositMemo"),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _decimal_to_sats_8dp(decimal_str: str | float | int) -> int:
    """Convert a SideShift human-readable amount (e.g. "0.0005") to integer sats.

    Hardcoded to 8 decimal places — the precision used by BTC and the Liquid
    assets we sign on (L-BTC + USDt-Liquid). The broadcast path runs only on
    `bitcoin` or `liquid` networks, so callers should never pass values from
    higher-precision chains (e.g. ETH at 18dp) here.

    Rounding is HALF_UP: SideShift quotes are pre-rounded to the chain's
    native precision in practice, so the sub-sat cases this affects are rare;
    if one occurs, we'd rather over-pay 1 sat than land below `depositMin`.
    """
    from decimal import Decimal, ROUND_HALF_UP

    d = Decimal(str(decimal_str))
    sats = (d * Decimal(100_000_000)).quantize(Decimal("1."), rounding=ROUND_HALF_UP)
    return int(sats)
