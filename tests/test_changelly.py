"""Tests for Changelly cross-chain swap integration.

Mocks `urllib.request.urlopen` for the HTTP client; the manager-level tests
fake the wallet manager since Changelly never touches PSETs (custodial; we
just send to a deposit address).
"""

from __future__ import annotations

import io
import json
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aqua.changelly import (
    ALLOWED_PAIRS,
    CHANGELLY_BASE_URL,
    EXTERNAL_USDT_IDS,
    LIQUID_USDT_HEX,
    LIQUID_USDT_ID,
    NETWORK_TO_USDT_ID,
    ChangellyClient,
    ChangellyManager,
    ChangellySwap,
    _check_pair_allowed,
    _decimal_to_sats,
    _validate_settle_address,
    changelly_track_url,
    network_to_asset_id,
    swap_is_failed,
    swap_is_final,
    swap_is_success,
)
from aqua.storage import Storage, WalletData


def _mock_response(data, status=200):
    resp = MagicMock()
    if isinstance(data, (dict, list)):
        resp.read.return_value = json.dumps(data).encode()
    elif isinstance(data, str):
        # Status endpoint returns a bare string
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
    @pytest.mark.parametrize("decimal_str, expected", [
        ("100", 10_000_000_000),  # 100 USDt-Liquid (8-decimal)
        ("0.0005", 50_000),
        ("0.00000001", 1),
        ("0", 0),
    ])
    def test_known_amounts(self, decimal_str, expected):
        assert _decimal_to_sats(decimal_str) == expected


class TestStatusHelpers:
    @pytest.mark.parametrize("s", ["finished"])
    def test_finished_is_success(self, s):
        assert swap_is_success(s) is True
        assert swap_is_final(s) is True
        assert swap_is_failed(s) is False

    @pytest.mark.parametrize("s", ["failed", "refunded", "expired", "overdue"])
    def test_failed_terminal_states(self, s):
        assert swap_is_success(s) is False
        assert swap_is_final(s) is True
        assert swap_is_failed(s) is True

    @pytest.mark.parametrize("s", ["new", "waiting", "confirming", "exchanging", "sending"])
    def test_pending_states(self, s):
        assert swap_is_final(s) is False
        assert swap_is_success(s) is False
        assert swap_is_failed(s) is False

    def test_hold_is_terminal_but_not_success_or_failed(self):
        # "hold" means under manual review — terminal-ish but ambiguous
        assert swap_is_final("hold") is True
        assert swap_is_success("hold") is False
        assert swap_is_failed("hold") is False

    def test_case_insensitive(self):
        assert swap_is_success("FINISHED") is True

    def test_track_url(self):
        assert changelly_track_url("abc123") == "https://changelly.com/track/abc123"


class TestNetworkToAssetId:
    @pytest.mark.parametrize("network, expected", [
        ("liquid", "lusdt"),
        ("ethereum", "usdt20"),
        ("tron", "usdtrx"),
        ("bsc", "usdtbsc"),
        ("solana", "usdtsol"),
        ("polygon", "usdtpolygon"),
        ("ton", "usdton"),
        ("TRON", "usdtrx"),  # case-insensitive
    ])
    def test_known_networks(self, network, expected):
        assert network_to_asset_id(network) == expected

    def test_unknown_network_raises(self):
        with pytest.raises(ValueError, match="Unknown network"):
            network_to_asset_id("avalanche")

    def test_error_message_lists_supported(self):
        with pytest.raises(ValueError) as exc:
            network_to_asset_id("doge")
        assert "tron" in str(exc.value)
        assert "ethereum" in str(exc.value)


# ---------------------------------------------------------------------------
# Allowlist (ALLOWED_PAIRS) — matches AQUA Flutter's curated Changelly set
# ---------------------------------------------------------------------------


