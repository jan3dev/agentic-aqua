"""Liquid network CLI commands."""

import sys

import click

from ..tools import (
    lw_address,
    lw_balance,
    lw_send,
    lw_send_asset,
    lw_transactions,
    lw_tx_status,
)
from .output import render, render_error


def _handle_password_retry(fn, kwargs, ctx_fmt):
    """Call fn(**kwargs); if password is required and missing, prompt and retry once."""
    try:
        return fn(**kwargs)
    except ValueError as e:
        if "password required" in str(e).lower() and kwargs.get("password") is None:
            kwargs["password"] = click.prompt("Password", hide_input=True)
            return fn(**kwargs)
        raise


@click.group()
def liquid():
    """Liquid network operations (L-BTC, assets)."""
    pass


@liquid.command("balance")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.pass_obj
def balance(ctx, wallet_name):
    """Get Liquid wallet balance (all assets)."""
    try:
        result = lw_balance(wallet_name)
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@liquid.command("address")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.option("--index", type=int, default=None, help="Specific address index.")
@click.pass_obj
def address(ctx, wallet_name, index):
    """Generate a Liquid receive address."""
    try:
        result = lw_address(wallet_name, index)
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@liquid.command("transactions")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.option("--limit", type=int, default=10, show_default=True, help="Max transactions.")
@click.pass_obj
def transactions(ctx, wallet_name, limit):
    """List Liquid transaction history."""
    try:
        result = lw_transactions(wallet_name, limit)
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@liquid.command("send")
@click.option("--wallet-name", required=True, help="Name of the wallet.")
@click.option("--address", required=True, help="Destination Liquid address.")
@click.option("--amount", required=True, type=int, help="Amount in satoshis.")
@click.option("--password", default=None, help="Password to decrypt mnemonic.")
@click.pass_obj
def send(ctx, wallet_name, address, amount, password):
    """Send L-BTC to an address."""
    try:
        result = _handle_password_retry(
            lw_send, {"wallet_name": wallet_name, "address": address, "amount": amount, "password": password}, ctx.fmt
        )
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@liquid.command("send-asset")
@click.option("--wallet-name", required=True, help="Name of the wallet.")
@click.option("--address", required=True, help="Destination Liquid address.")
@click.option("--amount", required=True, type=int, help="Amount in satoshis.")
@click.option("--asset-id", required=True, help="Asset ID (hex string).")
@click.option("--password", default=None, help="Password to decrypt mnemonic.")
@click.pass_obj
def send_asset(ctx, wallet_name, address, amount, asset_id, password):
    """Send a Liquid asset to an address."""
    try:
        result = _handle_password_retry(
            lw_send_asset,
            {"wallet_name": wallet_name, "address": address, "amount": amount, "asset_id": asset_id, "password": password},
            ctx.fmt,
        )
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@liquid.command("tx-status")
@click.option("--tx", required=True, help="Transaction ID (hex) or Blockstream URL.")
@click.pass_obj
def tx_status(ctx, tx):
    """Get Liquid transaction status."""
    try:
        result = lw_tx_status(tx)
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)
