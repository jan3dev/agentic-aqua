from pathlib import Path

import pytest
import qrcode
from PIL import Image

from aqua.qr import decode_qr
from aqua.tools import qr_decode as qr_decode_tool

ASSETS = Path(__file__).parent / "assets"

# A real 100-sat BOLT11 invoice ("test_invoice") encoded in the committed QR
# fixture. BOLT11 QR codes are encoded in UPPERCASE per the spec for denser
# alphanumeric QR encoding; decoding therefore returns the uppercase form,
# which downstream pay_invoice() lowercases before use.
SATS100_INVOICE = (
    "lnbc1u1p4zp9lgdq5w3jhxazld9h8vmmfvdjsnp4qgt72s92ak77wsszt7dqs8shkjy0re5r8fs8tnsay4zg7gpjekrr7"
    "pp5077e3lgzgvc6ua9mdng45asrtt6mmzj79q5pmhx4xrxxfw64n2eqsp596qxgpqy5780x8zvd73nh7axyaju3l42szt"
    "xjp5s3cp645j467us9qyysgqcqzp2xqyz5vqp6hfhfg74lqprd3gcsv3y2vtgmv3stvz05999vx4xf0nmc3y0pu9la05lz"
    "4rzj7v0eer8rz2d7vxafcuz4j7jtjyqcmmrnv6yf293lcqg90w9y"
)


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


def test_decode_qr_real_100sats_invoice_image():
    """Scanning the committed 100-sat invoice QR returns the BOLT11 invoice.

    BOLT11 QR payloads are uppercase, so the decoded content is the uppercase
    form; lowercasing it yields the canonical `lnbc...` invoice.
    """
    img_path = ASSETS / "100sats_invoice.png"
    content = decode_qr(str(img_path))

    assert content == SATS100_INVOICE.upper()
    assert content.lower() == SATS100_INVOICE


def test_qr_decode_tool_real_100sats_invoice_image():
    """The qr_decode MCP tool returns the invoice from the committed fixture."""
    img_path = ASSETS / "100sats_invoice.png"
    result = qr_decode_tool(str(img_path))

    assert isinstance(result, dict)
    assert result["content"].lower() == SATS100_INVOICE
