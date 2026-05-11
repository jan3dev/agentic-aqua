"""Changelly CLI — USDt cross-chain swaps via AQUA's Ankara proxy."""

from __future__ import annotations

import logging

import click

logger = logging.getLogger(__name__)

from ..tools import (
    changelly_list_currencies,
    changelly_quote,
    changelly_receive,
    changelly_send,
    changelly_status,
)
from .output import run_tool
from .password import handle_password_retry, resolve_secret


_EXTERNAL_NETWORK = click.Choice([
    "ethereum", "tron", "bsc", "solana", "polygon", "ton",
])
_DIRECTION = click.Choice(["send", "receive"])
_PASSWORD_HELP = (
    "Read wallet password from stdin (piped) or prompt interactively. "
    "Without this flag, falls back to the AQUA_PASSWORD environment variable, "
    "then to no password."
)


@click.group()
def changelly():
    """Changelly — USDt cross-chain swaps (Liquid ↔ ETH/Tron/BSC/Solana/Polygon/TON).

    Routed through AQUA's Ankara backend proxy. For Liquid-only swaps
    (e.g. L-BTC ↔ USDt-Liquid) prefer `aqua sideswap` (atomic on Liquid).

    Scope: USDt-Liquid ↔ USDt on the 6 supported external chains. For BTC,
    L-BTC, or non-USDt swaps, use `aqua sideswap` or `aqua sideshift`.
    """


@changelly.command("currencies")
@click.pass_obj
def currencies(ctx):
    """List currencies Changelly supports (read-only; for discovery)."""
    run_tool(ctx, lambda: changelly_list_currencies())


@changelly.command("quote")
@click.option(
    "--external-network", required=True, type=_EXTERNAL_NETWORK,
    help="USDt network on the non-Liquid side.",
)
@click.option(
    "--direction", type=_DIRECTION, default="send", show_default=True,
    help="'send' = deposit USDt-Liquid; 'receive' = deposit USDt on external chain.",
)
@click.option("--amount-from", default=None, help="Deposit-side amount (decimal string).")
@click.option("--amount-to", default=None, help="Settle-side amount (decimal string).")
@click.pass_obj
def quote(ctx, external_network, direction, amount_from, amount_to):
    """Get a fixed-rate quote for a USDt-Liquid ↔ USDt-on-X swap."""
    if (amount_from is None) == (amount_to is None):
        raise click.UsageError("Provide exactly one of --amount-from or --amount-to.")
    run_tool(
        ctx,
        lambda: changelly_quote(
            external_network=external_network,
            direction=direction,
            amount_from=amount_from,
            amount_to=amount_to,
        ),
    )


@changelly.command("send")
@click.option(
    "--external-network", required=True, type=_EXTERNAL_NETWORK,
    help="Target USDt network (USDt arrives here).",
)
@click.option("--settle-address", required=True, help="External chain address to receive at.")
@click.option(
    "--amount-from", required=True,
    help="USDt-Liquid to send (decimal string, e.g. '100').",
)
@click.option("--wallet-name", default="default", show_default=True)
@click.option(
    "--yes", "-y", "skip_confirm", is_flag=True, default=False,
    help="Skip the interactive quote-confirmation prompt.",
)
@click.option(
    "--password-stdin", "password_stdin", is_flag=True, default=False,
    help=_PASSWORD_HELP,
)
@click.pass_obj
def send(ctx, external_network, settle_address, amount_from, wallet_name,
         skip_confirm, password_stdin):
    """Send USDt-Liquid out to USDt on another chain (fixed-rate).

    Gets a quote, creates the order, and broadcasts the USDt-Liquid deposit
    from the local wallet. A refund address is set automatically (the
    wallet's own Liquid address).
    """
    rate_id = None
    if not skip_confirm:
        click.echo("Fetching Changelly quote…", err=True)
        try:
            preview = changelly_quote(
                external_network=external_network,
                direction="send",
                amount_from=amount_from,
            )
        except Exception as e:
            raise click.UsageError(f"Could not fetch quote: {e}") from e
        rate_id = preview.get("id")
        click.echo(
            f"Send: {preview.get('amountFrom')} USDt-Liquid\n"
            f"Recv: {preview.get('amountTo')} USDt on {external_network} "
            f"at {settle_address}\n"
            f"Network fee: {preview.get('networkFee')}\n"
            f"Quote expires (epoch): {preview.get('expiredAt')}",
            err=True,
        )
        click.confirm("Proceed with this swap?", abort=True, err=True)

    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            changelly_send,
            {
                "external_network": external_network,
                "settle_address": settle_address,
                "amount_from": amount_from,
                "wallet_name": wallet_name,
                "password": password,
                "rate_id": rate_id,
            },
        ),
    )


@changelly.command("receive")
@click.option(
    "--external-network", required=True, type=_EXTERNAL_NETWORK,
    help="Source USDt network (external sender pays from here).",
)
@click.option("--wallet-name", default="default", show_default=True)
@click.option(
    "--external-refund-address", default=None,
    help="Source-chain refund address (STRONGLY RECOMMENDED).",
)
@click.option(
    "--amount-from", required=True,
    help="Amount the external sender will deposit (decimal string, e.g. '50').",
)
@click.pass_obj
def receive(ctx, external_network, wallet_name, external_refund_address, amount_from):
    """Create a variable-rate Changelly order to receive USDt-Liquid.

    Returns a deposit address on the source chain. The external sender pays
    to it from any USDt-supporting wallet on that network. Without an
    `--external-refund-address`, a stuck order requires manual intervention
    via Changelly's web UI.
    """
    if external_refund_address is None or not str(external_refund_address).strip():
        logger.warning(
            "Changelly receive: no --external-refund-address. Omitting it may leave "
            "orders stuck and require manual intervention via Changelly's web UI; "
            "use --external-refund-address with a source-chain refund address when possible."
        )
    run_tool(
        ctx,
        lambda: changelly_receive(
            external_network=external_network,
            wallet_name=wallet_name,
            external_refund_address=external_refund_address,
            amount_from=amount_from,
        ),
    )


@changelly.command("status")
@click.option("--order-id", required=True, help="ID returned from `aqua changelly send` or `… receive`.")
@click.pass_obj
def status(ctx, order_id):
    """Check the status of a Changelly swap order."""
    run_tool(ctx, lambda: changelly_status(order_id))
