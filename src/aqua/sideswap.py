"""SideSwap integration for BTC ↔ L-BTC pegs and Liquid asset swaps.

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

- `login_client`           — anonymous (api_key=None), identifies us as agentic-aqua
- `server_status`          — fees, min amounts, hot-wallet balances
- `peg_fee`                — quote fee for a given amount and direction
- `peg`                    — initiate peg-in (BTC→L-BTC) or peg-out (L-BTC→BTC)
- `peg_status`             — poll order status
- `assets`                 — list supported assets for swap quoting
- `subscribe_price_stream` / `unsubscribe_price_stream`
                           — get a price quote for a Liquid asset swap
- `start_swap_web`         — convert a quote into an order with `upload_url`
                             for the HTTP `swap_start` / `swap_sign` calls

Plus HTTP (POST to `upload_url` returned by `start_swap_web`):

- `swap_start`             — server returns the half-built PSET to sign
- `swap_sign`              — submit the locally-signed PSET; server broadcasts

PSET verification (security-critical): before signing, we call
`wollet.pset_details(pset).balance.balances()` and confirm the wallet's net
balance change matches the agreed quote (recv_asset gains exactly recv_amount,
send_asset loses no more than send_amount + fee_tolerance, no other assets
move). The server is trusted-but-verify; without this check, a hostile or
buggy server could craft a PSET that takes our funds and pays us nothing.

Execution (`SideSwapSwapManager.execute_swap`) currently supports only the
L-BTC → asset direction (`send_bitcoins=True`). The reverse direction needs
careful fee handling and is excluded; users should use the AQUA mobile app
or sideswap.io for asset → L-BTC swaps until that's implemented and audited.
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


@dataclass
class SideSwapSwap:
    """Persistent record of an executed Liquid asset swap on SideSwap."""

    order_id: str
    submit_id: Optional[str]  # Returned by swap_start; needed for swap_sign
    send_asset: str
    send_amount: int
    recv_asset: str
    recv_amount: int
    price: float
    wallet_name: str
    network: str  # "mainnet" | "testnet"
    status: str  # "pending" | "verified" | "signed" | "submitted" | "broadcast" | "failed"
    created_at: str
    txid: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SideSwapSwap":
        data = {**data}
        for f in ("submit_id", "txid", "last_error"):
            data.setdefault(f, None)
        return cls(**data)


# ---------------------------------------------------------------------------
# PSET verification — security-critical
# ---------------------------------------------------------------------------


class PsetVerificationError(RuntimeError):
    """Raised when the PSET returned by SideSwap does not match the agreed quote.

    On this exception the caller MUST NOT sign the PSET — the server may have
    crafted a transaction that takes our funds and pays us nothing.
    """


def verify_pset_balances(
    balances: dict[str, int],
    *,
    send_asset: str,
    send_amount: int,
    recv_asset: str,
    recv_amount: int,
    fee_tolerance_sats: int = 1_000,
    fee_asset: Optional[str] = None,
) -> None:
    """Verify a Liquid PSET's effect on the wallet matches the agreed quote.

    Pure function — operates only on the dict returned by
    `wollet.pset_details(pset).balance.balances()` (mapping asset_id → signed
    int sats; negative = wallet is sending, positive = wallet is receiving).

    Verification rules (any failure raises `PsetVerificationError`):

    1. The wallet must gain at least `recv_amount` of `recv_asset`. Strict
       equality is required — the server should not deliver a different amount
       than what it quoted.
    2. The wallet must lose **at most** `send_amount + fee_tolerance_sats` of
       `send_asset`. We allow a small overage to cover the network fee when it
       comes from the same asset (which is typical for L-BTC sends, since the
       Liquid network fee is denominated in L-BTC).
    3. No other asset may have a non-zero balance change. This blocks "extra
       output" attacks where the server siphons a bit of an unrelated asset.

    Args:
        balances: Net balance change per asset id (from LWK pset_details).
        send_asset: Asset id we agreed to send.
        send_amount: Amount we agreed to send (sats, positive).
        recv_asset: Asset id we agreed to receive.
        recv_amount: Amount we agreed to receive (sats, positive).
        fee_tolerance_sats: How many extra sats of `send_asset` we'll tolerate
            being deducted to cover the on-chain fee. Default 1000 — Liquid
            fees are in the tens of sats range, so this is comfortably above
            normal but well below an attacker payday.
        fee_asset: If set, only this asset is allowed to absorb the fee
            tolerance. If unset, defaults to `send_asset`.
    """
    if send_amount <= 0:
        raise ValueError("send_amount must be positive")
    if recv_amount <= 0:
        raise ValueError("recv_amount must be positive")
    if fee_tolerance_sats < 0:
        raise ValueError("fee_tolerance_sats must be non-negative")
    if send_asset == recv_asset:
        # SideSwap doesn't quote same-asset swaps and we can't reason about
        # net balances unambiguously if it did.
        raise PsetVerificationError(
            f"send_asset and recv_asset are the same ({send_asset!r}); refusing to sign"
        )
    fee_asset = fee_asset or send_asset

    # Rule 1: receive amount is exactly what was agreed
    recv_delta = balances.get(recv_asset, 0)
    if recv_delta != recv_amount:
        raise PsetVerificationError(
            f"PSET delivers {recv_delta} sats of recv_asset {recv_asset[:8]}…, "
            f"expected exactly {recv_amount} sats"
        )

    # Rule 2: send amount is within tolerance
    send_delta = balances.get(send_asset, 0)
    # send_delta is negative when we're sending. Convert to "sats sent" (positive).
    sats_sent = -send_delta
    if send_asset == fee_asset:
        max_sats_sent = send_amount + fee_tolerance_sats
    else:
        max_sats_sent = send_amount
    if sats_sent > max_sats_sent:
        raise PsetVerificationError(
            f"PSET deducts {sats_sent} sats of send_asset {send_asset[:8]}…, "
            f"more than the agreed {send_amount} (tolerance {max_sats_sent - send_amount})"
        )
    if sats_sent < send_amount:
        # Sending less than agreed is suspicious too — could be a bait-and-switch
        # where the server later reverses the swap or delivers a malformed tx.
        raise PsetVerificationError(
            f"PSET deducts only {sats_sent} sats of send_asset, less than agreed {send_amount}"
        )

    # Rule 3: no unexpected balance changes
    for asset, delta in balances.items():
        if asset in (send_asset, recv_asset):
            continue
        if delta != 0:
            raise PsetVerificationError(
                f"PSET unexpectedly moves asset {asset[:8]}… by {delta} sats; refusing to sign"
            )


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

    async def start_swap_web(
        self,
        asset: str,
        price: float,
        send_bitcoins: bool,
        send_amount: int,
        recv_amount: int,
    ) -> dict:
        """Convert an accepted price quote into an order. Returns an `upload_url`
        that the HTTP `swap_start` / `swap_sign` calls go to."""
        return await self.call(
            "start_swap_web",
            {
                "asset": asset,
                "price": price,
                "send_bitcoins": send_bitcoins,
                "send_amount": send_amount,
                "recv_amount": recv_amount,
            },
        )


# ---------------------------------------------------------------------------
# HTTP client for swap_start / swap_sign — POSTs JSON-RPC to upload_url
# ---------------------------------------------------------------------------


class SideSwapHTTPClient:
    """Synchronous JSON-RPC HTTP client for the SideSwap swap_start/swap_sign
    endpoint. Format mirrors `sideswap_api/src/http_rpc.rs` (snake_case method
    names; `params` for requests, `result` for responses)."""

    def __init__(self, upload_url: str) -> None:
        self.upload_url = upload_url

    def _post(self, method: str, params: dict, request_id: int = 1) -> dict:
        body = json.dumps({"id": request_id, "method": method, "params": params}).encode()
        req = urllib.request.Request(
            self.upload_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=WS_TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err_body = json.loads(e.read().decode())
                if "error" in err_body:
                    err = err_body["error"] or {}
                    detail = err.get("message") or str(err)
                else:
                    detail = err_body.get("message", "")
            except Exception:
                pass
            msg = f"SideSwap HTTP {method} error ({e.code})"
            if detail:
                msg += f": {detail}"
            raise SideSwapWSError(msg) from e
        except urllib.error.URLError as e:
            raise SideSwapWSError(f"SideSwap HTTP {method} unreachable: {e.reason}") from e

        if "error" in payload:
            err = payload["error"] or {}
            raise SideSwapWSError(
                f"SideSwap HTTP {method} error ({err.get('code')}): {err.get('message')}"
            )
        return payload.get("result") or {}

    def swap_start(
        self,
        order_id: str,
        inputs: list[dict],
        recv_addr: str,
        change_addr: str,
        send_asset: str,
        send_amount: int,
        recv_asset: str,
        recv_amount: int,
    ) -> dict:
        """Upload selected UTXOs + addresses; returns {submit_id, pset (b64)}."""
        return self._post(
            "swap_start",
            {
                "order_id": order_id,
                "inputs": inputs,
                "recv_addr": recv_addr,
                "change_addr": change_addr,
                "send_asset": send_asset,
                "send_amount": send_amount,
                "recv_asset": recv_asset,
                "recv_amount": recv_amount,
            },
        )

    def swap_sign(self, order_id: str, submit_id: str, signed_pset_b64: str) -> dict:
        """Submit our locally-signed PSET; returns {txid}."""
        return self._post(
            "swap_sign",
            {
                "order_id": order_id,
                "submit_id": submit_id,
                "pset": signed_pset_b64,
            },
        )


# ---------------------------------------------------------------------------
# UTXO selection — confidential, non-AMP, wpkh only, send_asset only
# ---------------------------------------------------------------------------


def select_swap_utxos(
    utxos: list,
    send_asset: str,
    send_amount: int,
) -> list[dict]:
    """Pick UTXOs of `send_asset` covering `send_amount`, formatted for SideSwap.

    Filters apply per `sideswap_lwk` reference (`sideswap_lwk/src/lib.rs`):
    - Must be confidential (asset_bf and value_bf both non-zero)
    - Must hold the requested send_asset
    - We don't filter by script type here because the wallet's descriptor is
      always wpkh (BIP84 m/84'/1776'/0') in agentic-aqua.

    Args:
        utxos: List of `lwk.WalletTxOut` (or compatible objects exposing
            `.outpoint`, `.unblinded` with `.asset`, `.value`, `.asset_bf`,
            `.value_bf`).
        send_asset: Asset id to send.
        send_amount: Total sats to cover.

    Returns:
        List of dicts in the SideSwap `Utxo` shape:
        {txid, vout, asset, asset_bf, value, value_bf, redeem_script: null}.

    Raises:
        ValueError if there isn't enough confidential balance to cover send_amount.
    """
    if send_amount <= 0:
        raise ValueError("send_amount must be positive")

    # Filter to confidential UTXOs of the right asset
    candidates = []
    for u in utxos:
        unblinded = u.unblinded()
        if str(unblinded.asset()) != send_asset:
            continue
        # asset_bf and value_bf are 32-byte hex; "0"*64 means non-confidential
        asset_bf = str(unblinded.asset_bf())
        value_bf = str(unblinded.value_bf())
        if asset_bf == "0" * 64 or value_bf == "0" * 64:
            continue
        candidates.append((u, unblinded))

    # Sort descending by value to minimise input count
    candidates.sort(key=lambda pair: pair[1].value(), reverse=True)

    selected: list[dict] = []
    accumulated = 0
    for u, unblinded in candidates:
        outpoint = u.outpoint()
        selected.append(
            {
                "txid": str(outpoint.txid()),
                "vout": int(outpoint.vout()),
                "asset": send_asset,
                "asset_bf": str(unblinded.asset_bf()),
                "value": int(unblinded.value()),
                "value_bf": str(unblinded.value_bf()),
                "redeem_script": None,
            }
        )
        accumulated += int(unblinded.value())
        if accumulated >= send_amount:
            return selected

    raise ValueError(
        f"Insufficient confidential balance for {send_asset[:8]}…: "
        f"have {accumulated} sats across {len(selected)} UTXOs, need {send_amount}"
    )


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
# Asset swap manager — the verify-then-sign-then-broadcast orchestrator.
# ---------------------------------------------------------------------------


# Reasonable upper bound for the network fee absorbed from send_asset when
# send_asset is L-BTC. Liquid fees are typically ~30-50 sats; 1000 is plenty
# of slack while still small enough to make a "siphon attack" obvious.
DEFAULT_FEE_TOLERANCE_SATS = 1_000


class SideSwapSwapManager:
    """Orchestrates a SideSwap atomic asset swap end-to-end.

    Flow (legacy `start_swap_web` + HTTP `swap_start`/`swap_sign`):

      1. Subscribe to a price stream and capture a quote (price + amounts)
      2. Send `start_swap_web` with the captured price → returns `upload_url`
      3. Select confidential UTXOs of `send_asset` covering `send_amount`
      4. POST `swap_start` with inputs + recv_addr + change_addr → returns PSET
      5. **Verify** the PSET with the wallet's `pset_details` against the quote.
         Aborts (raises `PsetVerificationError`) if the PSET doesn't match.
      6. Sign the PSET with `signer.sign(pset)`
      7. POST `swap_sign` with the signed PSET → returns `txid`
      8. Persist throughout; on broadcast, save `txid` and status="broadcast"
    """

    def __init__(self, storage, wallet_manager) -> None:
        self.storage = storage
        self.wallet_manager = wallet_manager

    def execute_swap(
        self,
        asset_id: str,
        send_amount: int,
        wallet_name: str = "default",
        password: Optional[str] = None,
        *,
        fee_tolerance_sats: int = DEFAULT_FEE_TOLERANCE_SATS,
        quote_wait_seconds: float = QUOTE_WAIT_SECONDS,
    ) -> "SideSwapSwap":
        """Execute a swap of L-BTC for `asset_id` (i.e. `send_bitcoins=True`).

        Only L-BTC → asset is supported in this version. The reverse direction
        (asset → L-BTC) needs more thought re: fee handling and is intentionally
        excluded; the user should be directed to the AQUA mobile app for that.

        Args:
            asset_id: Liquid asset id to receive (e.g. USDt).
            send_amount: L-BTC sats to send.
            wallet_name: Wallet to sign with.
            password: Mnemonic decryption password (if encrypted at rest).
            fee_tolerance_sats: Extra L-BTC sats we'll allow for the network
                fee. Default 1000 — Liquid fees are tens of sats.
            quote_wait_seconds: How long to wait for the streamed quote.
        """
        # Load wallet & validate signing capability
        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        if wallet_data.watch_only:
            raise ValueError("Watch-only wallet cannot sign a SideSwap swap")
        if wallet_data.encrypted_mnemonic and self.storage.is_mnemonic_encrypted(
            wallet_data.encrypted_mnemonic
        ):
            if not password:
                raise ValueError("Password required to decrypt mnemonic")
        if send_amount <= 0:
            raise ValueError("send_amount must be positive")

        network = wallet_data.network
        # Make sure the signer is loaded (wallet_manager.load_wallet caches it)
        self.wallet_manager.load_wallet(wallet_name, password)
        # Sync the wallet so utxos() reflects the current chain state
        self.wallet_manager.sync_wallet(wallet_name)

        send_asset = self.wallet_manager._get_policy_asset(network)
        if asset_id == send_asset:
            raise ValueError("Cannot swap L-BTC for L-BTC")

        async def _quote_and_start() -> tuple[dict, dict]:
            async with SideSwapWSClient(network) as client:
                await client.login_client()
                # Get the streamed price quote
                initial = await client.subscribe_price_stream(
                    asset=asset_id,
                    send_bitcoins=True,
                    send_amount=send_amount,
                )
                quote = initial or {}
                if not quote.get("price"):
                    notif = await client.next_notification(
                        "update_price_stream", timeout=quote_wait_seconds
                    )
                    quote = (notif or {}).get("params") or {}
                if not quote.get("price"):
                    raise SideSwapWSError(
                        "SideSwap did not return a usable price quote"
                    )
                if quote.get("error_msg"):
                    raise SideSwapWSError(f"SideSwap quote error: {quote['error_msg']}")
                # Accept the price and start the order
                start_resp = await client.start_swap_web(
                    asset=asset_id,
                    price=float(quote["price"]),
                    send_bitcoins=True,
                    send_amount=int(quote["send_amount"]),
                    recv_amount=int(quote["recv_amount"]),
                )
                try:
                    await client.unsubscribe_price_stream(asset_id)
                except Exception:
                    pass
                return quote, start_resp

        quote, start_resp = _run(_quote_and_start())

        order_id = start_resp.get("order_id")
        upload_url = start_resp.get("upload_url")
        recv_amount = int(start_resp.get("recv_amount") or quote.get("recv_amount"))
        if not order_id or not upload_url:
            raise SideSwapWSError(f"Unexpected start_swap_web response: {start_resp!r}")

        # Persist the in-progress swap before any HTTP work
        swap = SideSwapSwap(
            order_id=order_id,
            submit_id=None,
            send_asset=send_asset,
            send_amount=send_amount,
            recv_asset=asset_id,
            recv_amount=recv_amount,
            price=float(quote["price"]),
            wallet_name=wallet_name,
            network=network,
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
        )
        self.storage.save_sideswap_swap(swap)

        try:
            # Build the inputs/addresses for swap_start
            wollet = self.wallet_manager._get_wollet(wallet_name)
            inputs = select_swap_utxos(wollet.utxos(), send_asset, send_amount)
            recv_addr = str(wollet.address(None).address())
            change_addr = str(wollet.address(None).address())

            http = SideSwapHTTPClient(upload_url)
            start_payload = http.swap_start(
                order_id=order_id,
                inputs=inputs,
                recv_addr=recv_addr,
                change_addr=change_addr,
                send_asset=send_asset,
                send_amount=send_amount,
                recv_asset=asset_id,
                recv_amount=recv_amount,
            )
            submit_id = start_payload.get("submit_id")
            pset_b64 = start_payload.get("pset")
            if not submit_id or not pset_b64:
                raise SideSwapWSError(
                    f"Unexpected swap_start response: {start_payload!r}"
                )
            swap.submit_id = submit_id
            self.storage.save_sideswap_swap(swap)

            # Verify before signing — security-critical
            self._verify_pset(
                pset_b64,
                wollet,
                send_asset=send_asset,
                send_amount=send_amount,
                recv_asset=asset_id,
                recv_amount=recv_amount,
                fee_tolerance_sats=fee_tolerance_sats,
            )
            swap.status = "verified"
            self.storage.save_sideswap_swap(swap)

            # Sign locally
            signer = self.wallet_manager._signers[wallet_name]
            import lwk

            pset = lwk.Pset(pset_b64)
            signed = signer.sign(pset)
            signed_b64 = str(signed)
            swap.status = "signed"
            self.storage.save_sideswap_swap(swap)

            # Submit signed PSET; server merges & broadcasts
            sign_payload = http.swap_sign(
                order_id=order_id, submit_id=submit_id, signed_pset_b64=signed_b64
            )
            txid = sign_payload.get("txid")
            if not txid:
                raise SideSwapWSError(f"Unexpected swap_sign response: {sign_payload!r}")
            swap.txid = txid
            swap.status = "broadcast"
            self.storage.save_sideswap_swap(swap)
            return swap

        except PsetVerificationError as e:
            swap.status = "failed"
            swap.last_error = f"PSET verification failed: {e}"
            self.storage.save_sideswap_swap(swap)
            raise
        except Exception as e:
            swap.status = "failed"
            swap.last_error = str(e)
            self.storage.save_sideswap_swap(swap)
            raise

    def _verify_pset(
        self,
        pset_b64: str,
        wollet,
        *,
        send_asset: str,
        send_amount: int,
        recv_asset: str,
        recv_amount: int,
        fee_tolerance_sats: int,
    ) -> None:
        """Run the PSET balance check via LWK and raise on mismatch."""
        import lwk

        pset = lwk.Pset(pset_b64)
        details = wollet.pset_details(pset)
        balances_dict_raw = details.balance().balances()
        # LWK returns AssetId objects; normalise to hex strings keyed by asset id.
        balances: dict[str, int] = {str(asset): int(amount) for asset, amount in balances_dict_raw.items()}
        verify_pset_balances(
            balances,
            send_asset=send_asset,
            send_amount=send_amount,
            recv_asset=recv_asset,
            recv_amount=recv_amount,
            fee_tolerance_sats=fee_tolerance_sats,
        )

    def status(self, order_id: str) -> dict:
        """Return persisted swap status. Asset swaps are atomic — once
        `status="broadcast"` is set the txid is final on Liquid; agents check
        confirmations via `lw_tx_status`."""
        swap = self.storage.load_sideswap_swap(order_id)
        if not swap:
            raise ValueError(f"SideSwap swap not found: {order_id}")
        result = {
            "order_id": swap.order_id,
            "submit_id": swap.submit_id,
            "send_asset": swap.send_asset,
            "send_amount": swap.send_amount,
            "recv_asset": swap.recv_asset,
            "recv_amount": swap.recv_amount,
            "price": swap.price,
            "wallet_name": swap.wallet_name,
            "network": swap.network,
            "status": swap.status,
            "created_at": swap.created_at,
        }
        if swap.txid:
            result["txid"] = swap.txid
        if swap.last_error:
            result["last_error"] = swap.last_error
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