class TestAllowedPairs:
    def test_allowlist_matches_aqua_flutter(self):
        # Drift from AQUA's `ChangellyAssetIds` set in
        # `lib/features/changelly/models/changelly_models.dart` should fail
        # loudly so we have a forced conversation about it.
        expected_external = {"usdt20", "usdtrx", "usdtbsc", "usdtsol", "usdtpolygon", "usdton"}
        assert set(EXTERNAL_USDT_IDS) == expected_external
        # 6 chains × 2 directions = 12 ordered pairs
        assert len(ALLOWED_PAIRS) == 12

    def test_btc_lbtc_usdc_etc_NOT_in_allowlist(self):
        # Explicitly: only USDt, only the 6 external chains, only paired
        # with USDt-Liquid.
        assert ("btc", "lusdt") not in ALLOWED_PAIRS
        assert ("lbtc", "usdtrx") not in ALLOWED_PAIRS
        assert ("usdc", "lusdt") not in ALLOWED_PAIRS
        # Same external chain ↔ same external chain not allowed (must touch Liquid)
        assert ("usdtrx", "usdt20") not in ALLOWED_PAIRS

    @pytest.mark.parametrize("from_id, to_id", [
        ("lusdt", "usdt20"),
        ("lusdt", "usdtrx"),
        ("usdt20", "lusdt"),
        ("usdtrx", "lusdt"),
        ("LUSDT", "USDT20"),  # case-insensitive
    ])
    def test_allowed_pairs_pass(self, from_id, to_id):
        _check_pair_allowed(from_id, to_id)

    @pytest.mark.parametrize("from_id, to_id", [
        ("btc", "lusdt"),       # BTC not allowed
        ("lbtc", "usdtrx"),     # L-BTC not allowed
        ("usdc", "lusdt"),      # USDC not allowed
        ("usdtrx", "usdt20"),   # external-to-external not allowed
        ("lusdt", "lusdt"),     # same on both sides
    ])
    def test_disallowed_pairs_raise(self, from_id, to_id):
        with pytest.raises(ValueError, match="not in the curated allowlist"):
            _check_pair_allowed(from_id, to_id)

    def test_error_message_includes_helpful_info(self):
        with pytest.raises(ValueError) as exc:
            _check_pair_allowed("btc", "lusdt")
        msg = str(exc.value)
        assert "lusdt" in msg
        assert "usdt20" in msg or "usdtrx" in msg
        assert "CHANGELLY_ALLOW_ALL_PAIRS" in msg

    def test_override_env_var_bypasses_check(self, monkeypatch):
        monkeypatch.setenv("CHANGELLY_ALLOW_ALL_PAIRS", "1")
        # Now arbitrary pairs pass
        _check_pair_allowed("btc", "lusdt")
        _check_pair_allowed("usdtrx", "usdt20")

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
    def test_override_env_var_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("CHANGELLY_ALLOW_ALL_PAIRS", value)
        _check_pair_allowed("btc", "lusdt")

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "blah"])
    def test_override_env_var_falsy_values_keep_enforcement(self, monkeypatch, value):
        monkeypatch.setenv("CHANGELLY_ALLOW_ALL_PAIRS", value)
        with pytest.raises(ValueError, match="not in the curated allowlist"):
            _check_pair_allowed("btc", "lusdt")


# ---------------------------------------------------------------------------
# settle_address validation
# ---------------------------------------------------------------------------


