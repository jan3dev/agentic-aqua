"""BIP-327 (MuSig2) vendored implementation checked against the official vectors.

Vectors in `tests/assets/bip327/` are copied verbatim from
https://github.com/bitcoin/bips/tree/master/bip-0327/vectors.
"""

import json
from pathlib import Path

import pytest

from aqua._bip327 import (
    InvalidContributionError,
    SessionContext,
    key_agg,
    key_agg_and_tweak,
    nonce_agg,
    partial_sig_agg,
    partial_sig_verify,
    sign,
)

VECTORS_DIR = Path(__file__).parent / "assets" / "bip327"


def load_vectors(name: str) -> dict:
    with open(VECTORS_DIR / f"{name}.json") as f:
        return json.load(f)


def hexlist(values) -> list[bytes]:
    return [bytes.fromhex(v) for v in values]


class TestKeyAgg:
    def test_valid_vectors(self):
        v = load_vectors("key_agg_vectors")
        pubkeys = hexlist(v["pubkeys"])
        tweaks = hexlist(v["tweaks"])
        for case in v["valid_test_cases"]:
            keys = [pubkeys[i] for i in case["key_indices"]]
            twks = [tweaks[i] for i in case.get("tweak_indices", [])]
            xonly = case.get("is_xonly", [])
            ctx = key_agg_and_tweak(keys, twks, xonly)
            from aqua._bip327 import get_xonly_pk

            assert get_xonly_pk(ctx).hex().upper() == case["expected"]

    def test_error_vectors(self):
        v = load_vectors("key_agg_vectors")
        pubkeys = hexlist(v["pubkeys"])
        tweaks = hexlist(v["tweaks"])
        for case in v["error_test_cases"]:
            keys = [pubkeys[i] for i in case["key_indices"]]
            twks = [tweaks[i] for i in case.get("tweak_indices", [])]
            xonly = case.get("is_xonly", [])
            with pytest.raises((InvalidContributionError, ValueError)):
                key_agg_and_tweak(keys, twks, xonly)


class TestNonceAgg:
    def test_valid_vectors(self):
        v = load_vectors("nonce_agg_vectors")
        pnonces = hexlist(v["pnonces"])
        for case in v["valid_test_cases"]:
            nonces = [pnonces[i] for i in case["pnonce_indices"]]
            assert nonce_agg(nonces).hex().upper() == case["expected"]

    def test_error_vectors(self):
        v = load_vectors("nonce_agg_vectors")
        pnonces = hexlist(v["pnonces"])
        for case in v["error_test_cases"]:
            nonces = [pnonces[i] for i in case["pnonce_indices"]]
            with pytest.raises(InvalidContributionError):
                nonce_agg(nonces)


class TestSignVerify:
    """Covers the full signing path: sign -> partial_sig_verify."""

    def test_valid_vectors(self):
        v = load_vectors("sign_verify_vectors")
        sk = bytes.fromhex(v["sk"])
        pubkeys = hexlist(v["pubkeys"])
        secnonces = hexlist(v["secnonces"])
        pnonces = hexlist(v["pnonces"])
        aggnonces = hexlist(v["aggnonces"])
        msgs = hexlist(v["msgs"])

        for case in v["valid_test_cases"]:
            keys = [pubkeys[i] for i in case["key_indices"]]
            nonces = [pnonces[i] for i in case["nonce_indices"]]
            aggnonce = aggnonces[case["aggnonce_index"]]
            assert nonce_agg(nonces) == aggnonce

            session_ctx = SessionContext(
                aggnonce, keys, [], [], msgs[case["msg_index"]]
            )
            # sign() mutates secnonce in place, so hand it a fresh copy.
            psig = sign(bytearray(secnonces[0]), sk, session_ctx)
            assert psig.hex().upper() == case["expected"]

            signer_index = case["signer_index"]
            assert partial_sig_verify(
                psig, nonces, keys, [], [], msgs[case["msg_index"]], signer_index
            )


class TestTweak:
    """x-only tweaks are what the taproot refund path relies on."""

    def test_valid_vectors(self):
        v = load_vectors("tweak_vectors")
        sk = bytes.fromhex(v["sk"])
        pubkeys = hexlist(v["pubkeys"])
        secnonce = bytes.fromhex(v["secnonce"])
        pnonces = hexlist(v["pnonces"])
        aggnonce = bytes.fromhex(v["aggnonce"])
        tweaks = hexlist(v["tweaks"])
        msg = bytes.fromhex(v["msg"])

        for case in v["valid_test_cases"]:
            keys = [pubkeys[i] for i in case["key_indices"]]
            nonces = [pnonces[i] for i in case["nonce_indices"]]
            twks = [tweaks[i] for i in case["tweak_indices"]]
            xonly = case["is_xonly"]
            assert nonce_agg(nonces) == aggnonce

            session_ctx = SessionContext(aggnonce, keys, twks, xonly, msg)
            psig = sign(bytearray(secnonce), sk, session_ctx)
            assert psig.hex().upper() == case["expected"]
            assert partial_sig_verify(
                psig, nonces, keys, twks, xonly, msg, case["signer_index"]
            )


class TestSigAgg:
    def test_valid_vectors(self):
        v = load_vectors("sig_agg_vectors")
        pubkeys = hexlist(v["pubkeys"])
        pnonces = hexlist(v["pnonces"])
        tweaks = hexlist(v["tweaks"])
        psigs = hexlist(v["psigs"])
        msg = bytes.fromhex(v["msg"])

        for case in v["valid_test_cases"]:
            keys = [pubkeys[i] for i in case["key_indices"]]
            twks = [tweaks[i] for i in case["tweak_indices"]]
            xonly = case["is_xonly"]
            sigs = [psigs[i] for i in case["psig_indices"]]
            aggnonce = bytes.fromhex(case["aggnonce"])
            assert nonce_agg([pnonces[i] for i in case["nonce_indices"]]) == aggnonce

            session_ctx = SessionContext(aggnonce, keys, twks, xonly, msg)
            sig = partial_sig_agg(sigs, session_ctx)
            assert sig.hex().upper() == case["expected"]

            from aqua._bip327 import get_xonly_pk, schnorr_verify

            aggpk = get_xonly_pk(key_agg_and_tweak(keys, twks, xonly))
            assert schnorr_verify(msg, aggpk, sig)


def test_key_agg_is_order_sensitive():
    """Boltz aggregates [claim, refund] in a fixed order — never key-sorted."""
    v = load_vectors("key_agg_vectors")
    a, b = hexlist(v["pubkeys"])[:2]
    from aqua._bip327 import get_xonly_pk

    assert get_xonly_pk(key_agg([a, b])) != get_xonly_pk(key_agg([b, a]))
