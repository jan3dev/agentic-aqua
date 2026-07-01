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
        return self.amount / (10**self.precision)

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
        password: Optional[str] = None,
    ) -> WalletData:
        """Import wallet from mnemonic.

        ``password`` is used only to encrypt the mnemonic at rest.
        The derived Liquid descriptor depends solely on the mnemonic.
        """
        if self.storage.wallet_exists(wallet_name):
            raise ValueError(f"Wallet '{wallet_name}' already exists")

        # Create signer and get descriptor
        net = self._get_network(network)
        lwk_mnemonic = lwk.Mnemonic(mnemonic)
        signer = lwk.Signer(lwk_mnemonic, net)
        descriptor = str(signer.wpkh_slip77_descriptor())

        encrypted = self.storage.store_mnemonic(mnemonic, password)

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
        password: Optional[str] = None,
    ) -> WalletData:
        """Load wallet, optionally decrypting mnemonic."""
        wallet = self.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        if wallet.encrypted_mnemonic:
            needs_password = self.storage.requires_user_password(wallet.encrypted_mnemonic)
            if not needs_password or password:
                mnemonic = self.storage.read_and_migrate_mnemonic(wallet, password)
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
            balances.append(
                Balance(
                    asset_id=asset_id,
                    asset_name=name,
                    ticker=ticker,
                    amount=amount,
                    precision=precision,
                    logo=logo,
                )
            )

        return balances

    def get_address(
        self,
        wallet_name: str,
        index: Optional[int] = None,
    ) -> Address:
        """Get a receive address.

        With no ``index``, hands out the next previously-unhanded-out address
        and bumps the wallet's persisted ``next_address_index`` counter so two
        flows never share an address. lwk's own "next-unused" tip only advances
        after the chain observes usage, which is too slow for off-chain
        handouts (LN-address registration, Boltz claim addresses, …) — so we
        max it against our own counter.

        With an explicit ``index``, returns that exact address without
        advancing the counter. Callers asserting a specific index are assumed
        to know what they're doing.

        Note: the no-arg path is NOT idempotent — each call writes to disk and
        consumes an index. Callers that need to peek without committing should
        pass an explicit ``index``.
        """
        if index is None:
            # Same load → max(lwk_tip, counter) → derive → advance → save
            # sequence lives once, in reserve_addresses.
            return self.reserve_addresses(wallet_name, 1)[0]
        wollet = self._get_wollet(wallet_name)
        addr = wollet.address(index)
        return Address(address=str(addr.address()), index=addr.index())

    def reserve_addresses(
        self,
        wallet_name: str,
        count: int,
    ) -> list[Address]:
        """Hand out ``count`` fresh receive addresses in one shot.

        Same semantics as calling ``get_address(name)`` ``count`` times — each
        address is distinct and the persisted counter is advanced past all of
        them — but batched into a single load + save of the wallet record. Use
        when minting many addresses at once (e.g. for LN-address registration).
        """
        if count <= 0:
            raise ValueError("count must be positive")
        wollet = self._get_wollet(wallet_name)
        wallet_record = self.storage.load_wallet(wallet_name)
        if wallet_record is None:
            raise ValueError(f"Wallet {wallet_name!r} not found")
        lwk_tip = wollet.address(None).index()
        start = max(lwk_tip, wallet_record.next_address_index)
        addresses: list[Address] = []
        for offset in range(count):
            addr = wollet.address(start + offset)
            addresses.append(Address(address=str(addr.address()), index=addr.index()))
        wallet_record.next_address_index = start + count
        self.storage.save_wallet(wallet_record)
        return addresses

    def fingerprint(
        self,
        wallet_name: str,
        password: Optional[str] = None,
    ) -> str:
        """Return the BIP32 master fingerprint (8 hex chars) for ``wallet_name``.

        Format matches the AQUA Ankara backend's ``fingerprint`` field on
        ``UserResponse`` / ``UserLiquidAddressesUpsert``: ``HASH160(master xpub)[:4]``
        in hex. For hot wallets this comes straight from the LWK signer; for
        watch-only wallets we parse the embedded ``[fp/derivation]`` block from
        the stored descriptor.
        """
        if wallet_name in self._signers:
            return self._signers[wallet_name].fingerprint()

        # ``load_wallet`` decrypts the mnemonic (if needed) and caches the
        # resulting lwk.Signer in ``self._signers`` as a side effect — so for
        # hot wallets the next check succeeds and we get the canonical
        # BIP32-master fingerprint straight from the signer.
        wallet = self.load_wallet(wallet_name, password=password)
        if wallet_name in self._signers:
            return self._signers[wallet_name].fingerprint()

        # Watch-only: best-effort parse of the [fp/derivation]xpub block.
        from .bitcoin import _extract_xpub_metadata

        meta = _extract_xpub_metadata(wallet.descriptor)
        fp = meta.get("fingerprint")
        if not fp:
            raise ValueError(
                f"Cannot determine fingerprint for watch-only wallet {wallet_name!r}: "
                "descriptor has no [fingerprint/derivation] block."
            )
        return fp

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

        txs = wollet.transactions()
        if limit:
            txs = txs[:limit]

        result = []
        for tx in txs:
            balance = {}
            for asset_id, amount in tx.balance().items():
                ticker = resolve_asset_name(asset_id, wallet.network)
                balance[ticker] = {"asset_id": asset_id, "amount": amount}

            result.append(
                Transaction(
                    txid=str(tx.txid()),
                    height=tx.height(),
                    timestamp=tx.timestamp(),
                    balance=balance,
                    fee=tx.fee() or 0,
                )
            )

        return result

    def send(
        self,
        wallet_name: str,
        address: str,
        amount: int,
        asset_id: Optional[str] = None,
        password: Optional[str] = None,
    ) -> str:
        """Send transaction. Returns txid."""
        wallet = self.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        if wallet.watch_only:
            raise ValueError("Cannot sign with watch-only wallet")

        if amount <= 0:
            raise ValueError("Amount must be positive")

        if wallet_name not in self._signers:
            if not wallet.encrypted_mnemonic:
                raise ValueError("No mnemonic available for signing")
            needs_password = self.storage.requires_user_password(wallet.encrypted_mnemonic)
            if needs_password and not password:
                raise ValueError("Password required to decrypt mnemonic")
            self.load_wallet(wallet_name, password)

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

    def sweep(
        self,
        wallet_name: str,
        address: str,
        asset_id: Optional[str] = None,
        password: Optional[str] = None,
    ) -> str:
        """Sweep a wallet balance to a single address. Returns txid.

        With ``asset_id`` omitted (or set to the network's L-BTC policy asset),
        spends every L-BTC input and routes the remainder to ``address`` — the
        on-chain fee is paid from those inputs, so 0 L-BTC remains in the
        wallet afterwards. (Wraps LWK's ``drain_lbtc_*`` builder API.)

        With ``asset_id`` set to a non-L-BTC asset, sends the entire balance of
        that asset to ``address``. The on-chain fee is still paid in L-BTC, so
        L-BTC change may be returned to the wallet — call ``sweep`` again
        without ``asset_id`` to also sweep the L-BTC remainder.
        """
        wallet = self.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        if wallet.watch_only:
            raise ValueError("Cannot sign with watch-only wallet")

        if wallet_name not in self._signers:
            if not wallet.encrypted_mnemonic:
                raise ValueError("No mnemonic available for signing")
            needs_password = self.storage.requires_user_password(wallet.encrypted_mnemonic)
            if needs_password and not password:
                raise ValueError("Password required to decrypt mnemonic")
            self.load_wallet(wallet_name, password)

        signer = self._signers[wallet_name]
        wollet = self._get_wollet(wallet_name)
        net = self._get_network(wallet.network)
        client = self._get_client(wallet.network)
        policy_asset = self._get_policy_asset(wallet.network)

        # An explicit policy-asset id is just an L-BTC sweep — collapse to
        # the L-BTC path so callers can pass either spelling.
        if asset_id == policy_asset:
            asset_id = None

        self.sync_wallet(wallet_name)
        raw_balance = wollet.balance()

        builder = net.tx_builder()
        lwk_address = lwk.Address(address)

        if asset_id is None:
            lbtc_balance = int(raw_balance.get(policy_asset, 0))
            if lbtc_balance <= 0:
                raise ValueError("No L-BTC to sweep")
            builder.drain_lbtc_wallet()
            builder.drain_lbtc_to(lwk_address)
        else:
            asset_balance = int(raw_balance.get(asset_id, 0))
            if asset_balance <= 0:
                ticker = resolve_asset_name(asset_id, wallet.network)
                raise ValueError(f"No {ticker} balance to sweep")
            builder.add_recipient(lwk_address, asset_balance, asset_id)

        unsigned_pset = builder.finish(wollet)
        signed_pset = signer.sign(unsigned_pset)
        tx = signed_pset.finalize()

        txid = client.broadcast(tx)
        return str(txid)

    def craft_raw_tx(
        self,
        wallet_name: str,
        address: str,
        amount: int,
        asset_id: Optional[str] = None,
        password: Optional[str] = None,
    ) -> str:
        """Build, sign, finalize a Liquid tx. Returns hex; does NOT broadcast.

        Used when a third party (JAN3 Ankara backend, etc.) wants to broadcast
        on our behalf. Branches on whether the destination address is
        confidential (blinded) or explicit (unblinded), since LWK's tx builder
        exposes different methods for each.
        """
        wallet = self.storage.load_wallet(wallet_name)
        if not wallet:
            raise ValueError(f"Wallet '{wallet_name}' not found")

        if wallet.watch_only:
            raise ValueError("Cannot sign with watch-only wallet")

        if amount <= 0:
            raise ValueError("Amount must be positive")

        if wallet_name not in self._signers:
            if not wallet.encrypted_mnemonic:
                raise ValueError("No mnemonic available for signing")
            needs_password = self.storage.requires_user_password(wallet.encrypted_mnemonic)
            if needs_password and not password:
                raise ValueError("Password required to decrypt mnemonic")
            self.load_wallet(wallet_name, password)

        signer = self._signers[wallet_name]
        wollet = self._get_wollet(wallet_name)
        net = self._get_network(wallet.network)
        policy_asset_id = self._get_policy_asset(wallet.network)

        self.sync_wallet(wallet_name)

        builder = net.tx_builder()
        lwk_address = lwk.Address(address)
        effective_asset_id = asset_id or policy_asset_id

        if lwk_address.is_blinded():
            if effective_asset_id == policy_asset_id:
                builder.add_lbtc_recipient(lwk_address, amount)
            else:
                builder.add_recipient(lwk_address, amount, effective_asset_id)
        else:
            builder.add_explicit_recipient(lwk_address, amount, effective_asset_id)

        pset = builder.finish(wollet)
        # Populate wallet-aware details (e.g. input/output values) so callers
        # can introspect the PSET before handing the hex off.
        pset = wollet.add_details(pset)
        signed_pset = signer.sign(pset)
        return str(signed_pset.finalize())
