"""Tests for SideSwap integration (peg + asset swap quoting + execution).

The WebSocket client is exercised via a fake `SideSwapWSClient` that records
calls and returns canned responses, avoiding a real network connection. Storage,
recommendation logic, and the PSET balance verifier are tested directly.
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
    PsetVerificationError,
    SideSwapPeg,
    SideSwapPegManager,
    SideSwapPriceQuote,
    SideSwapServerStatus,
    SideSwapSwap,
    map_peg_status,
    recommend_peg_or_swap,
    verify_pset_balances,
)
from aqua.storage import Storage


L_BTC = "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
USDT = "ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2"
EVIL = "deadbeef" * 8


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

    async def start_swap_web(self, asset, price, send_bitcoins, send_amount, recv_amount):
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


# ---------------------------------------------------------------------------
# PSET verifier — security-critical, tested with adversarial inputs
# ---------------------------------------------------------------------------


class TestVerifyPsetBalances:
    """Encodes the security contract for `verify_pset_balances`.

    This function is the only barrier between SideSwap's server and our
    `signer.sign(pset)` call. If it accepts a malicious balance dict, we sign
    a transaction that loses the user's funds. Each test below represents a
    real attack class.
    """

    # -- Happy path -----------------------------------------------------------

    def test_exact_match_with_no_fee_passes(self):
        # SideSwap dealer pays the network fee, so our send_asset balance is
        # exactly -send_amount.
        verify_pset_balances(
            {L_BTC: -100_000, USDT: 9_500_000},
            send_asset=L_BTC,
            send_amount=100_000,
            recv_asset=USDT,
            recv_amount=9_500_000,
        )

    def test_send_with_small_fee_within_tolerance_passes(self):
        # Wallet pays a small Liquid fee; -100_050 is -100k + 50 sat fee.
        verify_pset_balances(
            {L_BTC: -100_050, USDT: 9_500_000},
            send_asset=L_BTC,
            send_amount=100_000,
            recv_asset=USDT,
            recv_amount=9_500_000,
            fee_tolerance_sats=1_000,
        )

    # -- Attack: server delivers nothing --------------------------------------

    def test_server_keeps_recv_amount_rejected(self):
        # The deadliest attack: PSET takes our L-BTC, recv_asset balance is 0.
        with pytest.raises(PsetVerificationError, match="delivers 0"):
            verify_pset_balances(
                {L_BTC: -100_000, USDT: 0},
                send_asset=L_BTC,
                send_amount=100_000,
                recv_asset=USDT,
                recv_amount=9_500_000,
            )

    def test_recv_asset_missing_from_balance_rejected(self):
        # Even if recv_asset isn't in the dict at all, it's still 0 received.
        with pytest.raises(PsetVerificationError, match="delivers 0"):
            verify_pset_balances(
                {L_BTC: -100_000},
                send_asset=L_BTC,
                send_amount=100_000,
                recv_asset=USDT,
                recv_amount=9_500_000,
            )

    # -- Attack: server delivers less than agreed -----------------------------

    def test_short_recv_amount_rejected(self):
        with pytest.raises(PsetVerificationError, match="delivers 9499999"):
            verify_pset_balances(
                {L_BTC: -100_000, USDT: 9_499_999},
                send_asset=L_BTC,
                send_amount=100_000,
                recv_asset=USDT,
                recv_amount=9_500_000,
            )

    def test_excess_recv_amount_also_rejected(self):
        # Strict equality: refuse to sign if the server is "over-delivering"
        # too — this could signal a confused/buggy server, and we want the
        # contract to be exact.
        with pytest.raises(PsetVerificationError, match="delivers 10000000"):
            verify_pset_balances(
                {L_BTC: -100_000, USDT: 10_000_000},
                send_asset=L_BTC,
                send_amount=100_000,
                recv_asset=USDT,
                recv_amount=9_500_000,
            )

    # -- Attack: server takes more than agreed --------------------------------

    def test_overcharge_send_amount_rejected(self):
        # Server takes 200k L-BTC even though we agreed to send 100k.
        with pytest.raises(PsetVerificationError, match="deducts 200000"):
            verify_pset_balances(
                {L_BTC: -200_000, USDT: 9_500_000},
                send_asset=L_BTC,
                send_amount=100_000,
                recv_asset=USDT,
                recv_amount=9_500_000,
            )

    def test_undercharge_send_amount_rejected(self):
        # Less than agreed is also suspicious — possible bait-and-switch.
        with pytest.raises(PsetVerificationError, match="less than agreed"):
            verify_pset_balances(
                {L_BTC: -50_000, USDT: 9_500_000},
                send_asset=L_BTC,
                send_amount=100_000,
                recv_asset=USDT,
                recv_amount=9_500_000,
            )

    def test_fee_tolerance_does_not_let_attacker_steal(self):
        # 1000-sat tolerance is for a real fee, not a 100k overage.
        with pytest.raises(PsetVerificationError, match="more than the agreed"):
            verify_pset_balances(
                {L_BTC: -101_500, USDT: 9_500_000},
                send_asset=L_BTC,
                send_amount=100_000,
                recv_asset=USDT,
                recv_amount=9_500_000,
                fee_tolerance_sats=1_000,
            )

    # -- Attack: extra-output / siphon ----------------------------------------

    def test_unrelated_asset_movement_rejected(self):
        # Server adds an extra output that takes some of an unrelated asset
        # we hold (e.g. EURx, MEX). Very nasty if unchecked.
        with pytest.raises(PsetVerificationError, match="unexpectedly moves"):
            verify_pset_balances(
                {L_BTC: -100_000, USDT: 9_500_000, EVIL: -42_000},
                send_asset=L_BTC,
                send_amount=100_000,
                recv_asset=USDT,
                recv_amount=9_500_000,
            )

    def test_unrelated_positive_balance_rejected(self):
        # Even a positive movement of an unrelated asset gets rejected — we
        # don't want surprise inputs we didn't agree to receive.
        with pytest.raises(PsetVerificationError, match="unexpectedly moves"):
            verify_pset_balances(
                {L_BTC: -100_000, USDT: 9_500_000, EVIL: 1},
                send_asset=L_BTC,
                send_amount=100_000,
                recv_asset=USDT,
                recv_amount=9_500_000,
            )

    # -- Argument validation --------------------------------------------------

    def test_same_send_and_recv_asset_rejected(self):
        with pytest.raises(PsetVerificationError, match="same"):
            verify_pset_balances(
                {L_BTC: 0},
                send_asset=L_BTC,
                send_amount=100_000,
                recv_asset=L_BTC,
                recv_amount=100_000,
            )

    def test_zero_amounts_rejected(self):
        with pytest.raises(ValueError):
            verify_pset_balances(
                {}, send_asset=L_BTC, send_amount=0, recv_asset=USDT, recv_amount=1
            )
        with pytest.raises(ValueError):
            verify_pset_balances(
                {}, send_asset=L_BTC, send_amount=1, recv_asset=USDT, recv_amount=0
            )

    def test_negative_fee_tolerance_rejected(self):
        with pytest.raises(ValueError):
            verify_pset_balances(
                {},
                send_asset=L_BTC,
                send_amount=1,
                recv_asset=USDT,
                recv_amount=1,
                fee_tolerance_sats=-1,
            )


# ---------------------------------------------------------------------------
# SideSwapSwap dataclass + storage round-trip
# ---------------------------------------------------------------------------


class TestSideSwapSwap:
    def _make(self, **overrides) -> SideSwapSwap:
        defaults = dict(
            order_id="ord_xyz",
            submit_id="sub_abc",
            send_asset=L_BTC,
            send_amount=100_000,
            recv_asset=USDT,
            recv_amount=9_500_000,
            price=95.0,
            wallet_name="default",
            network="mainnet",
            status="pending",
            created_at="2026-05-07T12:00:00+00:00",
        )
        defaults.update(overrides)
        return SideSwapSwap(**defaults)

    def test_roundtrip_to_dict_from_dict(self):
        original = self._make(txid="tx" * 32, last_error="some error")
        reconstructed = SideSwapSwap.from_dict(original.to_dict())
        assert reconstructed == original

    def test_from_dict_backward_compat(self):
        # Earlier files might lack txid/last_error
        data = {
            "order_id": "old1",
            "submit_id": None,
            "send_asset": L_BTC,
            "send_amount": 1,
            "recv_asset": USDT,
            "recv_amount": 1,
            "price": 1.0,
            "wallet_name": "w",
            "network": "mainnet",
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        swap = SideSwapSwap.from_dict(data)
        assert swap.txid is None
        assert swap.last_error is None


# ---------------------------------------------------------------------------
# UTXO selection
# ---------------------------------------------------------------------------


class _Outpoint:
    def __init__(self, txid_hex: str, vout: int):
        self._txid = txid_hex
        self._vout = vout

    def txid(self):
        return self._txid

    def vout(self):
        return self._vout


class _Unblinded:
    def __init__(self, asset: str, value: int, asset_bf: str, value_bf: str):
        self._asset = asset
        self._value = value
        self._asset_bf = asset_bf
        self._value_bf = value_bf

    def asset(self):
        return self._asset

    def value(self):
        return self._value

    def asset_bf(self):
        return self._asset_bf

    def value_bf(self):
        return self._value_bf


class _FakeUtxo:
    def __init__(self, txid_hex: str, vout: int, asset: str, value: int,
                 asset_bf: str = "ab" * 32, value_bf: str = "cd" * 32):
        self._outpoint = _Outpoint(txid_hex, vout)
        self._unblinded = _Unblinded(asset, value, asset_bf, value_bf)

    def outpoint(self):
        return self._outpoint

    def unblinded(self):
        return self._unblinded


class TestSelectSwapUtxos:
    def test_selects_largest_first(self):
        from aqua.sideswap import select_swap_utxos

        utxos = [
            _FakeUtxo("aa" * 32, 0, L_BTC, 50_000),
            _FakeUtxo("bb" * 32, 1, L_BTC, 200_000),
            _FakeUtxo("cc" * 32, 0, L_BTC, 100_000),
        ]
        selected = select_swap_utxos(utxos, L_BTC, 150_000)
        assert len(selected) == 1
        assert selected[0]["value"] == 200_000

    def test_accumulates_across_multiple_utxos(self):
        from aqua.sideswap import select_swap_utxos

        utxos = [
            _FakeUtxo("aa" * 32, 0, L_BTC, 30_000),
            _FakeUtxo("bb" * 32, 0, L_BTC, 30_000),
            _FakeUtxo("cc" * 32, 0, L_BTC, 30_000),
        ]
        selected = select_swap_utxos(utxos, L_BTC, 70_000)
        assert len(selected) == 3
        assert sum(s["value"] for s in selected) == 90_000
        for s in selected:
            assert s["redeem_script"] is None

    def test_skips_other_assets(self):
        from aqua.sideswap import select_swap_utxos

        utxos = [
            _FakeUtxo("aa" * 32, 0, USDT, 9_000_000),
            _FakeUtxo("bb" * 32, 0, L_BTC, 100_000),
        ]
        selected = select_swap_utxos(utxos, L_BTC, 50_000)
        assert len(selected) == 1
        assert selected[0]["asset"] == L_BTC

    def test_skips_non_confidential_utxos(self):
        from aqua.sideswap import select_swap_utxos

        utxos = [
            # Both blinding factors zero = non-confidential, must be skipped
            _FakeUtxo("aa" * 32, 0, L_BTC, 100_000, asset_bf="0" * 64, value_bf="0" * 64),
            _FakeUtxo("bb" * 32, 0, L_BTC, 50_000),
        ]
        selected = select_swap_utxos(utxos, L_BTC, 50_000)
        assert len(selected) == 1
        assert selected[0]["txid"] == "bb" * 32

    def test_insufficient_funds_raises(self):
        from aqua.sideswap import select_swap_utxos

        utxos = [_FakeUtxo("aa" * 32, 0, L_BTC, 10_000)]
        with pytest.raises(ValueError, match="Insufficient confidential balance"):
            select_swap_utxos(utxos, L_BTC, 50_000)


# ---------------------------------------------------------------------------
# HTTP swap_start / swap_sign client
# ---------------------------------------------------------------------------


class TestSideSwapHTTPClient:
    @patch("aqua.sideswap.urllib.request.urlopen")
    def test_swap_start_sends_correct_body(self, mock_urlopen):
        import json as _json
        from aqua.sideswap import SideSwapHTTPClient

        # Mock successful response
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.read.return_value = _json.dumps({
            "id": 1,
            "result": {"submit_id": "submit_xyz", "pset": "cHNldP8B..."},
        }).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        client = SideSwapHTTPClient("https://api-testnet.sideswap.io/upload/foo")
        result = client.swap_start(
            order_id="ord_1",
            inputs=[{"txid": "aa" * 32, "vout": 0, "asset": L_BTC,
                     "asset_bf": "ab" * 32, "value": 100_000, "value_bf": "cd" * 32,
                     "redeem_script": None}],
            recv_addr="lq1qreceive",
            change_addr="lq1qchange",
            send_asset=L_BTC,
            send_amount=100_000,
            recv_asset=USDT,
            recv_amount=9_500_000,
        )
        assert result["submit_id"] == "submit_xyz"
        assert result["pset"] == "cHNldP8B..."

        # Verify wire format
        sent_request = mock_urlopen.call_args[0][0]
        body = _json.loads(sent_request.data.decode())
        assert body["method"] == "swap_start"
        assert body["params"]["order_id"] == "ord_1"
        assert body["params"]["recv_addr"] == "lq1qreceive"
        assert body["params"]["send_amount"] == 100_000
        assert body["params"]["recv_amount"] == 9_500_000

    @patch("aqua.sideswap.urllib.request.urlopen")
    def test_swap_sign_sends_signed_pset(self, mock_urlopen):
        import json as _json
        from aqua.sideswap import SideSwapHTTPClient
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.read.return_value = _json.dumps({
            "id": 1, "result": {"txid": "ff" * 32}
        }).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        client = SideSwapHTTPClient("https://api-testnet.sideswap.io/upload/foo")
        result = client.swap_sign("ord_1", "submit_xyz", "cHNldP8BSIGNED...")
        assert result["txid"] == "ff" * 32

        body = _json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["method"] == "swap_sign"
        assert body["params"]["pset"] == "cHNldP8BSIGNED..."

    @patch("aqua.sideswap.urllib.request.urlopen")
    def test_http_error_propagates_message(self, mock_urlopen):
        import io
        import json as _json
        import urllib.error
        from aqua.sideswap import SideSwapHTTPClient, SideSwapWSError

        err = urllib.error.HTTPError(
            url="https://api-testnet.sideswap.io/upload/foo",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(_json.dumps({"error": {"code": -32602, "message": "no liquidity"}}).encode()),
        )
        mock_urlopen.side_effect = err

        client = SideSwapHTTPClient("https://api-testnet.sideswap.io/upload/foo")
        with pytest.raises(SideSwapWSError, match="no liquidity"):
            client.swap_sign("ord_1", "submit_xyz", "cHNldP8BSIGNED...")

    @patch("aqua.sideswap.urllib.request.urlopen")
    def test_jsonrpc_error_in_body_propagates(self, mock_urlopen):
        import json as _json
        from aqua.sideswap import SideSwapHTTPClient, SideSwapWSError
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.read.return_value = _json.dumps({
            "id": 1,
            "error": {"code": -32000, "message": "order expired"},
        }).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        client = SideSwapHTTPClient("https://api-testnet.sideswap.io/upload/foo")
        with pytest.raises(SideSwapWSError, match="order expired"):
            client.swap_sign("ord_1", "submit_xyz", "cHNldP8BSIGNED...")


# ---------------------------------------------------------------------------
# SwapManager — integration with mocked WS, HTTP, LWK
# ---------------------------------------------------------------------------


class _FakeWollet:
    """Stand-in for `lwk.Wollet`. The manager only calls .utxos(), .address(),
    and .pset_details(pset)."""

    def __init__(self, utxos: list, balances: dict[str, int]):
        self._utxos = utxos
        self._balances = balances
        self._addr_idx = 0

    def utxos(self):
        return self._utxos

    def address(self, _index):
        idx = self._addr_idx
        self._addr_idx += 1
        return _FakeAddrResult(f"lq1qaddr{idx}", idx)

    def pset_details(self, _pset):
        return _FakePsetDetails(self._balances)


class _FakeAddrResult:
    def __init__(self, addr_str, idx):
        self._addr = addr_str
        self._idx = idx

    def address(self):
        return self._addr

    def index(self):
        return self._idx


class _FakePsetDetails:
    def __init__(self, balances: dict[str, int]):
        self._balances = balances

    def balance(self):
        return _FakePsetBalance(self._balances)


class _FakePsetBalance:
    def __init__(self, balances: dict[str, int]):
        self._b = balances

    def balances(self):
        return dict(self._b)

    def fee(self):
        return 50

    def recipients(self):
        return []


class _FakeSigner:
    def __init__(self):
        self.signed: list = []

    def sign(self, pset):
        self.signed.append(pset)

        class _Signed:
            def __str__(self):
                return "cHNldP8BSIGNED"

        return _Signed()


@pytest.fixture
def swap_manager_setup(storage):
    """Build a SideSwapSwapManager with mocked LWK + WS + HTTP layers."""
    from aqua.sideswap import SideSwapSwapManager
    from aqua.storage import WalletData

    wallet = WalletData(
        name="default",
        network="testnet",
        descriptor="ct(slip77(deadbeef),elwpkh([fp/84'/1776'/0']tpubD.../0/*))",
        encrypted_mnemonic=None,
    )
    storage.save_wallet(wallet)

    fake_signer = _FakeSigner()
    fake_wollet = _FakeWollet(
        utxos=[_FakeUtxo("aa" * 32, 0, L_BTC, 500_000)],
        balances={L_BTC: -100_050, USDT: 9_500_000},  # honest balance
    )

    class FakeWalletManager:
        def __init__(self):
            self._signers = {"default": fake_signer}
            self._wollets = {"default": fake_wollet}
            self.synced = []

        def load_wallet(self, name, password=None):  # noqa: ARG002
            return wallet

        def sync_wallet(self, name):
            self.synced.append(name)

        def _get_policy_asset(self, network):  # noqa: ARG002
            return L_BTC

        def _get_wollet(self, name):
            return self._wollets[name]

    wm = FakeWalletManager()
    mgr = SideSwapSwapManager(storage=storage, wallet_manager=wm)
    return mgr, wm, fake_wollet, fake_signer, storage


class _FakeHTTPClient:
    """Recorded HTTP client. Set `.swap_start_response` / `.swap_sign_response`."""

    swap_start_response = {"submit_id": "submit_xyz", "pset": "cHNldP8BUNSIGNED"}
    swap_sign_response = {"txid": "ee" * 32}
    last_swap_start_call: dict | None = None
    last_swap_sign_call: dict | None = None
    upload_url: str = ""

    def __init__(self, upload_url: str) -> None:
        _FakeHTTPClient.upload_url = upload_url

    def swap_start(self, **kwargs):
        _FakeHTTPClient.last_swap_start_call = kwargs
        if isinstance(_FakeHTTPClient.swap_start_response, Exception):
            raise _FakeHTTPClient.swap_start_response
        return _FakeHTTPClient.swap_start_response

    def swap_sign(self, order_id, submit_id, signed_pset_b64):
        _FakeHTTPClient.last_swap_sign_call = {
            "order_id": order_id,
            "submit_id": submit_id,
            "signed_pset_b64": signed_pset_b64,
        }
        if isinstance(_FakeHTTPClient.swap_sign_response, Exception):
            raise _FakeHTTPClient.swap_sign_response
        return _FakeHTTPClient.swap_sign_response


@pytest.fixture(autouse=True)
def _reset_fake_http():
    _FakeHTTPClient.swap_start_response = {"submit_id": "submit_xyz", "pset": "cHNldP8BUNSIGNED"}
    _FakeHTTPClient.swap_sign_response = {"txid": "ee" * 32}
    _FakeHTTPClient.last_swap_start_call = None
    _FakeHTTPClient.last_swap_sign_call = None
    _FakeHTTPClient.upload_url = ""
    yield


def _patch_swap_layers():
    """Patch WS + HTTP + lwk.Pset for the manager flow."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(_patch_ws())
    stack.enter_context(patch("aqua.sideswap.SideSwapHTTPClient", _FakeHTTPClient))

    # Patch lwk.Pset to a no-op shim — we don't have real PSETs in tests
    class _FakePset:
        def __init__(self, b64):
            self.b64 = b64

    import lwk

    stack.enter_context(patch.object(lwk, "Pset", _FakePset))
    return stack