class TestValidateSettleAddress:
    @pytest.mark.parametrize("network, address", [
        ("tron",     "T" + "X" * 33),                           # T + 33 base58 = 34
        ("ethereum", "0xAbCdEf1234567890abcdef1234567890abCdEf12"),  # 0x + 40 hex = 42
        ("bsc",      "0xAbCdEf1234567890abcdef1234567890abCdEf12"),
        ("polygon",  "0xAbCdEf1234567890abcdef1234567890abCdEf12"),
        ("solana",   "So11111111111111111111111111111111111111112"),  # 44-char wrapped SOL mint
        ("ton",      "EQ" + "D" * 46),                          # EQ + 46 base64url = 48
        ("ton",      "UQ" + "D" * 46),                          # UQ + 46 base64url = 48
    ])
    def test_valid_addresses_pass(self, network, address):
        _validate_settle_address(network, address)

    @pytest.mark.parametrize("network, address, match", [
        ("tron",     "",                    "empty"),
        ("tron",     "   ",                 "empty"),
        ("tron",     "0xAbCd1234",          "valid tron"),  # Ethereum addr on Tron
        ("ethereum", "TXYZabc123456789012", "valid ethereum"),  # Tron addr on ETH
        ("ethereum", "0xShort",            "valid ethereum"),
        ("solana",   "0x1234",             "valid solana"),
        ("ton",      "0x1234",             "valid ton"),
    ])
    def test_invalid_addresses_raise(self, network, address, match):
        with pytest.raises(ValueError, match=match):
            _validate_settle_address(network, address)

    def test_unknown_network_passes_through(self):
        # No pattern for unknown network → no crash, just no validation.
        _validate_settle_address("unknownchain", "some_address_123")


# ---------------------------------------------------------------------------
# ChangellySwap dataclass + storage round-trip
# ---------------------------------------------------------------------------


