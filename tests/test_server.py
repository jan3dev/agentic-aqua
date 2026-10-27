"""Tests for MCP server prompt handlers."""

import asyncio

import pytest
from mcp.types import (
    GetPromptRequest,
    GetPromptRequestParams,
    GetPromptResult,
    PromptMessage,
    ServerResult,
)

from aqua.server import create_server


@pytest.fixture
def prompt_handler():
    """Get the registered get_prompt handler from the server."""
    server = create_server()
    return server.request_handlers[GetPromptRequest]


def _call(handler, name: str, arguments: dict | None = None) -> ServerResult:
    """Helper to call the async prompt handler synchronously."""
    req = GetPromptRequest(
        method="prompts/get",
        params=GetPromptRequestParams(name=name, arguments=arguments),
    )
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(handler(req))
    finally:
        loop.close()


ALL_PROMPTS = [
    ("create_new_wallet", {"wallet_name": "test", "network": "mainnet"}),
    ("import_seed", {"wallet_name": "test"}),
    ("show_balance", {"wallet_name": "test"}),
    ("bitcoin_balance", {"wallet_name": "test"}),
    ("liquid_balance", {"wallet_name": "test"}),
    ("generate_address", {"network": "bitcoin", "wallet_name": "test"}),
    ("show_transactions", {"network": "bitcoin", "wallet_name": "test"}),
    ("show_transactions", {"wallet_name": "test"}),
    ("send_bitcoin", {"wallet_name": "test"}),
    ("send_liquid", {"wallet_name": "test"}),
    ("transaction_status", None),
    ("list_wallets", None),
    ("export_descriptor", {"wallet_name": "test"}),
    ("delete_wallet", {"wallet_name": "test"}),
    ("pay_lightning", {"wallet_name": "test"}),
]


@pytest.mark.parametrize("name,arguments", ALL_PROMPTS)
def test_get_prompt_returns_valid_result(prompt_handler, name, arguments):
    """Every prompt must return ServerResult wrapping GetPromptResult with PromptMessages."""
    result = _call(prompt_handler, name, arguments)

    assert isinstance(result, ServerResult)
    prompt_result = result.root
    assert isinstance(prompt_result, GetPromptResult)
    assert len(prompt_result.messages) > 0
    for msg in prompt_result.messages:
        assert isinstance(msg, PromptMessage)
        assert msg.role == "user"
        assert msg.content.text  # non-empty text


def test_get_prompt_unknown_raises(prompt_handler):
    """Unknown prompt names should raise ValueError."""
    with pytest.raises(ValueError, match="Unknown prompt"):
        _call(prompt_handler, "nonexistent_prompt", None)


def test_tool_schemas_match_tools_registry():
    """Drift guard: every TOOLS entry has a TOOL_SCHEMAS entry and vice versa.

    A gap would mean a tool is silently uncallable or unlisted over MCP.
    """
    from aqua.server import TOOL_SCHEMAS
    from aqua.tools import TOOLS

    assert set(TOOL_SCHEMAS) == set(TOOLS), (
        "missing schemas: "
        + str(set(TOOLS) - set(TOOL_SCHEMAS))
        + "; schemas without a tool: "
        + str(set(TOOL_SCHEMAS) - set(TOOLS))
    )


def test_lightning_send_schema_includes_amount_sats():
    """lightning_send tool schema exposes amount_sats and mentions Lightning Address."""
    from aqua.server import TOOL_SCHEMAS

    schema = TOOL_SCHEMAS["lightning_send"]
    props = schema["inputSchema"]["properties"]
    assert "amount_sats" in props
    assert props["amount_sats"]["type"] == "integer"
    assert "Lightning Address" in props["invoice"]["description"]
    # amount_sats must remain optional (only invoice is required)
    assert schema["inputSchema"]["required"] == ["invoice"]


_SEND = {"wallet_name": "default", "address": "bc1q", "amount": 1000}


def _call_tool(name: str, arguments: dict) -> dict:
    """Invoke the registered call_tool handler and return the decoded JSON payload."""
    import json

    from mcp.types import CallToolRequest, CallToolRequestParams

    handler = create_server().request_handlers[CallToolRequest]
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(handler(req))
    finally:
        loop.close()
    return json.loads(result.root.content[0].text)


def test_every_password_schema_also_has_password_file():
    from aqua.server import TOOL_SCHEMAS

    with_password = [
        n for n, s in TOOL_SCHEMAS.items() if "password" in s["inputSchema"].get("properties", {})
    ]
    assert with_password
    for name in with_password:
        assert "password_file" in TOOL_SCHEMAS[name]["inputSchema"]["properties"], name


def test_call_tool_password_file_passes_password(tmp_path):
    from unittest.mock import MagicMock, patch

    from aqua.server import TOOLS

    pw = tmp_path / "pw"
    pw.write_text("file-pass\n")
    fake = MagicMock(return_value={"ok": True})
    with patch.dict(TOOLS, {"btc_send": fake}):
        out = _call_tool("btc_send", {**_SEND, "password_file": str(pw)})
    assert out == {"ok": True}
    assert fake.call_args.kwargs == {**_SEND, "password": "file-pass"}


def test_call_tool_password_and_password_file_conflict(tmp_path):
    from unittest.mock import MagicMock, patch

    from aqua.server import TOOLS

    pw = tmp_path / "pw"
    pw.write_text("file-pass\n")
    fake = MagicMock()
    with patch.dict(TOOLS, {"btc_send": fake}):
        out = _call_tool(
            "btc_send", {**_SEND, "password": "inline-secret", "password_file": str(pw)}
        )
    assert "error" in out
    assert "inline-secret" not in str(out) and "file-pass" not in str(out)
    fake.assert_not_called()


def test_call_tool_missing_password_file_returns_error_envelope(tmp_path):
    from unittest.mock import MagicMock, patch

    from aqua.server import TOOLS

    fake = MagicMock()
    with patch.dict(TOOLS, {"btc_send": fake}):
        out = _call_tool("btc_send", {**_SEND, "password_file": str(tmp_path / "nope")})
    assert out["error"]["code"] == "ValueError"
    assert "nope" in out["error"]["message"]
    fake.assert_not_called()
