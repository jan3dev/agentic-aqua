"""Serve command — starts the MCP stdio server."""

import asyncio

import click


@click.command("serve")
def serve():
    """Start the MCP server over stdio (`aqua serve` or `aqua-mcp`)."""
    from ..server import run_server

    asyncio.run(run_server())
