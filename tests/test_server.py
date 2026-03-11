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

from aqua_mcp.server import create_server


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
