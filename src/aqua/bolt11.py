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
_TAG_DESCRIPTION = 13  # 'd'
_TAG_EXPIRY = 6        # 'x'


def decode_bolt11_amount_sats(invoice: str) -> int | None:
    """Extract the amount in satoshis from a BOLT11 invoice.

    Returns None for zero-amount invoices or if the amount cannot be parsed.
    """
    invoice = invoice.lower().strip()
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


def decode_bolt11_fields(invoice: str) -> dict:
    """Decode key fields from a BOLT11 invoice without paying it.

    Returns amount_sats (None for zero-amount), description (None if absent),
    and expiry (default 3600 seconds per spec).
    """
    invoice = invoice.lower().strip()
    amount_sats = decode_bolt11_amount_sats(invoice)
    result: dict = {"amount_sats": amount_sats, "description": None, "expiry": 3600}

    sep = invoice.rfind("1")
    if sep < 0:
        return result
    data_str = invoice[sep + 1:-6]  # strip 6-char checksum
    try:
        groups = [_BECH32_CHARSET.index(c) for c in data_str]
    except ValueError:
        return result

    if len(groups) < 7 + 104:
        return result

    pos = 7
    end = len(groups) - 104

    while pos + 3 <= end:
        field_type = groups[pos]
        field_len = (groups[pos + 1] << 5) | groups[pos + 2]
        pos += 3
        if pos + field_len > end:
            break
        field_data = groups[pos: pos + field_len]
        pos += field_len

        if field_type == _TAG_DESCRIPTION:
            acc = 0
            bits = 0
            raw: list[int] = []
            for g in field_data:
                acc = (acc << 5) | g
                bits += 5
                while bits >= 8:
                    bits -= 8
                    raw.append((acc >> bits) & 0xFF)
            try:
                result["description"] = bytes(raw).decode("utf-8")
            except UnicodeDecodeError:
                pass
        elif field_type == _TAG_EXPIRY:
            expiry = 0
            for g in field_data:
                expiry = (expiry << 5) | g
            result["expiry"] = expiry

    return result
