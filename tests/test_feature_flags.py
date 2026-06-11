"""Tests for runtime feature-flag gating of MCP tools and CLI commands."""

import json
import logging
import tempfile
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from mcp.server import Server

from aqua.cli.commands import register_commands
from aqua.features import (
    CLI_COMMAND_TO_MCP_TOOL,
    MCP_ONLY_TOOLS,
    SHIPPED_DEFAULTS_ENABLED_TOOLS,
    load_config_with_merge,
)
from aqua.server import _make_handlers
from aqua.storage import Config, Storage
from aqua.tools import TOOLS


@pytest.fixture
def temp_storage():
    """Fresh Storage rooted at a tmpdir for full isolation from ~/.aqua/."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Storage(Path(tmpdir))


def _make_test_cli() -> click.Group:
    """Build a minimal root group mirroring main.cli for testing."""

    @click.group()
    def root():
        pass

    return root


def _registered_handler(server: Server, request_type) -> callable:
    """Pull the registered handler for a given request type out of `server`."""
    handler = server.request_handlers.get(request_type)
    assert handler is not None, f"no handler registered for {request_type}"
    return handler


@pytest.mark.asyncio
async def test_disabled_tool_hidden_from_list_tools():
    """A tool with `enabled_tools[name] = False` is excluded from list_tools."""
    import mcp.types as types

    config = Config(enabled_tools={"lw_balance": False})
    server = Server("test")
    _make_handlers(server, config)

    handler = _registered_handler(server, types.ListToolsRequest)
    req = types.ListToolsRequest(method="tools/list")
    result = await handler(req)
    # ServerResult wraps a ListToolsResult under .root
    tools = result.root.tools
    names = [t.name for t in tools]
    assert "lw_balance" not in names
    # other tools unaffected
    assert "btc_balance" in names


@pytest.mark.asyncio
async def test_disabled_tool_unknown_on_call_tool():
    """call_tool on a disabled tool returns the EXACT `Unknown tool: <name>` text."""
    import mcp.types as types

    config = Config(enabled_tools={"lw_balance": False})
    server = Server("test")
    _make_handlers(server, config)

    handler = _registered_handler(server, types.CallToolRequest)

    # Disabled tool
    req_disabled = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="lw_balance", arguments={}),
    )
    res_disabled = await handler(req_disabled)
    text_disabled = res_disabled.root.content[0].text

    # Genuinely unknown tool
    req_unknown = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="nonexistent_tool", arguments={}),
    )
    res_unknown = await handler(req_unknown)
    text_unknown = res_unknown.root.content[0].text

    assert text_disabled == "Unknown tool: lw_balance"
    assert text_unknown == "Unknown tool: nonexistent_tool"
    # Same shape — no leakage of the "disabled" state.
    assert text_disabled.startswith("Unknown tool: ")
    assert text_unknown.startswith("Unknown tool: ")


def test_cli_help_omits_disabled(temp_storage):
    """`aqua liquid --help` omits commands whose mapped MCP tool is disabled."""
    enabled = dict(SHIPPED_DEFAULTS_ENABLED_TOOLS)
    enabled["lw_balance"] = False
    temp_storage.save_config(Config(enabled_tools=enabled))
    config = load_config_with_merge(temp_storage)

    fresh_cli = _make_test_cli()
    register_commands(fresh_cli, config=config)

    runner = CliRunner()
    result = runner.invoke(fresh_cli, ["liquid", "--help"])
    assert result.exit_code == 0
    assert "balance" not in result.output


def test_cli_disabled_command_exits_2(temp_storage):
    """Invoking a disabled CLI command returns Click's `No such command` (exit 2)."""
    enabled = dict(SHIPPED_DEFAULTS_ENABLED_TOOLS)
    enabled["lw_balance"] = False
    temp_storage.save_config(Config(enabled_tools=enabled))
    config = load_config_with_merge(temp_storage)

    fresh_cli = _make_test_cli()
    register_commands(fresh_cli, config=config)

    runner = CliRunner()
    result = runner.invoke(fresh_cli, ["liquid", "balance"])
    assert result.exit_code == 2
    assert "No such command" in result.output


def test_mapping_targets_real_tools():
    """Every value in CLI_COMMAND_TO_MCP_TOOL must be a real MCP tool name."""
    for mcp_name in CLI_COMMAND_TO_MCP_TOOL.values():
        assert mcp_name in TOOLS, f"mapping target {mcp_name!r} not in TOOLS"


