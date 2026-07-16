"""Doctor command — diagnose and optionally repair ~/.aqua/config.json."""

import sys

import click

from .output import run_tool


@click.command("doctor")
@click.option("--fix", is_flag=True, help="Apply the repairs (default: diagnose only).")
@click.pass_obj
def doctor(ctx, fix):
    """Diagnose (and with --fix, repair) your AQUA config file.

    Exit code 0 if healthy or fully repaired, 1 if issues remain.
    """
    from ..doctor import run_doctor

    report = run_tool(ctx, lambda: run_doctor(fix=fix))
    # healthy already reflects post-fix state, so it's the sole exit-code source.
    sys.exit(0 if report["healthy"] else 1)
