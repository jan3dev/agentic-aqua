"""SideSwap CLI commands — pegs (BTC ↔ L-BTC) and atomic Liquid asset swaps.

Wraps the SideSwap MCP tools so users can drive pegs and swaps from a shell
without spinning up the MCP server. Mirrors the tool surface 1:1 — the
security-critical layer (PSET verification, fee_asset pinning) lives in the
manager and is unchanged here; this file is just argument parsing + output.
"""

from __future__ import annotations

import click

from ..tools import (
    sideswap_execute_swap,
    sideswap_list_assets,
    sideswap_peg_in,
    sideswap_peg_out,
    sideswap_peg_quote,
    sideswap_peg_status,
    sideswap_quote,
    sideswap_recommend,
    sideswap_server_status,
    sideswap_swap_status,
)
from .output import run_tool
from .password import handle_password_retry, resolve_secret


_NETWORK_OPTION = click.Choice(["mainnet", "testnet"])
_DIRECTION_OPTION = click.Choice(["btc_to_lbtc", "lbtc_to_btc"])

_PASSWORD_HELP = (
    "Read wallet password from stdin (piped) or prompt interactively. "
    "Without this flag, falls back to the AQUA_PASSWORD environment variable, "
    "then to no password."
)


@click.group()
def sideswap():
    """SideSwap operations — BTC ↔ L-BTC pegs and atomic Liquid asset swaps.

    Pegs charge ~0.1% (vs 0.2% on instant atomic swaps) and take ~20-60 minutes
    depending on direction and size. Swaps complete in seconds but pay slightly
    more in fees. Use `aqua sideswap recommend` for a quick decision-helper
    when converting between BTC and L-BTC.
    """


# ---------------------------------------------------------------------------
# Server / general info
# ---------------------------------------------------------------------------


@sideswap.command("status")
@click.option(
    "--network", type=_NETWORK_OPTION, default="mainnet", show_default=True,
    help="SideSwap network to query.",
)
@click.pass_obj
def status(ctx, network):
    """Show SideSwap server status — live fees, peg minimums, hot-wallet balance."""
    run_tool(ctx, lambda: sideswap_server_status(network))


@sideswap.command("recommend")
@click.option(
    "--amount", required=True, type=click.IntRange(min=1),
    help="Amount in satoshis to convert.",
)
@click.option(
    "--direction", required=True, type=_DIRECTION_OPTION,
    help="Direction of conversion (btc_to_lbtc or lbtc_to_btc).",
)
@click.option("--network", type=_NETWORK_OPTION, default="mainnet", show_default=True)
@click.pass_obj
def recommend(ctx, amount, direction, network):
    """Recommend peg vs swap-market trade for a BTC ↔ L-BTC conversion.

    Surfaces the time-vs-fee trade-off and warns if the amount exceeds
    SideSwap's hot-wallet liquidity (which would force the 102-confirmation
    cold-wallet path on peg-in).
    """
    run_tool(ctx, lambda: sideswap_recommend(amount, direction, network))


# ---------------------------------------------------------------------------
# Pegs
# ---------------------------------------------------------------------------


@sideswap.command("peg-quote")
@click.option(
    "--amount", required=True, type=click.IntRange(min=1),
    help="Send amount in satoshis.",
)
@click.option(
    "--peg-out", "peg_out", is_flag=True, default=False,
    help="Quote peg-out (L-BTC → BTC). Default: peg-in (BTC → L-BTC).",
)
@click.option("--network", type=_NETWORK_OPTION, default="mainnet", show_default=True)
@click.pass_obj
def peg_quote(ctx, amount, peg_out, network):
    """Quote receive amount for a peg at current SideSwap fees (0.1%)."""
    run_tool(ctx, lambda: sideswap_peg_quote(amount, not peg_out, network))


@sideswap.command("peg-in")
@click.option(
    "--wallet-name", default="default", show_default=True,
    help="Liquid wallet to receive L-BTC into.",
)
@click.option(
    "--password-stdin", "password_stdin", is_flag=True, default=False,
    help=_PASSWORD_HELP,
)
@click.pass_obj
def peg_in(ctx, wallet_name, password_stdin):
    """Initiate a peg-in (BTC → L-BTC). Prints the BTC deposit address.

    After this command, send BTC to the printed `peg_addr` from any Bitcoin
    wallet (including `aqua btc send`). Track progress with
    `aqua sideswap peg-status --order-id <id>`.

    Hot-wallet path: ~20 min for 2 BTC confirmations. For very large amounts
    that exceed SideSwap's hot-wallet liquidity, the cold-wallet path takes
    102 BTC confirmations (~17 hours). Run `aqua sideswap recommend` first
    to see which path applies.
    """
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            sideswap_peg_in,
            {"wallet_name": wallet_name, "password": password},
        ),
    )


