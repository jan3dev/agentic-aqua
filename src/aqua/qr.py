"""QR decoding helpers."""

from pathlib import Path
from typing import Any

from PIL import Image
from pyzbar.pyzbar import decode


class QrDecodeError(ValueError):
    """Raised when a QR image cannot be decoded."""


def decode_qr_image(image_path: str) -> dict[str, Any]:
    """Decode a QR image file and return its raw string contents.

    Args:
        image_path: Path to an image file containing a QR code.

    Returns:
        Dict with the image path and decoded text.

    Raises:
        QrDecodeError: if no QR is found, more than one QR is found, or the QR
            payload cannot be decoded as UTF-8 text.
    """
    path = Path(image_path).expanduser()
    if not path.exists():
        raise QrDecodeError(f"Image file not found: {image_path}")
    if not path.is_file():
        raise QrDecodeError(f"Path is not a file: {image_path}")

    with Image.open(path) as img:
        results = decode(img)

    if not results:
        raise QrDecodeError("No QR code found in image")
    if len(results) > 1:
        raise QrDecodeError(
            f"Expected exactly one QR code, found {len(results)}"
        )

    raw_bytes = results[0].data
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QrDecodeError("QR payload is not valid UTF-8 text") from exc

    return {
        "image_path": str(path),
        "text": raw_text,
    }
