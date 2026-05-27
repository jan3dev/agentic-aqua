"""Tests for the Liquid asset registry lookups."""

import pytest

from aqua.assets import (
    MAINNET_ASSETS,
    lookup_asset,
    lookup_asset_by_ticker,
    resolve_asset_name,
    resolve_liquid_asset_id,
)

USDT_ASSET_ID = "ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2"
LBTC_ASSET_ID = "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
DEPIX_ASSET_ID = "02f22f8d9c76ab41661a2729e4752e2c5d1a263012141b86ea98af5472df5189"


class TestLookupAssetByTicker:
    def test_exact_match(self):
        info = lookup_asset_by_ticker("USDt")
        assert info is not None
        assert info.asset_id == USDT_ASSET_ID
        assert info.ticker == "USDt"

    def test_case_insensitive_upper(self):
        info = lookup_asset_by_ticker("USDT")
        assert info is not None
        assert info.asset_id == USDT_ASSET_ID

    def test_case_insensitive_lower(self):
        info = lookup_asset_by_ticker("usdt")
        assert info is not None
        assert info.asset_id == USDT_ASSET_ID

    def test_case_insensitive_mixed(self):
        info = lookup_asset_by_ticker("DePix")
        assert info is not None
        info2 = lookup_asset_by_ticker("depix")
        assert info2 is not None
        assert info.asset_id == info2.asset_id

    def test_lbtc_ticker(self):
        info = lookup_asset_by_ticker("l-btc")
        assert info is not None
        assert info.asset_id == LBTC_ASSET_ID

    def test_unknown_ticker_returns_none(self):
        assert lookup_asset_by_ticker("NOTAREAL") is None

    def test_empty_ticker_returns_none(self):
        assert lookup_asset_by_ticker("") is None

    def test_testnet_registry_empty(self):
        """Testnet registry currently has no assets, so any ticker returns None."""
        assert lookup_asset_by_ticker("USDt", network="testnet") is None

    def test_round_trip_with_lookup_asset(self):
        """Ticker -> asset_id -> ticker should match for every known asset."""
        for info in MAINNET_ASSETS.values():
            resolved = lookup_asset_by_ticker(info.ticker)
            assert resolved is not None
            assert resolved.asset_id == info.asset_id
            assert lookup_asset(resolved.asset_id).ticker == info.ticker
            assert resolve_asset_name(resolved.asset_id) == info.ticker


class TestResolveLiquidAssetId:
    def test_bitcoin_network_returns_none(self):
        # BTC mainchain sends don't take a Liquid asset id.
        assert resolve_liquid_asset_id("btc", "bitcoin") is None

    def test_bitcoin_network_with_random_coin_returns_none(self):
        # Network gate: anything not "liquid" short-circuits before the
        # ticker is even inspected, so this must not raise.
        assert resolve_liquid_asset_id("usdt", "tron") is None
        assert resolve_liquid_asset_id("eth", "ethereum") is None

    def test_lbtc_returns_none(self):
        # L-BTC: the wallet's send path defaults to L-BTC when no asset_id
        # is given, so None is the correct sentinel here.
        assert resolve_liquid_asset_id("btc", "liquid") is None
        assert resolve_liquid_asset_id("BTC", "Liquid") is None

    def test_resolves_usdt_on_liquid(self):
        assert resolve_liquid_asset_id("usdt", "liquid") == USDT_ASSET_ID
        assert resolve_liquid_asset_id("USDT", "liquid") == USDT_ASSET_ID
        assert resolve_liquid_asset_id("USDt", "LIQUID") == USDT_ASSET_ID

    def test_resolves_depix_on_liquid(self):
        assert resolve_liquid_asset_id("depix", "liquid") == DEPIX_ASSET_ID
        assert resolve_liquid_asset_id("DePix", "liquid") == DEPIX_ASSET_ID

    def test_explicit_id_passes_through(self):
        # Power-user override: caller already has the hex, helper must not
        # second-guess it.
        custom = "deadbeef" * 8
        assert resolve_liquid_asset_id("usdt", "liquid", explicit_id=custom) == custom

    def test_explicit_id_ignored_for_lbtc(self):
        # L-BTC short-circuits to None even if an explicit id was passed —
        # the send path treats None as "use policy asset". Matches the
        # SideShiftManager guard.
        assert resolve_liquid_asset_id(
            "btc", "liquid", explicit_id="deadbeef" * 8,
        ) is None

    def test_unknown_ticker_raises(self):
        with pytest.raises(ValueError, match="Unknown Liquid asset ticker"):
            resolve_liquid_asset_id("NOTAREAL", "liquid")

    def test_unknown_ticker_error_lists_known_tickers(self):
        # The error message must be actionable: callers should be able to
        # see which tickers WILL resolve.
        with pytest.raises(ValueError) as exc_info:
            resolve_liquid_asset_id("WAT", "liquid")
        msg = str(exc_info.value)
        assert "USDt" in msg
        assert "DePix" in msg
        # Mentions the override escape hatch — actionable for both CLI and
        # MCP callers without naming a layer-specific command.
        assert "override the registry" in msg

    def test_testnet_network_param(self):
        # Testnet registry is currently empty, so any ticker raises.
        with pytest.raises(ValueError, match="Unknown Liquid asset ticker"):
            resolve_liquid_asset_id("usdt", "liquid", asset_network="testnet")
