"""Lightning abstraction layer for unified send/receive interface."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, UTC
from typing import Optional

from .ankara import AnkaraClient
from .boltz import BoltzClient, MIN_SWAP_AMOUNT_SATS as BOLTZ_MIN_SATS, MAX_SWAP_AMOUNT_SATS as BOLTZ_MAX_SATS, decode_bolt11_amount_sats, generate_keypair

# Ankara swap amount limits (satoshis)
ANKARA_MIN_SATS = 100
ANKARA_MAX_SATS = 25_000_000


@dataclass
class LightningSwap:
    """Unified Lightning swap record for both send and receive."""

    swap_id: str
    swap_type: str  # "send" | "receive"
    provider: str  # "boltz" | "ankara"
    invoice: str
    amount: int
    wallet_name: str
    status: str  # "pending" | "processing" | "completed" | "failed"
    network: str
    created_at: str
    receive_address: Optional[str] = None
    preimage: Optional[str] = None
    lockup_txid: Optional[str] = None
    claim_txid: Optional[str] = None
    refund_private_key: Optional[str] = None
    timeout_block_height: Optional[int] = None

    def to_dict(self) -> dict:
        """Convert to dict (includes internal fields for storage)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LightningSwap":
        """Reconstruct from dict with backward compatibility."""
        data = {**data}
        for field_name in [
            "receive_address", "preimage", "lockup_txid", "claim_txid",
            "refund_private_key", "timeout_block_height"
        ]:
            data.setdefault(field_name, None)
        return cls(**data)


