"""Tests for SideShift integration (sideshift.ai cross-chain swaps).

Mocks `urllib.request.urlopen` for the HTTP client; the manager-level tests
fake the wallet managers since SideShift never touches PSETs (custodial; we
just send to a deposit address). No async machinery to mock — SideShift is
plain REST.
"""

from __future__ import annotations

import io
import json
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aqua.sideshift import (
    AFFILIATE_ID,
    ALLOWED_PAIRS,
    SideShiftClient,
    SideShiftManager,
    SideShiftShift,
    _check_pair_allowed,
    _decimal_to_sats,
    recommend_shift_or_swap,
    shift_is_failed,
    shift_is_final,
    shift_is_success,
)
from aqua.storage import Storage, WalletData


L_BTC = "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
USDT_LIQUID = "ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2"


def _mock_response(data, status=200):
    resp = MagicMock()
    if isinstance(data, dict) or isinstance(data, list):
        resp.read.return_value = json.dumps(data).encode()
    elif data is None:
        resp.read.return_value = b""
    else:
        resp.read.return_value = data
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Storage(Path(tmpdir))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestDecimalToSats:
    @pytest.mark.parametrize(
        "decimal_str, expected",
        [
            ("0.0005", 50_000),
            ("0.00000001", 1),
            ("1", 100_000_000),
            ("0", 0),
            ("0.12345678", 12_345_678),
        ],
    )
    def test_known_amounts(self, decimal_str, expected):
        assert _decimal_to_sats(decimal_str) == expected

    def test_rounding_half_up_at_9th_decimal(self):
        # 0.000000005 = 0.5 sats; round half up → 1 sat
        assert _decimal_to_sats("0.000000005") == 1

    def test_accepts_int_and_float(self):
        assert _decimal_to_sats(1) == 100_000_000
        assert _decimal_to_sats(0.0005) == 50_000


class TestStatusHelpers:
    @pytest.mark.parametrize("s", ["settled"])
    def test_settled_is_success(self, s):
        assert shift_is_success(s) is True
        assert shift_is_final(s) is True
        assert shift_is_failed(s) is False

    @pytest.mark.parametrize("s", ["refunded", "expired"])
    def test_failed_terminal_states(self, s):
        assert shift_is_success(s) is False
        assert shift_is_final(s) is True
        assert shift_is_failed(s) is True

    @pytest.mark.parametrize("s", ["waiting", "pending", "processing", "settling", "refund", "refunding"])
    def test_pending_states(self, s):
        assert shift_is_final(s) is False
        assert shift_is_success(s) is False
        assert shift_is_failed(s) is False

    def test_review_is_terminal_but_not_success(self):
        assert shift_is_final("review") is True
        assert shift_is_success("review") is False
        assert shift_is_failed("review") is False  # ambiguous; SideShift may eventually settle

    def test_case_insensitive(self):
        assert shift_is_success("SETTLED") is True


class TestRecommendation:
    def test_btc_to_lbtc_recommends_sideswap(self):
        rec = recommend_shift_or_swap("btc", "bitcoin", "btc", "liquid")
        assert rec["recommendation"] == "sideswap"

    def test_lbtc_to_btc_recommends_sideswap(self):
        rec = recommend_shift_or_swap("btc", "liquid", "btc", "bitcoin")
        assert rec["recommendation"] == "sideswap"

    def test_lbtc_to_usdt_liquid_recommends_sideswap(self):
        rec = recommend_shift_or_swap("btc", "liquid", "usdt", "liquid")
        assert rec["recommendation"] == "sideswap"

    def test_usdt_liquid_to_usdt_tron_recommends_sideshift(self):
        rec = recommend_shift_or_swap("usdt", "liquid", "usdt", "tron")
        assert rec["recommendation"] == "sideshift"

    def test_lbtc_to_eth_recommends_sideshift(self):
        rec = recommend_shift_or_swap("btc", "liquid", "eth", "ethereum")
        assert rec["recommendation"] == "sideshift"

    def test_btc_to_eth_recommends_sideshift(self):
        rec = recommend_shift_or_swap("btc", "bitcoin", "eth", "ethereum")
        assert rec["recommendation"] == "sideshift"

    def test_recommendation_is_case_insensitive_on_network(self):
        rec = recommend_shift_or_swap("BTC", "BITCOIN", "USDT", "TRON")
        assert rec["recommendation"] == "sideshift"
        assert rec["from_network"] == "bitcoin"
        assert rec["to_network"] == "tron"


