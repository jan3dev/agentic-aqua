"""Doctor command — diagnose and optionally repair ~/.aqua/config.json."""

import sys

import click

from .output import render, render_error


@click.command("doctor")
@click.option("--fix", is_flag=True, help="Apply the repairs (default: diagnose only).")
@click.pass_obj
def doctor(ctx, fix):
    """Diagnose (and with --fix, repair) your AQUA config file.

    Exit code 0 if healthy or fully repaired, 1 if issues remain.
    """
    from ..doctor import run_doctor

    # Match cli.output.run_tool's error envelope instead of a raw traceback.
    try:
        report = run_doctor(fix=fix)
    except Exception as exc:  # noqa: BLE001 — surface any failure as an envelope
        click.echo(render_error(type(exc).__name__, str(exc), ctx.fmt), err=True)
        sys.exit(1)

    click.echo(render(report, ctx.fmt))

    # healthy already reflects post-fix state, so it's the sole exit-code source.
    sys.exit(0 if report["healthy"] else 1)