class LightningManager:
    """Unified Lightning transaction manager (send via Boltz, receive via Ankara)."""

    def __init__(self, storage, wallet_manager):
        """
        Args:
            storage: Storage instance for persisting swaps
            wallet_manager: WalletManager for wallet operations
        """
        self.storage = storage
        self.wallet_manager = wallet_manager

    def create_receive_invoice(
        self,
        amount: int,
        wallet_name: str = "default",
        passphrase: Optional[str] = None,
    ) -> LightningSwap:
        """
        Generate a Lightning invoice to receive funds.

        Args:
            amount: Amount in satoshis (100 – 25,000,000)
            wallet_name: Liquid wallet to receive into
            passphrase: Passphrase to decrypt mnemonic (if encrypted)

        Returns:
            LightningSwap with pending status
        """
        if amount < ANKARA_MIN_SATS:
            raise ValueError(
                f"Amount {amount} sats is below minimum ({ANKARA_MIN_SATS} sats)"
            )
        if amount > ANKARA_MAX_SATS:
            raise ValueError(
                f"Amount {amount} sats exceeds maximum ({ANKARA_MAX_SATS} sats)"
            )

        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        if wallet_data.watch_only:
            raise ValueError("Watch-only wallet cannot receive funds directly")
        if wallet_data.encrypted_mnemonic and self.storage.is_mnemonic_encrypted(
            wallet_data.encrypted_mnemonic
        ):
            if not passphrase:
                raise ValueError("Passphrase required to decrypt mnemonic")

        addr = self.wallet_manager.get_address(wallet_name)
        address = addr.address

        client = AnkaraClient()
        try:
            swap_resp = client.create_swap(amount, address)
        except Exception as e:
            raise RuntimeError(f"Failed to create Ankara swap: {e}") from e

        swap = LightningSwap(
            swap_id=swap_resp["swap_id"],
            swap_type="receive",
            provider="ankara",
            invoice=swap_resp["invoice"],
            amount=amount,
            wallet_name=wallet_name,
            status="pending",
            network=wallet_data.network,
            created_at=datetime.now(UTC).isoformat(),
            receive_address=address,
        )
        self.storage.save_lightning_swap(swap)

        return swap

    def pay_invoice(
        self,
        invoice: str,
        wallet_name: str = "default",
        passphrase: Optional[str] = None,
    ) -> LightningSwap:
        """
        Pay a Lightning invoice using L-BTC via Boltz submarine swap.

        Args:
            invoice: BOLT11 Lightning invoice (lnbc... or lntb...)
            wallet_name: Liquid wallet to pay from
            passphrase: Passphrase to decrypt mnemonic (if encrypted)

        Returns:
            LightningSwap with pending status and lockup_txid
        """
        valid_prefixes = ("lnbc", "lntb")
        if not invoice or not any(invoice.startswith(p) for p in valid_prefixes):
            raise ValueError(
                "Invalid invoice: must be a BOLT11 Lightning invoice starting with 'lnbc' (mainnet) or 'lntb' (testnet)"
            )

        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        if wallet_data.watch_only:
            raise ValueError("Watch-only wallet cannot sign transactions")
        if wallet_data.encrypted_mnemonic and self.storage.is_mnemonic_encrypted(
            wallet_data.encrypted_mnemonic
        ):
            if not passphrase:
                raise ValueError("Passphrase required to decrypt mnemonic")

        network = wallet_data.network

        invoice_amount = decode_bolt11_amount_sats(invoice)
        if invoice_amount is not None:
            if invoice_amount < BOLTZ_MIN_SATS:
                raise ValueError(
                    f"Invoice amount {invoice_amount} sats is below minimum ({BOLTZ_MIN_SATS} sats)"
                )
            if invoice_amount > BOLTZ_MAX_SATS:
                raise ValueError(
                    f"Invoice amount {invoice_amount} sats exceeds maximum ({BOLTZ_MAX_SATS} sats)"
                )
            # Validate balance before creating Boltz swap
            balances = self.wallet_manager.get_balance(wallet_name)
            lbtc_balance = next((b.amount for b in balances if b.ticker == "L-BTC"), 0)
            if lbtc_balance < invoice_amount:
                raise ValueError(
                    f"Insufficient L-BTC balance: have {lbtc_balance} sats, need at least {invoice_amount} sats"
                )

        client = BoltzClient(network=network)
        pairs = client.get_submarine_pairs()
        pair = pairs.get("L-BTC", {}).get("BTC")
        if not pair:
            raise ValueError("L-BTC/BTC pair not available on Boltz")

        refund_privkey, refund_pubkey = generate_keypair()
        swap_resp = client.create_submarine_swap(invoice, refund_pubkey)
        expected_amount = swap_resp["expectedAmount"]

        swap = LightningSwap(
            swap_id=swap_resp["id"],
            swap_type="send",
            provider="boltz",
            invoice=invoice,
            amount=expected_amount,
            wallet_name=wallet_name,
            status="pending",
            network=network,
            created_at=datetime.now(UTC).isoformat(),
            refund_private_key=refund_privkey,
            timeout_block_height=swap_resp["timeoutBlockHeight"],
        )
        self.storage.save_lightning_swap(swap)

        lockup_txid = self.wallet_manager.send(
            wallet_name, swap_resp["address"], expected_amount, passphrase=passphrase
        )

        swap.lockup_txid = lockup_txid
        swap.status = "processing"
        self.storage.save_lightning_swap(swap)

        return swap

    def get_receive_status(self, swap_id: str) -> dict:
        """
        Check the status of a Lightning receive swap and auto-claim when settled.

        Args:
            swap_id: Swap ID from create_receive_invoice

        Returns:
            Dict with swap_id, status, amount, wallet_name, invoice, and optional preimage
        """
        swap = self.storage.load_lightning_swap(swap_id)
        if not swap:
            raise ValueError(f"Lightning swap not found: {swap_id}")
        if swap.swap_type != "receive":
            raise ValueError(f"Swap {swap_id} is a send swap, not a receive swap")

        client = AnkaraClient()
        warning = None
        try:
            verify_resp = client.verify_swap(swap_id)
            settled = verify_resp.get("settled", False)
            preimage = verify_resp.get("preimage")

            # Auto-claim if settled and not already completed
            claim_warning = None
            if settled and swap.status != "completed":
                try:
                    client.claim_swap(swap_id)
                    swap.status = "completed"
                    if preimage:
                        swap.preimage = preimage
                    self.storage.save_lightning_swap(swap)
                except Exception as e:
                    claim_warning = f"Swap settled but claim failed: {e}"
        except Exception as e:
            warning = f"Could not fetch remote status: {e}"
            verify_resp = {}

        result = {
            "swap_id": swap.swap_id,
            "status": swap.status,
            "amount": swap.amount,
            "wallet_name": swap.wallet_name,
            "invoice": swap.invoice,
        }

        if swap.preimage:
            result["preimage"] = swap.preimage
        if warning:
            result["warning"] = warning
        if "claim_warning" in locals() and claim_warning:
            result["claim_warning"] = claim_warning

        return result
