import click

from ..tools import qr_decode, qr_generate
from .output import run_tool


@click.group("qr")
def qr():
    """QR code utilities."""


@qr.command("generate")
@click.argument("data")
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=str),
    default=None,
    help="Directory where the PNG should be written (default: ~/.aqua/qr).",
)
@click.option(
    "--filename",
    default=None,
    help="Optional PNG filename. Defaults to qr_<sha256[:16]>.png.",
)
@click.option(
    "--terminal",
    is_flag=True,
    help="Also include a terminal-friendly block QR in the output.",
)
@click.pass_obj
def generate(ctx, data, output_dir, filename, terminal):
    """Generate a QR code PNG for DATA."""
    run_tool(
        ctx,
        lambda: qr_generate(
            data=data,
            output_dir=output_dir,
            filename=filename,
            terminal=terminal,
        ),
    )


@qr.command("decode")
@click.argument("image_path")
@click.pass_obj
def decode(ctx, image_path):
    """Decode a QR code from an image file."""
    run_tool(ctx, lambda: qr_decode(image_path=image_path))
