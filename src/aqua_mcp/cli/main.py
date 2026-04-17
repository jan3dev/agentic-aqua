"""Root CLI group for AQUA wallet operations."""

import logging
from pathlib import Path

import click
from dotenv import load_dotenv

# Load .env independently (not via server.py) so CLI works without MCP imports
_project_root = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_project_root / ".env")

# Default to WARNING so tool-level INFO logs don't spam stderr in CLI mode
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


class AquaContext:
    """Shared context for all CLI commands."""

    def __init__(self, fmt: str | None = None, verbose: bool = False):
        self.fmt = fmt
        self.verbose = verbose


@click.group(context_settings={"auto_envvar_prefix": "AQUA"})
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "pretty"]),
    default=None,
    help="Output format. Default: pretty on terminal, json when piped.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logging.")
@click.version_option(package_name="aqua-mcp")
@click.pass_context
def cli(ctx, fmt, verbose):
    """AQUA wallet CLI — manage Bitcoin, Liquid, and Lightning wallets."""
    ctx.ensure_object(dict)
    ctx.obj = AquaContext(fmt=fmt, verbose=verbose)
    if verbose:
        logging.getLogger().setLevel(logging.INFO)


# Import and register subcommands
from .commands import register_commands  # noqa: E402

register_commands(cli)
