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


# Well-known asset IDs. Liquid policy assets are global constants — these will
# never change, so importing the canonical id is preferable to redefining it.
LBTC_ASSET_ID = "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
USDT_LIQUID_ASSET_ID = "ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2"


# Mainnet known assets
MAINNET_ASSETS: dict[str, AssetInfo] = {
    info.asset_id: info
    for info in [
        AssetInfo(
            asset_id=LBTC_ASSET_ID,
            name="Liquid Bitcoin",
            ticker="L-BTC",
            logo="https://aqua-asset-logos.s3.us-west-2.amazonaws.com/L-BTC.svg",
            precision=8,
        ),
        AssetInfo(
            asset_id=USDT_LIQUID_ASSET_ID,
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


def lookup_asset_by_ticker(ticker: str, network: str = "mainnet") -> Optional[AssetInfo]:
    """Look up asset metadata by ticker (case-insensitive). Returns None if unknown."""
    registry = MAINNET_ASSETS if network == "mainnet" else TESTNET_ASSETS
    target = ticker.lower()
    for info in registry.values():
        if info.ticker.lower() == target:
            return info
    return None


def resolve_liquid_asset_id(
    coin: str,
    network: str,
    explicit_id: Optional[str] = None,
    *,
    asset_network: str = "mainnet",
) -> Optional[str]:
    """Resolve the Liquid asset id from a (coin, network) pair.

    Single source of truth for "given the user said `usdt` on Liquid, what
    is the hex asset id?" Used by CLI and MCP entrypoints so the agent /
    user never has to paste a 64-char hex.

    Returns:
        - None when `network` is not Liquid (Bitcoin sends don't take an
          asset id).
        - None when the deposit is L-BTC (coin == "btc" on liquid) — the
          wallet's `send` path defaults to L-BTC when no asset id is given.
        - `explicit_id` unchanged when the caller already supplied one
          (lets power users override the registry).
        - The hex asset id from the registry when the ticker matches a
          known Liquid asset.

    Raises:
        ValueError when network is Liquid, coin is not L-BTC, no explicit
        id was supplied, and the ticker is unknown. Error message lists
        every known ticker so the caller can correct their input.
    """
    if network.lower() != "liquid":
        return None
    if coin.lower() == "btc":
        return None
    if explicit_id:
        return explicit_id
    info = lookup_asset_by_ticker(coin, asset_network)
    if info is None:
        registry = MAINNET_ASSETS if asset_network == "mainnet" else TESTNET_ASSETS
        known = ", ".join(sorted(i.ticker for i in registry.values()))
        raise ValueError(
            f"Unknown Liquid asset ticker {coin!r}. Known tickers: {known}. "
            "Pass an explicit asset_id to override the registry."
        )
    return info.asset_id
