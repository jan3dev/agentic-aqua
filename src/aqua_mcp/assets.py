"""Known Liquid Network asset registry."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class AssetInfo:
    """Metadata for a known Liquid asset."""
    asset_id: str
    name: str
    ticker: str
    logo: str
    precision: int  # Number of decimal places (e.g. 8 means divide by 10^8)


# Mainnet known assets
MAINNET_ASSETS: dict[str, AssetInfo] = {
    info.asset_id: info for info in [
        AssetInfo(
            asset_id="6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d",
            name="Liquid Bitcoin",
            ticker="L-BTC",
            logo="https://aqua-asset-logos.s3.us-west-2.amazonaws.com/L-BTC.svg",
            precision=8,
        ),
        AssetInfo(
            asset_id="ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2",
            name="Tether USDt",
            ticker="USDt",
            logo="https://aqua-asset-logos.s3.us-west-2.amazonaws.com/USDt.svg",
            precision=8,
        ),
        AssetInfo(
            asset_id="3438ecb49fc45c08e687de4749ed628c511e326460ea4336794e1cf02741329e",
            name="JPY Stablecoin",
            ticker="JPYS",
            logo="https://aqua-asset-logos.s3.us-west-2.amazonaws.com/JPYS.svg",
            precision=8,
        ),
        AssetInfo(
            asset_id="18729918ab4bca843656f08d4dd877bed6641fbd596a0a963abbf199cfeb3cec",
            name="PEGx EURx",
            ticker="EURx",
            logo="https://aqua-asset-logos.s3.us-west-2.amazonaws.com/EURx.svg",
            precision=8,
        ),
        AssetInfo(
            asset_id="26ac924263ba547b706251635550a8649545ee5c074fe5db8d7140557baaf32e",
            name="Mexas",
            ticker="MEX",
            logo="https://aqua-asset-logos.s3.us-west-2.amazonaws.com/MEX.svg",
            precision=8,
        ),
        AssetInfo(
            asset_id="02f22f8d9c76ab41661a2729e4752e2c5d1a263012141b86ea98af5472df5189",
            name="DePix",
            ticker="DePix",
            logo="https://aqua-asset-logos.s3.us-west-2.amazonaws.com/DePix.svg",
            precision=8,
        ),
    ]
}

# Testnet known assets (policy asset only for now)
TESTNET_ASSETS: dict[str, AssetInfo] = {}


def lookup_asset(asset_id: str, network: str = "mainnet") -> Optional[AssetInfo]:
    """Look up asset metadata by ID. Returns None if unknown."""
    registry = MAINNET_ASSETS if network == "mainnet" else TESTNET_ASSETS
    return registry.get(asset_id)


def resolve_asset_name(asset_id: str, network: str = "mainnet") -> str:
    """Return ticker if known, otherwise truncated asset ID."""
    info = lookup_asset(asset_id, network)
    if info:
        return info.ticker
    return asset_id[:8] + "..."
