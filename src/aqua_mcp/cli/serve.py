"""Serve command — starts the MCP stdio server."""

import asyncio

import click


@click.command("serve")
def serve():
    """Start the MCP server over stdio."""
    from ..server import run_server

    asyncio.run(run_server())