def test_every_mcp_tool_is_mapped_or_explicitly_mcp_only():
    """Drift guard: every MCP tool has a CLI mapping OR is in MCP_ONLY_TOOLS."""
    mapped = set(CLI_COMMAND_TO_MCP_TOOL.values())
    assert set(TOOLS.keys()) == mapped | MCP_ONLY_TOOLS, (
        "unmapped: "
        + str(set(TOOLS.keys()) - mapped - MCP_ONLY_TOOLS)
        + "; phantom mappings: "
        + str(mapped - set(TOOLS.keys()))
    )


def test_unknown_enabled_tools_key_warns(temp_storage, caplog):
    """Unknown tool names in enabled_tools log a warning but do not crash."""
    temp_storage.save_config(Config(enabled_tools={"bogus_tool": True}))
    with caplog.at_level(logging.WARNING, logger="aqua.features"):
        config = load_config_with_merge(temp_storage)
    assert any("bogus_tool" in rec.message for rec in caplog.records)
    # No crash; config still loaded with shipped defaults merged in.
    assert "lw_balance" in config.enabled_tools


def test_fresh_install_writes_default_config(temp_storage):
    """On a brand-new install (no config.json), defaults are persisted on load."""
    assert not temp_storage.config_path.exists()
    config = load_config_with_merge(temp_storage)
    assert temp_storage.config_path.exists()
    with open(temp_storage.config_path) as f:
        data = json.load(f)
    assert "enabled_tools" in data
    # Every shipped-default tool key is present.
    for tool_name in SHIPPED_DEFAULTS_ENABLED_TOOLS:
        assert tool_name in data["enabled_tools"]
    assert config.enabled_tools == SHIPPED_DEFAULTS_ENABLED_TOOLS


def test_cli_only_serve_always_registers(temp_storage):
    """`serve` is CLI-only and must register even when every tool is disabled."""
    enabled = {name: False for name in SHIPPED_DEFAULTS_ENABLED_TOOLS}
    temp_storage.save_config(Config(enabled_tools=enabled))
    config = load_config_with_merge(temp_storage)

    fresh_cli = _make_test_cli()
    register_commands(fresh_cli, config=config)
    assert "serve" in fresh_cli.commands


def test_enabled_tools_invalid_types_are_coerced(caplog):
    """Non-bool values in enabled_tools are dropped (fails closed, not open).

    Without coercion, `{"lw_send": "yes"}` would silently treat lw_send as
    enabled via Python truthiness. The coercion at Config.from_dict prevents
    that.
    """
    with caplog.at_level(logging.WARNING, logger="aqua.storage"):
        config = Config.from_dict(
            {
                "enabled_tools": {
                    "lw_send": "yes",  # invalid: string instead of bool
                    "lw_balance": True,  # valid
                    "btc_send": {"nested": True},  # invalid: dict
                    42: True,  # invalid: non-string key
                }
            }
        )
    # Only the well-typed entry survives.
    assert config.enabled_tools == {"lw_balance": True}
    # All three invalid entries logged.
    assert sum("Dropping invalid" in r.message for r in caplog.records) == 3


def test_sideswap_and_pix_disabled_by_default():
    """SideSwap + PIX + WapuPay tools ship disabled-by-default (manual opt-in).

    WapuPay is dark-launched OFF to mirror Ankara's `wapupay_direct_payments`
    waffle switch; users opt in via ~/.aqua/config.json.
    """
    expected_disabled = {
        "pix_receive", "pix_status",
        "sideswap_server_status", "sideswap_recommend",
        "sideswap_peg_quote", "sideswap_peg_in", "sideswap_peg_out",
        "sideswap_peg_status", "sideswap_list_assets", "sideswap_quote",
        "sideswap_execute_swap", "sideswap_swap_status",
        "wapupay_login", "wapupay_verify", "wapupay_logout", "wapupay_session",
        "wapupay_exchange_rates", "wapupay_quote", "wapupay_create_order",
        "wapupay_fund_order", "wapupay_order_status", "wapupay_transactions",
        "wapupay_transaction", "wapupay_spending_limit",
    }
    for name in expected_disabled:
        assert SHIPPED_DEFAULTS_ENABLED_TOOLS[name] is False, name
    for name, enabled in SHIPPED_DEFAULTS_ENABLED_TOOLS.items():
        if name not in expected_disabled:
            assert enabled is True, name


def test_enabled_tools_non_dict_value_resets_to_empty(caplog):
    """Top-level enabled_tools must be an object; lists/strings are rejected."""
    with caplog.at_level(logging.WARNING, logger="aqua.storage"):
        config = Config.from_dict({"enabled_tools": ["lw_send"]})
    assert config.enabled_tools == {}
    assert any("must be an object" in r.message for r in caplog.records)