# ---------------------------------------------------------------------------
# Allowlist (ALLOWED_PAIRS) — matches AQUA Flutter's curated SideShift surface
# ---------------------------------------------------------------------------


class TestAllowedPairs:
    """Encodes the contract that we expose the same SideShift surface as AQUA
    Flutter: USDt across 7 chains + BTC mainchain. L-BTC and arbitrary altcoins
    are not in the allowlist; users hit the override env var if they want them.
    """

    def test_allowlist_matches_aqua_flutter(self):
        # Drift from AQUA's `lib/features/sideshift/models/sideshift_assets.dart`
        # should fail loudly so we have a forced conversation about it.
        expected = {
            ("usdt", "ethereum"),
            ("usdt", "tron"),
            ("usdt", "bsc"),
            ("usdt", "solana"),
            ("usdt", "polygon"),
            ("usdt", "ton"),
            ("usdt", "liquid"),
            ("btc", "bitcoin"),
        }
        assert set(ALLOWED_PAIRS) == expected

    def test_lbtc_is_NOT_in_allowlist(self):
        # Explicitly: L-BTC is not exposed via SideShift. Use SideSwap instead.
        assert ("btc", "liquid") not in ALLOWED_PAIRS

    @pytest.mark.parametrize("coin, network", sorted(
        {("usdt", "ethereum"), ("usdt", "tron"), ("usdt", "liquid"),
         ("btc", "bitcoin"), ("USDT", "Tron")}  # case-insensitive
    ))
    def test_allowed_pairs_pass(self, coin, network):
        # No exception
        _check_pair_allowed(coin, network, side="deposit")

    @pytest.mark.parametrize("coin, network", [
        ("btc", "liquid"),       # L-BTC
        ("eth", "ethereum"),     # ETH
        ("ltc", "litecoin"),     # LTC
        ("xmr", "monero"),       # XMR
        ("usdc", "ethereum"),    # USDC (only USDt is on the allowlist)
    ])
    def test_disallowed_pairs_raise(self, coin, network):
        with pytest.raises(ValueError, match="not in the curated allowlist"):
            _check_pair_allowed(coin, network, side="deposit")

    def test_error_message_includes_allowlist_contents(self):
        with pytest.raises(ValueError) as exc:
            _check_pair_allowed("eth", "ethereum", side="deposit")
        msg = str(exc.value)
        assert "btc-bitcoin" in msg
        assert "usdt-tron" in msg
        # Mentions the override env var
        assert "SIDESHIFT_ALLOW_ALL_NETWORKS" in msg

    def test_override_env_var_bypasses_check(self, monkeypatch):
        monkeypatch.setenv("SIDESHIFT_ALLOW_ALL_NETWORKS", "1")
        # Now arbitrary chains pass
        _check_pair_allowed("eth", "ethereum", side="deposit")
        _check_pair_allowed("xmr", "monero", side="settle")

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
    def test_override_env_var_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("SIDESHIFT_ALLOW_ALL_NETWORKS", value)
        _check_pair_allowed("eth", "ethereum", side="deposit")

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "blah"])
    def test_override_env_var_falsy_values_keep_enforcement(self, monkeypatch, value):
        monkeypatch.setenv("SIDESHIFT_ALLOW_ALL_NETWORKS", value)
        with pytest.raises(ValueError, match="not in the curated allowlist"):
            _check_pair_allowed("eth", "ethereum", side="deposit")

    def test_error_message_distinguishes_deposit_and_settle(self):
        with pytest.raises(ValueError, match="deposit pair"):
            _check_pair_allowed("eth", "ethereum", side="deposit")
        with pytest.raises(ValueError, match="settle pair"):
            _check_pair_allowed("eth", "ethereum", side="settle")


# ---------------------------------------------------------------------------
# SideShiftShift dataclass + storage round-trip
# ---------------------------------------------------------------------------


