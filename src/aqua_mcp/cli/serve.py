"""Serve command — starts the MCP stdio server."""

import asyncio

import click


@click.command("serve")
@click.option(
    "--transport",
    type=click.Choice(["stdio"]),
    default="stdio",
    show_default=True,
    help="Transport protocol (only stdio supported).",
)
def serve(transport):
    """Start the MCP server (stdio transport)."""
    from ..server import run_server

    asyncio.run(run_server())
