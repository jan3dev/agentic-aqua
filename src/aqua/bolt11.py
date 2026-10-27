"""BOLT11 Lightning invoice parsing — pure protocol logic, no provider dependencies."""

import re

_BOLT11_MULTIPLIERS: dict[str, float] = {
    "m": 100_000,     # milli-BTC
    "u": 100,         # micro-BTC
    "n": 0.1,         # nano-BTC
    "p": 0.0001,      # pico-BTC
    "": 100_000_000,  # BTC (no suffix)
}

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GEN = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
_TAG_PAYMENT_HASH = 1  # 'p'
_TAG_DESCRIPTION = 13  # 'd'
_TAG_EXPIRY = 6        # 'x'


def _polymod(values: list[int]) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _BECH32_GEN[i]
    return chk


def _verify_bolt11(invoice: str) -> str:
    """Return the lowercase invoice after verifying its bech32 checksum (BIP-173).

    BOLT11 uses plain bech32 (not bech32m) and is exempt from the 90-char limit.
    Raises ValueError on mixed case, bad charset, malformed layout or bad checksum.
    """
    invoice = invoice.strip()
    if invoice != invoice.lower() and invoice != invoice.upper():
        raise ValueError("Invalid BOLT11 invoice: mixed-case")
    invoice = invoice.lower()
    sep = invoice.rfind("1")
    hrp, data_str = invoice[:sep], invoice[sep + 1:]
    if sep < 2 or not hrp.startswith("ln") or len(data_str) < 6:
        raise ValueError("Invalid BOLT11 invoice: malformed")
    if any(c not in _BECH32_CHARSET for c in data_str) or any(not 33 <= ord(c) <= 126 for c in hrp):
        raise ValueError("Invalid BOLT11 invoice: invalid characters")
    data = [_BECH32_CHARSET.index(c) for c in data_str]
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    if _polymod(expanded + data) != 1:
        raise ValueError("Invalid BOLT11 invoice: bad checksum")
    return invoice


def decode_bolt11_amount_sats(invoice: str) -> int | None:
    """Extract the amount in satoshis from a BOLT11 invoice.

    Returns None for zero-amount invoices or if the amount cannot be parsed.
    """
    invoice = _verify_bolt11(invoice)
    for prefix in ("lnbcrt", "lnbc", "lntb", "lntbs"):
        if invoice.startswith(prefix):
            hrp_rest = invoice[len(prefix):]
            break
    else:
        return None

    if not hrp_rest:
        return None

    match = re.match(r"^(\d+)([munp]?)1", hrp_rest)
    if not match:
        return None

    amount = int(match.group(1))
    multiplier = match.group(2)
    sats = amount * _BOLT11_MULTIPLIERS[multiplier]
    return int(sats)


def _iter_tagged_fields(invoice: str):
    """Yield (field_type, 5-bit groups) for each tagged field of a verified invoice."""
    sep = invoice.rfind("1")
    if sep < 0:
        return
    data_str = invoice[sep + 1:-6]  # strip 6-char checksum
    try:
        groups = [_BECH32_CHARSET.index(c) for c in data_str]
    except ValueError:
        return

    if len(groups) < 7 + 104:  # 7 timestamp + 104 signature groups
        return

    pos = 7
    end = len(groups) - 104
    while pos + 3 <= end:
        field_type = groups[pos]
        field_len = (groups[pos + 1] << 5) | groups[pos + 2]
        pos += 3
        if pos + field_len > end:
            return
        yield field_type, groups[pos: pos + field_len]
        pos += field_len


def _groups_to_bytes(groups: list[int]) -> bytes:
    """Convert 5-bit bech32 groups to bytes, dropping trailing padding bits."""
    acc = 0
    bits = 0
    raw: list[int] = []
    for g in groups:
        acc = (acc << 5) | g
        bits += 5
        while bits >= 8:
            bits -= 8
            raw.append((acc >> bits) & 0xFF)
    return bytes(raw)


def decode_bolt11_fields(invoice: str) -> dict:
    """Decode key fields from a BOLT11 invoice without paying it.

    Returns amount_sats (None for zero-amount), description (None if absent),
    and expiry (default 3600 seconds per spec).
    """
    invoice = _verify_bolt11(invoice)
    result: dict = {
        "amount_sats": decode_bolt11_amount_sats(invoice),
        "description": None,
        "expiry": 3600,
    }

    for field_type, field_data in _iter_tagged_fields(invoice):
        if field_type == _TAG_DESCRIPTION:
            try:
                result["description"] = _groups_to_bytes(field_data).decode("utf-8")
            except UnicodeDecodeError:
                pass
        elif field_type == _TAG_EXPIRY:
            expiry = 0
            for g in field_data:
                expiry = (expiry << 5) | g
            result["expiry"] = expiry

    return result


def decode_bolt11_payment_hash(invoice: str) -> str:
    """Extract the 32-byte payment hash ('p' field) from a BOLT11 invoice, as hex.

    Raises ValueError when the invoice carries no well-formed payment hash.
    """
    for field_type, field_data in _iter_tagged_fields(_verify_bolt11(invoice)):
        # The spec fixes 'p' at 52 groups (260 bits); readers MUST skip other lengths.
        if field_type == _TAG_PAYMENT_HASH and len(field_data) == 52:
            return _groups_to_bytes(field_data)[:32].hex()
    raise ValueError("Invoice does not contain a valid payment hash")
