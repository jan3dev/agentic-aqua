"""
op_policy.py — opt-in Observer Protocol pre-sign policy check for Agentic Aqua.

Called from WalletManager.send() immediately before the signer is invoked. When
enabled (OP_POLICY_ENABLED), it submits the *exact* unsigned transaction to the
Observer Protocol policy engine and only permits signing on a verified "allow"
decision. Every other outcome fails closed — the wallet MUST NOT sign:

    engine unreachable            -> PolicyError  (fail closed)
    non-2xx from engine           -> PolicyError  (fail closed)
    unverifiable / expired PEC    -> PolicyError  (fail closed)
    decision == "deny"            -> PolicyDenied (deny artifact persisted, then raised)
    any non-"allow" decision      -> PolicyError  (fail closed)
    decision == "allow"           -> returns the verified credential; signing proceeds

When OP_POLICY_ENABLED is unset/false this module is inert: enabled() returns False and
the wallet behaves exactly as before. This is additive and opt-in.

Config is env-driven (see README). Renamed from op_policy_demo.py; the working-tree
refinements (stderr logging, unit resolution, deny artifact) are folded in here, plus
PEC signature verification on the return path.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aqua import op_verify  # vendored; see op_verify.py "READ BEFORE SHIPPING"

_DEFAULT_SIDECAR_URL = "https://api.observerprotocol.org/policy/evaluate"
_USER_AGENT = "agentic-aqua-op-policy/0.1"
_DEFAULT_TIMEOUT = 10.0
_DEFAULT_DENY_DIR = "./op-artifacts"
_TRUE = {"1", "true", "yes"}


class PolicyError(RuntimeError):
    """Fail-closed condition. The wallet MUST NOT sign."""


class PolicyDenied(PolicyError):
    """Engine returned a signed deny. The wallet MUST NOT sign."""

    def __init__(self, message: str, credential: Optional[dict] = None) -> None:
        super().__init__(message)
        self.credential = credential


def _emit(msg: str) -> None:
    """Operator-visible log line on stderr. Kept quiet unless the hook is active."""
    print(f"[op-policy] {msg}", file=sys.stderr, flush=True)


def enabled() -> bool:
    return os.environ.get("OP_POLICY_ENABLED", "").strip().lower() in _TRUE


@dataclass(frozen=True)
class _Config:
    sidecar_url: str
    delegation_path: str
    timeout: float
    verify_pec: bool
    deny_dir: str
    unit_override: Optional[str]


def canonical_bytes(unsigned_pset: Any) -> str:
    """Hex of the consensus-serialized unsigned tx extracted from THIS exact Pset.

    Ported verbatim from the proven demo extraction. The caller MUST pass the same
    lwk.Pset instance that will reach signer.sign(); we refuse anything lacking the
    pre-sign extract_tx()/to_bytes() surface so a wrong type fails loudly rather than
    silently hashing something else. The engine recomputes proposalHash from these
    bytes, so we return only the hex.
    """
    if not hasattr(unsigned_pset, "extract_tx"):
        raise PolicyError(
            "unsigned_pset must be an lwk.Pset (has extract_tx); "
            f"got {type(unsigned_pset)!r} (fail-closed)"
        )
    unsigned_tx = unsigned_pset.extract_tx()
    # lwk.Transaction.to_bytes() = elements::Transaction::serialize; bytes() is a
    # deprecated alias; Display is hex of the same consensus bytes.
    if hasattr(unsigned_tx, "to_bytes"):
        raw = bytes(unsigned_tx.to_bytes())
    elif hasattr(unsigned_tx, "bytes"):
        raw = bytes(unsigned_tx.bytes())
    else:
        raw = bytes.fromhex(str(unsigned_tx))
    return raw.hex()


def _load_config() -> _Config:
    delegation_path = os.environ.get("OP_DELEGATION_PATH", "").strip()
    if not delegation_path:
        raise PolicyError(
            "OP_POLICY_ENABLED is set but OP_DELEGATION_PATH is missing (fail-closed)"
        )
    try:
        timeout = float(os.environ.get("OP_POLICY_TIMEOUT", _DEFAULT_TIMEOUT))
    except ValueError:
        raise PolicyError("OP_POLICY_TIMEOUT is not a number (fail-closed)")
    return _Config(
        sidecar_url=os.environ.get("OP_SIDECAR_URL", _DEFAULT_SIDECAR_URL).strip(),
        delegation_path=delegation_path,
        timeout=timeout,
        # Verification is on by default. It can be disabled for local testing, but doing
        # so is loud and drops the wallet's independent-trust guarantee.
        verify_pec=os.environ.get("OP_VERIFY_PEC", "true").strip().lower() in _TRUE,
        deny_dir=os.environ.get("OP_DENY_ARTIFACT_DIR", _DEFAULT_DENY_DIR).strip(),
        unit_override=(os.environ.get("OP_POLICY_UNIT", "").strip() or None),
    )


def _load_delegation(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"could not load delegation credential from {path}: {exc} (fail-closed)")


def _delegation_cap_unit(delegation: dict, rail: str, asset_id: Optional[str]) -> Optional[str]:
    """Read the unit of the spending cap for this rail from the delegation.

    NOTE: reconcile this navigation with the authoritative delegation schema (v2.1).
    The shape below matches the spending-delegation form; if the real credential nests
    differently, fix it here. We deliberately return None (not a guess) when we can't
    find it, so the caller fails closed rather than sending a wrong unit — that wrong
    unit is exactly the spurious unit-mismatch DENY the refinement removed.
    """
    try:
        scope = delegation["credentialSubject"]["delegation"]["scope"]
        limits = scope["spending_limits"]["per_rail"][rail]
    except (KeyError, TypeError):
        return None
    per_tx = limits.get("per_transaction") if isinstance(limits, dict) else None
    # Real delegation stores the denomination as `currency`; tolerate `unit` too.
    if isinstance(per_tx, dict) and (per_tx.get("currency") or per_tx.get("unit")):
        return per_tx.get("currency") or per_tx.get("unit")
    if isinstance(limits, dict) and (limits.get("currency") or limits.get("unit")):
        return limits.get("currency") or limits.get("unit")
    return None


def _resolve_unit(
    delegation: dict, rail: str, asset_id: Optional[str], override: Optional[str]
) -> str:
    """Resolve the unit sent in humanReadable so it matches the delegation cap.

    Sending a unit that disagrees with the cap unit yields a spurious unit-mismatch DENY
    from the engine — a false negative. So we prefer the cap's own unit; an explicit
    OP_POLICY_UNIT overrides; if neither is available we fail closed rather than guess.
    """
    if override:
        return override
    unit = _delegation_cap_unit(delegation, rail, asset_id)
    if unit:
        return unit
    raise PolicyError(
        "could not resolve the spending-cap unit from the delegation for "
        f"rail={rail!r} asset_id={asset_id!r}; set OP_POLICY_UNIT or fix the "
        "delegation (fail-closed)"
    )


def _post(cfg: _Config, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        cfg.sidecar_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Cloudflare fronts api.observerprotocol.org and 403s the default
            # "Python-urllib/*" UA (browser_signature_banned). Send an explicit UA
            # or every real request fails closed on a 403.
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            status = getattr(resp, "status", resp.getcode())
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise PolicyError(
            f"policy engine returned HTTP {exc.code}. FAIL-CLOSED. The wallet MUST NOT sign."
        )
    except urllib.error.URLError as exc:
        raise PolicyError(
            f"policy engine unreachable ({exc.reason}). FAIL-CLOSED. The wallet MUST NOT sign."
        )

    # Belt-and-suspenders: some stacks don't raise on all non-2xx.
    if status != 200:
        raise PolicyError(
            f"policy engine returned HTTP {status}. FAIL-CLOSED. The wallet MUST NOT sign."
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise PolicyError("policy engine returned a non-JSON body. FAIL-CLOSED.")


def _pec_not_expired(pec: dict) -> None:
    """If the credential carries validUntil, honor it. Absence is tolerated (forward-compat)."""
    valid_until = pec.get("validUntil")
    if not valid_until:
        return
    try:
        exp = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise PolicyError(f"credential validUntil is unparseable ({valid_until!r}). FAIL-CLOSED.")
    if datetime.now(timezone.utc) >= exp:
        raise PolicyError(f"credential expired at {valid_until}. FAIL-CLOSED.")


def _persist_deny_artifact(cfg: _Config, pec: dict) -> Optional[str]:
    try:
        Path(cfg.deny_dir).mkdir(parents=True, exist_ok=True)
        fname = f"deny-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
        fpath = Path(cfg.deny_dir) / fname
        with open(fpath, "w", encoding="utf-8") as fh:
            json.dump(pec, fh, indent=2)
        return str(fpath)
    except OSError as exc:
        # Failing to persist the artifact must not turn a deny into an allow.
        _emit(f"warning: could not persist deny artifact: {exc}")
        return None


def evaluate(
    *,
    rail: str,
    canonical_bytes_hex: str,
    human_readable: dict,
    delegation: Optional[dict] = None,
) -> dict:
    """Evaluate the exact unsigned transaction. Returns the verified allow credential,
    or raises (PolicyDenied / PolicyError). Never returns on anything but a clean allow.

    The caller (wallet.py) is responsible for the same-PSET identity guard: it must sign
    exactly the transaction whose canonical bytes were passed here.
    """
    cfg = _load_config()
    delegation = delegation if delegation is not None else _load_delegation(cfg.delegation_path)

    hr = dict(human_readable)
    hr["unit"] = _resolve_unit(delegation, rail, hr.get("asset_id"), cfg.unit_override)
    # The engine reads humanReadable.notional ONLY when it is a JSON number
    # (proposal-hints.ts: `typeof hr.notional === "number" ? ... : undefined`).
    # A string/Decimal is silently dropped -> "no notional hint" -> spurious DENY.
    # Coerce here so a well-formed send is actually evaluated against the cap.
    if hr.get("notional") is not None and not isinstance(hr["notional"], bool):
        try:
            n = float(hr["notional"])
            hr["notional"] = int(n) if n.is_integer() else n
        except (TypeError, ValueError):
            raise PolicyError(
                f"notional {hr.get('notional')!r} is not numeric; the engine cannot "
                f"evaluate the spending cap (fail-closed)"
            )

    body = {
        "proposal": {
            "rail": rail,
            "canonicalBytes": canonical_bytes_hex,
            "humanReadable": hr,
        },
        "delegationCredential": delegation,
        # Liquid attestation graph is not seeded; credentials carry
        # evaluatedWithAttestations=false by design.
        "attestations": [],
    }

    pec = _post(cfg, body)

    # PEC-out verification: trust a signed decision, not the transport. Absence of a
    # proof is a hard fail. Unknown *extra* fields (validUntil, proof.@context, and
    # future additive fields such as a Crossrail budget) are tolerated by design.
    if cfg.verify_pec:
        try:
            op_verify.verify_pec(pec, timeout=cfg.timeout)
        except op_verify.VerificationError as exc:
            raise PolicyError(f"could not verify policy credential signature: {exc}. FAIL-CLOSED.")
        _pec_not_expired(pec)
    else:
        _emit(
            "WARNING: OP_VERIFY_PEC is off — honoring the decision "
            "WITHOUT verifying its signature."
        )

    decision = pec.get("decision") or (pec.get("credentialSubject") or {}).get("decision")

    if decision == "allow":
        _emit(f"allow: rail={rail} notional={hr.get('notional')} {hr.get('unit')}")
        return pec

    if decision == "deny":
        artifact = _persist_deny_artifact(cfg, pec)
        reason = (
            pec.get("denyReason")
            or (pec.get("credentialSubject") or {}).get("denyReason")
            or {}
        )
        _emit(f"deny: {reason} artifact={artifact}")
        raise PolicyDenied(f"policy denied the transaction: {reason}", credential=pec)

    # Unknown / missing decision -> fail closed. (The wallet path speaks allow/deny;
    # permit/pending_approval belong to the OP-internal /op-evaluate route, not this one.)
    raise PolicyError(f"unexpected policy decision {decision!r}. FAIL-CLOSED.")
