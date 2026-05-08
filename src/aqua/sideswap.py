"""SideSwap integration for BTC ↔ L-BTC pegs and Liquid asset swap quoting.

Wire formats (mirroring the AQUA Flutter wallet's `sideswap_websocket_provider`):

- WebSocket JSON-RPC 2.0:
    request:      {"id": <int>, "method": "<snake_case>", "params": {...}}
    response:     {"id": <int>, "method": "<method>", "result": {...}}
                  {"id": <int>, "error": {"code": <int>, "message": "<str>"}}
    notification: {"method": "<method>", "params": {...}}   (no `id`)

- WebSocket endpoints:
    mainnet: wss://api.sideswap.io/json-rpc-ws
    testnet: wss://api-testnet.sideswap.io/json-rpc-ws

Methods used here:

- `login_client`         — anonymous (api_key=None), identifies us as agentic-aqua
- `server_status`        — fees, min amounts, hot-wallet balances
- `peg_fee`              — quote fee for a given amount and direction
- `peg`                  — initiate peg-in (BTC→L-BTC) or peg-out (L-BTC→BTC)
- `peg_status`           — poll order status
- `assets`               — list supported assets for swap quoting
- `subscribe_price_stream` / `unsubscribe_price_stream`
                         — get a price quote for a Liquid asset swap (read-only)

Asset swap *execution* (`start_swap_web` + HTTP `swap_start`/`swap_sign` with
local PSET verification) is intentionally NOT implemented in this module: the
PSET output check is security-critical and must be audited against LWK's
unblinding API before live signing. Use this module to fetch quotes and direct
users to AQUA / SideSwap for execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# WebSocket endpoints
SIDESWAP_WS_URL = {
    "mainnet": "wss://api.sideswap.io/json-rpc-ws",
    "testnet": "wss://api-testnet.sideswap.io/json-rpc-ws",
}

# REST base for legacy `swap_start` / `swap_sign` (returned as `upload_url`
# by `start_swap_web`; included here for documentation/fallback)
SIDESWAP_HTTP_URL = {
    "mainnet": "https://api.sideswap.io",
    "testnet": "https://api-testnet.sideswap.io",
}

USER_AGENT = "agentic-aqua"
PROTOCOL_VERSION = "1.0.0"

# Network defaults: SideSwap surfaces live values via `server_status`; these
# are conservative fallbacks for when the WS is unreachable. Treat `server_status`
# return values as authoritative when available.
FALLBACK_MIN_PEG_IN_SATS = 1_286
FALLBACK_MIN_PEG_OUT_SATS = 100_000
FALLBACK_PEG_IN_FEE_PERCENT = 0.1  # of send amount
FALLBACK_PEG_OUT_FEE_PERCENT = 0.1

# Threshold above which a BTC ↔ L-BTC peg saves enough on fees to justify the
# wait over a swap-market trade. Pegs charge 0.1% versus the 0.2% taker fee on
# the swap market, so above ~0.01 BTC the saving is ≥ 1,000 sats and grows
# linearly. Below this, the user may prefer the speed of an instant swap.
PEG_RECOMMENDATION_THRESHOLD_SATS = 1_000_000

WS_TIMEOUT_SECONDS = 30.0
QUOTE_WAIT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SideSwapPeg:
    """Persistent record of a SideSwap peg (peg-in or peg-out)."""

    order_id: str
    peg_in: bool  # True = BTC → L-BTC, False = L-BTC → BTC
    peg_addr: str  # Where the user sends funds (BTC addr for peg-in, L-BTC addr for peg-out)
    recv_addr: str  # Where the user receives funds (L-BTC for peg-in, BTC for peg-out)
    amount: Optional[int]  # Send amount in sats (set for peg-out, may be None for peg-in)
    expected_recv: Optional[int]  # Expected recv amount (after fees) when known
    wallet_name: str
    network: str  # "mainnet" | "testnet"
    status: str  # "pending" | "detected" | "processing" | "completed" | "failed"
    created_at: str
    expires_at: Optional[int] = None  # Unix ms, from server
    lockup_txid: Optional[str] = None  # User's send tx (peg-out only, local-broadcast)
    payout_txid: Optional[str] = None  # Server's payout tx (set on completion)
    detected_confs: Optional[int] = None
    total_confs: Optional[int] = None
    tx_state: Optional[str] = None  # InsufficientAmount | Detected | Processing | Done
    last_checked_at: Optional[str] = None
    return_address: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SideSwapPeg":
        data = {**data}
        for f in (
            "expires_at",
            "lockup_txid",
            "payout_txid",
            "detected_confs",
            "total_confs",
            "tx_state",
            "last_checked_at",
            "return_address",
        ):
            data.setdefault(f, None)
        return cls(**data)


@dataclass
class SideSwapServerStatus:
    """Subset of `server_status` response we surface to callers."""

    elements_fee_rate: Optional[float] = None
    min_peg_in_amount: Optional[int] = None
    min_peg_out_amount: Optional[int] = None
    server_fee_percent_peg_in: Optional[float] = None
    server_fee_percent_peg_out: Optional[float] = None
    peg_in_wallet_balance: Optional[int] = None
    peg_out_wallet_balance: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SideSwapAsset:
    """A SideSwap-supported Liquid asset (subset of `assets` response fields)."""

    asset_id: str
    ticker: str
    name: str
    precision: int
    instant_swaps: bool = False
    icon_url: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SideSwapPriceQuote:
    """Snapshot of an `update_price_stream` notification."""

    asset_id: str
    send_bitcoins: bool  # If True, user sends L-BTC for the asset
    send_amount: int
    recv_amount: int
    price: float
    fixed_fee: int
    error_msg: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# WebSocket JSON-RPC client (async)
# ---------------------------------------------------------------------------


class SideSwapWSError(RuntimeError):
    """Raised for SideSwap JSON-RPC error responses or connection failures."""


class SideSwapWSClient:
    """Minimal async JSON-RPC client over WebSocket.

    One-shot usage pattern:

        async with SideSwapWSClient(network) as client:
            await client.login_client()
            status = await client.server_status()

    Keeps a per-call request-id counter and a queue of incoming notifications
    so callers can `await client.next_notification(method=...)` for streaming
    messages (e.g. `update_price_stream`).
    """

    def __init__(self, network: str = "mainnet") -> None:
        if network not in SIDESWAP_WS_URL:
            raise ValueError(f"Unknown network: {network}")
        self.network = network
        self.url = SIDESWAP_WS_URL[network]
        self._ws = None  # type: ignore[assignment]
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._notifications: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._closed = False

    async def __aenter__(self) -> "SideSwapWSClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        # Imported lazily so tests that don't exercise the network never need
        # the optional `websockets` dependency.
        import websockets

        self._ws = await asyncio.wait_for(
            websockets.connect(self.url, max_size=4 * 1024 * 1024),
            timeout=WS_TIMEOUT_SECONDS,
        )
        self._reader_task = asyncio.create_task(self._reader())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    logger.warning("SideSwap: dropped malformed message: %r", msg[:200])
                    continue
                msg_id = data.get("id")
                if msg_id is None:
                    # Notification
                    await self._notifications.put(data)
                    continue
                fut = self._pending.pop(msg_id, None)
                if fut is None or fut.done():
                    continue
                if "error" in data:
                    err = data["error"] or {}
                    fut.set_exception(
                        SideSwapWSError(
                            f"SideSwap RPC error ({err.get('code')}): {err.get('message')}"
                        )
                    )
                else:
                    fut.set_result(data.get("result"))
        except asyncio.CancelledError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(SideSwapWSError(f"WS reader failed: {e}"))
            self._pending.clear()

    async def call(self, method: str, params: Any = None, *, timeout: float = WS_TIMEOUT_SECONDS) -> Any:
        """Send a JSON-RPC request and await the matching response."""
        if self._ws is None:
            raise SideSwapWSError("WebSocket is not connected")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[request_id] = fut
        payload = {"id": request_id, "method": method, "params": params}
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(request_id, None)
            raise SideSwapWSError(f"SideSwap RPC '{method}' timed out after {timeout}s") from e

    async def next_notification(
        self, method: Optional[str] = None, *, timeout: float = WS_TIMEOUT_SECONDS
    ) -> dict:
        """Wait for the next notification, optionally filtered by `method`.

        Notifications that don't match are dropped. For multi-stream consumers,
        write a custom reader; this helper assumes one subscription at a time.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise SideSwapWSError(
                    f"Timed out waiting for notification (method={method!r})"
                )
            data = await asyncio.wait_for(self._notifications.get(), timeout=remaining)
            if method is None or data.get("method") == method:
                return data

    # -- High-level method wrappers ------------------------------------------------

    async def login_client(self) -> dict:
        return await self.call(
            "login_client",
            {
                "api_key": None,
                "cookie": None,
                "user_agent": USER_AGENT,
                "version": PROTOCOL_VERSION,
            },
        )

    async def server_status(self) -> dict:
        return await self.call("server_status", None)

    async def peg_fee(self, send_amount: int, peg_in: bool) -> dict:
        return await self.call("peg_fee", {"send_amount": send_amount, "peg_in": peg_in})

    async def peg(self, recv_addr: str, peg_in: bool) -> dict:
        return await self.call("peg", {"recv_addr": recv_addr, "peg_in": peg_in})

    async def peg_status(self, order_id: str, peg_in: bool) -> dict:
        return await self.call("peg_status", {"order_id": order_id, "peg_in": peg_in})

    async def assets(self, embedded_icons: bool = False) -> dict:
        return await self.call("assets", {"all_assets": True, "embedded_icons": embedded_icons})

    async def subscribe_price_stream(
        self,
        asset: str,
        send_bitcoins: bool,
        send_amount: Optional[int] = None,
        recv_amount: Optional[int] = None,
    ) -> dict:
        params: dict[str, Any] = {"asset": asset, "send_bitcoins": send_bitcoins}
        if send_amount is not None:
            params["send_amount"] = send_amount
        if recv_amount is not None:
            params["recv_amount"] = recv_amount
        return await self.call("subscribe_price_stream", params)

    async def unsubscribe_price_stream(self, asset: str) -> dict:
        return await self.call("unsubscribe_price_stream", {"asset": asset})


