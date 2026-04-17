"""Lightning CLI commands."""

import sys

import click

from ..tools import (
    lightning_receive,
    lightning_send,
    lightning_transaction_status,
)
from .output import render, render_error
from .password import handle_password_retry


@click.group()
def lightning():
    """Lightning network operations (receive, send, status)."""
    pass


@lightning.command("receive")
@click.option("--amount", required=True, type=int, help="Amount in satoshis (100-25,000,000).")
@click.option("--wallet-name", default="default", show_default=True, help="Liquid wallet to receive into.")
@click.option("--password", default=None, help="Password to decrypt mnemonic.")
@click.pass_obj
def receive(ctx, amount, wallet_name, password):
    """Generate a Lightning invoice to receive L-BTC into a Liquid wallet."""
    try:
        result = handle_password_retry(
            lightning_receive,
            {"amount": amount, "wallet_name": wallet_name, "password": password},
        )
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@lightning.command("send")
@click.option("--invoice", required=True, help="BOLT11 Lightning invoice (lnbc... or lntb...).")
@click.option("--wallet-name", default="default", show_default=True, help="Liquid wallet to pay from.")
@click.option("--password", default=None, help="Password to decrypt mnemonic.")
@click.pass_obj
def send(ctx, invoice, wallet_name, password):
    """Pay a Lightning invoice using L-BTC (submarine swap via Boltz)."""
    try:
        result = handle_password_retry(
            lightning_send,
            {"invoice": invoice, "wallet_name": wallet_name, "password": password},
        )
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@lightning.command("status")
@click.option("--swap-id", required=True, help="Swap ID from lightning receive or send.")
@click.pass_obj
def status(ctx, swap_id):
    """Check the status of a Lightning swap (send or receive)."""
    try:
        result = lightning_transaction_status(swap_id)
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)
