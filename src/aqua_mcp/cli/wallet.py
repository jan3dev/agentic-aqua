"""Wallet management CLI commands."""

import sys

import click
from click.core import ParameterSource

from ..tools import (
    delete_wallet as _delete_wallet,
)
from ..tools import (
    lw_balance,
    lw_export_descriptor,
    lw_generate_mnemonic,
    lw_import_descriptor,
    lw_import_mnemonic,
    lw_list_wallets,
)
from .output import render, run_tool
from .password import handle_password_retry


@click.group()
def wallet():
    """Wallet management (create, import, list, delete)."""


@wallet.command("generate-mnemonic")
@click.pass_obj
def generate_mnemonic(ctx):
    """Generate a new BIP39 mnemonic phrase (12 words)."""
    run_tool(ctx, lw_generate_mnemonic)


@wallet.command("import-mnemonic")
@click.option(
    "--mnemonic",
    default=None,
    envvar="AQUA_MNEMONIC",
    help=(
        "BIP39 mnemonic. Omit to enter interactively, or set AQUA_MNEMONIC. "
        "Passing a seed via this flag can be stored in shell history; prefer "
        "interactive input or the environment variable."
    ),
)
@click.option("--wallet-name", default="default", show_default=True, help="Name for the wallet.")
@click.option(
    "--network",
    type=click.Choice(["mainnet", "testnet"]),
    default="mainnet",
    show_default=True,
    help="Network to use.",
)
@click.option(
    "--password",
    default=None,
    help="Password to encrypt mnemonic at rest. Prompted if wallet needs it.",
)
@click.pass_obj
def import_mnemonic(ctx, mnemonic, wallet_name, network, password):
    """Import a wallet from a BIP39 mnemonic (creates Liquid + Bitcoin wallets)."""
    click_ctx = click.get_current_context()
    try:
        src = click_ctx.get_parameter_source("mnemonic")
    except KeyError:
        src = None
    if src == ParameterSource.COMMANDLINE and mnemonic and str(mnemonic).strip():
        click.echo(
            "Warning: --mnemonic on the command line can be stored in shell history. "
            "Prefer interactive entry or the AQUA_MNEMONIC environment variable.",
            err=True,
        )
    if not mnemonic or not str(mnemonic).strip():
        mnemonic = click.prompt("Mnemonic", hide_input=True)
    else:
        mnemonic = str(mnemonic).strip()
    run_tool(
        ctx,
        lambda: handle_password_retry(
            lw_import_mnemonic,
            {
                "mnemonic": mnemonic,
                "wallet_name": wallet_name,
                "network": network,
                "password": password,
            },
        ),
    )


@wallet.command("import-descriptor")
@click.option("--descriptor", required=True, help="CT descriptor string.")
@click.option("--wallet-name", required=True, help="Name for the wallet.")
@click.option(
    "--network",
    type=click.Choice(["mainnet", "testnet"]),
    default="mainnet",
    show_default=True,
    help="Network to use.",
)
@click.pass_obj
def import_descriptor(ctx, descriptor, wallet_name, network):
    """Import a watch-only wallet from a CT descriptor."""
    run_tool(ctx, lambda: lw_import_descriptor(descriptor, wallet_name, network))


@wallet.command("export-descriptor")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.pass_obj
def export_descriptor(ctx, wallet_name):
    """Export the CT descriptor for a wallet (watch-only import elsewhere)."""
    run_tool(ctx, lambda: lw_export_descriptor(wallet_name))


@wallet.command("list")
@click.pass_obj
def list_wallets(ctx):
    """List all wallets."""
    run_tool(ctx, lw_list_wallets)


@wallet.command("delete")
@click.option("--wallet-name", required=True, help="Name of the wallet to delete.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_obj
def delete(ctx, wallet_name, yes):
    """Delete a wallet and all its cached data."""
    if not yes:
        try:
            balance = lw_balance(wallet_name)
            click.echo("Current Liquid wallet balance:", err=True)
            click.echo(render(balance, "pretty"), err=True)
        except Exception:
            pass  # Wallet may not exist yet

        click.echo(
            "\nMake sure you have backed up your seed phrase (mnemonic) before proceeding.",
            err=True,
        )
        click.echo(
            "Without it, you will permanently lose access to any funds.",
            err=True,
        )
        confirm = click.prompt(
            f"Type '{wallet_name}' to confirm deletion",
            default="",
            show_default=False,
        )
        if confirm != wallet_name:
            click.echo("Deletion cancelled.", err=True)
            sys.exit(1)

    run_tool(ctx, lambda: _delete_wallet(wallet_name))
