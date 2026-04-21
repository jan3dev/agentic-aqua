"""Bitcoin CLI commands."""

import click

from ..tools import (
    btc_address,
    btc_balance,
    btc_send,
    btc_transactions,
)
from .output import run_tool
from .password import handle_password_retry


@click.group()
def btc():
    """Bitcoin on-chain operations."""
    pass


@btc.command("balance")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.pass_obj
def balance(ctx, wallet_name):
    """Get Bitcoin wallet balance in satoshis."""
    run_tool(ctx, lambda: btc_balance(wallet_name))


@btc.command("address")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.option("--index", type=int, default=None, help="Specific address index.")
@click.pass_obj
def address(ctx, wallet_name, index):
    """Generate a Bitcoin receive address (bc1...)."""
    run_tool(ctx, lambda: btc_address(wallet_name, index))


@btc.command("transactions")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.option("--limit", type=int, default=10, show_default=True, help="Max transactions.")
@click.pass_obj
def transactions(ctx, wallet_name, limit):
    """List Bitcoin transaction history."""
    run_tool(ctx, lambda: btc_transactions(wallet_name, limit))


@btc.command("send")
@click.option("--wallet-name", required=True, help="Name of the wallet.")
@click.option("--address", required=True, help="Destination Bitcoin address (bc1...).")
@click.option("--amount", required=True, type=int, help="Amount in satoshis.")
@click.option("--fee-rate", type=int, default=None, help="Fee rate in sat/vB.")
@click.option("--password", default=None, help="Password to decrypt mnemonic.")
@click.pass_obj
def send(ctx, wallet_name, address, amount, fee_rate, password):
    """Send BTC to an address."""
    run_tool(ctx, lambda: handle_password_retry(
        btc_send,
        {"wallet_name": wallet_name, "address": address, "amount": amount,
         "fee_rate": fee_rate, "password": password},
    ))
