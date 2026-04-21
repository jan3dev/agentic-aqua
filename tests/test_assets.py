"""Tests for the Liquid asset registry lookups."""

from aqua_mcp.assets import (
    MAINNET_ASSETS,
    lookup_asset,
    lookup_asset_by_ticker,
    resolve_asset_name,
)

USDT_ASSET_ID = "ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2"
LBTC_ASSET_ID = "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"


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
