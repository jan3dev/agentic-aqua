import click

from ..tools import qr_decode
from .output import run_tool


@click.group("qr")
def qr():
    """QR code utilities."""


@qr.command("decode")
@click.argument("image_path")
@click.pass_obj
def decode(ctx, image_path):
    """Decode a QR code from an image file."""
    run_tool(ctx, lambda: qr_decode(image_path=image_path))
