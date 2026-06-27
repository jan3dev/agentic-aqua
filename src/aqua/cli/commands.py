"""Register all CLI subcommand groups."""

from __future__ import annotations

import copy

import click

from ..features import (
    CLI_COMMAND_TO_MCP_TOOL,
    is_tool_enabled,
    load_config_with_merge,
)
from ..storage import Config
from ..tools import unified_balance
from .output import run_tool


def register_commands(cli: click.Group, config: Config | None = None) -> None:
    """Register all subcommand groups and top-level commands on the root CLI group.

    Re-runnable and side-effect-free: clears `cli.commands`, then for each
    subgroup registers a shallow copy whose `.commands` omits any entry mapped
    to an MCP tool disabled in `config.enabled_tools`. The module-level group
    singletons (populated by `@group.command()` decorators at import time) are
    never mutated, so repeated calls with different configs — including the
    import-time call on the real `cli` — cannot leak gating state across each
    other. Tests pass a fresh `Config` per case for deterministic gating.
    """
    if config is None:
        config = load_config_with_merge()

    from .auth import auth
    from .btc import btc
    from .changelly import changelly
    from .jan3 import jan3
    from .lightning import lightning
    from .liquid import liquid
    from .qr import qr
    from .serve import serve
    from .sideshift import sideshift
    from .sideswap import sideswap
    from .wallet import wallet
    from .wapupay import wapupay

    # Reset root.
    cli.commands.clear()

    groups: list[tuple[str, click.Group]] = [
        ("auth", auth),
        ("wallet", wallet),
        ("liquid", liquid),
        ("btc", btc),
        ("lightning", lightning),
        ("changelly", changelly),
        ("sideshift", sideshift),
        ("sideswap", sideswap),
        ("wapupay", wapupay),
        ("jan3", jan3),
        ("qr", qr),
    ]

    for group_name, group in groups:
        # Build the enabled-command subset WITHOUT mutating the shared singleton
        # `group`: an entry is kept if it has no MCP mapping or its mapped tool
        # is enabled. Register a shallow copy carrying the filtered dict.
        enabled_commands = {
            cmd_name: cmd
            for cmd_name, cmd in group.commands.items()
            if (mcp_name := CLI_COMMAND_TO_MCP_TOOL.get((group_name, cmd_name))) is None
            or is_tool_enabled(mcp_name, config)
        }
        # Skip the group entirely if every command in it was disabled —
        # otherwise an empty group label still appears in `aqua --help`.
        if not enabled_commands:
            continue
        registered = copy.copy(group)
        registered.commands = enabled_commands
        cli.add_command(registered)

    # Top-level commands. `serve` is CLI-only (no MCP twin) — always register.
    # `qr` is gated as a group above (its `decode` subcommand maps to `qr_decode`).
    cli.add_command(serve)

    # `balance` is gated by `unified_balance`'s flag.
    if is_tool_enabled("unified_balance", config):
        cli.add_command(balance)


@click.command("balance")
@click.option("--wallet-name", default="default", show_default=True, help="Name of the wallet.")
@click.pass_obj
def balance(ctx, wallet_name):
    """Get unified balance for both Bitcoin and Liquid networks."""
    run_tool(ctx, lambda: unified_balance(wallet_name))
