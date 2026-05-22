from pathlib import Path

import zxingcpp
from PIL import Image


def decode_qr(image_path: str) -> str:
    path = Path(image_path)
    if not path.is_file():
        raise ValueError(f"Image file not found: {image_path}")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError(f"Image file too large (max 20MB): {image_path}")
    try:
        img = Image.open(str(path))
    except Exception as exc:
        raise ValueError(f"Could not read image: {image_path}") from exc
    results = zxingcpp.read_barcodes(img)
    if not results:
        raise ValueError("No QR code found in image")
    return results[0].text
