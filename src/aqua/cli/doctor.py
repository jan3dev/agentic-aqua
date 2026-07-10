"""Doctor command — diagnose and optionally repair ~/.aqua/config.json."""

import sys

import click

from .output import render, render_error


@click.command("doctor")
@click.option("--fix", is_flag=True, help="Apply the repairs (default: diagnose only).")
@click.pass_obj
def doctor(ctx, fix):
    """Diagnose (and with --fix, repair) your AQUA config file.

    By default this only reports issues. Exit code is 0 when the config is
    healthy (or was fully repaired) and 1 when fixable issues remain or manual
    intervention is needed.
    """
    from ..doctor import run_doctor

    # Match the CLI convention (see cli/output.run_tool): render the error
    # envelope instead of dumping a raw traceback if the fix write path raises
    # (e.g. read-only FS, disk full, permission denied).
    try:
        report = run_doctor(fix=fix)
    except Exception as exc:  # noqa: BLE001 — surface any failure as an envelope
        click.echo(render_error(type(exc).__name__, str(exc), ctx.fmt), err=True)
        sys.exit(1)

    click.echo(render(report, ctx.fmt))

    # `healthy` already reflects the post-fix state (False if any manual-only
    # finding remains), so it is the single source of truth for the exit code.
    sys.exit(0 if report["healthy"] else 1)
