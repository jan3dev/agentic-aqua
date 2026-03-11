"""Boltz Exchange integration for submarine swaps (L-BTC -> Lightning)."""

import hashlib
import json
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Optional

import coincurve

BOLTZ_API = {
    "mainnet": "https://api.boltz.exchange",
    "testnet": "https://api.testnet.boltz.exchange",
}

# Client-side swap amount limits (satoshis)
MIN_SWAP_AMOUNT_SATS = 100
MAX_SWAP_AMOUNT_SATS = 25_000_000

# BOLT11 amount multiplier → satoshis factor
_BOLT11_MULTIPLIERS: dict[str, float] = {
    "m": 100_000,        # milli-BTC
    "u": 100,            # micro-BTC
    "n": 0.1,            # nano-BTC
    "p": 0.0001,         # pico-BTC
    "":  100_000_000,    # BTC (no suffix)
}


@dataclass
class SwapInfo:
    """Holds all data for an active/completed submarine swap."""

    swap_id: str
    address: str
    expected_amount: int
    claim_public_key: str
    swap_tree: dict
    timeout_block_height: int
    refund_private_key: str
    refund_public_key: str
    invoice: str
    status: str
    network: str
    created_at: str
    lockup_txid: Optional[str] = None
    preimage: Optional[str] = None
    claim_txid: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class BoltzClient:
    """HTTP client for Boltz API v2."""

    def __init__(self, network: str = "mainnet"):
        self.base_url = BOLTZ_API[network]
        self.network = network

    def _api_request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Make HTTP request to Boltz API."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "aqua-mcp",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # Try to extract Boltz error message from response body
            detail = ""
            try:
                err_body = json.loads(e.read().decode())
                detail = err_body.get("error", err_body.get("message", ""))
            except Exception:
                pass
            msg = f"Boltz API error ({e.code} {method} {path})"
            if detail:
                msg += f": {detail}"
            raise RuntimeError(msg) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Boltz API unreachable ({method} {path}): {e.reason}"
            ) from e

    def get_submarine_pairs(self) -> dict:
        """GET /v2/swap/submarine - fetch available pairs, fees, limits."""
        return self._api_request("GET", "/v2/swap/submarine")

    def create_submarine_swap(self, invoice: str, refund_public_key: str) -> dict:
        """POST /v2/swap/submarine - create a new swap."""
        return self._api_request("POST", "/v2/swap/submarine", {
            "invoice": invoice,
            "from": "L-BTC",
            "to": "BTC",
            "refundPublicKey": refund_public_key,
        })

    def get_swap_status(self, swap_id: str) -> dict:
        """GET /v2/swap/{swap_id} - get current swap status."""
        return self._api_request("GET", f"/v2/swap/{swap_id}")

    def get_claim_details(self, swap_id: str) -> dict:
        """GET /v2/swap/submarine/{swap_id}/claim - get preimage after invoice paid."""
        return self._api_request("GET", f"/v2/swap/submarine/{swap_id}/claim")


def generate_keypair() -> tuple[str, str]:
    """Generate ephemeral secp256k1 keypair for refund.

    Returns (private_key_hex, public_key_hex).
    """
    privkey = secrets.token_bytes(32)
    pubkey = coincurve.PublicKey.from_secret(privkey)
    return privkey.hex(), pubkey.format(compressed=True).hex()


def verify_preimage(preimage_hex: str, expected_hash_hex: str) -> bool:
    """Verify SHA256(preimage) == expected_hash. Pure stdlib."""
    preimage = bytes.fromhex(preimage_hex)
    computed = hashlib.sha256(preimage).hexdigest()
    return computed == expected_hash_hex.lower()


def decode_bolt11_amount_sats(invoice: str) -> int | None:
    """Extract the amount in satoshis from a BOLT11 invoice.

    Returns None for zero-amount invoices or if the amount cannot be parsed.
    """
    invoice = invoice.lower().strip()
    # Strip Lightning prefix to get the amount portion of the HRP
    for prefix in ("lnbcrt", "lnbc", "lntb", "lntbs"):
        if invoice.startswith(prefix):
            hrp_rest = invoice[len(prefix):]
            break
    else:
        return None

    if not hrp_rest:
        return None

    # Match amount (digits) + optional multiplier + '1' separator.
    # Zero-amount invoices (e.g. "lnbc1p...") won't match because
    # the regex requires digits followed by separator '1'.
    match = re.match(r"^(\d+)([munp]?)1", hrp_rest)
    if not match:
        return None

    amount = int(match.group(1))
    multiplier = match.group(2)
    sats = amount * _BOLT11_MULTIPLIERS[multiplier]
    return int(sats)
