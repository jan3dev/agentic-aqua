"""Bitcoin wallet management using BDK."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import bdkpython as bdk

from .storage import Storage, WalletData


ESPLORA_URLS = {
    "mainnet": "https://blockstream.info/api",
    "testnet": "https://blockstream.info/testnet/api",
}

STOP_GAP = 20
PARALLEL_REQUESTS = 3


@dataclass
class BitcoinAddress:
    """Bitcoin receive address."""

    address: str
    index: int

    def to_dict(self) -> dict:
        return {"address": self.address, "index": self.index}


@dataclass
class BitcoinTransaction:
    """Bitcoin transaction info."""

    txid: str
    height: Optional[int]
    received: int
    sent: int
    fee: Optional[int]

    def to_dict(self) -> dict:
        return {
            "txid": self.txid,
            "height": self.height,
            "received": self.received,
            "sent": self.sent,
            "fee": self.fee,
        }


def _network_bdk(network: str) -> bdk.Network:
    if network == "mainnet":
        return bdk.Network.BITCOIN
    if network == "testnet":
        return bdk.Network.TESTNET
    raise ValueError(f"Unknown network: {network}")


def _extract_confirmation_height(tx: object) -> Optional[int]:
    """Best-effort extraction of tx confirmation height across bdkpython shapes."""
    # Older bindings may expose direct heights on the tx record.
    height = getattr(tx, "height", None) or getattr(tx, "confirmation_height", None)
    if height is not None:
        return height

    # Newer bindings expose chain_position enums, where confirmed data
    # is nested under confirmation_block_time.block_id.height.
    cp = getattr(tx, "chain_position", None)
    if cp is None:
        return None
    cbt = getattr(cp, "confirmation_block_time", None)
    if cbt is None:
        return None
    block_id = getattr(cbt, "block_id", None)
    return getattr(block_id, "height", None)


class BitcoinWalletManager:
    """Manages Bitcoin wallets using BDK."""

    def __init__(self, storage: Optional[Storage] = None):
        self.storage = storage or Storage()
        self._wallets: dict[str, bdk.Wallet] = {}
        self._persisters: dict[str, bdk.Persister] = {}
        self._networks: dict[str, str] = {}
        self._clients: dict[str, bdk.EsploraClient] = {}

    def _get_esplora_url(self, network: str) -> str:
        return ESPLORA_URLS.get(network) or ESPLORA_URLS["mainnet"]

    def _get_client(self, network: str) -> bdk.EsploraClient:
        if network not in self._clients:
            url = self._get_esplora_url(network)
            self._clients[network] = bdk.EsploraClient(url)
        return self._clients[network]

    def _get_btc_cache_path(self, wallet_name: str) -> Path:
        base = self.storage.get_cache_path(wallet_name)
        btc_dir = base / "btc"
        btc_dir.mkdir(exist_ok=True, mode=0o700)
        return btc_dir / "bdk.sqlite"

    def create_wallet(
        self,
        mnemonic: str,
        wallet_name: str,
        network: str = "mainnet",
        passphrase: Optional[str] = None,
    ) -> WalletData:
        """Create a Bitcoin wallet from mnemonic (BIP84). Persists descriptors to storage."""
        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found (create Liquid wallet first)")

        net = _network_bdk(network)
        bdk_mnemonic = bdk.Mnemonic.from_string(mnemonic)
        secret_key = bdk.DescriptorSecretKey(net, bdk_mnemonic, passphrase)
        external_desc = bdk.Descriptor.new_bip84(
            secret_key, bdk.KeychainKind.EXTERNAL, net
        )
        change_desc = bdk.Descriptor.new_bip84(
            secret_key, bdk.KeychainKind.INTERNAL, net
        )

        cache_path = self._get_btc_cache_path(wallet_name)
        persister = bdk.Persister.new_sqlite(str(cache_path))
        wallet = bdk.Wallet(external_desc, change_desc, net, persister)
        wallet.persist(persister)

        self._wallets[wallet_name] = wallet
        self._persisters[wallet_name] = persister
        self._networks[wallet_name] = network

        wallet_data.btc_descriptor = str(external_desc)
        wallet_data.btc_change_descriptor = str(change_desc)
        self.storage.save_wallet(wallet_data)

        return wallet_data

    def _get_wallet(
        self,
        wallet_name: str,
        passphrase: Optional[str] = None,
    ) -> tuple[bdk.Wallet, str]:
        """Get or create BDK wallet. Returns (Wallet, network)."""
        if wallet_name in self._wallets and wallet_name in self._networks:
            return self._wallets[wallet_name], self._networks[wallet_name]

        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        if not wallet_data.btc_descriptor or not wallet_data.btc_change_descriptor:
            raise ValueError(
                f"Wallet '{wallet_name}' has no Bitcoin descriptors (import with mnemonic first)"
            )

        net = _network_bdk(wallet_data.network)
        external_desc = bdk.Descriptor(
            wallet_data.btc_descriptor,
            net,
        )
        change_desc = bdk.Descriptor(
            wallet_data.btc_change_descriptor,
            net,
        )
        cache_path = self._get_btc_cache_path(wallet_name)
        persister = bdk.Persister.new_sqlite(str(cache_path))
        wallet = bdk.Wallet.load(
            external_desc,
            change_desc,
            persister,
        )
        self._wallets[wallet_name] = wallet
        self._persisters[wallet_name] = persister
        self._networks[wallet_name] = wallet_data.network
        return wallet, wallet_data.network

    def _get_wallet_with_signer(
        self,
        wallet_name: str,
        mnemonic: str,
        passphrase: Optional[str] = None,
    ) -> tuple[bdk.Wallet, str]:
        """Get or create BDK wallet with signing capability (from mnemonic)."""
        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        net = _network_bdk(wallet_data.network)
        bdk_mnemonic = bdk.Mnemonic.from_string(mnemonic)
        secret_key = bdk.DescriptorSecretKey(net, bdk_mnemonic, passphrase)
        external_desc = bdk.Descriptor.new_bip84(
            secret_key, bdk.KeychainKind.EXTERNAL, net
        )
        change_desc = bdk.Descriptor.new_bip84(
            secret_key, bdk.KeychainKind.INTERNAL, net
        )
        cache_path = self._get_btc_cache_path(wallet_name)
        persister = bdk.Persister.new_sqlite(str(cache_path))
        wallet = bdk.Wallet.load(
            external_desc,
            change_desc,
            persister,
        )
        self._wallets[wallet_name] = wallet
        self._persisters[wallet_name] = persister
        self._networks[wallet_name] = wallet_data.network
        return wallet, wallet_data.network

    def sync_wallet(self, wallet_name: str) -> None:
        """Sync wallet with blockchain via Esplora."""
        wallet, network = self._get_wallet(wallet_name)
        client = self._get_client(network)
        request = wallet.start_full_scan().build()
        update = client.full_scan(request, STOP_GAP, PARALLEL_REQUESTS)
        wallet.apply_update(update)
        persister = self._persisters[wallet_name]
        wallet.persist(persister)

    def get_balance(self, wallet_name: str) -> int:
        """Get Bitcoin balance in satoshis."""
        self.sync_wallet(wallet_name)
        wallet, _ = self._get_wallet(wallet_name)
        return wallet.balance().total.to_sat()

    def get_address(
        self,
        wallet_name: str,
        index: Optional[int] = None,
    ) -> BitcoinAddress:
        """Get receive address (bc1...)."""
        wallet, _ = self._get_wallet(wallet_name)
        if index is not None:
            addr_info = wallet.peek_address(bdk.KeychainKind.EXTERNAL, index)
        else:
            addr_info = wallet.reveal_next_address(bdk.KeychainKind.EXTERNAL)
            persister = self._persisters.get(wallet_name)
            if persister:
                wallet.persist(persister)
        return BitcoinAddress(
            address=str(addr_info.address),
            index=addr_info.index,
        )

    def get_transactions(
        self,
        wallet_name: str,
        limit: Optional[int] = None,
    ) -> list[BitcoinTransaction]:
        """Get transaction history."""
        self.sync_wallet(wallet_name)
        wallet, _ = self._get_wallet(wallet_name)
        txs = wallet.transactions()
        if limit is not None:
            txs = txs[:limit]
        result = []
        for tx in txs:
            txid = tx.transaction.compute_txid().serialize()[::-1].hex()
            height = _extract_confirmation_height(tx)
            # Use wallet methods to get sent/received/fee (bdkpython 2.2+ API)
            sr = wallet.sent_and_received(tx.transaction)
            received = sr.received.to_sat()
            sent = sr.sent.to_sat()
            try:
                fee = wallet.calculate_fee(tx.transaction).to_sat()
            except Exception:
                fee = None
            result.append(
                BitcoinTransaction(
                    txid=txid,
                    height=height,
                    received=received,
                    sent=sent,
                    fee=fee,
                )
            )
        return result

    def send(
        self,
        wallet_name: str,
        address: str,
        amount: int,
        fee_rate: Optional[int] = None,
        passphrase: Optional[str] = None,
    ) -> str:
        """Build, sign and broadcast a Bitcoin transaction. Returns txid."""
        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        if wallet_data.watch_only:
            raise ValueError("Cannot sign with watch-only wallet")
        if amount <= 0:
            raise ValueError("Amount must be positive")
        if fee_rate is not None and fee_rate <= 0:
            raise ValueError("Fee rate must be positive")
        if not wallet_data.encrypted_mnemonic:
            raise ValueError(
                "No mnemonic available for signing (import wallet with mnemonic to enable sending)"
            )
        needs_passphrase = self.storage.is_mnemonic_encrypted(wallet_data.encrypted_mnemonic)
        if needs_passphrase and not passphrase:
            raise ValueError("Passphrase required to decrypt mnemonic")
        mnemonic = self.storage.retrieve_mnemonic(
            wallet_data.encrypted_mnemonic, passphrase
        )
        wallet, network = self._get_wallet_with_signer(
            wallet_name, mnemonic, passphrase
        )
        self.sync_wallet(wallet_name)

        net = _network_bdk(network)
        bdk_address = bdk.Address(address, net)
        spk = bdk_address.script_pubkey()
        amt = bdk.Amount.from_sat(amount)

        builder = bdk.TxBuilder()
        builder = builder.add_recipient(spk, amt)
        if fee_rate is not None:
            builder = builder.fee_rate(bdk.FeeRate.from_sat_per_vb(fee_rate))

        psbt = builder.finish(wallet)
        sign_opts = bdk.SignOptions(
            trust_witness_utxo=True,
            assume_height=None,
            allow_all_sighashes=False,
            try_finalize=True,
            sign_with_tap_internal_key=True,
            allow_grinding=True,
        )
        finalized = wallet.sign(psbt, sign_opts)
        if not finalized:
            raise RuntimeError("Failed to finalize PSBT")
        tx = psbt.extract_tx()
        client = self._get_client(network)
        client.broadcast(tx)
        return tx.compute_txid().serialize()[::-1].hex()
