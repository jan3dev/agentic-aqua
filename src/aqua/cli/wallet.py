"""Wallet management CLI commands."""

import sys

import click

from ..tools import (
    delete_wallet as _delete_wallet,
)
from ..tools import (
    get_manager,
    lw_backup_mnemonic,
    lw_balance,
    lw_create_wallet,
    lw_import_mnemonic,
    lw_list_wallets,
)
from .output import render, run_tool
from .password import handle_password_retry, resolve_secret


@click.group()
def wallet():
    """Wallet management (create, import, list, delete)."""


@wallet.command("generate-mnemonic")
@click.pass_obj
def generate_mnemonic(ctx):
    """Generate a new BIP39 mnemonic phrase (12 words).

    CLI-only: prints the seed to the local terminal. There is deliberately no MCP
    twin, so the seed never reaches an LLM context.
    """

    def _generate():
        mnemonic = get_manager().generate_mnemonic()
        return {
            "mnemonic": mnemonic,
            "words": len(mnemonic.split()),
            "warning": "Store this mnemonic securely. Anyone with access can control your funds.",
        }

    run_tool(ctx, _generate)


_NETWORK_OPT = click.option(
    "--network",
    type=click.Choice(["mainnet", "testnet"]),
    default="mainnet",
    show_default=True,
    help="Network to use.",
)
_PASSWORD_STDIN_OPT = click.option(
    "--password-stdin",
    "password_stdin",
    is_flag=True,
    default=False,
    help=(
        "Read wallet password from stdin (piped) or prompt interactively. "
        "When absent, falls back to the AQUA_PASSWORD environment variable, "
        "then to no password."
    ),
)


@wallet.command("create")
@click.option("--wallet-name", default="default", show_default=True, help="Name for the wallet.")
@_NETWORK_OPT
@_PASSWORD_STDIN_OPT
@click.pass_obj
def create(ctx, wallet_name, network, password_stdin):
    """Create a wallet from a new mnemonic, without displaying it."""
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: lw_create_wallet(
            wallet_name=wallet_name, network=network, password=password
        ),
    )


@wallet.command("backup-mnemonic")
@click.option("--wallet-name", default="default", show_default=True, help="Wallet to back up.")
@_PASSWORD_STDIN_OPT
@click.pass_obj
def backup_mnemonic(ctx, wallet_name, password_stdin):
    """Write the mnemonic to <aqua dir>/backups/<wallet>-mnemonic.txt (plain text)."""
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
    run_tool(
        ctx,
        lambda: handle_password_retry(
            lw_backup_mnemonic, {"wallet_name": wallet_name, "password": password}
        ),
    )


@wallet.command("import-mnemonic")
@click.option(
    "--mnemonic-stdin",
    "mnemonic_stdin",
    is_flag=True,
    default=False,
    help=(
        "Read BIP39 mnemonic from stdin (piped) or prompt interactively. "
        "When absent, falls back to the AQUA_MNEMONIC environment variable, "
        "then to an interactive prompt."
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
    "--password-stdin",
    "password_stdin",
    is_flag=True,
    default=False,
    help=(
        "Read wallet password from stdin (piped) or prompt interactively. "
        "When absent, falls back to the AQUA_PASSWORD environment variable, "
        "then to no password (wallet stored in plaintext)."
    ),
)
@click.pass_obj
def import_mnemonic(ctx, mnemonic_stdin, wallet_name, network, password_stdin):
    """Import a wallet from a BIP39 mnemonic (creates Liquid + Bitcoin wallets)."""
    mnemonic = resolve_secret(
        "Mnemonic", mnemonic_stdin, env_var="AQUA_MNEMONIC", required=True
    )
    password = resolve_secret(
        "Password", password_stdin, env_var="AQUA_PASSWORD", required=False
    )
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
