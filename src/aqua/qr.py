import hashlib
import os
import tempfile
from pathlib import Path

import qrcode
import zxingcpp
from PIL import Image


def generate_qr(data: str, output_dir: str | Path, filename: str | None = None) -> str:
    """Generate a PNG QR code for ``data`` and return the saved file path.

    Used to render deposit addresses, Lightning invoices and swap deposit
    addresses as scannable QR images. The file is written atomically (temp
    file + ``os.replace``) with ``0o600`` permissions, mirroring the rest of
    the on-disk ``~/.aqua`` layout.

    Args:
        data: The string to encode (address, BOLT11 invoice, etc.).
        output_dir: Directory to write the PNG into (created at ``0o700`` if
            missing). Typically ``Storage.qr_dir``.
        filename: Optional file name. Defaults to a content-addressed name
            ``qr_<sha256(data)[:16]>.png`` so identical payloads reuse one file.

    Returns:
        Absolute path to the written PNG.

    Raises:
        ValueError: If ``data`` is not a non-empty string.
    """
    if not isinstance(data, str) or not data:
        raise ValueError("QR data must be a non-empty string")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    if filename is None:
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]
        filename = f"qr_{digest}.png"
    path = out_dir / filename

    img = qrcode.make(data)
    fd, tmp_name = tempfile.mkstemp(dir=str(out_dir), suffix=".png.tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            img.save(fh, format="PNG")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return str(path.resolve())


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
