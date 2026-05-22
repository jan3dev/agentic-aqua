"""Boltz Exchange integration for submarine swaps (L-BTC -> Lightning)."""

import hashlib
import json
import logging
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

logger = logging.getLogger(__name__)

# Client-side swap amount limits (satoshis)
MIN_SWAP_AMOUNT_SATS = 100
MAX_SWAP_AMOUNT_SATS = 25_000_000




class BoltzSwapAlreadyExistsError(RuntimeError):
    """Raised when Boltz reports an invoice already has a swap."""


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
                "User-Agent": "agentic-aqua",
            },
        )
        logger.info("Boltz request %s %s body=%s", method, path, body)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                logger.info(
                    "Boltz response %s %s status=%s body=%s",
                    method,
                    path,
                    getattr(resp, "status", "unknown"),
                    raw,
                )
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            # Try to extract Boltz error message from response body
            detail = ""
            raw_error = ""
            try:
                raw_error = e.read().decode()
                err_body = json.loads(raw_error)
                detail = err_body.get("error", err_body.get("message", ""))
            except Exception:
                pass
            logger.error(
                "Boltz HTTP error %s %s status=%s reason=%s body=%s",
                method,
                path,
                e.code,
                getattr(e, "reason", ""),
                raw_error,
            )
            msg = f"Boltz API error ({e.code} {method} {path})"
            if detail:
                msg += f": {detail}"
            normalized = detail.lower().strip() if detail else ""
            if e.code == 409 and "swap with this invoice exists already" in normalized:
                raise BoltzSwapAlreadyExistsError(
                    "A swap for this Lightning invoice already exists on Boltz. "
                    "This usually means the same invoice was already submitted before, "
                    "even if the local wallet did not finish the payment flow."
                ) from e
            raise RuntimeError(msg) from e
        except urllib.error.URLError as e:
            logger.error("Boltz URL error %s %s reason=%s", method, path, e.reason)
            raise RuntimeError(f"Boltz API unreachable ({method} {path}): {e.reason}") from e

    def get_submarine_pairs(self) -> dict:
        """GET /v2/swap/submarine - fetch available pairs, fees, limits."""
        return self._api_request("GET", "/v2/swap/submarine")

    def create_submarine_swap(self, invoice: str, refund_public_key: str) -> dict:
        """POST /v2/swap/submarine - create a new swap."""
        return self._api_request(
            "POST",
            "/v2/swap/submarine",
            {
                "invoice": invoice,
                "from": "L-BTC",
                "to": "BTC",
                "refundPublicKey": refund_public_key,
            },
        )

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


