"""Register all CLI subcommand groups."""

import click

from ..tools import unified_balance
from .output import run_tool


def register_commands(cli):
    """Register all subcommand groups and top-level commands on the root CLI group."""
    from .btc import btc
    from .lightning import lightning
    from .liquid import liquid
    from .serve import serve
    from .wallet import wallet

    cli.add_command(wallet)
    cli.add_command(liquid)
    cli.add_command(btc)
    cli.add_command(lightning)
    cli.add_command(serve)
    cli.add_command(balance)


@click.command("balance")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.pass_obj
def balance(ctx, wallet_name):
    """Get unified balance for both Bitcoin and Liquid networks."""
    run_tool(ctx, lambda: unified_balance(wallet_name))
