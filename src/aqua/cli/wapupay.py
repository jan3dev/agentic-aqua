"""WapuPay CLI — Argentine direct-fiat payments funded with USDT on Liquid.

Pay an Argentine bank account (alias / CBU / CVU) in ARS, funded with USDT on
Liquid. These commands call WapuPay directly and require `WAPUPAY_API_KEY` set in
the environment. `create-order` returns a Liquid USDT address to fund; pay it with
`aqua liquid send-asset` and WapuPay settles the ARS payout.
"""

from __future__ import annotations

import click

from ..tools import (
    wapupay_create_order,
    wapupay_exchange_rates,
    wapupay_fund_order,
    wapupay_order_status,
    wapupay_provision_account,
    wapupay_quote,
    wapupay_spending_limit,
    wapupay_transaction,
    wapupay_transactions,
)
from ..wapupay import _validate_liquid_refund_address
from .output import run_tool

_TRANSFER_TYPE = click.Choice(["fiat_transfer", "fast_fiat_transfer"])


@click.group()
def wapupay():
    """WapuPay — pay Argentine bank accounts in ARS, funded with USDT on Liquid.

    Calls WapuPay directly; set WAPUPAY_API_KEY in your environment first.
    `create-order` returns a Liquid USDT address to fund — pay it with
    `aqua liquid send-asset` and WapuPay settles the pesos.
    """


@wapupay.command("rates")
@click.pass_obj
def rates(ctx):
    """Show WapuPay's current exchange rates (e.g. USDT/ARS)."""
    run_tool(ctx, lambda: wapupay_exchange_rates())


@wapupay.command("quote")
@click.option("--amount-ars", required=True, help="Amount in Argentine pesos (e.g. '10000').")
@click.option(
    "--type", "transfer_type", type=_TRANSFER_TYPE,
    default="fiat_transfer", show_default=True,
)
@click.option(
    "--alias", default=None,
    help="Recipient bank alias / CBU / CVU (optional; enables validation).",
)
@click.pass_obj
def quote(ctx, amount_ars, transfer_type, alias):
    """Preview the USDT cost, fee, and rate for an ARS payment (no order created)."""
    run_tool(ctx, lambda: wapupay_quote(amount_ars=amount_ars, type=transfer_type, alias=alias))


@wapupay.command("create-order")
@click.option(
    "--amount-ars", required=True,
    help="Amount to pay in Argentine pesos (e.g. '10000').",
)
@click.option("--alias", required=True, help="Recipient bank alias / CBU / CVU.")
@click.option(
    "--type", "transfer_type", type=_TRANSFER_TYPE,
    default="fiat_transfer", show_default=True,
)
@click.option("--receiver-name", default=None, help="Recipient name (optional).")
@click.option(
    "--refund-address", default=None,
    help="Liquid mainnet refund address (lq1…/ex1…) if funding cannot execute (optional).",
)
@click.option(
    "--wallet-name", default="default", show_default=True,
    help="Wallet you intend to fund from.",
)
@click.option(
    "--yes", "-y", "skip_confirm", is_flag=True, default=False,
    help="Skip the interactive quote-confirmation prompt.",
)
@click.pass_obj
def create_order(ctx, amount_ars, alias, transfer_type, receiver_name, refund_address,
                 wallet_name, skip_confirm):
    """Create a direct-fiat order and get a Liquid USDT funding address.

    Fetches a quote for confirmation, then creates the order and issues funding
    instructions. Fund the returned address with `aqua liquid send-asset`;
    WapuPay then pays the pesos. This command never broadcasts a payment.
    """
    if refund_address and refund_address.strip():
        try:
            _validate_liquid_refund_address(refund_address)
        except ValueError as e:
            raise click.UsageError(str(e)) from e

    if not skip_confirm:
        click.echo("Fetching WapuPay quote…", err=True)
        try:
            preview = wapupay_quote(amount_ars=amount_ars, type=transfer_type, alias=alias)
        except Exception as e:
            raise click.UsageError(f"Could not fetch quote: {e}") from e
        if preview.get("valid_cbu_alias") is False:
            raise click.UsageError(
                f"WapuPay reports the alias/CBU {alias!r} is not valid — aborting."
            )
        click.echo(
            f"Pay: {amount_ars} ARS to {alias}\n"
            f"Cost: {preview.get('usdt_amount')} USDT + {preview.get('fee')} fee "
            f"= {preview.get('total_amount')} USDT\n"
            f"Rate: {preview.get('exchange_rate')} ARS/USDT",
            err=True,
        )
        click.confirm("Create this payment order?", abort=True, err=True)

    run_tool(
        ctx,
        lambda: wapupay_create_order(
            amount_ars=amount_ars,
            alias=alias,
            type=transfer_type,
            receiver_name=receiver_name,
            refund_address=refund_address,
            wallet_name=wallet_name,
        ),
    )


@wapupay.command("fund-order")
@click.option("--tentative-id", required=True, help="Order id from `create-order`.")
@click.pass_obj
def fund_order(ctx, tentative_id):
    """Issue (or re-issue) the Liquid USDT funding instructions for an order."""
    run_tool(ctx, lambda: wapupay_fund_order(tentative_id))


@wapupay.command("order-status")
@click.option("--tentative-id", required=True, help="Order id from `create-order`.")
@click.pass_obj
def order_status(ctx, tentative_id):
    """Check a direct-fiat order's status (re-read from WapuPay)."""
    run_tool(ctx, lambda: wapupay_order_status(tentative_id))


@wapupay.command("transactions")
@click.pass_obj
def transactions(ctx):
    """List your WapuPay transactions."""
    run_tool(ctx, lambda: wapupay_transactions())


@wapupay.command("transaction")
@click.option("--id", "tx_id", required=True, help="WapuPay transaction id (uuid or numeric).")
@click.pass_obj
def transaction(ctx, tx_id):
    """Get a single WapuPay transaction by id."""
    run_tool(ctx, lambda: wapupay_transaction(tx_id))


@wapupay.command("spending-limit")
@click.pass_obj
def spending_limit(ctx):
    """Show your monthly WapuPay spending limit (USDT)."""
    run_tool(ctx, lambda: wapupay_spending_limit())


@wapupay.command("provision-account")
@click.pass_obj
def provision_account(ctx):
    """Provision your WapuPay API key via your AQUA account and store it locally.

    Use this when you have no WAPUPAY_API_KEY set. Requires an AQUA/JAN3 session first.
    The key is stored under ~/.aqua/wapupay/ and used automatically by `aqua wapupay` commands.
    """
    run_tool(ctx, lambda: wapupay_provision_account())
