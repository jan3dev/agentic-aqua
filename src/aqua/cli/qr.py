"""QR-related CLI commands."""

import click

from ..tools import decode_payment_qr
from .output import run_tool


@click.group()
def qr():
    """QR operations."""
    pass


@qr.command("decode")
@click.option("--image", "image_path", required=True, help="Path to the QR image file.")
@click.pass_obj
def decode_cmd(ctx, image_path):
    """Decode a QR image and return the raw string it contains."""
    run_tool(ctx, lambda: decode_payment_qr(image_path))
