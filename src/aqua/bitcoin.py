"""Bitcoin wallet management using BDK."""

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TypeVar

import bdkpython as bdk

from .storage import Storage, WalletData

_XPUB_RE = re.compile(
    r"\[(?P<fp>[0-9a-fA-F]{8})/(?P<path>[^\]]+)\](?P<xpub>[xt]pub[A-Za-z0-9]+)"
    r"|(?P<bare>[xt]pub[A-Za-z0-9]+)"
)


def _extract_xpub_metadata(descriptor: str) -> dict:
    """Return xpub, fingerprint, derivation_path from a descriptor; missing keys are None."""
    m = _XPUB_RE.search(descriptor)
    if not m:
        return {"xpub": None, "fingerprint": None, "derivation_path": None}
    if m.group("xpub"):
        return {
            "xpub": m.group("xpub"),
            "fingerprint": m.group("fp"),
            "derivation_path": m.group("path"),
        }
    return {"xpub": m.group("bare"), "fingerprint": None, "derivation_path": None}


def _derive_change_from_external(external: str) -> str:
    """Replace last '/0/*' with '/1/*', dropping the now-stale checksum. Raises if '/0/*' absent."""
    if "/0/*" not in external:
        raise ValueError("External descriptor missing '/0/*'; cannot auto-derive change")
    head, _sep, tail = external.rpartition("/0/*")
    derived = head + "/1/*" + tail
    return re.sub(r"#[a-zA-Z0-9]+$", "", derived)


ESPLORA_URLS = {
    "mainnet": [
        "https://blockstream.info/api",
        "https://mempool.space/api",
    ],
    "testnet": [
        "https://blockstream.info/testnet/api",
        "https://mempool.space/testnet/api",
    ],
}

STOP_GAP = 20
PARALLEL_REQUESTS = 3
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 1.0

_T = TypeVar("_T")


