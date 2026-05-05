"""Bitcoin CLI commands."""

import click

from ..tools import (
    btc_address,
    btc_balance,
    btc_export_descriptor,
    btc_import_descriptor,
    btc_send,
    btc_transactions,
)
from .output import run_tool
from .password import handle_password_retry, resolve_secret


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
def send(ctx, wallet_name, address, amount, fee_rate, password_stdin):
    """Send BTC to an address. If the wallet mnemonic is encrypted, a password is required."""
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            btc_send,
            {
                "wallet_name": wallet_name,
                "address": address,
                "amount": amount,
                "fee_rate": fee_rate,
                "password": password,
            },
        ),
    )


@btc.command("import-descriptor")
@click.option(
    "--descriptor",
    required=True,
    help="BIP84 external descriptor (with or without [fp/path] prefix).",
)
@click.option(
    "--change-descriptor",
    default=None,
    help="Change descriptor. Auto-derived from external if omitted (replaces /0/* with /1/*).",
)
@click.option("--wallet-name", required=True, help="Name for the wallet.")
@click.option(
    "--network",
    type=click.Choice(["mainnet", "testnet"]),
    default="mainnet",
    show_default=True,
    help="Network to use.",
)
@click.pass_obj
def import_descriptor(ctx, descriptor, change_descriptor, wallet_name, network):
    """Import a watch-only Bitcoin wallet from a BIP84 descriptor.

    Note: imports Bitcoin only. For Liquid watch-only, separately run
    `aqua liquid import-descriptor`. The Liquid descriptor cannot be derived
    from the Bitcoin xpub (different derivation paths + SLIP-77 blinding key
    required).
    """
    run_tool(
        ctx,
        lambda: btc_import_descriptor(descriptor, wallet_name, network, change_descriptor),
    )


@btc.command("export-descriptor")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.pass_obj
def export_descriptor(ctx, wallet_name):
    """Export Bitcoin (BDK) descriptors + xpub.

    Note: exports Bitcoin only. For the Liquid CT descriptor of the same
    wallet, run `aqua liquid export-descriptor`.
    """
    run_tool(ctx, lambda: btc_export_descriptor(wallet_name))
