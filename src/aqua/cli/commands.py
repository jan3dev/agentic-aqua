"""Register all CLI subcommand groups."""

from __future__ import annotations

import click

from ..features import (
    CLI_COMMAND_TO_MCP_TOOL,
    is_tool_enabled,
    load_config_with_merge,
)
from ..storage import Config
from ..tools import unified_balance
from .output import run_tool

# Snapshot of each subgroup's `.commands` dict the first time
# `register_commands` is invoked. Click subgroups are module-level singletons
# populated by `@group.command()` decorators at import time, so deleting
# entries from `group.commands` mutates that singleton permanently. The
# snapshot lets us restore each subgroup before applying the disabled-filter
# on every subsequent call (tests rely on this for isolation).
_ORIGINAL_GROUP_COMMANDS: dict[str, dict[str, click.Command]] = {}


def register_commands(cli: click.Group, config: Config | None = None) -> None:
    """Register all subcommand groups and top-level commands on the root CLI group.

    Re-runnable: clears `cli.commands` and restores each subgroup's `.commands`
    from a one-time snapshot, then strips entries for any MCP tool disabled
    in `config.enabled_tools`. Tests pass a fresh `Config` per case for
    deterministic gating.
    """
    if config is None:
        config = load_config_with_merge()

    from .btc import btc
    from .changelly import changelly
    from .lightning import lightning
    from .liquid import liquid
    from .serve import serve
    from .sideshift import sideshift
    from .sideswap import sideswap
    from .wallet import wallet

    # Reset root.
    cli.commands.clear()

    groups: list[tuple[str, click.Group]] = [
        ("wallet", wallet),
        ("liquid", liquid),
        ("btc", btc),
        ("lightning", lightning),
        ("changelly", changelly),
        ("sideshift", sideshift),
        ("sideswap", sideswap),
    ]

    # First call: snapshot every subgroup's `.commands` so subsequent calls
    # can restore before filtering.
    for group_name, group in groups:
        if group_name not in _ORIGINAL_GROUP_COMMANDS:
            _ORIGINAL_GROUP_COMMANDS[group_name] = dict(group.commands)

    for group_name, group in groups:
        # Restore from snapshot, then strip disabled.
        group.commands = dict(_ORIGINAL_GROUP_COMMANDS[group_name])
        for cmd_name in list(group.commands):
            mcp_name = CLI_COMMAND_TO_MCP_TOOL.get((group_name, cmd_name))
            if mcp_name is not None and not is_tool_enabled(mcp_name, config):
                del group.commands[cmd_name]
        cli.add_command(group)

    # Top-level commands. `serve` is CLI-only (no MCP twin) — always register.
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
