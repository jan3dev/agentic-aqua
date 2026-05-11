"""SideShift CLI commands — custodial cross-chain swaps via sideshift.ai."""

from __future__ import annotations

import click

from ..tools import (
    sideshift_list_coins,
    sideshift_pair_info,
    sideshift_quote,
    sideshift_receive,
    sideshift_recommend,
    sideshift_send,
    sideshift_status,
)
from .output import run_tool
from .password import handle_password_retry, resolve_secret


_NATIVE_NETWORK = click.Choice(["bitcoin", "liquid"])
_PASSWORD_HELP = (
    "Read wallet password from stdin (piped) or prompt interactively. "
    "Without this flag, falls back to the AQUA_PASSWORD environment variable, "
    "then to no password."
)


@click.group()
def sideshift():
    """SideShift — custodial cross-chain swaps (USDt across networks, BTC ↔ USDt-on-X, etc.).

    SideShift is a custodial service: they take the deposit and send the
    converted asset from their hot wallet. The trust model is "trust SideShift
    the company" rather than "trust an on-chain protocol." For pairs where
    both legs are on Bitcoin or Liquid, prefer `aqua sideswap` (atomic on
    Liquid, or Liquid Federation peg).

    Curated pair allowlist (mirrors AQUA Flutter):
      USDt on ethereum / tron / bsc / solana / polygon / ton / liquid
      BTC on bitcoin

    Off-allowlist pairs raise an error from `send` and `receive`. Set
    SIDESHIFT_ALLOW_ALL_NETWORKS=1 in the environment to bypass.
    """


# ---------------------------------------------------------------------------
# Discovery / quote
# ---------------------------------------------------------------------------


@sideshift.command("coins")
@click.pass_obj
def coins(ctx):
    """List the coins and networks SideShift supports."""
    run_tool(ctx, lambda: sideshift_list_coins())


@sideshift.command("pair-info")
@click.option("--from-coin", required=True, help="Deposit coin ticker (e.g. USDT).")
@click.option("--from-network", required=True, help="Deposit network (e.g. liquid, tron).")
@click.option("--to-coin", required=True, help="Settle coin ticker.")
@click.option("--to-network", required=True, help="Settle network.")
@click.option("--amount", default=None, help="Optional reference amount in deposit-coin units (decimal string).")
@click.pass_obj
def pair_info(ctx, from_coin, from_network, to_coin, to_network, amount):
    """Show rate / min / max for a SideShift pair."""
    run_tool(
        ctx,
        lambda: sideshift_pair_info(from_coin, from_network, to_coin, to_network, amount),
    )


@sideshift.command("quote")
@click.option("--deposit-coin", required=True)
@click.option("--deposit-network", required=True)
@click.option("--settle-coin", required=True)
@click.option("--settle-network", required=True)
@click.option(
    "--deposit-amount", default=None,
    help="Amount the user is sending (decimal string). One of deposit/settle required.",
)
@click.option(
    "--settle-amount", default=None,
    help="Amount the user wants to receive (decimal string). One of deposit/settle required.",
)
@click.pass_obj
def quote(ctx, deposit_coin, deposit_network, settle_coin, settle_network,
          deposit_amount, settle_amount):
    """Request a fixed-rate SideShift quote (~15 min TTL)."""
    if (deposit_amount is None) == (settle_amount is None):
        raise click.UsageError("Provide exactly one of --deposit-amount or --settle-amount.")
    run_tool(
        ctx,
        lambda: sideshift_quote(
            deposit_coin, deposit_network, settle_coin, settle_network,
            deposit_amount=deposit_amount, settle_amount=settle_amount,
        ),
    )


@sideshift.command("recommend")
@click.option("--from-coin", required=True)
@click.option("--from-network", required=True)
@click.option("--to-coin", required=True)
@click.option("--to-network", required=True)
@click.pass_obj
def recommend(ctx, from_coin, from_network, to_coin, to_network):
    """Recommend SideSwap vs SideShift for a given pair."""
    run_tool(
        ctx,
        lambda: sideshift_recommend(from_coin, from_network, to_coin, to_network),
    )


# ---------------------------------------------------------------------------
# Send (we sign on Liquid/BTC, user provides external settle address)
# ---------------------------------------------------------------------------