@sideswap.command("peg-out")
@click.option(
    "--amount", required=True, type=click.IntRange(min=1),
    help="L-BTC sats to peg out.",
)
@click.option(
    "--btc-address", required=True,
    help="Destination Bitcoin address (bc1...).",
)
@click.option(
    "--wallet-name", default="default", show_default=True,
    help="Liquid wallet to send L-BTC from.",
)
@click.option(
    "--password-stdin", "password_stdin", is_flag=True, default=False,
    help=_PASSWORD_HELP,
)
@click.pass_obj
def peg_out(ctx, amount, btc_address, wallet_name, password_stdin):
    """Initiate a peg-out (L-BTC → BTC) and broadcast the L-BTC send.

    After 2 Liquid confirmations (~2 min) and the federation BTC sweep
    (typically 15-60 min total), BTC arrives at the destination address.
    Track progress with `aqua sideswap peg-status --order-id <id>`.
    """
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            sideswap_peg_out,
            {
                "wallet_name": wallet_name,
                "amount": amount,
                "btc_address": btc_address,
                "password": password,
            },
        ),
    )


@sideswap.command("peg-status")
@click.option("--order-id", required=True, help="Order ID returned from peg-in or peg-out.")
@click.pass_obj
def peg_status(ctx, order_id):
    """Check status of a peg order — confirmations, tx_state, payout txid."""
    run_tool(ctx, lambda: sideswap_peg_status(order_id))


# ---------------------------------------------------------------------------
# Asset swaps
# ---------------------------------------------------------------------------


def _resolve_asset(asset_id_arg: str | None, ticker_arg: str | None, network: str) -> str:
    """Mirror `aqua liquid send-asset`'s asset_id/ticker resolution."""
    from ..assets import lookup_asset_by_ticker

    if bool(asset_id_arg) == bool(ticker_arg):
        raise click.UsageError("Provide exactly one of --asset-id or --asset-ticker.")
    if asset_id_arg:
        return asset_id_arg
    info = lookup_asset_by_ticker(ticker_arg, network)
    if info is None:
        raise click.UsageError(
            f"Unknown ticker '{ticker_arg}' on {network}. "
            "Run 'aqua sideswap assets' to list known tickers."
        )
    return info.asset_id


@sideswap.command("assets")
@click.option("--network", type=_NETWORK_OPTION, default="mainnet", show_default=True)
@click.pass_obj
def assets(ctx, network):
    """List Liquid assets that SideSwap supports for atomic swaps."""
    run_tool(ctx, lambda: sideswap_list_assets(network))


@sideswap.command("quote")
@click.option("--asset-id", default=None, help="Asset ID (hex). One of --asset-id or --asset-ticker required.")
@click.option(
    "--asset-ticker", default=None,
    help="Asset ticker (case-insensitive, e.g. USDt). Resolved via the registry.",
)
@click.option(
    "--send-amount", default=None, type=click.IntRange(min=1),
    help="Amount the user sends (sats). Provide one of send/recv.",
)
@click.option(
    "--recv-amount", default=None, type=click.IntRange(min=1),
    help="Amount the user receives (sats). Provide one of send/recv.",
)
@click.option(
    "--reverse", is_flag=True, default=False,
    help="Reverse direction: sending the asset for L-BTC. Default: sending L-BTC for the asset.",
)
@click.option("--network", type=_NETWORK_OPTION, default="mainnet", show_default=True)
@click.pass_obj
def quote(ctx, asset_id, asset_ticker, send_amount, recv_amount, reverse, network):
    """Read-only price quote for a Liquid asset swap. No execution."""
    if (send_amount is None) == (recv_amount is None):
        raise click.UsageError("Provide exactly one of --send-amount or --recv-amount.")
    asset_id = _resolve_asset(asset_id, asset_ticker, network)
    run_tool(
        ctx,
        lambda: sideswap_quote(
            asset_id=asset_id,
            send_amount=send_amount,
            recv_amount=recv_amount,
            send_bitcoins=not reverse,
            network=network,
        ),
    )


