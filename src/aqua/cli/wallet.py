"""Wallet management CLI commands."""

import sys

import click

from ..tools import (
    _delete_wallet_now,
    get_manager,
    lw_backup_mnemonic,
    lw_create_wallet,
    lw_import_mnemonic,
    lw_list_wallets,
)
from ..tools import delete_wallet as _delete_wallet
from .output import run_tool
from .password import handle_password_retry, password_file_option, resolve_secret


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
@password_file_option
@click.pass_obj
def create(ctx, wallet_name, network, password_stdin, password_file):
    """Create a wallet from a new mnemonic, without displaying it."""
    password = resolve_secret(
        "Password",
        password_stdin,
        password_file=password_file,
        env_var="AQUA_PASSWORD",
        required=False,
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
@password_file_option
@click.pass_obj
def backup_mnemonic(ctx, wallet_name, password_stdin, password_file):
    """Write the mnemonic to <aqua dir>/backups/<wallet>-mnemonic.txt (plain text)."""
    password = resolve_secret(
        "Password",
        password_stdin,
        password_file=password_file,
        env_var="AQUA_PASSWORD",
        required=False,
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
        "then to no password (seed encrypted with a public built-in key: "
        "use a password for real funds)."
    ),
)
@password_file_option
@click.pass_obj
def import_mnemonic(ctx, mnemonic_stdin, wallet_name, network, password_stdin, password_file):
    """Import a wallet from a BIP39 mnemonic (creates Liquid + Bitcoin wallets)."""
    mnemonic = resolve_secret(
        "Mnemonic", mnemonic_stdin, env_var="AQUA_MNEMONIC", required=True
    )
    password = resolve_secret(
        "Password",
        password_stdin,
        password_file=password_file,
        env_var="AQUA_PASSWORD",
        required=False,
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


def _preview_delete(wallet_name: str) -> dict:
    """MCP preview minus the agent-only confirm token and instructions."""
    result = _delete_wallet(wallet_name)
    for key in ("confirm_token", "confirm_token_expires_in_seconds", "warning"):
        result.pop(key, None)
    return result


@wallet.command("delete")
@click.option("--wallet-name", required=True, help="Name of the wallet to delete.")
@click.option(
    "--confirm",
    is_flag=True,
    help="Delete the wallet and its seed (irreversible). Without it, only a preview is shown.",
)
@click.pass_obj
def delete(ctx, wallet_name, confirm):
    """Permanently delete a wallet, its seed and all its cached data.

    Without --confirm nothing is deleted: the current balances are shown and the
    command exits with status 1.
    """
    # A human typed --confirm; CLI processes don't share the MCP server's preview tokens.
    result = run_tool(
        ctx,
        lambda: _delete_wallet_now(wallet_name) if confirm else _preview_delete(wallet_name),
    )
    if not result.get("deleted"):
        click.echo(
            "\nNothing was deleted. Deleting permanently removes the seed from this "
            "computer: without a backup of the seed, the funds will be lost forever. "
            "Re-run with --confirm to delete.",
            err=True,
        )
        sys.exit(1)
