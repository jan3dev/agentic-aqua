"""Wallet management using LWK."""

from dataclasses import dataclass
from typing import Optional
import lwk

from .assets import lookup_asset, resolve_asset_name
from .storage import Storage, WalletData


@dataclass
class Balance:
    """Wallet balance."""
    asset_id: str
    asset_name: str
    ticker: str
    amount: int  # In satoshis (smallest unit)
    precision: int = 8  # Decimal places
    logo: Optional[str] = None

    @property
    def value(self) -> float:
        """Human-readable amount (e.g. 100_000_000 sats with precision=8 -> 1.0)."""
        return self.amount / (10 ** self.precision)

    def to_dict(self) -> dict:
        d = {
            "asset_id": self.asset_id,
            "asset_name": self.asset_name,
            "ticker": self.ticker,
            "amount_sats": self.amount,
            "precision": self.precision,
            "value": self.value,
        }
        if self.logo:
            d["logo"] = self.logo
        return d


@dataclass
class Address:
    """Wallet address."""
    address: str
    index: int
    
    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "index": self.index,
        }


@dataclass
class Transaction:
    """Transaction info."""
    txid: str
    height: Optional[int]
    timestamp: Optional[int]
    balance: dict[str, int]  # asset_id -> amount change
    fee: int
    
    def to_dict(self) -> dict:
        return {
            "txid": self.txid,
            "height": self.height,
            "timestamp": self.timestamp,
            "balance": self.balance,
            "fee": self.fee,
        }