class TestSideShiftShift:
    def _make(self, **overrides) -> SideShiftShift:
        defaults = dict(
            shift_id="abc123",
            shift_type="fixed",
            direction="send",
            deposit_coin="BTC",
            deposit_network="liquid",
            settle_coin="USDT",
            settle_network="tron",
            settle_address="TXYZ",
            deposit_address="lq1qdeposit",
            refund_address="lq1qrefund",
            wallet_name="default",
            status="waiting",
            created_at="2026-05-08T12:00:00+00:00",
        )
        defaults.update(overrides)
        return SideShiftShift(**defaults)

    def test_to_dict_from_dict_roundtrip(self):
        s = self._make(deposit_amount="0.0005", settle_amount="100", rate="200000")
        assert SideShiftShift.from_dict(s.to_dict()) == s

    def test_from_dict_backward_compat(self):
        # Older record without the optional fields should still load
        data = {
            "shift_id": "old1",
            "shift_type": "variable",
            "direction": "receive",
            "deposit_coin": "USDT",
            "deposit_network": "tron",
            "settle_coin": "USDT",
            "settle_network": "liquid",
            "settle_address": "lq1q",
            "deposit_address": "TXYZ",
            "refund_address": None,
            "wallet_name": "default",
            "status": "waiting",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        shift = SideShiftShift.from_dict(data)
        assert shift.deposit_amount is None
        assert shift.settle_amount is None
        assert shift.deposit_min is None
        assert shift.deposit_max is None
        assert shift.last_error is None


class TestStorage:
    def test_save_load_roundtrip(self, storage):
        shift = SideShiftShift(
            shift_id="abc123",
            shift_type="fixed",
            direction="send",
            deposit_coin="BTC",
            deposit_network="liquid",
            settle_coin="USDT",
            settle_network="tron",
            settle_address="TXYZ",
            deposit_address="lq1qdeposit",
            refund_address="lq1qrefund",
            wallet_name="default",
            status="waiting",
            created_at="2026-05-08T12:00:00+00:00",
        )
        storage.save_sideshift_shift(shift)
        loaded = storage.load_sideshift_shift("abc123")
        assert loaded == shift

    def test_load_missing_returns_none(self, storage):
        assert storage.load_sideshift_shift("nope") is None

    def test_list_shifts(self, storage):
        for sid in ("a", "b", "c"):
            shift = SideShiftShift(
                shift_id=sid,
                shift_type="fixed",
                direction="send",
                deposit_coin="BTC",
                deposit_network="liquid",
                settle_coin="USDT",
                settle_network="tron",
                settle_address="TXYZ",
                deposit_address="lq1q",
                refund_address=None,
                wallet_name=None,
                status="waiting",
                created_at="2026-05-08T12:00:00+00:00",
            )
            storage.save_sideshift_shift(shift)
        assert set(storage.list_sideshift_shifts()) == {"a", "b", "c"}

    def test_invalid_shift_id_rejected(self, storage):
        shift = SideShiftShift(
            shift_id="../escape",
            shift_type="fixed",
            direction="send",
            deposit_coin="BTC",
            deposit_network="liquid",
            settle_coin="USDT",
            settle_network="tron",
            settle_address="TXYZ",
            deposit_address="lq1q",
            refund_address=None,
            wallet_name=None,
            status="waiting",
            created_at="2026-05-08T12:00:00+00:00",
        )
        with pytest.raises(ValueError, match="Invalid SideShift shift ID"):
            storage.save_sideshift_shift(shift)

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX file permissions not enforced on Windows",
    )
    def test_file_permissions_0600(self, storage):
        import os

        shift = SideShiftShift(
            shift_id="permcheck",
            shift_type="fixed",
            direction="send",
            deposit_coin="BTC",
            deposit_network="liquid",
            settle_coin="USDT",
            settle_network="tron",
            settle_address="TXYZ",
            deposit_address="lq1q",
            refund_address=None,
            wallet_name=None,
            status="waiting",
            created_at="2026-05-08T12:00:00+00:00",
        )
        storage.save_sideshift_shift(shift)
        path = storage.sideshift_shifts_dir / "permcheck.json"
        assert (os.stat(path).st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# REST client (HTTP layer)
# ---------------------------------------------------------------------------


class TestSideShiftClient:
    def test_default_affiliate_id_is_aqua_id(self):
        # Default to JAN3's AQUA Flutter affiliate ID.
        client = SideShiftClient()
        assert client.affiliate_id == AFFILIATE_ID
        assert client.affiliate_id == "PVmPh4Mp3"

    def test_explicit_none_affiliate_id_disables_it(self):
        client = SideShiftClient(affiliate_id="")
        assert client.affiliate_id is None

    def test_custom_affiliate_id(self):
        client = SideShiftClient(affiliate_id="custom")
        assert client.affiliate_id == "custom"

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_get_coins(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            [{"coin": "BTC", "name": "Bitcoin", "networks": ["bitcoin", "liquid"]}]
        )
        client = SideShiftClient()
        result = client.get_coins()
        assert result == [{"coin": "BTC", "name": "Bitcoin", "networks": ["bitcoin", "liquid"]}]

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_get_pair_uses_lowercase_coin_network_ids(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"rate": "20000"})
        client = SideShiftClient()
        client.get_pair("BTC", "Bitcoin", "USDT", "TRON")
        sent = mock_urlopen.call_args[0][0]
        assert sent.full_url.startswith("https://sideshift.ai/api/v2/pair/btc-bitcoin/usdt-tron")
        assert "affiliateId=PVmPh4Mp3" in sent.full_url

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_get_pair_includes_amount_when_provided(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"rate": "20000"})
        client = SideShiftClient()
        client.get_pair("usdt", "tron", "btc", "bitcoin", amount="100")
        sent = mock_urlopen.call_args[0][0]
        assert "amount=100" in sent.full_url

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_request_quote_sends_uppercase_coin_lowercase_network(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"id": "q1", "depositAmount": "0.001"})
        client = SideShiftClient()
        client.request_quote("usdt", "Tron", "BTC", "bitcoin", deposit_amount="100")
        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["depositCoin"] == "USDT"
        assert body["depositNetwork"] == "tron"
        assert body["settleCoin"] == "BTC"
        assert body["settleNetwork"] == "bitcoin"
        assert body["depositAmount"] == "100"
        assert body["affiliateId"] == "PVmPh4Mp3"

    def test_request_quote_requires_exactly_one_amount(self):
        client = SideShiftClient()
        with pytest.raises(ValueError, match="exactly one"):
            client.request_quote("usdt", "tron", "btc", "bitcoin")
        with pytest.raises(ValueError, match="exactly one"):
            client.request_quote(
                "usdt", "tron", "btc", "bitcoin",
                deposit_amount="100", settle_amount="0.001",
            )

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_create_fixed_shift_sends_quote_id_and_addresses(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"id": "shift1", "depositAddress": "addr"})
        client = SideShiftClient()
        client.create_fixed_shift("q1", "settle_addr", refund_address="refund_addr")
        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["quoteId"] == "q1"
        assert body["settleAddress"] == "settle_addr"
        assert body["refundAddress"] == "refund_addr"
        assert body["affiliateId"] == "PVmPh4Mp3"

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_create_variable_shift_sends_correct_body(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"id": "shift1", "depositAddress": "addr"})
        client = SideShiftClient()
        client.create_variable_shift(
            deposit_coin="usdt", deposit_network="tron",
            settle_coin="usdt", settle_network="liquid",
            settle_address="lq1q", refund_address="TXrefund",
        )
        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["depositCoin"] == "USDT"
        assert body["depositNetwork"] == "tron"
        assert body["settleCoin"] == "USDT"
        assert body["settleNetwork"] == "liquid"
        assert body["settleAddress"] == "lq1q"
        assert body["refundAddress"] == "TXrefund"

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_get_shift_path(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"id": "shift1", "status": "settled"})
        client = SideShiftClient()
        client.get_shift("shift1")
        sent = mock_urlopen.call_args[0][0]
        assert sent.full_url == "https://sideshift.ai/api/v2/shifts/shift1"

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_http_error_extracts_message(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="https://sideshift.ai/api/v2/shifts/fixed",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"error": {"message": "amount too small"}}).encode()),
        )
        mock_urlopen.side_effect = err
        client = SideShiftClient()
        with pytest.raises(RuntimeError, match="amount too small"):
            client.get_coins()

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_unreachable_host_raises(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("name resolution failed")
        client = SideShiftClient()
        with pytest.raises(RuntimeError, match="unreachable"):
            client.get_coins()


# ---------------------------------------------------------------------------
# Manager (with mocked HTTP and wallet managers)
# ---------------------------------------------------------------------------


@pytest.fixture
def manager_setup(storage):
    """Build a SideShiftManager with fake wallet/btc managers and a temp Storage.

    The manager only calls `get_address(name).address`, `send(name, addr, sats, …)`
    on the wallet managers — narrow surface, easy to fake.
    """
    wallet = WalletData(
        name="default",
        network="mainnet",
        descriptor="ct(slip77(deadbeef),elwpkh([fp/84'/1776'/0']tpubD.../0/*))",
        encrypted_mnemonic=None,
    )
    storage.save_wallet(wallet)

    class _AddrResult:
        def __init__(self, addr):
            self.address = addr

    class FakeWalletManager:
        def __init__(self):
            self.sent: list[tuple] = []
            self.next_address = "lq1qreceive"

        def get_address(self, name, index=None):  # noqa: ARG002
            return _AddrResult(self.next_address)

        def send(self, name, address, amount, asset_id=None, password=None):  # noqa: ARG002
            self.sent.append(("liquid", name, address, amount, asset_id, password))
            return "lqtxid" + ("0" * 58)

    class FakeBtcManager:
        def __init__(self):
            self.sent: list[tuple] = []
            self.next_address = "bc1qreceive"

        def get_address(self, name, index=None):  # noqa: ARG002
            return _AddrResult(self.next_address)

        def send(self, name, address, amount, fee_rate=None, password=None):  # noqa: ARG002
            self.sent.append(("bitcoin", name, address, amount, password))
            return "btctxid" + ("0" * 58)

    wm = FakeWalletManager()
    btc = FakeBtcManager()
    mgr = SideShiftManager(storage=storage, wallet_manager=wm, btc_wallet_manager=btc)
    return mgr, wm, btc, storage


class TestManagerSend:
    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_send_usdt_liquid_to_usdt_tron_happy_path(self, mock_urlopen, manager_setup):
        # The headline AQUA flow: USDt-Liquid → USDt-Tron via SideShift.
        mgr, wm, _, storage = manager_setup
        mock_urlopen.side_effect = [
            _mock_response({
                "id": "q_xyz",
                "depositAmount": "100",
                "settleAmount": "99.5",
                "rate": "0.995",
            }),
            _mock_response({
                "id": "shift_xyz",
                "depositAddress": "lq1qdeposit",
                "depositAmount": "100",
                "settleAmount": "99.5",
                "rate": "0.995",
                "status": "waiting",
                "expiresAt": "2026-05-08T12:15:00Z",
                "depositCoin": "USDT",
                "depositNetwork": "liquid",
                "settleCoin": "USDT",
                "settleNetwork": "tron",
                "settleAddress": "TXYZ",
            }),
        ]
        shift = mgr.send_shift(
            deposit_coin="usdt",
            deposit_network="liquid",
            settle_coin="usdt",
            settle_network="tron",
            settle_address="TXYZ",
            deposit_amount="100",
            wallet_name="default",
            liquid_asset_id=USDT_LIQUID,
        )
        assert shift.shift_id == "shift_xyz"
        assert shift.deposit_address == "lq1qdeposit"
        assert shift.deposit_hash and shift.deposit_hash.startswith("lqtxid")
        # Refund address is the wallet's own Liquid address (the deposit chain)
        assert shift.refund_address == "lq1qreceive"
        # 100 USDt = 100 * 1e8 sats (Liquid USDt is 8-decimal)
        assert wm.sent == [("liquid", "default", "lq1qdeposit", 10_000_000_000,
                            USDT_LIQUID, None)]
        # Persisted
        assert storage.load_sideshift_shift("shift_xyz") is not None

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_send_usdt_liquid_passes_asset_id(self, mock_urlopen, manager_setup):
        mgr, wm, _, _ = manager_setup
        mock_urlopen.side_effect = [
            _mock_response({"id": "q1", "depositAmount": "100", "settleAmount": "99",
                            "rate": "1"}),
            _mock_response({
                "id": "shift_u",
                "depositAddress": "lq1qassetdeposit",
                "depositAmount": "100",
                "settleAmount": "99",
                "rate": "1",
                "status": "waiting",
                "depositCoin": "USDT",
                "depositNetwork": "liquid",
                "settleCoin": "USDT",
                "settleNetwork": "tron",
                "settleAddress": "TXYZ",
            }),
        ]
        mgr.send_shift(
            deposit_coin="usdt",
            deposit_network="liquid",
            settle_coin="usdt",
            settle_network="tron",
            settle_address="TXYZ",
            deposit_amount="100",
            wallet_name="default",
            liquid_asset_id=USDT_LIQUID,
        )
        assert wm.sent == [("liquid", "default", "lq1qassetdeposit", 100 * 100_000_000,
                            USDT_LIQUID, None)]

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_send_btc_uses_btc_manager(self, mock_urlopen, manager_setup):
        mgr, wm, btc, _ = manager_setup
        mock_urlopen.side_effect = [
            _mock_response({"id": "q1", "depositAmount": "0.001", "settleAmount": "10",
                            "rate": "10000"}),
            _mock_response({
                "id": "shift_btc",
                "depositAddress": "bc1qdeposit",
                "depositAmount": "0.001",
                "depositCoin": "BTC",
                "depositNetwork": "bitcoin",
                "settleCoin": "USDT",
                "settleNetwork": "tron",
                "status": "waiting",
            }),
        ]
        mgr.send_shift(
            deposit_coin="btc",
            deposit_network="bitcoin",
            settle_coin="usdt",
            settle_network="tron",
            settle_address="TXYZ",
            deposit_amount="0.001",
            wallet_name="default",
        )
        # Used the BTC manager, not the Liquid one
        assert wm.sent == []
        assert btc.sent == [("bitcoin", "default", "bc1qdeposit", 100_000, None)]

    def test_send_rejects_non_native_deposit_chain(self, manager_setup):
        mgr, _, _, _ = manager_setup
        with pytest.raises(ValueError, match="Cannot sign on"):
            mgr.send_shift(
                deposit_coin="usdt",
                deposit_network="tron",
                settle_coin="btc",
                settle_network="liquid",
                settle_address="lq1qfoo",
                deposit_amount="100",
                wallet_name="default",
            )

    def test_send_rejects_unknown_wallet(self, manager_setup):
        mgr, _, _, _ = manager_setup
        with pytest.raises(ValueError, match="not found"):
            mgr.send_shift(
                deposit_coin="usdt",
                deposit_network="liquid",
                settle_coin="usdt",
                settle_network="tron",
                settle_address="TXYZ",
                deposit_amount="100",
                wallet_name="ghost",
                liquid_asset_id=USDT_LIQUID,
            )

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_send_persists_shift_before_broadcast_so_failure_is_recoverable(
        self, mock_urlopen, manager_setup
    ):
        # If create_fixed_shift succeeded but broadcast failed, the shift
        # must still be on disk so the user can refund/retry.
        mgr, wm, _, storage = manager_setup
        mock_urlopen.side_effect = [
            _mock_response({"id": "q1", "depositAmount": "100",
                            "settleAmount": "99.5", "rate": "0.995"}),
            _mock_response({
                "id": "shift_persist",
                "depositAddress": "lq1qdeposit",
                "depositAmount": "100",
                "depositCoin": "USDT",
                "depositNetwork": "liquid",
                "settleCoin": "USDT",
                "settleNetwork": "tron",
                "status": "waiting",
            }),
        ]

        def boom(*args, **kwargs):
            raise RuntimeError("broadcast network down")

        wm.send = boom

        with pytest.raises(RuntimeError, match="broadcast network down"):
            mgr.send_shift(
                deposit_coin="usdt",
                deposit_network="liquid",
                settle_coin="usdt",
                settle_network="tron",
                settle_address="TXYZ",
                deposit_amount="100",
                wallet_name="default",
                liquid_asset_id=USDT_LIQUID,
            )

        loaded = storage.load_sideshift_shift("shift_persist")
        assert loaded is not None
        assert loaded.last_error and "broadcast network down" in loaded.last_error

    def test_send_rejects_lbtc_pair_not_in_allowlist(self, manager_setup):
        # L-BTC (`btc-liquid`) is intentionally not in the allowlist — for
        # L-BTC ↔ external the agent should use SideSwap or chain through
        # USDt-Liquid.
        mgr, _, _, _ = manager_setup
        with pytest.raises(ValueError, match="not in the curated allowlist"):
            mgr.send_shift(
                deposit_coin="btc",
                deposit_network="liquid",
                settle_coin="usdt",
                settle_network="tron",
                settle_address="TXYZ",
                deposit_amount="0.0005",
                wallet_name="default",
            )

    def test_send_rejects_unrecognized_settle_pair(self, manager_setup):
        # Settle leg must also be on the allowlist (e.g. ETH is not).
        mgr, _, _, _ = manager_setup
        with pytest.raises(ValueError, match="settle pair"):
            mgr.send_shift(
                deposit_coin="usdt",
                deposit_network="liquid",
                settle_coin="eth",
                settle_network="ethereum",
                settle_address="0xfoo",
                deposit_amount="100",
                wallet_name="default",
                liquid_asset_id=USDT_LIQUID,
            )

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_send_with_override_env_var_allows_lbtc(self, mock_urlopen, manager_setup, monkeypatch):
        # Power-user escape hatch: the env var bypasses the allowlist entirely.
        monkeypatch.setenv("SIDESHIFT_ALLOW_ALL_NETWORKS", "1")
        mgr, wm, _, _ = manager_setup
        mock_urlopen.side_effect = [
            _mock_response({"id": "q1", "depositAmount": "0.0005",
                            "settleAmount": "100", "rate": "200000"}),
            _mock_response({
                "id": "shift_override",
                "depositAddress": "lq1qdeposit",
                "depositAmount": "0.0005",
                "depositCoin": "BTC",
                "depositNetwork": "liquid",
                "settleCoin": "USDT",
                "settleNetwork": "tron",
                "status": "waiting",
            }),
        ]
        # L-BTC pair, normally rejected — passes with the override
        mgr.send_shift(
            deposit_coin="btc",
            deposit_network="liquid",
            settle_coin="usdt",
            settle_network="tron",
            settle_address="TXYZ",
            deposit_amount="0.0005",
            wallet_name="default",
        )
        assert wm.sent and wm.sent[0][3] == 50_000


class TestManagerReceive:
    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_receive_into_liquid_returns_deposit_address(self, mock_urlopen, manager_setup):
        mgr, _, _, storage = manager_setup
        mock_urlopen.return_value = _mock_response({
            "id": "shift_r",
            "depositAddress": "TXdepositAddr",
            "depositMin": "10",
            "depositMax": "10000",
            "settleAddress": "lq1qreceive",
            "depositCoin": "USDT",
            "depositNetwork": "tron",
            "settleCoin": "USDT",
            "settleNetwork": "liquid",
            "status": "waiting",
            "expiresAt": "2026-06-01T00:00:00Z",
        })
        shift = mgr.receive_shift(
            deposit_coin="usdt",
            deposit_network="tron",
            settle_coin="usdt",
            settle_network="liquid",
            wallet_name="default",
            external_refund_address="TXrefund",
        )
        assert shift.shift_id == "shift_r"
        assert shift.shift_type == "variable"
        assert shift.direction == "receive"
        assert shift.deposit_address == "TXdepositAddr"
        assert shift.settle_address == "lq1qreceive"
        assert shift.refund_address == "TXrefund"
        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["refundAddress"] == "TXrefund"
        assert body["settleAddress"] == "lq1qreceive"
        # Persisted
        assert storage.load_sideshift_shift("shift_r") is not None

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_receive_into_bitcoin_uses_btc_manager_address(self, mock_urlopen, manager_setup):
        mgr, _, btc, _ = manager_setup
        mock_urlopen.return_value = _mock_response({
            "id": "shift_btc_r",
            "depositAddress": "TXdepositAddr",
            "depositCoin": "USDT",
            "depositNetwork": "tron",
            "settleCoin": "BTC",
            "settleNetwork": "bitcoin",
            "settleAddress": "bc1qreceive",
            "status": "waiting",
        })
        mgr.receive_shift(
            deposit_coin="usdt", deposit_network="tron",
            settle_coin="btc", settle_network="bitcoin",
            wallet_name="default",
        )
        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["settleAddress"] == "bc1qreceive"

    def test_receive_rejects_non_native_settle_chain(self, manager_setup):
        mgr, _, _, _ = manager_setup
        with pytest.raises(ValueError, match="Cannot receive on"):
            mgr.receive_shift(
                deposit_coin="usdt", deposit_network="tron",
                settle_coin="usdt", settle_network="ethereum",
                wallet_name="default",
            )

    def test_receive_rejects_unrecognized_deposit_pair(self, manager_setup):
        # The deposit leg must be on the allowlist (only USDt across the 7
        # major chains and BTC mainchain).
        mgr, _, _, _ = manager_setup
        with pytest.raises(ValueError, match="deposit pair"):
            mgr.receive_shift(
                deposit_coin="eth", deposit_network="ethereum",
                settle_coin="usdt", settle_network="liquid",
                wallet_name="default",
            )

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_receive_with_override_env_var_allows_eth(
        self, mock_urlopen, manager_setup, monkeypatch
    ):
        monkeypatch.setenv("SIDESHIFT_ALLOW_ALL_NETWORKS", "1")
        mgr, _, _, _ = manager_setup
        mock_urlopen.return_value = _mock_response({
            "id": "shift_eth",
            "depositAddress": "0xeth_deposit",
            "depositCoin": "ETH",
            "depositNetwork": "ethereum",
            "settleCoin": "USDT",
            "settleNetwork": "liquid",
            "settleAddress": "lq1qreceive",
            "status": "waiting",
        })
        # ETH pair, normally rejected — passes with the override
        shift = mgr.receive_shift(
            deposit_coin="eth", deposit_network="ethereum",
            settle_coin="usdt", settle_network="liquid",
            wallet_name="default",
        )
        assert shift.deposit_address == "0xeth_deposit"


class TestManagerStatus:
    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_status_refreshes_persisted_record(self, mock_urlopen, manager_setup):
        mgr, _, _, storage = manager_setup
        # Pre-seed a pending shift
        original = SideShiftShift(
            shift_id="poll1",
            shift_type="variable",
            direction="receive",
            deposit_coin="USDT",
            deposit_network="tron",
            settle_coin="USDT",
            settle_network="liquid",
            settle_address="lq1qreceive",
            deposit_address="TXdepositAddr",
            refund_address=None,
            wallet_name="default",
            status="waiting",
            created_at="2026-05-08T12:00:00+00:00",
        )
        storage.save_sideshift_shift(original)

        mock_urlopen.return_value = _mock_response({
            "id": "poll1",
            "status": "settled",
            "depositHash": "TXhash",
            "settleHash": "lqsettlehash",
            "rate": "1.0",
            "depositAmount": "100",
            "settleAmount": "99.5",
        })
        result = mgr.status("poll1")
        assert result["status"] == "settled"
        assert result["deposit_hash"] == "TXhash"
        assert result["settle_hash"] == "lqsettlehash"
        assert result["is_final"] is True
        assert result["is_success"] is True
        assert result["is_failed"] is False
        # Updated record persisted
        loaded = storage.load_sideshift_shift("poll1")
        assert loaded.status == "settled"
        assert loaded.last_checked_at is not None

    def test_status_unknown_raises(self, manager_setup):
        mgr, _, _, _ = manager_setup
        with pytest.raises(ValueError, match="not found"):
            mgr.status("nope")

    @patch("aqua.sideshift.urllib.request.urlopen")
    def test_status_warns_on_remote_error(self, mock_urlopen, manager_setup):
        mgr, _, _, storage = manager_setup
        original = SideShiftShift(
            shift_id="poll2",
            shift_type="fixed",
            direction="send",
            deposit_coin="BTC",
            deposit_network="liquid",
            settle_coin="USDT",
            settle_network="tron",
            settle_address="TXYZ",
            deposit_address="lq1q",
            refund_address="lq1q",
            wallet_name="default",
            status="waiting",
            created_at="2026-05-08T12:00:00+00:00",
        )
        storage.save_sideshift_shift(original)

        mock_urlopen.side_effect = urllib.error.URLError("boom")
        result = mgr.status("poll2")
        assert "warning" in result
        # Status unchanged
        assert result["status"] == "waiting"