def _retry_on_network_error(fn: Callable[[], _T]) -> _T:
    """Retry a network call on transient Esplora failures (connection reset, timeouts).

    Blockstream's public Esplora occasionally drops connections mid-scan when
    BDK fires many parallel requests. We retry with a short delay before giving up.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except Exception as exc:
            msg = str(exc).lower()
            transient = (
                "connection reset" in msg
                or "timed out" in msg
                or "timeout" in msg
                or "minreq" in msg
            )
            if not transient:
                raise
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS)
    assert last_exc is not None
    raise last_exc


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
        self._clients: dict[str, list[bdk.EsploraClient]] = {}

    def _get_esplora_urls(self, network: str) -> list[str]:
        return ESPLORA_URLS.get(network) or ESPLORA_URLS["mainnet"]

    def _get_clients(self, network: str) -> list[bdk.EsploraClient]:
        if network not in self._clients:
            urls = self._get_esplora_urls(network)
            self._clients[network] = [bdk.EsploraClient(u) for u in urls]
        return self._clients[network]

    def _get_client(self, network: str) -> bdk.EsploraClient:
        return self._get_clients(network)[0]

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
    ) -> WalletData:
        """Create a Bitcoin wallet from mnemonic (BIP84). Persists descriptors to storage.

        The BIP39 passphrase is intentionally NOT used here. LWK (Liquid) does not
        support a BIP39 passphrase, so applying one on the Bitcoin side would make
        the two networks derive from different seeds and produce descriptors that
        do not match when the same mnemonic is imported into another wallet.
        """
        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found (create Liquid wallet first)")

        net = _network_bdk(network)
        bdk_mnemonic = bdk.Mnemonic.from_string(mnemonic)
        secret_key = bdk.DescriptorSecretKey(net, bdk_mnemonic, None)
        external_desc = bdk.Descriptor.new_bip84(secret_key, bdk.KeychainKind.EXTERNAL, net)
        change_desc = bdk.Descriptor.new_bip84(secret_key, bdk.KeychainKind.INTERNAL, net)

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

    def import_descriptor(
        self,
        descriptor: str,
        wallet_name: str,
        network: str = "mainnet",
        change_descriptor: Optional[str] = None,
    ) -> WalletData:
        """Import a watch-only BIP84 Bitcoin wallet; adds BTC to an existing Liquid-only wallet if needed."""
        net = _network_bdk(network)

        ext_desc = bdk.Descriptor(descriptor, net)

        if change_descriptor is not None:
            change_desc = bdk.Descriptor(change_descriptor, net)
            change_desc_str = change_descriptor
        else:
            try:
                change_desc_str = _derive_change_from_external(descriptor)
            except ValueError:
                raise ValueError(
                    "Could not auto-derive change descriptor; provide change_descriptor explicitly"
                )
            change_desc = bdk.Descriptor(change_desc_str, net)

        existing = self.storage.load_wallet(wallet_name)
        if existing is not None and existing.btc_descriptor:
            raise ValueError(f"Wallet '{wallet_name}' already has a Bitcoin descriptor")
        # Reachable via library layer when LiquidWalletManager.import_mnemonic is called without btc_manager.create_wallet.
        if existing is not None and existing.encrypted_mnemonic:
            raise ValueError(
                f"Wallet '{wallet_name}' has a mnemonic; the Bitcoin descriptor is derived "
                "from it automatically. Use a different wallet_name for a watch-only import."
            )

        if existing is not None:
            wallet_data = existing
            wallet_data.btc_descriptor = str(ext_desc)
            wallet_data.btc_change_descriptor = str(change_desc)
            wallet_data.watch_only = True
        else:
            wallet_data = WalletData(
                name=wallet_name,
                network=network,
                descriptor="",
                btc_descriptor=str(ext_desc),
                btc_change_descriptor=str(change_desc),
                encrypted_mnemonic=None,
                watch_only=True,
            )

        cache_path = self._get_btc_cache_path(wallet_name)
        persister = bdk.Persister.new_sqlite(str(cache_path))
        wallet = bdk.Wallet(ext_desc, change_desc, net, persister)
        wallet.persist(persister)

        self._wallets[wallet_name] = wallet
        self._persisters[wallet_name] = persister
        self._networks[wallet_name] = network

        self.storage.save_wallet(wallet_data)
        return wallet_data

    def export_descriptor(self, wallet_name: str) -> dict:
        """Return external + change descriptors and parsed xpub metadata for a wallet."""
        wallet_data = self.storage.load_wallet(wallet_name)
        if wallet_data is None:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        if not wallet_data.btc_descriptor or not wallet_data.btc_change_descriptor:
            raise ValueError(f"Wallet '{wallet_name}' has no Bitcoin descriptors")

        meta = _extract_xpub_metadata(wallet_data.btc_descriptor)
        return {
            "wallet_name": wallet_data.name,
            "network": wallet_data.network,
            "external_descriptor": wallet_data.btc_descriptor,
            "change_descriptor": wallet_data.btc_change_descriptor,
            "xpub": meta["xpub"],
            "fingerprint": meta["fingerprint"],
            "derivation_path": meta["derivation_path"],
        }

    def _get_wallet(
        self,
        wallet_name: str,
    ) -> tuple[bdk.Wallet, str]:
        """Get or create BDK wallet (watch-only loader). Returns (Wallet, network)."""
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
    ) -> tuple[bdk.Wallet, str]:
        """Get or create BDK wallet with signing capability (from mnemonic)."""
        wallet_data = self.storage.load_wallet(wallet_name)
        if not wallet_data:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        net = _network_bdk(wallet_data.network)
        bdk_mnemonic = bdk.Mnemonic.from_string(mnemonic)
        secret_key = bdk.DescriptorSecretKey(net, bdk_mnemonic, None)
        external_desc = bdk.Descriptor.new_bip84(secret_key, bdk.KeychainKind.EXTERNAL, net)
        change_desc = bdk.Descriptor.new_bip84(secret_key, bdk.KeychainKind.INTERNAL, net)
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
        clients = self._get_clients(network)

        last_exc: Optional[Exception] = None
        for client in clients:

            def _scan(c=client):
                request = wallet.start_full_scan().build()
                return c.full_scan(request, STOP_GAP, PARALLEL_REQUESTS)

            try:
                update = _retry_on_network_error(_scan)
                wallet.apply_update(update)
                persister = self._persisters[wallet_name]
                wallet.persist(persister)
                return
            except Exception as exc:
                last_exc = exc
                continue
        assert last_exc is not None
        raise last_exc

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
        password: Optional[str] = None,
    ) -> str:
        """Build, sign and broadcast a Bitcoin transaction. Returns txid.

        ``password`` is used only to decrypt the at-rest mnemonic when the wallet
        was imported with an encryption password. It is NOT a BIP39 passphrase.
        """
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
        needs_password = self.storage.is_mnemonic_encrypted(wallet_data.encrypted_mnemonic)
        if needs_password and not password:
            raise ValueError("Password required to decrypt mnemonic")
        mnemonic = self.storage.retrieve_mnemonic(wallet_data.encrypted_mnemonic, password)
        wallet, network = self._get_wallet_with_signer(wallet_name, mnemonic)
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
        _retry_on_network_error(lambda: client.broadcast(tx))
        return tx.compute_txid().serialize()[::-1].hex()
