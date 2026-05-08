"""Tests for SideSwap integration (peg + asset swap quoting).

The WebSocket client is exercised via a fake `SideSwapWSClient` that records
calls and returns canned responses, avoiding a real network connection. Storage
and recommendation logic are tested directly.
"""

import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from aqua.sideswap import (
    PEG_RECOMMENDATION_THRESHOLD_SATS,
    SideSwapPeg,
    SideSwapPegManager,
    SideSwapPriceQuote,
    SideSwapServerStatus,
    map_peg_status,
    recommend_peg_or_swap,
)
from aqua.storage import Storage


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeWSClient:
    """Stand-in for SideSwapWSClient — records calls, returns canned responses.

    Use class attribute ``responses`` (mapping method name -> result/exception)
    to script behavior per test. Use ``responses_seq`` for queued responses
    when the same method is called multiple times with different results.
    """

    responses: dict[str, Any] = {}
    responses_seq: dict[str, list[Any]] = {}
    calls: list[tuple[str, dict | None]] = []

    def __init__(self, network: str = "mainnet") -> None:
        self.network = network

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def call(self, method: str, params=None, *, timeout=30.0):  # noqa: ARG002
        FakeWSClient.calls.append((method, params))
        if method in FakeWSClient.responses_seq and FakeWSClient.responses_seq[method]:
            value = FakeWSClient.responses_seq[method].pop(0)
        else:
            value = FakeWSClient.responses.get(method)
        if isinstance(value, Exception):
            raise value
        return value

    async def login_client(self):
        return await self.call("login_client", {})

    async def server_status(self):
        return await self.call("server_status", None)

    async def peg_fee(self, send_amount, peg_in):
        return await self.call("peg_fee", {"send_amount": send_amount, "peg_in": peg_in})

    async def peg(self, recv_addr, peg_in):
        return await self.call("peg", {"recv_addr": recv_addr, "peg_in": peg_in})

    async def peg_status(self, order_id, peg_in):
        return await self.call("peg_status", {"order_id": order_id, "peg_in": peg_in})

    async def assets(self, embedded_icons=False):
        return await self.call("assets", {"all_assets": True, "embedded_icons": embedded_icons})

    async def subscribe_price_stream(self, asset, send_bitcoins, send_amount=None, recv_amount=None):
        params = {"asset": asset, "send_bitcoins": send_bitcoins}
        if send_amount is not None:
            params["send_amount"] = send_amount
        if recv_amount is not None:
            params["recv_amount"] = recv_amount
        return await self.call("subscribe_price_stream", params)

    async def unsubscribe_price_stream(self, asset):
        return await self.call("unsubscribe_price_stream", {"asset": asset})

    async def next_notification(self, method=None, *, timeout=30.0):  # noqa: ARG002
        FakeWSClient.calls.append(("notification", {"method": method}))
        notif = FakeWSClient.responses.get("__notification__")
        if isinstance(notif, Exception):
            raise notif
        return notif


@pytest.fixture(autouse=True)
def _reset_fake_ws():
    FakeWSClient.responses = {}
    FakeWSClient.responses_seq = {}
    FakeWSClient.calls = []
    yield
    FakeWSClient.responses = {}
    FakeWSClient.responses_seq = {}
    FakeWSClient.calls = []


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Storage(Path(tmpdir))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestMapPegStatus:
    def test_empty_list_is_pending(self):
        assert map_peg_status(None, list_empty=True) == "pending"

    @pytest.mark.parametrize("state, expected", [
        ("Detected", "processing"),
        ("Processing", "processing"),
        ("Done", "completed"),
        ("InsufficientAmount", "failed"),
        ("Unknown", "pending"),
        (None, "pending"),
    ])
    def test_state_mapping(self, state, expected):
        assert map_peg_status(state, list_empty=False) == expected


class TestRecommendPegOrSwap:
    def test_lbtc_to_btc_always_recommends_peg(self):
        rec = recommend_peg_or_swap(50_000, "lbtc_to_btc")
        assert rec["recommendation"] == "peg"
        assert "peg-out" in rec["reason"].lower()

    def test_btc_to_lbtc_above_threshold_recommends_peg(self):
        rec = recommend_peg_or_swap(PEG_RECOMMENDATION_THRESHOLD_SATS, "btc_to_lbtc")
        assert rec["recommendation"] == "peg"

    def test_btc_to_lbtc_below_threshold_returns_either(self):
        rec = recommend_peg_or_swap(PEG_RECOMMENDATION_THRESHOLD_SATS - 1, "btc_to_lbtc")
        assert rec["recommendation"] == "either"

    def test_warns_when_amount_exceeds_hot_wallet(self):
        rec = recommend_peg_or_swap(
            10_000_000_000,
            "btc_to_lbtc",
            server_status={"peg_in_wallet_balance": 100_000_000},
        )
        assert rec["recommendation"] == "peg"
        assert "cold-wallet" in rec["reason"]
        assert "102" in rec["reason"]

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError):
            recommend_peg_or_swap(1000, "nope")


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


