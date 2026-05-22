import pytest
import qrcode
from PIL import Image

from aqua.qr import decode_qr
from aqua.tools import qr_decode as qr_decode_tool


def _make_qr_image(data: str, path) -> str:
    img = qrcode.make(data)
    img.save(str(path))
    return str(path)


def test_decode_qr_bitcoin_address(tmp_path):
    address = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
    img_path = _make_qr_image(address, tmp_path / "btc.png")
    assert decode_qr(img_path) == address


def test_decode_qr_bolt11(tmp_path):
    invoice = "lnbc1000u1ptest123"
    img_path = _make_qr_image(invoice, tmp_path / "bolt11.png")
    assert decode_qr(img_path) == invoice


def test_decode_qr_file_not_found():
    with pytest.raises(ValueError, match="not found"):
        decode_qr("nonexistent.png")


def test_decode_qr_no_qr_in_image(tmp_path):
    blank = Image.new("RGB", (200, 200), color=(255, 255, 255))
    img_path = str(tmp_path / "blank.png")
    blank.save(img_path)
    with pytest.raises(ValueError, match="No QR code"):
        decode_qr(img_path)


def test_qr_decode_tool_returns_dict(tmp_path):
    address = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
    img_path = _make_qr_image(address, tmp_path / "btc_tool.png")
    result = qr_decode_tool(img_path)
    assert isinstance(result, dict)
    assert result["content"] == address