@sideshift.command("send")
@click.option(
    "--deposit-coin", required=True,
    help="L-BTC: 'btc' (network='liquid'); BTC mainchain: 'btc'; USDt-Liquid: 'usdt'.",
)
@click.option(
    "--deposit-network", required=True, type=_NATIVE_NETWORK,
    help="Chain we sign on — must be 'bitcoin' or 'liquid'.",
)
@click.option("--settle-coin", required=True, help="Settle coin ticker (any SideShift-supported coin).")
@click.option("--settle-network", required=True, help="Settle network (any SideShift-supported network).")
@click.option("--settle-address", required=True, help="Where SideShift sends the converted asset.")
@click.option(
    "--deposit-amount", default=None,
    help="User sends this much (decimal string). One of deposit/settle required.",
)
@click.option(
    "--settle-amount", default=None,
    help="User wants to receive exactly this much (decimal string). One of deposit/settle required.",
)
@click.option("--wallet-name", default="default", show_default=True)
@click.option(
    "--liquid-asset-id", default=None,
    help="Hex asset id; required when deposit-coin is a non-L-BTC Liquid asset.",
)
@click.option("--settle-memo", default=None, help="Required for memo networks (TON, BNB, etc.).")
@click.option("--refund-memo", default=None)
@click.option(
    "--yes", "-y", "skip_confirm", is_flag=True, default=False,
    help="Skip the interactive quote-confirmation prompt.",
)
@click.option(
    "--password-stdin", "password_stdin", is_flag=True, default=False,
    help=_PASSWORD_HELP,
)
@click.pass_obj
def send(ctx, deposit_coin, deposit_network, settle_coin, settle_network,
         settle_address, deposit_amount, settle_amount, wallet_name,
         liquid_asset_id, settle_memo, refund_memo, skip_confirm, password_stdin):
    """Send funds via a SideShift fixed-rate shift.

    Gets a quote, creates the shift, and broadcasts the deposit from the
    local wallet. A refund address is set automatically (the wallet's own
    deposit-chain address). For non-L-BTC Liquid assets like USDt-Liquid,
    pass `--liquid-asset-id <hex>`.
    """
    if (deposit_amount is None) == (settle_amount is None):
        raise click.UsageError("Provide exactly one of --deposit-amount or --settle-amount.")

    quote_id: str | None = None
    if not skip_confirm:
        click.echo("Fetching quote from SideShift…", err=True)
        try:
            preview = sideshift_quote(
                deposit_coin, deposit_network, settle_coin, settle_network,
                deposit_amount=deposit_amount, settle_amount=settle_amount,
            )
        except Exception as e:
            raise click.UsageError(f"Could not fetch quote: {e}") from e
        click.echo(
            f"Send: {preview.get('depositAmount')} {deposit_coin.upper()} on {deposit_network}\n"
            f"Recv: {preview.get('settleAmount')} {settle_coin.upper()} at {settle_address} on {settle_network}\n"
            f"Rate: {preview.get('rate')}\n"
            f"Quote expires: {preview.get('expiresAt')}",
            err=True,
        )
        click.confirm("Proceed with this swap?", abort=True, err=True)
        # Reuse the confirmed quote so the shift executes at the rate the
        # user just saw, not whatever a fresh quote returns moments later.
        quote_id = preview.get("id")

    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            sideshift_send,
            {
                "deposit_coin": deposit_coin,
                "deposit_network": deposit_network,
                "settle_coin": settle_coin,
                "settle_network": settle_network,
                "settle_address": settle_address,
                "deposit_amount": deposit_amount,
                "settle_amount": settle_amount,
                "wallet_name": wallet_name,
                "password": password,
                "liquid_asset_id": liquid_asset_id,
                "settle_memo": settle_memo,
                "refund_memo": refund_memo,
                "quote_id": quote_id,
            },
        ),
    )


# ---------------------------------------------------------------------------
# Receive (we provide Liquid/BTC settle address, user sends from external)
# ---------------------------------------------------------------------------


@sideshift.command("receive")
@click.option("--deposit-coin", required=True, help="Source coin ticker (any SideShift-supported coin).")
@click.option("--deposit-network", required=True, help="Source network (any SideShift-supported network).")
@click.option(
    "--settle-coin", required=True,
    help="L-BTC: 'btc' (settle-network='liquid'); BTC: 'btc'; USDt-Liquid: 'usdt'.",
)
@click.option(
    "--settle-network", required=True, type=_NATIVE_NETWORK,
    help="Settle chain — must be 'bitcoin' or 'liquid'.",
)
@click.option("--wallet-name", default="default", show_default=True)
@click.option(
    "--external-refund-address", default=None,
    help="Deposit-chain refund address (STRONGLY RECOMMENDED).",
)
@click.option("--external-refund-memo", default=None)
@click.option("--settle-memo", default=None)
@click.pass_obj
def receive(ctx, deposit_coin, deposit_network, settle_coin, settle_network,
            wallet_name, external_refund_address, external_refund_memo, settle_memo):
    """Create a variable-rate SideShift to receive into the local wallet.

    Returns a deposit address on the source chain. The user (or external
    sender) sends to it from any wallet. Without an `--external-refund-address`,
    a stuck shift requires manual intervention via SideShift's web UI.
    """
    run_tool(
        ctx,
        lambda: sideshift_receive(
            deposit_coin=deposit_coin,
            deposit_network=deposit_network,
            settle_coin=settle_coin,
            settle_network=settle_network,
            wallet_name=wallet_name,
            external_refund_address=external_refund_address,
            external_refund_memo=external_refund_memo,
            settle_memo=settle_memo,
        ),
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@sideshift.command("status")
@click.option("--shift-id", required=True, help="ID returned from `aqua sideshift send` or `… receive`.")
@click.pass_obj
def status(ctx, shift_id):
    """Check the status of a SideShift shift order."""
    run_tool(ctx, lambda: sideshift_status(shift_id))