class TestPegStorage:
    def _make_peg(self, **overrides) -> SideSwapPeg:
        defaults = {
            "order_id": "abc123",
            "peg_in": True,
            "peg_addr": "bc1qpegtarget",
            "recv_addr": "lq1qrecv",
            "amount": None,
            "expected_recv": 99_900,
            "wallet_name": "default",
            "network": "mainnet",
            "status": "pending",
            "created_at": "2026-05-07T12:00:00+00:00",
        }
        defaults.update(overrides)
        return SideSwapPeg(**defaults)

    def test_save_and_load_roundtrip(self, storage):
        peg = self._make_peg(lockup_txid="dead" * 16, payout_txid="beef" * 16)
        storage.save_sideswap_peg(peg)
        loaded = storage.load_sideswap_peg("abc123")
        assert loaded is not None
        assert loaded == peg

    def test_load_missing_returns_none(self, storage):
        assert storage.load_sideswap_peg("doesnotexist") is None

    def test_list_pegs(self, storage):
        storage.save_sideswap_peg(self._make_peg(order_id="aaa"))
        storage.save_sideswap_peg(self._make_peg(order_id="bbb", peg_in=False))
        ids = storage.list_sideswap_pegs()
        assert set(ids) == {"aaa", "bbb"}

    def test_invalid_order_id_rejected(self, storage):
        peg = self._make_peg(order_id="../escape")
        with pytest.raises(ValueError, match="Invalid SideSwap order ID"):
            storage.save_sideswap_peg(peg)

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX file permissions not enforced on Windows",
    )
    def test_file_permissions_0600(self, storage):
        import os

        peg = self._make_peg()
        storage.save_sideswap_peg(peg)
        path = storage.sideswap_pegs_dir / "abc123.json"
        assert path.exists()
        assert (os.stat(path).st_mode & 0o777) == 0o600

    def test_from_dict_backward_compat(self):
        # Older record without the optional fields should still load
        data = {
            "order_id": "old1",
            "peg_in": True,
            "peg_addr": "bc1q",
            "recv_addr": "lq1q",
            "amount": None,
            "expected_recv": None,
            "wallet_name": "w",
            "network": "mainnet",
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        peg = SideSwapPeg.from_dict(data)
        assert peg.lockup_txid is None
        assert peg.payout_txid is None
        assert peg.detected_confs is None


# ---------------------------------------------------------------------------
# Manager (with mocked WS client)
# ---------------------------------------------------------------------------


@pytest.fixture
def manager_setup(storage):
    """Build a SideSwapPegManager with mocked wallet manager and mocked WS client.

    The wallet_manager fake supports the methods the manager calls:
    `get_address`, `get_balance`, `send`, `load_wallet`. The storage fixture
    holds a real WalletData so wallet existence checks pass.
    """
    from aqua.storage import WalletData
    from aqua.wallet import Address, Balance

    wallet = WalletData(
        name="default",
        network="mainnet",
        descriptor="ct(slip77(deadbeef),elwpkh([fp/84'/1776'/0']tpubD.../0/*))",
        encrypted_mnemonic=None,
    )
    storage.save_wallet(wallet)

    class FakeWalletManager:
        def __init__(self):
            self.sent: list[tuple[str, str, int, str | None]] = []
            self.balance_lbtc = 1_000_000

        def get_address(self, name, index=None):  # noqa: ARG002
            return Address(address="lq1qreceiveaddr", index=0)

        def get_balance(self, name):  # noqa: ARG002
            return [
                Balance(
                    asset_id="6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d",
                    asset_name="Liquid Bitcoin",
                    ticker="L-BTC",
                    amount=self.balance_lbtc,
                    precision=8,
                )
            ]

        def send(self, name, address, amount, password=None):  # noqa: ARG002
            self.sent.append((name, address, amount, password))
            return "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

        def load_wallet(self, name, password=None):  # noqa: ARG002
            return wallet

    wm = FakeWalletManager()
    btc = object()  # not exercised directly by these tests
    mgr = SideSwapPegManager(storage=storage, wallet_manager=wm, btc_wallet_manager=btc)
    return mgr, wm, storage


@asynccontextmanager
async def _fake_ctx(*args, **kwargs):  # noqa: ARG001
    yield FakeWSClient()


def _patch_ws():
    """Patch the WS client used inside sideswap.py."""
    return patch("aqua.sideswap.SideSwapWSClient", FakeWSClient)


class TestServerStatus:
    def test_fetch_server_status_parses_fields(self, manager_setup):
        mgr, _, _ = manager_setup
        FakeWSClient.responses["server_status"] = {
            "elements_fee_rate": 0.1,
            "min_peg_in_amount": 1286,
            "min_peg_out_amount": 100000,
            "server_fee_percent_peg_in": 0.1,
            "server_fee_percent_peg_out": 0.1,
            "PegInWalletBalance": 50_000_000,
            "PegOutWalletBalance": 200_000_000,
        }
        with _patch_ws():
            status = mgr.get_server_status("mainnet")
        assert status["min_peg_in_amount"] == 1286
        assert status["server_fee_percent_peg_in"] == 0.1
        assert status["peg_in_wallet_balance"] == 50_000_000

    def test_falls_back_when_unreachable(self, manager_setup):
        mgr, _, _ = manager_setup
        FakeWSClient.responses["login_client"] = ConnectionError("nope")
        with _patch_ws():
            status = mgr.get_server_status("mainnet")
        assert status["min_peg_in_amount"] == 1286
        assert "warning" in status


class TestPegIn:
    def test_peg_in_returns_deposit_address(self, manager_setup):
        mgr, _, storage = manager_setup
        FakeWSClient.responses["peg"] = {
            "order_id": "order_aaa",
            "peg_addr": "bc1qsideswapdeposit",
            "expires_at": 1_900_000_000,
            "recv_amount": 99_900,
        }
        with _patch_ws():
            peg = mgr.peg_in(wallet_name="default")
        assert peg.peg_addr == "bc1qsideswapdeposit"
        assert peg.recv_addr == "lq1qreceiveaddr"
        assert peg.peg_in is True
        assert peg.status == "pending"
        # Was persisted
        loaded = storage.load_sideswap_peg("order_aaa")
        assert loaded is not None
        assert loaded.peg_addr == "bc1qsideswapdeposit"

    def test_peg_in_passes_recv_addr_to_server(self, manager_setup):
        mgr, _, _ = manager_setup
        FakeWSClient.responses["peg"] = {
            "order_id": "o1",
            "peg_addr": "bc1q",
        }
        with _patch_ws():
            mgr.peg_in()
        peg_call = next(c for c in FakeWSClient.calls if c[0] == "peg")
        assert peg_call[1]["peg_in"] is True
        assert peg_call[1]["recv_addr"] == "lq1qreceiveaddr"

    def test_peg_in_rejects_unknown_wallet(self, manager_setup):
        mgr, _, _ = manager_setup
        with pytest.raises(ValueError, match="not found"):
            mgr.peg_in(wallet_name="ghost")


class TestPegOut:
    def test_peg_out_broadcasts_lbtc_send(self, manager_setup):
        mgr, wm, storage = manager_setup
        FakeWSClient.responses["server_status"] = {"min_peg_out_amount": 100_000}
        FakeWSClient.responses["peg"] = {
            "order_id": "po_1",
            "peg_addr": "VJLdepositonliquid",
            "expires_at": 1_900_000_000,
            "recv_amount": 199_800,
        }
        with _patch_ws():
            peg = mgr.peg_out(
                wallet_name="default",
                amount=200_000,
                btc_address="bc1quserdest",
            )
        assert peg.lockup_txid is not None
        assert peg.status == "processing"
        assert wm.sent == [
            ("default", "VJLdepositonliquid", 200_000, None),
        ]
        # Persisted with lockup_txid
        loaded = storage.load_sideswap_peg("po_1")
        assert loaded.lockup_txid == peg.lockup_txid

    def test_peg_out_below_min_amount_rejected(self, manager_setup):
        mgr, _, _ = manager_setup
        FakeWSClient.responses["server_status"] = {"min_peg_out_amount": 100_000}
        with _patch_ws():
            with pytest.raises(ValueError, match="below SideSwap peg-out minimum"):
                mgr.peg_out(
                    wallet_name="default",
                    amount=50_000,
                    btc_address="bc1q",
                )

    def test_peg_out_insufficient_balance_rejected(self, manager_setup):
        mgr, wm, _ = manager_setup
        wm.balance_lbtc = 50_000
        FakeWSClient.responses["server_status"] = {"min_peg_out_amount": 100_000}
        with _patch_ws():
            with pytest.raises(ValueError, match="Insufficient L-BTC"):
                mgr.peg_out(
                    wallet_name="default",
                    amount=200_000,
                    btc_address="bc1q",
                )

    def test_peg_out_send_failure_marks_failed_and_persists(self, manager_setup):
        mgr, wm, storage = manager_setup
        FakeWSClient.responses["server_status"] = {"min_peg_out_amount": 100_000}
        FakeWSClient.responses["peg"] = {"order_id": "po_2", "peg_addr": "VJLdep"}

        def boom(*args, **kwargs):
            raise RuntimeError("broadcast failed")

        wm.send = boom  # type: ignore[assignment]
        with _patch_ws():
            with pytest.raises(RuntimeError, match="broadcast failed"):
                mgr.peg_out(wallet_name="default", amount=200_000, btc_address="bc1q")
        # Order persisted as failed for recovery
        loaded = storage.load_sideswap_peg("po_2")
        assert loaded is not None
        assert loaded.status == "failed"


class TestPegStatusPolling:
    def test_status_done_marks_completed(self, manager_setup):
        mgr, _, storage = manager_setup
        peg = SideSwapPeg(
            order_id="poll1",
            peg_in=True,
            peg_addr="bc1q",
            recv_addr="lq1q",
            amount=None,
            expected_recv=None,
            wallet_name="default",
            network="mainnet",
            status="processing",
            created_at="2026-05-07T12:00:00+00:00",
        )
        storage.save_sideswap_peg(peg)
        FakeWSClient.responses["peg_status"] = {
            "list": [
                {
                    "tx_state": "Done",
                    "detected_confs": None,
                    "total_confs": None,
                    "payout_txid": "payouttxid",
                }
            ]
        }
        with _patch_ws():
            result = mgr.status("poll1")
        assert result["status"] == "completed"
        assert result["payout_txid"] == "payouttxid"
        assert result["tx_state"] == "Done"

    def test_status_detected_includes_confirmations(self, manager_setup):
        mgr, _, storage = manager_setup
        peg = SideSwapPeg(
            order_id="poll2",
            peg_in=True,
            peg_addr="bc1q",
            recv_addr="lq1q",
            amount=None,
            expected_recv=None,
            wallet_name="default",
            network="mainnet",
            status="pending",
            created_at="2026-05-07T12:00:00+00:00",
        )
        storage.save_sideswap_peg(peg)
        FakeWSClient.responses["peg_status"] = {
            "list": [
                {
                    "tx_state": "Detected",
                    "detected_confs": 1,
                    "total_confs": 2,
                    "payout_txid": None,
                }
            ]
        }
        with _patch_ws():
            result = mgr.status("poll2")
        assert result["status"] == "processing"
        assert result["confirmations"] == "1/2"

    def test_status_unknown_order_raises(self, manager_setup):
        mgr, _, _ = manager_setup
        with pytest.raises(ValueError, match="not found"):
            mgr.status("missingid")

    def test_status_warns_when_remote_fetch_fails(self, manager_setup):
        mgr, _, storage = manager_setup
        peg = SideSwapPeg(
            order_id="poll3",
            peg_in=True,
            peg_addr="bc1q",
            recv_addr="lq1q",
            amount=None,
            expected_recv=None,
            wallet_name="default",
            network="mainnet",
            status="pending",
            created_at="2026-05-07T12:00:00+00:00",
        )
        storage.save_sideswap_peg(peg)
        FakeWSClient.responses["login_client"] = ConnectionError("offline")
        with _patch_ws():
            result = mgr.status("poll3")
        assert "warning" in result
        assert result["status"] == "pending"  # unchanged


# ---------------------------------------------------------------------------
# Asset listing & quote
# ---------------------------------------------------------------------------


class TestFetchAssets:
    def test_fetch_assets_parses_list(self):
        from aqua.sideswap import fetch_assets

        FakeWSClient.responses["assets"] = {
            "assets": [
                {
                    "asset_id": "abc",
                    "ticker": "USDt",
                    "name": "Tether",
                    "precision": 8,
                    "instant_swaps": True,
                    "icon_url": "https://example.com/usdt.png",
                },
                {"asset_id": "def", "ticker": "EURx", "name": "PEGx Euro", "precision": 8},
            ]
        }
        with _patch_ws():
            assets = fetch_assets("mainnet")
        assert len(assets) == 2
        assert assets[0].asset_id == "abc"
        assert assets[0].instant_swaps is True
        assert assets[1].instant_swaps is False  # default


class TestFetchSwapQuote:
    def test_fetch_swap_quote_returns_immediate_price_when_present(self):
        from aqua.sideswap import fetch_swap_quote

        FakeWSClient.responses["subscribe_price_stream"] = {
            "asset": "abc",
            "send_bitcoins": True,
            "send_amount": 100_000,
            "recv_amount": 9_500_000,
            "price": 95.0,
            "fixed_fee": 100,
        }
        FakeWSClient.responses["unsubscribe_price_stream"] = {}
        with _patch_ws():
            q = fetch_swap_quote(asset_id="abc", send_amount=100_000)
        assert isinstance(q, SideSwapPriceQuote)
        assert q.price == 95.0
        assert q.recv_amount == 9_500_000
        assert q.fixed_fee == 100

    def test_fetch_swap_quote_waits_for_notification_when_subscribe_empty(self):
        from aqua.sideswap import fetch_swap_quote

        FakeWSClient.responses["subscribe_price_stream"] = {
            "asset": "abc",
            "send_bitcoins": True,
        }
        FakeWSClient.responses["__notification__"] = {
            "method": "update_price_stream",
            "params": {
                "asset": "abc",
                "send_bitcoins": True,
                "send_amount": 100_000,
                "recv_amount": 9_500_000,
                "price": 95.0,
                "fixed_fee": 100,
            },
        }
        FakeWSClient.responses["unsubscribe_price_stream"] = {}
        with _patch_ws():
            q = fetch_swap_quote(asset_id="abc", send_amount=100_000)
        assert q.price == 95.0
        assert q.recv_amount == 9_500_000

    def test_fetch_swap_quote_requires_exactly_one_amount(self):
        from aqua.sideswap import fetch_swap_quote

        with pytest.raises(ValueError, match="exactly one"):
            fetch_swap_quote(asset_id="abc")
        with pytest.raises(ValueError, match="exactly one"):
            fetch_swap_quote(asset_id="abc", send_amount=1, recv_amount=2)


# ---------------------------------------------------------------------------
# WebSocket client (real implementation, mocked websocket)
# ---------------------------------------------------------------------------


class _FakeWSConnection:
    """Simulates a websockets.WebSocketClientProtocol."""

    def __init__(self, scripted_responses: list[str]):
        self._scripted = list(scripted_responses)
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self._closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)
        if self._scripted:
            await self._inbox.put(self._scripted.pop(0))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        return await self._inbox.get()

    async def close(self):
        self._closed = True