@sideswap.command("swap")
@click.option("--asset-id", default=None, help="Non-L-BTC asset ID (hex). One of --asset-id or --asset-ticker required.")
@click.option(
    "--asset-ticker", default=None,
    help="Asset ticker (case-insensitive, e.g. USDt). Resolved via the registry.",
)
@click.option(
    "--amount", required=True, type=click.IntRange(min=1),
    help="Send amount in satoshis (L-BTC if forward, asset if --reverse).",
)
@click.option(
    "--reverse", is_flag=True, default=False,
    help="Reverse direction: sending the asset for L-BTC. Default: sending L-BTC for the asset.",
)
@click.option(
    "--wallet-name", default="default", show_default=True,
    help="Liquid wallet to sign with.",
)
@click.option(
    "--yes", "-y", "skip_confirm", is_flag=True, default=False,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--password-stdin", "password_stdin", is_flag=True, default=False,
    help=_PASSWORD_HELP,
)
@click.pass_obj
def swap(ctx, asset_id, asset_ticker, amount, reverse, wallet_name, skip_confirm, password_stdin):
    """Execute an atomic Liquid asset swap on SideSwap.

    Both directions are supported via --reverse. The PSET returned by SideSwap
    is verified locally against the agreed quote BEFORE signing — refuses to
    sign if the wallet's net balance change does not match exactly.

    Without --yes, prompts for explicit confirmation showing the quote and the
    direction. Without --password-stdin, falls back to AQUA_PASSWORD env var
    or no password.
    """
    from ..assets import lookup_asset

    # Resolve asset using the wallet's network so testnet tickers resolve correctly
    network = "mainnet"
    try:
        from ..tools import get_manager

        wallet_data = get_manager().storage.load_wallet(wallet_name)
        if wallet_data is not None:
            network = wallet_data.network
    except Exception:
        pass

    asset_id = _resolve_asset(asset_id, asset_ticker, network)

    # Resolve a human-readable label for the non-L-BTC side once.
    asset_info = lookup_asset(asset_id, network)
    asset_label = (
        asset_ticker
        if asset_ticker is not None
        else (asset_info.ticker if asset_info is not None else asset_id[:8] + "…")
    )
    send_label = asset_label if reverse else "L-BTC"
    recv_label = "L-BTC" if reverse else asset_label

    # Confirmation: show a fresh quote unless the user opted out, and pin the
    # confirmed recv_amount as a floor for the executor — protects against
    # rate drift between this price-stream preview and the mkt::* quote that
    # actually executes the swap.
    min_recv_amount: int | None = None
    if not skip_confirm:
        click.echo("Fetching quote from SideSwap…", err=True)
        try:
            preview = sideswap_quote(
                asset_id=asset_id,
                send_amount=amount,
                send_bitcoins=not reverse,
                network=network,
            )
        except Exception as e:
            raise click.ClickException(f"Could not fetch quote: {e}") from e
        click.echo(
            f"Send: {preview.get('send_amount')} sats of {send_label}\n"
            f"Recv: {preview.get('recv_amount')} sats of {recv_label}\n"
            f"Price: {preview.get('price')}\n"
            f"Fixed fee: {preview.get('fixed_fee')} sats",
            err=True,
        )
        if preview.get("error_msg"):
            raise click.ClickException(f"SideSwap quote error: {preview['error_msg']}")
        click.confirm("Proceed with this swap?", abort=True, err=True)
        recv = preview.get("recv_amount")
        if not isinstance(recv, int) or recv <= 0:
            raise click.ClickException(
                f"SideSwap returned an invalid recv_amount in the quote preview: {recv!r}. "
                "Refusing to proceed without a confirmed rate."
            )
        min_recv_amount = recv

    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            sideswap_execute_swap,
            {
                "asset_id": asset_id,
                "send_amount": amount,
                "wallet_name": wallet_name,
                "password": password,
                "send_bitcoins": not reverse,
                "min_recv_amount": min_recv_amount,
            },
        ),
    )


@sideswap.command("swap-status")
@click.option("--order-id", required=True, help="Order ID returned from `aqua sideswap swap`.")
@click.pass_obj
def swap_status(ctx, order_id):
    """Check status of an atomic asset swap order."""
    run_tool(ctx, lambda: sideswap_swap_status(order_id))