class TestSwapManagerExecute:
    def _ws_responses_for_quote(self):
        FakeWSClient.responses["subscribe_price_stream"] = {
            "asset": USDT,
            "send_bitcoins": True,
            "send_amount": 100_000,
            "recv_amount": 9_500_000,
            "price": 95.0,
            "fixed_fee": 100,
        }
        FakeWSClient.responses["start_swap_web"] = {
            "order_id": "ord_happy",
            "send_asset": L_BTC,
            "send_amount": 100_000,
            "recv_asset": USDT,
            "recv_amount": 9_500_000,
            "upload_url": "https://api-testnet.sideswap.io/upload/foo",
        }
        FakeWSClient.responses["unsubscribe_price_stream"] = {}

    def test_happy_path_end_to_end(self, swap_manager_setup):
        mgr, _, _, fake_signer, storage = swap_manager_setup
        self._ws_responses_for_quote()

        with _patch_swap_layers():
            swap = mgr.execute_swap(
                asset_id=USDT, send_amount=100_000, wallet_name="default"
            )

        assert swap.status == "broadcast"
        assert swap.txid == "ee" * 32
        assert swap.order_id == "ord_happy"
        # We did sign exactly once
        assert len(fake_signer.signed) == 1
        # HTTP layer received the signed PSET
        assert _FakeHTTPClient.last_swap_sign_call["signed_pset_b64"] == "cHNldP8BSIGNED"
        # Persisted across the whole flow
        loaded = storage.load_sideswap_swap("ord_happy")
        assert loaded is not None
        assert loaded.status == "broadcast"

    def test_aborts_when_pset_balance_does_not_match(self, swap_manager_setup):
        # Override the fake wollet to return a malicious balance (server stole funds)
        mgr, _, fake_wollet, fake_signer, storage = swap_manager_setup
        self._ws_responses_for_quote()
        fake_wollet._balances = {L_BTC: -100_000, USDT: 0}  # we get nothing!

        with _patch_swap_layers():
            with pytest.raises(PsetVerificationError):
                mgr.execute_swap(
                    asset_id=USDT, send_amount=100_000, wallet_name="default"
                )

        # Critically: we did NOT sign, and we did NOT submit
        assert len(fake_signer.signed) == 0
        assert _FakeHTTPClient.last_swap_sign_call is None
        # And the order is persisted as failed for forensics
        loaded = storage.load_sideswap_swap("ord_happy")
        assert loaded is not None
        assert loaded.status == "failed"
        assert "PSET verification failed" in (loaded.last_error or "")

    def test_aborts_when_pset_takes_extra_lbtc(self, swap_manager_setup):
        mgr, _, fake_wollet, fake_signer, _ = swap_manager_setup
        self._ws_responses_for_quote()
        # Server overcharges: takes 200k instead of 100k
        fake_wollet._balances = {L_BTC: -200_000, USDT: 9_500_000}

        with _patch_swap_layers():
            with pytest.raises(PsetVerificationError, match="more than the agreed"):
                mgr.execute_swap(
                    asset_id=USDT, send_amount=100_000, wallet_name="default"
                )
        assert len(fake_signer.signed) == 0

    def test_aborts_when_pset_moves_unrelated_asset(self, swap_manager_setup):
        mgr, _, fake_wollet, fake_signer, _ = swap_manager_setup
        self._ws_responses_for_quote()
        fake_wollet._balances = {L_BTC: -100_050, USDT: 9_500_000, EVIL: -500}

        with _patch_swap_layers():
            with pytest.raises(PsetVerificationError, match="unexpectedly moves"):
                mgr.execute_swap(
                    asset_id=USDT, send_amount=100_000, wallet_name="default"
                )
        assert len(fake_signer.signed) == 0

    def test_rejects_swap_lbtc_for_lbtc(self, swap_manager_setup):
        mgr, _, _, _, _ = swap_manager_setup
        with _patch_swap_layers():
            with pytest.raises(ValueError, match="Cannot swap L-BTC for L-BTC"):
                mgr.execute_swap(
                    asset_id=L_BTC, send_amount=100_000, wallet_name="default"
                )

    def test_rejects_unknown_wallet(self, swap_manager_setup):
        mgr, _, _, _, _ = swap_manager_setup
        with pytest.raises(ValueError, match="not found"):
            mgr.execute_swap(
                asset_id=USDT, send_amount=100_000, wallet_name="ghost"
            )

    def test_no_quote_returned(self, swap_manager_setup):
        from aqua.sideswap import SideSwapWSError

        mgr, _, _, _, _ = swap_manager_setup
        FakeWSClient.responses["subscribe_price_stream"] = {"asset": USDT, "send_bitcoins": True}
        FakeWSClient.responses["__notification__"] = {
            "method": "update_price_stream",
            "params": {"error_msg": "no_liquidity"},
        }
        with _patch_swap_layers():
            with pytest.raises(SideSwapWSError):
                mgr.execute_swap(
                    asset_id=USDT, send_amount=100_000, wallet_name="default"
                )

    def test_swap_status_returns_persisted(self, swap_manager_setup):
        mgr, _, _, _, storage = swap_manager_setup
        self._ws_responses_for_quote()
        with _patch_swap_layers():
            mgr.execute_swap(
                asset_id=USDT, send_amount=100_000, wallet_name="default"
            )
        result = mgr.status("ord_happy")
        assert result["order_id"] == "ord_happy"
        assert result["status"] == "broadcast"
        assert result["txid"] == "ee" * 32
        assert result["recv_asset"] == USDT
        assert result["recv_amount"] == 9_500_000

    def test_swap_status_unknown_raises(self, swap_manager_setup):
        mgr, _, _, _, _ = swap_manager_setup
        with pytest.raises(ValueError, match="not found"):
            mgr.status("doesnotexist")
