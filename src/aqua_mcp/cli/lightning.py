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
@click.option("--invoice", required=True, help="BOLT11 Lightning invoice (lnbc... or lntb...).")
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
def send(ctx, invoice, wallet_name, password_stdin):
    """Pay a Lightning invoice using L-BTC (submarine swap via Boltz)."""
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            lightning_send,
            {"invoice": invoice, "wallet_name": wallet_name, "password": password},
        ),
    )


@lightning.command("status")
@click.option("--swap-id", required=True, help="Swap ID from lightning receive or send.")
@click.pass_obj
def status(ctx, swap_id):
    """Check the status of a Lightning swap (send or receive)."""
    run_tool(ctx, lambda: lightning_transaction_status(swap_id))