class WalletManager:
    """Manages Liquid wallets using LWK."""

    def __init__(self, storage: Optional[Storage] = None):
        self.storage = storage or Storage()
        self._signers: dict[str, lwk.Signer] = {}
        self._wollets: dict[str, lwk.Wollet] = {}
        self._clients: dict[str, lwk.ElectrumClient] = {}

    def _get_network(self, network: str) -> lwk.Network:
        """Get LWK network object."""
        if network == "mainnet":
            return lwk.Network.mainnet()
        elif network == "testnet":
            return lwk.Network.testnet()
        else:
            raise ValueError(f"Unknown network: {network}")

    def _get_client(self, network: str) -> lwk.ElectrumClient:
        """Get or create Electrum client for network."""
        if network not in self._clients:
            net = self._get_network(network)
            self._clients[network] = net.default_electrum_client()
        return self._clients[network]

    def _get_policy_asset(self, network: str) -> str:
        """Get L-BTC asset ID for network."""
        return str(self._get_network(network).policy_asset())

    # Mnemonic operations

    def generate_mnemonic(self) -> str:
        """Generate a new BIP39 mnemonic (12 words)."""
        network = lwk.Network.mainnet()  # Network doesn't matter for mnemonic gen
        signer = lwk.Signer.random(network)
        return str(signer.mnemonic())

    def import_mnemonic(
        self,
        mnemonic: str,
        wallet_name: str = "default",
        network: str = "mainnet",
        passphrase: Optional[str] = None,
    ) -> WalletData:
        """Import wallet from mnemonic."""
        if self.storage.wallet_exists(wallet_name):
            raise ValueError(f"Wallet '{wallet_name}' already exists")

        # Create signer and get descriptor
        net = self._get_network(network)
        lwk_mnemonic = lwk.Mnemonic(mnemonic)
        signer = lwk.Signer(lwk_mnemonic, net)
        descriptor = str(signer.wpkh_slip77_descriptor())

        # Encrypt mnemonic if passphrase provided
        encrypted = None
        if passphrase:
            encrypted = self.storage.encrypt_mnemonic(mnemonic, passphrase)

        # Create and save wallet
        wallet = WalletData(
            name=wallet_name,
            network=network,
            descriptor=descriptor,
            encrypted_mnemonic=encrypted,
            watch_only=False,
        )
        self.storage.save_wallet(wallet)

        # Cache signer
        self._signers[wallet_name] = signer

        return wallet

    def import_descriptor(
        self,
        descriptor: str,
        wallet_name: str,
        network: str = "mainnet",
    ) -> WalletData:
        """Import watch-only wallet from CT descriptor."""
        if self.storage.wallet_exists(wallet_name):
            raise ValueError(f"Wallet '{wallet_name}' already exists")

        wallet = WalletData(
            name=wallet_name,
            network=network,
            descriptor=descriptor,
            watch_only=True,
        )
        self.storage.save_wallet(wallet)
        return wallet

    def export_descriptor(self, wallet_name: str) -> str:
        """Export CT descriptor for wallet."""
        wallet = self.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet '{wallet_name}' not found")
        return wallet.descriptor

    def load_wallet(
        self,
        wallet_name: str,
        passphrase: Optional[str] = None,
    ) -> WalletData:
        """Load wallet, optionally decrypting mnemonic."""
        wallet = self.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        # Load signer if mnemonic available
        if wallet.encrypted_mnemonic and passphrase:
            mnemonic = self.storage.decrypt_mnemonic(
                wallet.encrypted_mnemonic, passphrase
            )
            net = self._get_network(wallet.network)
            lwk_mnemonic = lwk.Mnemonic(mnemonic)
            self._signers[wallet_name] = lwk.Signer(lwk_mnemonic, net)

        return wallet

    def _get_wollet(self, wallet_name: str) -> lwk.Wollet:
        """Get or create Wollet for wallet."""
        if wallet_name not in self._wollets:
            wallet = self.storage.load_wallet(wallet_name)
            if not wallet:
                raise ValueError(f"Wallet '{wallet_name}' not found")

            net = self._get_network(wallet.network)
            desc = lwk.WolletDescriptor(wallet.descriptor)
            cache_dir = str(self.storage.get_cache_path(wallet_name))
            self._wollets[wallet_name] = lwk.Wollet(net, desc, datadir=cache_dir)

        return self._wollets[wallet_name]

    def sync_wallet(self, wallet_name: str):
        """Sync wallet with blockchain."""
        wallet = self.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        wollet = self._get_wollet(wallet_name)
        client = self._get_client(wallet.network)
        update = client.full_scan(wollet)
        if update:
            wollet.apply_update(update)

    # Wallet operations

    def get_balance(self, wallet_name: str) -> list[Balance]:
        """Get wallet balance."""
        wallet = self.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        self.sync_wallet(wallet_name)
        wollet = self._get_wollet(wallet_name)
        raw_balance = wollet.balance()

        policy_asset = self._get_policy_asset(wallet.network)
        balances = []
        
        for asset_id, amount in raw_balance.items():
            info = lookup_asset(asset_id, wallet.network)
            if info:
                name = info.name
                ticker = info.ticker
                logo = info.logo
                precision = info.precision
            else:
                name = "L-BTC" if asset_id == policy_asset else asset_id[:8] + "..."
                ticker = "L-BTC" if asset_id == policy_asset else asset_id[:8] + "..."
                logo = None
                precision = 8  # Default for Liquid assets
            balances.append(Balance(
                asset_id=asset_id,
                asset_name=name,
                ticker=ticker,
                amount=amount,
                precision=precision,
                logo=logo,
            ))

        return balances

    def get_address(
        self,
        wallet_name: str,
        index: Optional[int] = None,
    ) -> Address:
        """Get receive address."""
        wollet = self._get_wollet(wallet_name)
        addr = wollet.address(index)
        return Address(
            address=str(addr.address()),
            index=addr.index(),
        )

    def get_transactions(
        self,
        wallet_name: str,
        limit: Optional[int] = None,
    ) -> list[Transaction]:
        """Get transaction history."""
        wallet = self.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        self.sync_wallet(wallet_name)
        wollet = self._get_wollet(wallet_name)
        policy_asset = self._get_policy_asset(wallet.network)

        txs = wollet.transactions()
        if limit:
            txs = txs[:limit]

        result = []
        for tx in txs:
            balance = {}
            for asset_id, amount in tx.balance().items():
                ticker = resolve_asset_name(asset_id, wallet.network)
                balance[ticker] = {"asset_id": asset_id, "amount": amount}
            
            result.append(Transaction(
                txid=str(tx.txid()),
                height=tx.height(),
                timestamp=tx.timestamp(),
                balance=balance,
                fee=tx.fee(policy_asset) or 0,
            ))

        return result

    def send(
        self,
        wallet_name: str,
        address: str,
        amount: int,
        asset_id: Optional[str] = None,
        passphrase: Optional[str] = None,
    ) -> str:
        """Send transaction. Returns txid."""
        wallet = self.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        if wallet.watch_only:
            raise ValueError("Cannot sign with watch-only wallet")

        # Ensure we have the signer
        if wallet_name not in self._signers:
            if not wallet.encrypted_mnemonic:
                raise ValueError("No mnemonic available for signing")
            if not passphrase:
                raise ValueError("Passphrase required to decrypt mnemonic")
            self.load_wallet(wallet_name, passphrase)

        signer = self._signers[wallet_name]
        wollet = self._get_wollet(wallet_name)
        net = self._get_network(wallet.network)
        client = self._get_client(wallet.network)

        # Sync first
        self.sync_wallet(wallet_name)

        # Build transaction
        builder = net.tx_builder()
        lwk_address = lwk.Address(address)

        if asset_id:
            builder.add_recipient(lwk_address, amount, asset_id)
        else:
            builder.add_lbtc_recipient(lwk_address, amount)

        unsigned_pset = builder.finish(wollet)
        signed_pset = signer.sign(unsigned_pset)
        tx = signed_pset.finalize()
        
        # Broadcast
        txid = client.broadcast(tx)
        return str(txid)