class TestChangellySwap:
    def _make(self, **overrides) -> ChangellySwap:
        defaults = dict(
            order_id="abc123",
            swap_type="fixed",
            direction="send",
            from_asset="lusdt",
            to_asset="usdtrx",
            settle_address="TXrecv",
            deposit_address="lq1qdeposit",
            refund_address="lq1qrefund",
            wallet_name="default",
            status="new",
            created_at="2026-05-08T12:00:00+00:00",
        )
        defaults.update(overrides)
        return ChangellySwap(**defaults)

    def test_roundtrip(self):
        s = self._make(amount_from="100", amount_to="99.5",
                       deposit_hash="lqtxid" * 8, track_url="https://changelly.com/track/abc123")
        assert ChangellySwap.from_dict(s.to_dict()) == s

    def test_from_dict_backward_compat(self):
        data = {
            "order_id": "old1",
            "swap_type": "variable",
            "direction": "receive",
            "from_asset": "usdtrx",
            "to_asset": "lusdt",
            "settle_address": "lq1q",
            "deposit_address": "TXY",
            "refund_address": None,
            "wallet_name": "default",
            "status": "new",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        swap = ChangellySwap.from_dict(data)
        assert swap.amount_from is None
        assert swap.deposit_hash is None
        assert swap.last_error is None


class TestStorage:
    def test_save_load_roundtrip(self, storage):
        swap = ChangellySwap(
            order_id="abc123",
            swap_type="fixed",
            direction="send",
            from_asset="lusdt",
            to_asset="usdtrx",
            settle_address="TXrecv",
            deposit_address="lq1qdeposit",
            refund_address="lq1qrefund",
            wallet_name="default",
            status="new",
            created_at="2026-05-08T12:00:00+00:00",
        )
        storage.save_changelly_swap(swap)
        loaded = storage.load_changelly_swap("abc123")
        assert loaded == swap

    def test_load_missing_returns_none(self, storage):
        assert storage.load_changelly_swap("nope") is None

    def test_invalid_order_id_rejected(self, storage):
        swap = ChangellySwap(
            order_id="../escape",
            swap_type="fixed",
            direction="send",
            from_asset="lusdt",
            to_asset="usdtrx",
            settle_address="TX",
            deposit_address="lq1q",
            refund_address=None,
            wallet_name=None,
            status="new",
            created_at="2026-05-08T12:00:00+00:00",
        )
        with pytest.raises(ValueError, match="Invalid Changelly order ID"):
            storage.save_changelly_swap(swap)

    def test_list_swaps(self, storage):
        for sid in ("a", "b", "c"):
            storage.save_changelly_swap(ChangellySwap(
                order_id=sid, swap_type="fixed", direction="send",
                from_asset="lusdt", to_asset="usdtrx",
                settle_address="TX", deposit_address="lq1q",
                refund_address=None, wallet_name=None,
                status="new", created_at="2026-05-08T12:00:00+00:00",
            ))
        assert set(storage.list_changelly_swaps()) == {"a", "b", "c"}

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX file permissions not enforced on Windows",
    )
    def test_file_permissions_0600(self, storage):
        import os

        storage.save_changelly_swap(ChangellySwap(
            order_id="permcheck", swap_type="fixed", direction="send",
            from_asset="lusdt", to_asset="usdtrx",
            settle_address="TX", deposit_address="lq1q",
            refund_address=None, wallet_name=None,
            status="new", created_at="2026-05-08T12:00:00+00:00",
        ))
        path = storage.changelly_swaps_dir / "permcheck.json"
        assert (os.stat(path).st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# REST client (HTTP layer)
# ---------------------------------------------------------------------------


class TestChangellyClient:
    def test_default_base_url_is_aqua_proxy(self):
        client = ChangellyClient()
        assert client.base_url == CHANGELLY_BASE_URL.rstrip("/")
        assert "ankara.aquabtc.com" in client.base_url

    def test_custom_base_url(self):
        client = ChangellyClient(base_url="https://example.com/proxy/")
        # Trailing slash stripped
        assert client.base_url == "https://example.com/proxy"

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_get_currencies_handles_wrapped_result(self, mock_urlopen):
        # AQUA's proxy wraps in {result: [...]}
        mock_urlopen.return_value = _mock_response({"result": ["btc", "lusdt", "usdt20"]})
        client = ChangellyClient()
        assert client.get_currencies() == ["btc", "lusdt", "usdt20"]

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_get_currencies_handles_bare_list(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(["btc", "lusdt"])
        client = ChangellyClient()
        assert client.get_currencies() == ["btc", "lusdt"]

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_get_pairs_lowercases_filters(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response([{"from": "lusdt", "to": "usdtrx"}])
        client = ChangellyClient()
        client.get_pairs(from_asset="LUSDT", to_asset="USDTRX")
        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body == {"from": "lusdt", "to": "usdtrx"}

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_get_fix_rate_for_amount_sends_amount_from(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"id": "q1", "result": "0.99",
                                                     "amountFrom": "100"})
        client = ChangellyClient()
        client.get_fix_rate_for_amount("lusdt", "usdtrx", amount_from="100")
        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["from"] == "lusdt"
        assert body["to"] == "usdtrx"
        assert body["amountFrom"] == "100"

    def test_get_fix_rate_requires_exactly_one_amount(self):
        client = ChangellyClient()
        with pytest.raises(ValueError, match="exactly one"):
            client.get_fix_rate_for_amount("lusdt", "usdtrx")
        with pytest.raises(ValueError, match="exactly one"):
            client.get_fix_rate_for_amount("lusdt", "usdtrx",
                                           amount_from="100", amount_to="99")

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_get_variable_quote_takes_first_of_list(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response([
            {"from": "lusdt", "to": "usdtrx", "rate": "0.99"},
            {"from": "lusdt", "to": "usdtrx", "rate": "0.98"},
        ])
        client = ChangellyClient()
        result = client.get_variable_quote("lusdt", "usdtrx", amount_from="100")
        assert result["rate"] == "0.99"

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_create_fixed_transaction_body(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"id": "ord1", "payinAddress": "lq1q"})
        client = ChangellyClient()
        client.create_fixed_transaction(
            from_asset="lusdt", to_asset="usdtrx",
            rate_id="r1", address="TX", refund_address="lq1qrefund",
            amount_from="100",
        )
        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body == {
            "from": "lusdt", "to": "usdtrx",
            "rateId": "r1", "address": "TX",
            "refundAddress": "lq1qrefund",
            "amountFrom": "100",
        }

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_create_variable_transaction_body(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"id": "ord2", "payinAddress": "TX"})
        client = ChangellyClient()
        client.create_variable_transaction(
            from_asset="usdtrx", to_asset="lusdt",
            address="lq1q", refund_address="TXrefund",
        )
        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["from"] == "usdtrx"
        assert body["to"] == "lusdt"
        assert body["address"] == "lq1q"
        assert body["refundAddress"] == "TXrefund"

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_get_status_handles_bare_string(self, mock_urlopen):
        # AQUA's proxy returns the status as a bare JSON string
        mock_urlopen.return_value = _mock_response("finished")
        client = ChangellyClient()
        assert client.get_status("ord1") == "finished"

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_http_error_extracts_message(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="https://ankara.aquabtc.com/api/v1/changelly/quote",
            code=400, msg="Bad Request", hdrs=None,
            fp=io.BytesIO(json.dumps({"error": "amount too small"}).encode()),
        )
        mock_urlopen.side_effect = err
        client = ChangellyClient()
        with pytest.raises(RuntimeError, match="amount too small"):
            client.get_currencies()

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_unreachable_host_raises(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("name resolution failed")
        client = ChangellyClient()
        with pytest.raises(RuntimeError, match="unreachable"):
            client.get_currencies()


# ---------------------------------------------------------------------------
# Manager (with mocked HTTP and wallet manager)
# ---------------------------------------------------------------------------


@pytest.fixture
def manager_setup(storage):
    """Build a ChangellyManager with a fake wallet manager + temp Storage."""
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
            self.sent.append((name, address, amount, asset_id, password))
            return "lqtxid" + ("0" * 58)

    wm = FakeWalletManager()
    mgr = ChangellyManager(storage=storage, wallet_manager=wm)
    return mgr, wm, storage


_TRON_ADDR = "T" + "X" * 33  # valid 34-char Tron address for tests


class TestManagerSend:
    @patch("aqua.changelly.urllib.request.urlopen")
    def test_send_lusdt_to_usdt_tron_happy_path(self, mock_urlopen, manager_setup):
        # The headline flow: USDt-Liquid → USDt-Tron via Changelly.
        mgr, wm, storage = manager_setup
        mock_urlopen.side_effect = [
            # Step 1: get_fix_rate_for_amount
            _mock_response({
                "id": "rate_xyz",
                "result": "0.99",
                "from": "lusdt",
                "to": "usdtrx",
                "amountFrom": "100",
                "amountTo": "99",
                "expiredAt": 1_900_000_000,
            }),
            # Step 2: create_fixed_transaction
            _mock_response({
                "id": "order_xyz",
                "trackUrl": "https://changelly.com/track/order_xyz",
                "createdAt": 1_700_000_000,
                "type": "fixed",
                "status": "new",
                "currencyFrom": "lusdt",
                "currencyTo": "usdtrx",
                "payinAddress": "lq1qdeposit",
                "amountExpectedFrom": "100",
                "payoutAddress": "TXrecv",
                "amountExpectedTo": "99",
                "networkFee": "1",
                "payTill": "2026-05-08T12:15:00Z",
            }),
        ]
        swap = mgr.send_swap(
            external_network="tron",
            amount_from="100",
            settle_address=_TRON_ADDR,
            wallet_name="default",
        )
        assert swap.order_id == "order_xyz"
        assert swap.deposit_address == "lq1qdeposit"
        assert swap.deposit_hash and swap.deposit_hash.startswith("lqtxid")
        # Refund address is the wallet's own Liquid address
        assert swap.refund_address == "lq1qreceive"
        # 100 USDt-Liquid = 100 * 1e8 sats; passed with the USDt-Liquid asset id
        assert wm.sent == [("default", "lq1qdeposit", 10_000_000_000, LIQUID_USDT_HEX, None)]
        # Persisted across the whole flow
        loaded = storage.load_changelly_swap("order_xyz")
        assert loaded is not None
        assert loaded.deposit_hash == swap.deposit_hash

    def test_send_rejects_unknown_external_network(self, manager_setup):
        mgr, _, _ = manager_setup
        with pytest.raises(ValueError, match="Unknown network"):
            mgr.send_swap(
                external_network="avalanche",
                amount_from="100",
                settle_address="0xfoo",
                wallet_name="default",
            )

    def test_send_rejects_unknown_wallet(self, manager_setup):
        mgr, _, _ = manager_setup
        with pytest.raises(ValueError, match="not found"):
            mgr.send_swap(
                external_network="tron",
                amount_from="100",
                settle_address=_TRON_ADDR,
                wallet_name="ghost",
            )

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_send_persists_swap_before_broadcast_so_failure_is_recoverable(
        self, mock_urlopen, manager_setup
    ):
        # If create_fixed_transaction succeeded but broadcast failed, the
        # swap must still be on disk for refund/retry.
        mgr, wm, storage = manager_setup
        mock_urlopen.side_effect = [
            _mock_response({"id": "rate1", "result": "0.99", "amountFrom": "100"}),
            _mock_response({
                "id": "order_persist",
                "payinAddress": "lq1qdeposit",
                "amountExpectedFrom": "100",
                "currencyFrom": "lusdt",
                "currencyTo": "usdtrx",
                "status": "new",
            }),
        ]

        def boom(*args, **kwargs):
            raise RuntimeError("broadcast network down")
        wm.send = boom

        with pytest.raises(RuntimeError, match="broadcast network down"):
            mgr.send_swap(
                external_network="tron",
                amount_from="100",
                settle_address=_TRON_ADDR,
                wallet_name="default",
            )
        loaded = storage.load_changelly_swap("order_persist")
        assert loaded is not None
        assert loaded.last_error and "broadcast network down" in loaded.last_error


class TestManagerReceive:
    @patch("aqua.changelly.urllib.request.urlopen")
    def test_receive_returns_deposit_address(self, mock_urlopen, manager_setup):
        mgr, _, storage = manager_setup
        mock_urlopen.return_value = _mock_response({
            "id": "ord_recv",
            "trackUrl": "https://changelly.com/track/ord_recv",
            "type": "float",
            "status": "new",
            "currencyFrom": "usdtrx",
            "currencyTo": "lusdt",
            "payinAddress": "TXdepositAddr",
            "payoutAddress": "lq1qreceive",
            "amountExpectedFrom": "0",
            "amountExpectedTo": "0",
            "networkFee": "1",
        })
        swap = mgr.receive_swap(
            external_network="tron",
            wallet_name="default",
            external_refund_address=_TRON_ADDR,
            amount_from="50",
        )
        assert swap.order_id == "ord_recv"
        assert swap.swap_type == "variable"
        assert swap.direction == "receive"
        assert swap.deposit_address == "TXdepositAddr"
        assert swap.refund_address == _TRON_ADDR
        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["from"] == "usdtrx"
        assert body["to"] == "lusdt"
        assert body["address"] == "lq1qreceive"
        assert body["refundAddress"] == _TRON_ADDR
        loaded = storage.load_changelly_swap("ord_recv")
        assert loaded is not None

    def test_receive_rejects_unknown_external_network(self, manager_setup):
        mgr, _, _ = manager_setup
        with pytest.raises(ValueError, match="Unknown network"):
            mgr.receive_swap(external_network="avalanche", wallet_name="default", amount_from="50")

    def test_receive_rejects_wrong_format_external_refund_address(self, manager_setup):
        # A Liquid-style refund address for a Tron deposit would result in a
        # stuck order if Changelly accepts it. Reject before the API call.
        mgr, _, _ = manager_setup
        with pytest.raises(ValueError, match="valid tron"):
            mgr.receive_swap(
                external_network="tron",
                wallet_name="default",
                external_refund_address="lq1qbogusrefund",
                amount_from="50",
            )


class TestManagerSendDepositSatsGuard:
    @patch("aqua.changelly.urllib.request.urlopen")
    def test_send_rejects_zero_deposit_sats(self, mock_urlopen, manager_setup):
        # Defence in depth: even if Changelly returns "0" as amountExpectedFrom
        # the manager must refuse to sign a zero-value send. We force the path
        # by skipping the quote call (rate_id supplied) and returning an order
        # with amountExpectedFrom == "0".
        mgr, _, storage = manager_setup
        mock_urlopen.return_value = _mock_response({
            "id": "order_zero",
            "payinAddress": "lq1qdeposit",
            "amountExpectedFrom": "0",
            "currencyFrom": "lusdt",
            "currencyTo": "usdtrx",
            "status": "new",
        })
        with pytest.raises(RuntimeError, match="non-positive deposit amount"):
            mgr.send_swap(
                external_network="tron",
                amount_from="100",
                settle_address=_TRON_ADDR,
                wallet_name="default",
                rate_id="prefetched",
            )
        # Swap must still be persisted with last_error so the user can
        # diagnose what happened.
        loaded = storage.load_changelly_swap("order_zero")
        assert loaded is not None
        assert loaded.status == "failed"
        assert loaded.last_error and "non-positive" in loaded.last_error


class TestValidateSettleAddressDriftGuard:
    def test_missing_pattern_for_supported_network_raises(self, monkeypatch):
        # If a future contributor adds a network to NETWORK_TO_USDT_ID without
        # a matching pattern in _ADDRESS_PATTERNS, validation must refuse to
        # silently no-op.
        from aqua import changelly as changelly_mod

        monkeypatch.setitem(changelly_mod.NETWORK_TO_USDT_ID, "newchain", "usdtnew")
        with pytest.raises(RuntimeError, match="_ADDRESS_PATTERNS is missing"):
            _validate_settle_address("newchain", "some_address_123")

    def test_truly_unknown_network_still_passes_through(self):
        # Networks not on the supported list (e.g. user typo) are not the
        # contributor-drift case; preserve the lenient no-op so we don't
        # block on unknown user input upstream of the asset-id lookup.
        _validate_settle_address("zzz_not_a_real_chain", "some_address_123")


class TestManagerStatus:
    @patch("aqua.changelly.urllib.request.urlopen")
    def test_status_refreshes_persisted_record(self, mock_urlopen, manager_setup):
        mgr, _, storage = manager_setup
        original = ChangellySwap(
            order_id="poll1", swap_type="fixed", direction="send",
            from_asset="lusdt", to_asset="usdtrx",
            settle_address="TX", deposit_address="lq1q",
            refund_address="lq1q", wallet_name="default",
            status="new", created_at="2026-05-08T12:00:00+00:00",
        )
        storage.save_changelly_swap(original)
        mock_urlopen.return_value = _mock_response("finished")

        result = mgr.status("poll1")
        assert result["status"] == "finished"
        assert result["is_final"] is True
        assert result["is_success"] is True
        assert result["is_failed"] is False
        loaded = storage.load_changelly_swap("poll1")
        assert loaded.status == "finished"
        assert loaded.last_checked_at is not None

    def test_status_unknown_raises(self, manager_setup):
        mgr, _, _ = manager_setup
        with pytest.raises(ValueError, match="not found"):
            mgr.status("nope")

    @patch("aqua.changelly.urllib.request.urlopen")
    def test_status_warns_on_remote_error(self, mock_urlopen, manager_setup):
        mgr, _, storage = manager_setup
        original = ChangellySwap(
            order_id="poll2", swap_type="fixed", direction="send",
            from_asset="lusdt", to_asset="usdtrx",
            settle_address="TX", deposit_address="lq1q",
            refund_address="lq1q", wallet_name="default",
            status="new", created_at="2026-05-08T12:00:00+00:00",
        )
        storage.save_changelly_swap(original)
        mock_urlopen.side_effect = urllib.error.URLError("boom")

        result = mgr.status("poll2")
        assert "warning" in result
        assert result["status"] == "new"  # unchanged
