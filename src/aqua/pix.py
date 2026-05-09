"""Pix → DePix integration via the Eulen public API.

Pix is Brazil's instant payment system; DePix is a BRL-pegged Liquid asset
issued by Eulen. This module mints Pix charges, persists them as PixSwap
records, and polls Eulen for delivery status. Unlike Ankara (Lightning →
L-BTC), there is no claim step — Eulen pushes DePix directly to the Liquid
address bound at deposit creation.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Optional

EULEN_API_URL = os.environ.get("EULEN_API_URL", "https://depix.eulen.app/api")
EULEN_API_TOKEN_ENV = "EULEN_API_TOKEN"

# Eulen first-transaction floor — surfaced in error messages so the agent can
# explain it to the user rather than passing the raw API error through.
PIX_MIN_AMOUNT_CENTS = 100  # R$1.00 — Eulen's documented absolute minimum

# Eulen status values returned by GET /deposit-status.
EULEN_STATUS_VALUES = frozenset(
    {
        "pending",
        "depix_sent",
        "under_review",
        "canceled",
        "error",
        "refunded",
        "expired",
    }
)
TERMINAL_STATUSES = frozenset({"depix_sent", "canceled", "error", "refunded", "expired"})


@dataclass
class PixSwap:
    """Persisted record of a Pix → DePix deposit."""

    swap_id: str  # Eulen deposit id
    amount_cents: int
    wallet_name: str
    depix_address: str
    qr_copy_paste: str
    status: str  # raw Eulen status (see EULEN_STATUS_VALUES)
    network: str
    created_at: str
    qr_image_url: Optional[str] = None
    expiration: Optional[str] = None
    blockchain_txid: Optional[str] = None
    payer_name: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PixSwap":
        data = {**data}
        for field_name in ("qr_image_url", "expiration", "blockchain_txid", "payer_name"):
            data.setdefault(field_name, None)
        return cls(**data)


class EulenClient:
    """HTTP client for the Eulen Pix-to-DePix API.

    The token is read once from the environment at construction time so unit
    tests can `monkeypatch.delenv` and assert the manager raises a clear error
    before any HTTP call is attempted.
    """

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self.base_url = (base_url or EULEN_API_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get(EULEN_API_TOKEN_ENV)
        if not self.token:
            raise RuntimeError(
                f"Eulen API token missing. Set {EULEN_API_TOKEN_ENV} in your environment "
                "(get one from https://depix.info/#partners)."
            )

    def _api_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        query: Optional[dict] = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "agentic-aqua",
                "Authorization": f"Bearer {self.token}",
                "X-Nonce": uuid.uuid4().hex,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err_body = json.loads(e.read().decode())
                detail = err_body.get("error") or err_body.get("message") or str(err_body)
            except Exception:
                pass
            msg = f"Eulen API error ({e.code} {method} {path})"
            if detail:
                msg += f": {detail}"
            raise RuntimeError(msg) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Eulen API unreachable ({method} {path}): {e.reason}") from e

    def create_deposit(self, amount_cents: int, depix_address: str) -> dict:
        """POST /deposit — create a Pix charge that pays out DePix to depix_address."""
        return self._api_request(
            "POST",
            "/deposit",
            {
                "amountInCents": amount_cents,
                "depixAddress": depix_address,
            },
        )

    def get_deposit_status(self, deposit_id: str) -> dict:
        """GET /deposit-status?id=<deposit_id> — poll Eulen for delivery state."""
        return self._api_request("GET", "/deposit-status", query={"id": deposit_id})


def _format_brl(amount_cents: int) -> str:
    """Render cents as 'R$1.234,56' (Brazilian convention)."""
    integer, fraction = divmod(amount_cents, 100)
    integer_str = f"{integer:,}".replace(",", ".")
    return f"R${integer_str},{fraction:02d}"


_STATUS_MESSAGES = {
    "pending": "Waiting for Pix payment. Pay the QR / Copia e Cola in your banking app.",
    "depix_sent": "DePix delivered to your Liquid wallet.",
    "under_review": "Eulen is reviewing the payment (compliance/AML). This may take time.",
    "canceled": "The deposit was canceled.",
    "error": "Eulen reported an error processing this deposit.",
    "refunded": "The Pix payment was refunded; no DePix was issued.",
    "expired": "The Pix charge expired before it was paid.",
}


class PixManager:
    """Pix → DePix manager. Mirrors the receive half of LightningManager."""

    def __init__(self, storage, wallet_manager):
        self.storage = storage
        self.wallet_manager = wallet_manager

    def create_deposit(
        self,
        amount_cents: int,
        wallet_name: str = "default",
        password: Optional[str] = None,
    ) -> PixSwap:
        """Create a Pix charge. DePix is delivered to the wallet's next address.

        Args:
            amount_cents: Amount in BRL cents (100 = R$1.00). Eulen's absolute
                minimum is R$1.00; the practical floor for first-time users is
                typically R$100, but limits scale up over time.
            wallet_name: Liquid wallet to receive DePix into. Default: "default".
            password: Accepted for symmetry with other receive flows but currently
                unused — receiving DePix needs only an address.

        Returns:
            Persisted PixSwap with status="pending".
        """
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool):
            raise ValueError("amount_cents must be an integer (cents of BRL, 100 = R$1.00)")
        if amount_cents < PIX_MIN_AMOUNT_CENTS:
            raise ValueError(
                f"amount_cents {amount_cents} is below the minimum "
                f"({PIX_MIN_AMOUNT_CENTS} cents = R$1.00)"
            )

        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        addr = self.wallet_manager.get_address(wallet_name)
        depix_address = addr.address

        client = EulenClient()
        try:
            resp = client.create_deposit(amount_cents, depix_address)
        except Exception as e:
            raise RuntimeError(f"Failed to create Pix deposit: {e}") from e

        deposit_id = resp.get("id") or resp.get("depositId")
        qr_copy_paste = resp.get("qrCopyPaste")
        if not deposit_id or not qr_copy_paste:
            raise RuntimeError(f"Eulen response missing required fields: {resp}")

        swap = PixSwap(
            swap_id=str(deposit_id),
            amount_cents=amount_cents,
            wallet_name=wallet_name,
            depix_address=depix_address,
            qr_copy_paste=qr_copy_paste,
            status="pending",
            network=wallet_data.network,
            created_at=datetime.now(UTC).isoformat(),
            qr_image_url=resp.get("qrImageUrl"),
            expiration=resp.get("expiration"),
        )
        self.storage.save_pix_swap(swap)
        return swap

    def get_deposit_status(self, swap_id: str) -> dict:
        """Poll Eulen for the deposit status and persist any changes."""
        swap = self.storage.load_pix_swap(swap_id)
        if not swap:
            raise ValueError(f"Pix swap not found: {swap_id}")

        warning = None
        try:
            client = EulenClient()
            resp = client.get_deposit_status(swap_id)
            new_status = resp.get("status")
            if new_status and new_status != swap.status:
                swap.status = new_status
            txid = resp.get("blockchainTxID") or resp.get("blockchainTxId")
            if txid:
                swap.blockchain_txid = txid
            payer = resp.get("payerName")
            if payer:
                swap.payer_name = payer
            expiration = resp.get("expiration")
            if expiration:
                swap.expiration = expiration
            self.storage.save_pix_swap(swap)
        except Exception as e:
            warning = f"Could not fetch remote status: {e}"

        result = {
            "swap_id": swap.swap_id,
            "status": swap.status,
            "amount_cents": swap.amount_cents,
            "amount_brl": _format_brl(swap.amount_cents),
            "wallet_name": swap.wallet_name,
            "depix_address": swap.depix_address,
            "network": swap.network,
            "message": _STATUS_MESSAGES.get(swap.status, f"Status: {swap.status}"),
        }
        if swap.blockchain_txid:
            result["blockchain_txid"] = swap.blockchain_txid
        if swap.payer_name:
            result["payer_name"] = swap.payer_name
        if swap.expiration:
            result["expiration"] = swap.expiration
        if warning:
            result["warning"] = warning
        return result
