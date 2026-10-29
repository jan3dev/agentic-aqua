"""BOLT11 bech32 checksum verification across every public decoder."""

import pytest

from aqua.bolt11 import (
    decode_bolt11_amount_sats,
    decode_bolt11_fields,
    decode_bolt11_payment_hash,
)

# BOLT11 spec vector: 250 000 sats, description "1 cup coffee", expiry 60.
SPEC_INVOICE = (
    "lnbc2500u1pvjluezpp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypqdq5xys"
    "xxatsyp3k7enxv4jsxqzpuaztrnwngzn3kdzw5hydlzf03qdgm2hdq27cqv3agm2awhz5se903vrua"
    "tfhq77w3ls4evs3ch9zw97j25emudupq63nyw24cg27h2rspfj9srp"
)
PAYMENT_HASH = "0001020304050607080900010203040506070809000102030405060708090102"

DECODERS = [decode_bolt11_amount_sats, decode_bolt11_fields, decode_bolt11_payment_hash]


def _flip_last(invoice: str) -> str:
    return invoice[:-1] + ("q" if invoice[-1] != "q" else "p")


def test_spec_vector_decodes():
    assert decode_bolt11_amount_sats(SPEC_INVOICE) == 250_000
    fields = decode_bolt11_fields(SPEC_INVOICE)
    assert fields["description"] == "1 cup coffee"
    assert fields["expiry"] == 60
    assert decode_bolt11_payment_hash(SPEC_INVOICE) == PAYMENT_HASH


def test_uppercase_is_accepted():
    assert decode_bolt11_payment_hash(SPEC_INVOICE.upper()) == PAYMENT_HASH
    assert decode_bolt11_amount_sats(f"  {SPEC_INVOICE.upper()}\n") == 250_000


@pytest.mark.parametrize("decode", DECODERS)
def test_flipped_checksum_char_is_rejected(decode):
    with pytest.raises(ValueError, match="bad checksum"):
        decode(_flip_last(SPEC_INVOICE))


@pytest.mark.parametrize("decode", DECODERS)
def test_tampered_data_char_is_rejected(decode):
    # Change a character inside the description field.
    pos = SPEC_INVOICE.index("5xys") + 1
    swap = "q" if SPEC_INVOICE[pos] != "q" else "p"
    tampered = SPEC_INVOICE[:pos] + swap + SPEC_INVOICE[pos + 1:]
    with pytest.raises(ValueError, match="bad checksum"):
        decode(tampered)


@pytest.mark.parametrize("decode", DECODERS)
def test_mixed_case_is_rejected(decode):
    with pytest.raises(ValueError, match="mixed-case"):
        decode("LNBC" + SPEC_INVOICE[4:])


@pytest.mark.parametrize("decode", DECODERS)
@pytest.mark.parametrize("bad", ["b", "i", "o", "1", "-"])
def test_invalid_charset_is_rejected(decode, bad):
    with pytest.raises(ValueError, match="Invalid BOLT11"):
        decode(SPEC_INVOICE[:-3] + bad + SPEC_INVOICE[-2:])


@pytest.mark.parametrize("decode", DECODERS)
@pytest.mark.parametrize("bad", ["", "lnbc", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "ln"])
def test_malformed_input_is_rejected(decode, bad):
    with pytest.raises(ValueError, match="Invalid BOLT11"):
        decode(bad)
