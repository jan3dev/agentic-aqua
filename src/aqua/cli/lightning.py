"""Lightning CLI commands."""

import click

from ..tools import (
    lightning_receive,
    lightning_send,
    lightning_transaction_status,
)
from .output import run_tool
from .password import handle_password_retry, resolve_secret


@click.group()
def lightning():
    """Lightning network operations (receive, send, status)."""
    pass


@lightning.command("receive")
@click.option("--amount", required=True, type=int, help="Amount in satoshis (100-25,000,000).")
@click.option(
    "--wallet-name", default="default", show_default=True, help="Liquid wallet to receive into."
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
def receive(ctx, amount, wallet_name, password_stdin):
    """Generate a Lightning invoice to receive L-BTC into a Liquid wallet."""
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            lightning_receive,
            {"amount": amount, "wallet_name": wallet_name, "password": password},
        ),
    )


@lightning.command("send")
@click.option(
    "--invoice",
    help="BOLT11 Lightning invoice (lnbc... or lntb...). Mutually exclusive with --ln-address.",
)
@click.option(
    "--ln-address",
    help="Lightning Address (user@domain.com). Requires --amount-sats. Mutually exclusive with --invoice.",
)
@click.option(
    "--amount-sats",
    type=int,
    help="Amount in satoshis. Required with --ln-address; optional with --invoice (must match encoded amount if supplied).",
)
@click.option(
    "--wallet-name", default="default", show_default=True, help="Liquid wallet to pay from."
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
def send(ctx, invoice, ln_address, amount_sats, wallet_name, password_stdin):
    """Pay a Lightning invoice or Lightning Address using L-BTC."""
    if bool(invoice) == bool(ln_address):
        raise click.UsageError("Provide exactly one of --invoice or --ln-address")

    if ln_address and amount_sats is None:
        raise click.UsageError("--amount-sats is required when using --ln-address")

    payment_target = ln_address or invoice

    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            lightning_send,
            {
                "invoice": payment_target,
                "wallet_name": wallet_name,
                "password": password,
                "amount_sats": amount_sats,
            },
        ),
    )


@lightning.command("status")
@click.option("--swap-id", required=True, help="Swap ID from lightning receive or send.")
@click.pass_obj
def status(ctx, swap_id):
    """Check the status of a Lightning swap (send or receive)."""
    run_tool(ctx, lambda: lightning_transaction_status(swap_id))