# ---------------------------------------------------------------------------
# Sync wrappers — internal asyncio.run() so existing sync tool code can call.
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine from sync code, raising a clean error if a loop
    is already running (e.g. inside the MCP server's async dispatch).

    We call this from the synchronous tool functions; the MCP `call_tool`
    handler awaits the tool result inside an asyncio loop, but the tool
    function itself is invoked synchronously, so `asyncio.run` is safe.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # If we're already in a loop, use a separate loop in a new thread to avoid
    # deadlocking on the running loop. This is the case under pytest-asyncio
    # auto mode and may apply to some MCP transports.
    import threading

    result_box: dict[str, Any] = {}
    exc_box: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result_box["result"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            exc_box["exc"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "exc" in exc_box:
        raise exc_box["exc"]
    return result_box["result"]


def fetch_server_status(network: str = "mainnet") -> SideSwapServerStatus:
    """Connect, log in, fetch server_status, return a typed snapshot."""

    async def _go() -> SideSwapServerStatus:
        async with SideSwapWSClient(network) as client:
            await client.login_client()
            data = await client.server_status()
            return _parse_server_status(data or {})

    return _run(_go())


def _parse_server_status(data: dict) -> SideSwapServerStatus:
    return SideSwapServerStatus(
        elements_fee_rate=data.get("elements_fee_rate"),
        min_peg_in_amount=data.get("min_peg_in_amount"),
        min_peg_out_amount=data.get("min_peg_out_amount"),
        server_fee_percent_peg_in=data.get("server_fee_percent_peg_in"),
        server_fee_percent_peg_out=data.get("server_fee_percent_peg_out"),
        peg_in_wallet_balance=data.get("PegInWalletBalance"),
        peg_out_wallet_balance=data.get("PegOutWalletBalance"),
    )


def fetch_peg_fee(amount: int, peg_in: bool, network: str = "mainnet") -> dict:
    """Quote fee for a peg, returning {send_amount, recv_amount, fee_amount}."""
    if amount <= 0:
        raise ValueError("amount must be positive")

    async def _go() -> dict:
        async with SideSwapWSClient(network) as client:
            await client.login_client()
            resp = await client.peg_fee(amount, peg_in)
            recv = resp.get("recv_amount") if resp else None
            return {
                "send_amount": amount,
                "recv_amount": recv,
                "fee_amount": (amount - recv) if isinstance(recv, int) else None,
                "peg_in": peg_in,
            }

    return _run(_go())


def fetch_assets(network: str = "mainnet") -> list[SideSwapAsset]:
    """Fetch the SideSwap-supported asset list."""

    async def _go() -> list[SideSwapAsset]:
        async with SideSwapWSClient(network) as client:
            await client.login_client()
            resp = await client.assets()
            raw = (resp or {}).get("assets", []) or []
            out: list[SideSwapAsset] = []
            for a in raw:
                out.append(
                    SideSwapAsset(
                        asset_id=a.get("asset_id", ""),
                        ticker=a.get("ticker", ""),
                        name=a.get("name", ""),
                        precision=a.get("precision", 8),
                        instant_swaps=bool(a.get("instant_swaps", False)),
                        icon_url=a.get("icon_url"),
                    )
                )
            return out

    return _run(_go())


def fetch_swap_quote(
    asset_id: str,
    send_amount: Optional[int] = None,
    recv_amount: Optional[int] = None,
    send_bitcoins: bool = True,
    network: str = "mainnet",
    quote_wait_seconds: float = QUOTE_WAIT_SECONDS,
) -> SideSwapPriceQuote:
    """Get a one-shot price quote for a Liquid asset swap.

    Subscribes to the price stream, waits for the first `update_price_stream`
    notification (or uses the immediate `subscribe_price_stream` response if it
    contains a price), unsubscribes, and returns the snapshot.

    Args:
        asset_id: Liquid asset to swap with L-BTC.
        send_amount: Amount of L-BTC (if `send_bitcoins`) or asset to send. One of
            `send_amount` or `recv_amount` is required.
        recv_amount: Amount of asset (if `send_bitcoins`) or L-BTC to receive.
        send_bitcoins: True if the user is sending L-BTC and receiving the asset.
        network: "mainnet" or "testnet".
        quote_wait_seconds: How long to wait for the first quote notification.
    """
    if (send_amount is None) == (recv_amount is None):
        raise ValueError("exactly one of send_amount or recv_amount must be provided")

    async def _go() -> SideSwapPriceQuote:
        async with SideSwapWSClient(network) as client:
            await client.login_client()
            initial = await client.subscribe_price_stream(
                asset=asset_id,
                send_bitcoins=send_bitcoins,
                send_amount=send_amount,
                recv_amount=recv_amount,
            )
            quote_data = initial or {}
            # First subscribe response often contains the quote already; if not,
            # wait for the streamed notification.
            if not quote_data.get("price"):
                try:
                    notif = await client.next_notification(
                        "update_price_stream", timeout=quote_wait_seconds
                    )
                    quote_data = (notif or {}).get("params") or {}
                except SideSwapWSError:
                    pass
            try:
                await client.unsubscribe_price_stream(asset_id)
            except Exception:
                pass
            return SideSwapPriceQuote(
                asset_id=asset_id,
                send_bitcoins=send_bitcoins,
                send_amount=quote_data.get("send_amount") or send_amount or 0,
                recv_amount=quote_data.get("recv_amount") or recv_amount or 0,
                price=float(quote_data.get("price") or 0.0),
                fixed_fee=int(quote_data.get("fixed_fee") or 0),
                error_msg=quote_data.get("error_msg"),
            )

    return _run(_go())


# ---------------------------------------------------------------------------
# Peg manager — orchestrates peg-in / peg-out using existing wallet manager.
# ---------------------------------------------------------------------------


def map_peg_status(tx_state: Optional[str], list_empty: bool) -> str:
    """Map SideSwap PegStatus.list[*].tx_state to local lifecycle status."""
    if list_empty:
        return "pending"
    return {
        "Detected": "processing",
        "Processing": "processing",
        "Done": "completed",
        "InsufficientAmount": "failed",
    }.get(tx_state or "", "pending")


class SideSwapPegManager:
    """High-level peg orchestration tied to AQUA's storage + wallet managers.

    Exposes:

    - `get_server_status()` for fee/min/balance info (drives recommendation logic)
    - `quote_peg_in(amount)` / `quote_peg_out(amount)` for a fee preview
    - `peg_in(wallet_name)` to start a BTC→L-BTC peg (returns deposit address)
    - `peg_out(wallet_name, amount, btc_address, password)` to start a peg-out
      and broadcast the L-BTC send to the deposit address
    - `peg_status(order_id, peg_in)` to poll status
    """

    def __init__(self, storage, wallet_manager, btc_wallet_manager) -> None:
        """
        Args:
            storage: Storage instance with `save_sideswap_peg`, `load_sideswap_peg`, etc.
            wallet_manager: WalletManager (Liquid/LWK) — used for peg-in receive
                addresses and peg-out send.
            btc_wallet_manager: BitcoinWalletManager (BDK) — used to optionally
                fund a peg-in directly from the user's local Bitcoin wallet.
        """
        self.storage = storage
        self.wallet_manager = wallet_manager
        self.btc_wallet_manager = btc_wallet_manager

    # -- Read-only helpers ----------------------------------------------------

    def get_server_status(self, network: str = "mainnet") -> dict:
        try:
            status = fetch_server_status(network)
            return status.to_dict()
        except Exception as e:
            logger.warning("SideSwap server_status fetch failed: %s", e)
            return {
                "min_peg_in_amount": FALLBACK_MIN_PEG_IN_SATS,
                "min_peg_out_amount": FALLBACK_MIN_PEG_OUT_SATS,
                "server_fee_percent_peg_in": FALLBACK_PEG_IN_FEE_PERCENT,
                "server_fee_percent_peg_out": FALLBACK_PEG_OUT_FEE_PERCENT,
                "warning": f"Could not reach SideSwap; showing fallback values: {e}",
            }

    def quote_peg(self, amount: int, peg_in: bool, network: str = "mainnet") -> dict:
        return fetch_peg_fee(amount, peg_in, network)

    # -- Peg-in (BTC → L-BTC) -------------------------------------------------

    def peg_in(
        self,
        wallet_name: str = "default",
        password: Optional[str] = None,
    ) -> SideSwapPeg:
        """Initiate a peg-in. Returns the SideSwapPeg with `peg_addr` (BTC) where
        the user must send funds. The user's Liquid wallet receives L-BTC after
        ~2 BTC confirmations (~20 min, hot-wallet path) or up to 102 confs
        (~17 hours, cold-wallet path) depending on hot-wallet liquidity.

        We do NOT broadcast the BTC send here. The caller (or agent) must send
        the BTC to `peg_addr` from any Bitcoin wallet (including the local
        `btc_send` tool).
        """
        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        if wallet_data.watch_only:
            raise ValueError(
                "Watch-only wallet cannot receive a peg-in (no Liquid receive address)"
            )
        # Decrypt mnemonic if needed; not strictly required to receive but matches
        # the precondition pattern used by other flows.
        if wallet_data.encrypted_mnemonic and self.storage.is_mnemonic_encrypted(
            wallet_data.encrypted_mnemonic
        ):
            if password:
                self.wallet_manager.load_wallet(wallet_name, password)

        addr = self.wallet_manager.get_address(wallet_name)
        recv_addr = addr.address

        async def _go() -> dict:
            async with SideSwapWSClient(wallet_data.network) as client:
                await client.login_client()
                return await client.peg(recv_addr=recv_addr, peg_in=True)

        resp = _run(_go())
        if not resp or not resp.get("order_id") or not resp.get("peg_addr"):
            raise SideSwapWSError(f"Unexpected peg response: {resp!r}")

        peg = SideSwapPeg(
            order_id=resp["order_id"],
            peg_in=True,
            peg_addr=resp["peg_addr"],
            recv_addr=recv_addr,
            amount=None,  # peg-in: user picks the amount when sending BTC
            expected_recv=resp.get("recv_amount"),
            wallet_name=wallet_name,
            network=wallet_data.network,
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
            expires_at=resp.get("expires_at"),
        )
        self.storage.save_sideswap_peg(peg)
        return peg

    # -- Peg-out (L-BTC → BTC) ------------------------------------------------

    def peg_out(
        self,
        wallet_name: str,
        amount: int,
        btc_address: str,
        password: Optional[str] = None,
    ) -> SideSwapPeg:
        """Initiate a peg-out and broadcast the L-BTC send to the deposit address.

        The flow:
          1. Fetch SideSwap server_status for min_peg_out_amount and validate.
          2. WS `peg(peg_in=False, recv_addr=<user BTC addr>)` → returns a Liquid
             deposit address (`peg_addr`).
          3. Send `amount` sats of L-BTC from the wallet to `peg_addr`.
          4. Persist the peg with `lockup_txid` populated; status="processing".
        """
        if amount <= 0:
            raise ValueError("amount must be positive")
        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        if wallet_data.watch_only:
            raise ValueError("Watch-only wallet cannot peg out (cannot sign)")
        if wallet_data.encrypted_mnemonic and self.storage.is_mnemonic_encrypted(
            wallet_data.encrypted_mnemonic
        ):
            if not password:
                raise ValueError("Password required to decrypt mnemonic")

        # Validate min/max against server
        try:
            status = fetch_server_status(wallet_data.network)
            min_amt = status.min_peg_out_amount or FALLBACK_MIN_PEG_OUT_SATS
            if amount < min_amt:
                raise ValueError(
                    f"Amount {amount} sats is below SideSwap peg-out minimum ({min_amt} sats)"
                )
        except SideSwapWSError as e:
            logger.warning("Skipping min-amount check: %s", e)

        # Balance check (best-effort)
        try:
            balances = self.wallet_manager.get_balance(wallet_name)
            lbtc_balance = next((b.amount for b in balances if b.ticker == "L-BTC"), 0)
            if lbtc_balance < amount:
                raise ValueError(
                    f"Insufficient L-BTC: have {lbtc_balance} sats, need at least {amount} sats"
                )
        except ValueError:
            raise
        except Exception as e:  # pragma: no cover - balance fetch best-effort
            logger.warning("Balance check skipped: %s", e)

        async def _start() -> dict:
            async with SideSwapWSClient(wallet_data.network) as client:
                await client.login_client()
                return await client.peg(recv_addr=btc_address, peg_in=False)

        resp = _run(_start())
        if not resp or not resp.get("order_id") or not resp.get("peg_addr"):
            raise SideSwapWSError(f"Unexpected peg response: {resp!r}")

        peg = SideSwapPeg(
            order_id=resp["order_id"],
            peg_in=False,
            peg_addr=resp["peg_addr"],
            recv_addr=btc_address,
            amount=amount,
            expected_recv=resp.get("recv_amount"),
            wallet_name=wallet_name,
            network=wallet_data.network,
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
            expires_at=resp.get("expires_at"),
        )
        # Persist before broadcast so the order survives a crash mid-broadcast.
        self.storage.save_sideswap_peg(peg)

        # Broadcast L-BTC to the SideSwap deposit address.
        try:
            lockup_txid = self.wallet_manager.send(
                wallet_name, peg.peg_addr, amount, password=password
            )
        except Exception as e:
            peg.status = "failed"
            peg.tx_state = "InsufficientAmount" if "insufficient" in str(e).lower() else None
            self.storage.save_sideswap_peg(peg)
            raise

        peg.lockup_txid = lockup_txid
        peg.status = "processing"
        self.storage.save_sideswap_peg(peg)
        return peg

    # -- Status polling -------------------------------------------------------

    def status(self, order_id: str) -> dict:
        peg = self.storage.load_sideswap_peg(order_id)
        if not peg:
            raise ValueError(f"SideSwap peg not found: {order_id}")

        warning = None
        try:

            async def _go() -> dict:
                async with SideSwapWSClient(peg.network) as client:
                    await client.login_client()
                    return await client.peg_status(order_id, peg.peg_in)

            resp = _run(_go())
            txns = (resp or {}).get("list") or []
            list_empty = len(txns) == 0
            tx_state = txns[-1].get("tx_state") if txns else None
            new_status = map_peg_status(tx_state, list_empty)
            peg.status = new_status
            peg.tx_state = tx_state
            if txns:
                last = txns[-1]
                peg.detected_confs = last.get("detected_confs")
                peg.total_confs = last.get("total_confs")
                payout = last.get("payout_txid")
                if payout:
                    peg.payout_txid = payout
            peg.last_checked_at = datetime.now(UTC).isoformat()
            self.storage.save_sideswap_peg(peg)
        except Exception as e:
            warning = f"Could not refresh status: {e}"

        result = {
            "order_id": peg.order_id,
            "peg_in": peg.peg_in,
            "status": peg.status,
            "amount": peg.amount,
            "expected_recv": peg.expected_recv,
            "wallet_name": peg.wallet_name,
            "network": peg.network,
            "peg_addr": peg.peg_addr,
            "recv_addr": peg.recv_addr,
            "created_at": peg.created_at,
        }
        if peg.tx_state is not None:
            result["tx_state"] = peg.tx_state
        if peg.detected_confs is not None and peg.total_confs is not None:
            result["confirmations"] = f"{peg.detected_confs}/{peg.total_confs}"
        if peg.lockup_txid:
            result["lockup_txid"] = peg.lockup_txid
        if peg.payout_txid:
            result["payout_txid"] = peg.payout_txid
        if peg.expires_at:
            result["expires_at"] = peg.expires_at
        if warning:
            result["warning"] = warning
        return result


# ---------------------------------------------------------------------------
# Recommendation logic — used by tools and prompts.
# ---------------------------------------------------------------------------


def recommend_peg_or_swap(
    amount_sats: int,
    direction: str,
    server_status: Optional[dict] = None,
) -> dict:
    """Decide whether to recommend a peg or a swap-market trade for a BTC↔L-BTC conversion.

    Args:
        amount_sats: Amount the user wants to convert (sats).
        direction: "btc_to_lbtc" | "lbtc_to_btc".
        server_status: Optional dict returned from `fetch_server_status` to honor
            the live `peg_in_wallet_balance` (warns about 102-conf path).

    Returns:
        {
          "recommendation": "peg" | "swap" | "either",
          "reason": <human-readable explanation>,
          "peg_pros": [...],
          "peg_cons": [...],
        }
    """
    if direction not in ("btc_to_lbtc", "lbtc_to_btc"):
        raise ValueError("direction must be 'btc_to_lbtc' or 'lbtc_to_btc'")

    peg_pros = [
        "Lower fee (~0.1% via SideSwap peg vs ~0.2% via swap markets).",
        "No order-book matching delay; deterministic flow.",
    ]
    peg_cons = [
        "Slower than an instant swap (peg-in: usually 20–40 min for 2 BTC confs; "
        "peg-out: usually 15–60 min after 2 Liquid confs).",
        "Below SideSwap's per-direction minimum, peg is unavailable.",
    ]

    if direction == "lbtc_to_btc":
        # Peg-out is the canonical L-BTC → BTC path; recommend it whenever the
        # amount is above the min and the user can wait ~30–60 min.
        return {
            "recommendation": "peg",
            "reason": (
                "Peg-out is the standard SideSwap path for L-BTC → BTC. "
                "Fee is 0.1% + Bitcoin network fee; settlement is usually "
                "15–60 minutes (waits for 2 Liquid confs, then federation "
                "releases BTC). Swap-market liquidity for L-BTC → BTC is "
                "typically shallow."
            ),
            "peg_pros": peg_pros,
            "peg_cons": peg_cons,
        }

    # btc_to_lbtc
    if amount_sats >= PEG_RECOMMENDATION_THRESHOLD_SATS:
        # Check hot-wallet capacity if we have it.
        hot_wallet = (server_status or {}).get("peg_in_wallet_balance")
        large_warning = ""
        if isinstance(hot_wallet, int) and amount_sats > hot_wallet:
            large_warning = (
                " ⚠️ This amount exceeds SideSwap's hot-wallet liquidity, so the "
                "peg will use the cold-wallet path (102 BTC confirmations, ~17 hours). "
                "If the wait is too long, consider splitting into smaller amounts or "
                "using a swap-market trade for the urgent portion."
            )
        return {
            "recommendation": "peg",
            "reason": (
                f"For amounts at or above {PEG_RECOMMENDATION_THRESHOLD_SATS:,} sats, "
                "peg-in is usually the cheaper option (0.1% vs 0.2%) and the "
                "20–40 minute settlement is typically acceptable." + large_warning
            ),
            "peg_pros": peg_pros,
            "peg_cons": peg_cons,
        }
    return {
        "recommendation": "either",
        "reason": (
            f"Amount is below {PEG_RECOMMENDATION_THRESHOLD_SATS:,} sats. The peg-in "
            "fee saving (0.1% vs 0.2%) is small here; if you want it instantly, an "
            "atomic swap on SideSwap's market is fine. If you don't mind waiting "
            "~20–40 min, peg-in is still slightly cheaper."
        ),
        "peg_pros": peg_pros,
        "peg_cons": peg_cons,
    }