class TestSideSwapWSClient:
    """Exercises the real SideSwapWSClient with a stubbed websocket."""

    def test_call_round_trips_payload(self):
        import json as _json
        from aqua.sideswap import SideSwapWSClient

        async def go():
            response_payload = _json.dumps({
                "id": 1,
                "method": "server_status",
                "result": {"min_peg_in_amount": 1286},
            })
            fake = _FakeWSConnection([response_payload])

            async def fake_connect(*args, **kwargs):  # noqa: ARG001
                return fake

            with patch("websockets.connect", new=fake_connect):
                client = SideSwapWSClient("mainnet")
                await client.connect()
                try:
                    result = await client.call("server_status", None)
                finally:
                    await client.close()
            return fake.sent, result

        sent, result = asyncio.run(go())
        assert result == {"min_peg_in_amount": 1286}
        assert len(sent) == 1
        msg = __import__("json").loads(sent[0])
        assert msg["id"] == 1
        assert msg["method"] == "server_status"
        assert msg["params"] is None

    def test_call_propagates_rpc_error(self):
        import json as _json
        from aqua.sideswap import SideSwapWSClient, SideSwapWSError

        async def go():
            response_payload = _json.dumps({
                "id": 1,
                "error": {"code": -32602, "message": "Invalid params"},
            })
            fake = _FakeWSConnection([response_payload])

            async def fake_connect(*args, **kwargs):  # noqa: ARG001
                return fake

            with patch("websockets.connect", new=fake_connect):
                client = SideSwapWSClient("mainnet")
                await client.connect()
                try:
                    await client.call("peg", {})
                finally:
                    await client.close()

        with pytest.raises(SideSwapWSError, match="Invalid params"):
            asyncio.run(go())


# ---------------------------------------------------------------------------
# Server status fallback constants sanity check
# ---------------------------------------------------------------------------


class TestServerStatusDataclass:
    def test_to_dict_round_trip(self):
        s = SideSwapServerStatus(min_peg_in_amount=1286, min_peg_out_amount=100_000)
        d = s.to_dict()
        assert d["min_peg_in_amount"] == 1286
        assert d["server_fee_percent_peg_in"] is None
