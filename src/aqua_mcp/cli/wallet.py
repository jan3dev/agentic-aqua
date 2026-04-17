"""Wallet management CLI commands."""

import sys

import click

from ..tools import (
    delete_wallet as _delete_wallet,
    lw_balance,
    lw_export_descriptor,
    lw_generate_mnemonic,
    lw_import_descriptor,
    lw_import_mnemonic,
    lw_list_wallets,
)
from .output import render, render_error
from .password import handle_password_retry


@click.group()
def wallet():
    """Wallet management (create, import, list, delete)."""
    pass


@wallet.command("generate-mnemonic")
@click.pass_obj
def generate_mnemonic(ctx):
    """Generate a new BIP39 mnemonic phrase (12 words)."""
    try:
        result = lw_generate_mnemonic()
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@wallet.command("import-mnemonic")
@click.option("--mnemonic", required=True, help="BIP39 mnemonic phrase (12 words).")
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
    try:
        result = handle_password_retry(
            lw_import_mnemonic,
            {"mnemonic": mnemonic, "wallet_name": wallet_name, "network": network, "password": password},
        )
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


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
    try:
        result = lw_import_descriptor(descriptor, wallet_name, network)
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@wallet.command("export-descriptor")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.pass_obj
def export_descriptor(ctx, wallet_name):
    """Export the CT descriptor for a wallet (watch-only import elsewhere)."""
    try:
        result = lw_export_descriptor(wallet_name)
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@wallet.command("list")
@click.pass_obj
def list_wallets(ctx):
    """List all wallets."""
    try:
        result = lw_list_wallets()
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)


@wallet.command("delete")
@click.option("--wallet-name", required=True, help="Name of the wallet to delete.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_obj
def delete(ctx, wallet_name, yes):
    """Delete a wallet and all its cached data."""
    try:
        if not yes:
            # Show Liquid balance before deletion (skip BTC to avoid slow network sync)
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

        result = _delete_wallet(wallet_name)
        click.echo(render(result, ctx.fmt))
    except Exception as e:
        click.echo(render_error(type(e).__name__, str(e), ctx.fmt), err=True)
        sys.exit(1)
