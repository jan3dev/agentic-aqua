"""Liquid network CLI commands."""

import click

from ..assets import MAINNET_ASSETS, TESTNET_ASSETS, lookup_asset_by_ticker
from ..tools import (
    get_manager,
    lw_address,
    lw_balance,
    lw_export_descriptor,
    lw_import_descriptor,
    lw_send,
    lw_send_asset,
    lw_transactions,
    lw_tx_status,
)
from .output import run_tool
from .password import handle_password_retry, resolve_secret


def _index_non_negative(ctx, param, value):
    if value is not None and value < 0:
        raise click.BadParameter("index must be non-negative", param=param, ctx=ctx)
    return value


@click.group()
def liquid():
    """Liquid network operations (L-BTC, assets)."""
    pass


@liquid.command("balance")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.pass_obj
def balance(ctx, wallet_name):
    """Get Liquid wallet balance (all assets)."""
    run_tool(ctx, lambda: lw_balance(wallet_name))


@liquid.command("address")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.option(
    "--index",
    type=int,
    default=None,
    callback=_index_non_negative,
    help="Specific address index.",
)
@click.pass_obj
def address(ctx, wallet_name, index):
    """Generate a Liquid receive address."""
    run_tool(ctx, lambda: lw_address(wallet_name, index))


@liquid.command("transactions")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="Max transactions.",
)
@click.pass_obj
def transactions(ctx, wallet_name, limit):
    """List Liquid transaction history."""
    run_tool(ctx, lambda: lw_transactions(wallet_name, limit))


@liquid.command("send")
@click.option("--wallet-name", required=True, help="Name of the wallet.")
@click.option("--address", required=True, help="Destination Liquid address.")
@click.option(
    "--amount",
    required=True,
    type=click.IntRange(min=1),
    help="Amount in satoshis (must be >= 1).",
)
@click.option(
    "--password-stdin",
    "password_stdin",
    is_flag=True,
    default=False,
    help=(
        "Read wallet password from stdin (piped) or prompt interactively. "
        "Without this flag, falls back to the AQUA_PASSWORD environment variable, "
        "then to no password."
    ),
)
@click.pass_obj
def send(ctx, wallet_name, address, amount, password_stdin):
    """Send L-BTC to an address."""
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            lw_send,
            {
                "wallet_name": wallet_name,
                "address": address,
                "amount": amount,
                "password": password,
            },
        ),
    )


@liquid.command("send-asset")
@click.option("--wallet-name", required=True, help="Name of the wallet.")
@click.option("--address", required=True, help="Destination Liquid address.")
@click.option("--amount", required=True, type=int, help="Amount in satoshis.")
@click.option("--asset-id", default=None, help="Asset ID (hex string).")
@click.option(
    "--asset-ticker",
    default=None,
    help="Asset ticker (case-insensitive, e.g. USDt, DePix). Resolved via the registry.",
)
@click.option(
    "--password-stdin",
    "password_stdin",
    is_flag=True,
    default=False,
    help=(
        "Read wallet password from stdin (piped) or prompt interactively. "
        "Without this flag, falls back to the AQUA_PASSWORD environment variable, "
        "then to no password."
    ),
)
@click.pass_obj
def send_asset(ctx, wallet_name, address, amount, asset_id, asset_ticker, password_stdin):
    """Send a Liquid asset to an address."""
    if amount <= 0:
        raise click.UsageError("Amount must be a positive integer.")
    if bool(asset_id) == bool(asset_ticker):
        raise click.UsageError("Provide exactly one of --asset-id or --asset-ticker.")
    if asset_ticker:
        wallet_data = get_manager().storage.load_wallet(wallet_name)
        if wallet_data is None:
            raise click.UsageError(f"Wallet '{wallet_name}' not found.")
        info = lookup_asset_by_ticker(asset_ticker, wallet_data.network)
        if info is None:
            raise click.UsageError(
                f"Unknown ticker '{asset_ticker}' on {wallet_data.network}. "
                "Run 'aqua-cli liquid assets' to list known tickers."
            )
        asset_id = info.asset_id
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            lw_send_asset,
            {
                "wallet_name": wallet_name,
                "address": address,
                "amount": amount,
                "asset_id": asset_id,
                "password": password,
            },
        ),
    )


@liquid.command("assets")
@click.option(
    "--network",
    type=click.Choice(["mainnet", "testnet"]),
    default="mainnet",
    show_default=True,
    help="Network registry to list.",
)
@click.pass_obj
def assets(ctx, network):
    """List known Liquid assets (asset_id, ticker, name, precision)."""
    registry = MAINNET_ASSETS if network == "mainnet" else TESTNET_ASSETS
    run_tool(
        ctx,
        lambda: {
            "network": network,
            "count": len(registry),
            "assets": [
                {
                    "asset_id": info.asset_id,
                    "ticker": info.ticker,
                    "name": info.name,
                    "precision": info.precision,
                }
                for info in registry.values()
            ],
        },
    )


@liquid.command("tx-status")
@click.option("--tx", required=True, help="Transaction ID (hex) or Blockstream URL.")
@click.pass_obj
def tx_status(ctx, tx):
    """Get Liquid transaction status."""
    run_tool(ctx, lambda: lw_tx_status(tx))


@liquid.command("import-descriptor")
@click.option("--descriptor", required=True, help="CT descriptor string.")
@click.option("--wallet-name", required=True, help="Name for the wallet.")
@click.option(
    "--network",
    type=click.Choice(["mainnet", "testnet"]),
    default="mainnet",
    show_default=True,
    help="Network to use.",
)
@click.pass_obj
def import_descriptor(ctx, descriptor, wallet_name, network):
    """Import a watch-only Liquid wallet from a CT descriptor.

    Note: imports Liquid only. For Bitcoin watch-only, use
    `aqua btc import-descriptor`. The Bitcoin descriptor cannot be derived
    from this CT descriptor (different derivation path + xpub).
    """
    run_tool(ctx, lambda: lw_import_descriptor(descriptor, wallet_name, network))


@liquid.command("export-descriptor")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.pass_obj
def export_descriptor(ctx, wallet_name):
    """Export the Liquid CT descriptor for watch-only import elsewhere.

    Note: exports Liquid only. For the Bitcoin descriptor of the same wallet,
    use `aqua btc export-descriptor`.
    """
    run_tool(ctx, lambda: lw_export_descriptor(wallet_name))
