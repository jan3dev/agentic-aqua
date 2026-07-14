"""
op_verify.py — self-contained verifier for Observer Protocol PolicyEvaluationCredentials.

The point of shipping this INSIDE the wallet is independence: the wallet does not trust
the transport, it trusts a signature it checks itself against the published DID.

Suite: eddsa-jcs-2022 (W3C Data Integrity), verification method
did:web:observerprotocol.org#key-3.

============================ READ BEFORE SHIPPING ============================
This is a REFERENCE implementation. Two things MUST be reconciled before this
goes into the PR:

  1. Prefer vendoring the proven ~200-LOC June verifier over this file. That verifier
     already verifies real PECs correctly; this reference exists so op_policy.py has a
     working import today, not to replace a proven artifact with an unproven one.

  2. If you do keep this file, VALIDATE IT AGAINST A LIVE PEC first. Two known traps:
       - JCS: the canonicalization here uses json.dumps(sort_keys, compact) as an
         approximation of RFC 8785. That is correct for string/object-only documents
         but NOT for arbitrary numbers. If a signed PEC field is numeric, use a real
         RFC 8785 JCS implementation (or the June verifier).
       - proof.@context drift: CC reported the live sidecar emits a proof.@context block
         that is absent from the checked-in policy-core source. The proof-config
         canonicalization MUST match exactly what the sidecar signed. Verify against a
         freshly fetched live PEC and adjust _proof_config() if the server's proof shape
         differs from what this file assumes.
=============================================================================
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# did:web:observerprotocol.org -> https://observerprotocol.org/.well-known/did.json
DID_WEB = "did:web:observerprotocol.org"
EXPECTED_VM = f"{DID_WEB}#key-3"
_DID_DOC_URL = "https://observerprotocol.org/.well-known/did.json"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


class VerificationError(Exception):
    """Raised when a PEC signature cannot be verified. Callers MUST fail closed."""


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        if ch not in _B58_INDEX:
            raise VerificationError(f"invalid base58 char: {ch!r}")
        n = n * 58 + _B58_INDEX[ch]
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + full


def _multibase_decode(mb: str) -> bytes:
    # We only handle the 'z' (base58btc) multibase prefix, which is what OP emits.
    if not mb or mb[0] != "z":
        raise VerificationError(f"unsupported multibase prefix in {mb[:1]!r}")
    return _b58decode(mb[1:])


def _jcs(obj: Any) -> bytes:
    # Approximate JCS. See "READ BEFORE SHIPPING" — validate against a live PEC.
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _reject_float_leaves(obj: Any, path: str = "$") -> None:
    """Fail closed if any leaf is a non-integer JSON number (a float).

    json.dumps and RFC 8785 agree on integers (shortest decimal), so integer leaves
    are safe — and real deny PECs legitimately carry integer bounds
    (denyReason.currentValue / proposedValue), which must stay verifiable. They
    diverge on FLOATS: RFC 8785 mandates ES6 %.17g canonicalization, which json.dumps
    does not reproduce, so a float leaf could let a mis-canonicalized signature verify.
    Every PEC signed today is integer/string-only, so this never fires — but if OP ever
    signs a fractional field, we turn that silent risk into a loud VerificationError
    (caller then fails closed) rather than trusting the approximation outside its range.
    Swap in a real RFC 8785 JCS implementation before signing fractional PEC fields.
    """
    if isinstance(obj, bool):
        return
    if isinstance(obj, float):
        raise VerificationError(
            f"float leaf at {path} in signed content; the JCS approximation does not "
            f"cover RFC 8785 float canonicalization — refusing to verify (fail-closed)."
        )
    if isinstance(obj, dict):
        for k, v in obj.items():
            _reject_float_leaves(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_float_leaves(v, f"{path}[{i}]")


def _fetch_public_key(vm_id: str, *, timeout: float) -> bytes:
    # observerprotocol.org is Cloudflare-fronted and 403s the default "Python-urllib/*"
    # UA, so an unadorned urlopen here fails closed on every verification. Send a UA.
    _req = urllib.request.Request(
        _DID_DOC_URL, headers={"User-Agent": "agentic-aqua-op-policy/0.1"}
    )
    with urllib.request.urlopen(_req, timeout=timeout) as resp:
        if resp.status != 200:
            raise VerificationError(f"DID doc fetch returned HTTP {resp.status}")
        did_doc = json.loads(resp.read().decode("utf-8"))

    methods = did_doc.get("verificationMethod", []) or []
    _frag = vm_id.split("#")[-1]
    vm = next(
        (m for m in methods if m.get("id") in (vm_id, _frag, "#" + _frag)), None
    )
    if vm is None:
        raise VerificationError(f"verification method {vm_id} not found in DID document")

    mb = vm.get("publicKeyMultibase")
    if not mb:
        # publicKeyJwk is the other legal shape; reconcile if OP switches to it.
        raise VerificationError(
            "verification method has no publicKeyMultibase (JWK not handled here)"
        )

    raw = _multibase_decode(mb)
    # multicodec ed25519-pub prefix is 0xed 0x01
    if raw[:2] == b"\xed\x01":
        raw = raw[2:]
    if len(raw) != 32:
        raise VerificationError(f"expected 32-byte ed25519 key, got {len(raw)}")
    return raw


def _proof_config(proof: dict, doc_context: Any) -> dict:
    cfg = {k: v for k, v in proof.items() if k != "proofValue"}
    # Data Integrity: proof config is canonicalized with the document's @context if the
    # proof does not carry its own. If the live proof DOES carry @context, keep it as-is.
    if "@context" not in cfg and doc_context is not None:
        cfg["@context"] = doc_context
    return cfg


def verify_pec(pec: dict, *, expected_vm: str = EXPECTED_VM, timeout: float = 10.0) -> None:
    """Verify a PolicyEvaluationCredential in place. Returns None on success, raises on failure.

    Verifies: (a) the proof's verificationMethod is the one we expect, and (b) the
    eddsa-jcs-2022 signature is valid against the key published in the OP DID document.
    Does NOT judge the decision field — that's op_policy's job.
    """
    proof = pec.get("proof")
    if not isinstance(proof, dict):
        raise VerificationError("credential has no proof")

    if proof.get("type") not in ("DataIntegrityProof",):
        raise VerificationError(f"unexpected proof.type: {proof.get('type')!r}")
    if proof.get("cryptosuite") not in ("eddsa-jcs-2022",):
        raise VerificationError(f"unexpected cryptosuite: {proof.get('cryptosuite')!r}")

    vm = proof.get("verificationMethod")
    if vm != expected_vm:
        raise VerificationError(f"unexpected verificationMethod {vm!r}, expected {expected_vm!r}")

    proof_value = proof.get("proofValue")
    if not proof_value:
        raise VerificationError("proof has no proofValue")
    signature = _multibase_decode(proof_value)
    if len(signature) != 64:
        raise VerificationError(f"expected 64-byte signature, got {len(signature)}")

    document = {k: v for k, v in pec.items() if k != "proof"}
    doc_context = pec.get("@context")
    proof_config = _proof_config(proof, doc_context)

    # Guard: the JCS approximation matches RFC 8785 for integers/strings but not
    # floats. Fail closed on float leaves rather than trust it outside that range.
    _reject_float_leaves(document, "$")
    _reject_float_leaves(proof_config, "$.proof")

    pc_hash = hashlib.sha256(_jcs(proof_config)).digest()
    doc_hash = hashlib.sha256(_jcs(document)).digest()
    signing_input = pc_hash + doc_hash  # proofConfig hash || document hash

    public_key = _fetch_public_key(expected_vm, timeout=timeout)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, signing_input)
    except InvalidSignature as exc:
        raise VerificationError(
            "PEC signature did not verify against did:web:observerprotocol.org#key-3"
        ) from exc
