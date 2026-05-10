"""MCP tool definitions for AQUA."""

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from .assets import MAINNET_ASSETS, TESTNET_ASSETS, resolve_asset_name
from .bitcoin import BitcoinWalletManager
from .wallet import WalletManager

logger = logging.getLogger(__name__)

ESPLORA_URLS = {
    "mainnet": "https://blockstream.info/liquid/api",
    "testnet": "https://blockstream.info/liquidtestnet/api",
}

EXPLORER_URLS = {
    "mainnet": "https://blockstream.info/liquid/tx",
    "testnet": "https://blockstream.info/liquidtestnet/tx",
}


# Global wallet manager instance
_manager: WalletManager | None = None
_btc_manager: BitcoinWalletManager | None = None
_lightning_manager: "LightningManager | None" = None
_sideswap_peg_manager: "SideSwapPegManager | None" = None
_sideswap_swap_manager: "SideSwapSwapManager | None" = None


def get_manager() -> WalletManager:
    """Get or create wallet manager."""
    global _manager
    if _manager is None:
        _manager = WalletManager()
    return _manager


def get_btc_manager() -> BitcoinWalletManager:
    """Get or create Bitcoin wallet manager (shares storage with Liquid manager)."""
    global _btc_manager
    if _btc_manager is None:
        _btc_manager = BitcoinWalletManager(storage=get_manager().storage)
    return _btc_manager


def get_lightning_manager() -> "LightningManager":
    """Get or create Lightning manager (shares storage and wallet manager)."""
    global _lightning_manager
    if _lightning_manager is None:
        from .lightning import LightningManager

        _lightning_manager = LightningManager(
            storage=get_manager().storage,
            wallet_manager=get_manager(),
        )
    return _lightning_manager


def get_sideswap_peg_manager() -> "SideSwapPegManager":
    """Get or create SideSwap peg manager (shares storage + wallet managers)."""
    global _sideswap_peg_manager
    if _sideswap_peg_manager is None:
        from .sideswap import SideSwapPegManager

        _sideswap_peg_manager = SideSwapPegManager(
            storage=get_manager().storage,
            wallet_manager=get_manager(),
            btc_wallet_manager=get_btc_manager(),
        )
    return _sideswap_peg_manager


def get_sideswap_swap_manager() -> "SideSwapSwapManager":
    """Get or create SideSwap asset-swap manager (shares storage + wallet manager)."""
    global _sideswap_swap_manager
    if _sideswap_swap_manager is None:
        from .sideswap import SideSwapSwapManager

        _sideswap_swap_manager = SideSwapSwapManager(
            storage=get_manager().storage,
            wallet_manager=get_manager(),
        )
    return _sideswap_swap_manager


# Tool implementations


def lw_generate_mnemonic() -> dict[str, Any]:
    """
    Generate a new BIP39 mnemonic phrase (12 words).

    Returns:
        mnemonic: The generated mnemonic phrase
    """
    manager = get_manager()
    mnemonic = manager.generate_mnemonic()
    return {
        "mnemonic": mnemonic,
        "words": len(mnemonic.split()),
        "warning": "Store this mnemonic securely. Anyone with access can control your funds.",
    }


def lw_import_mnemonic(
    mnemonic: str,
    wallet_name: str = "default",
    network: str = "mainnet",
    password: str | None = None,
) -> dict[str, Any]:
    """
    Import a wallet from a BIP39 mnemonic. Creates both Liquid (LWK) and Bitcoin (BDK)
    wallets from the same mnemonic (different derivation paths).

    Args:
        mnemonic: BIP39 mnemonic phrase
        wallet_name: Name for the wallet. Default: "default"
        network: "mainnet" or "testnet". Default: "mainnet"
        password: Optional password to encrypt the mnemonic at rest. NOT a BIP39
            passphrase: the derived keys depend only on the mnemonic, so the
            resulting descriptors match what other wallets (AQUA, Green, Jade)
            produce for the same seed.

    Returns:
        wallet_name: Name of the created wallet
        network: Network the wallet is on
        descriptor: CT descriptor (Liquid, can be shared for watch-only)
        btc_descriptor: BIP84 descriptor (Bitcoin)
        watch_only: False (this is a full wallet)
    """
    manager = get_manager()
    wallet = manager.import_mnemonic(mnemonic, wallet_name, network, password)
    btc_manager = get_btc_manager()
    btc_manager.create_wallet(mnemonic, wallet_name, network)
    wallet_data = manager.storage.load_wallet(wallet_name)
    return {
        "wallet_name": wallet.name,
        "network": wallet.network,
        "descriptor": wallet.descriptor,
        "btc_descriptor": wallet_data.btc_descriptor,
        "watch_only": wallet.watch_only,
    }


def lw_import_descriptor(
    descriptor: str,
    wallet_name: str,
    network: str = "mainnet",
) -> dict[str, Any]:
    """
    Import a watch-only wallet from a CT descriptor.

    Args:
        descriptor: CT descriptor string
        wallet_name: Name for the wallet
        network: "mainnet" or "testnet". Default: "mainnet"

    Returns:
        wallet_name: Name of the created wallet
        network: Network the wallet is on
        watch_only: True (cannot sign transactions)
    """
    manager = get_manager()
    wallet = manager.import_descriptor(descriptor, wallet_name, network)
    return {
        "wallet_name": wallet.name,
        "network": wallet.network,
        "watch_only": wallet.watch_only,
    }


def lw_export_descriptor(wallet_name: str = "default") -> dict[str, Any]:
    """
    Export the CT descriptor for a wallet.

    The descriptor can be used to create a watch-only wallet elsewhere.

    Args:
        wallet_name: Name of the wallet. Default: "default"

    Returns:
        descriptor: CT descriptor string
        wallet_name: Name of the wallet
    """
    manager = get_manager()
    descriptor = manager.export_descriptor(wallet_name)
    return {
        "wallet_name": wallet_name,
        "descriptor": descriptor,
    }


def lw_balance(wallet_name: str = "default") -> dict[str, Any]:
    """
    Get wallet balance for all assets.

    Args:
        wallet_name: Name of the wallet. Default: "default"

    Returns:
        balances: List of asset balances
        wallet_name: Name of the wallet
    """
    manager = get_manager()
    balances = manager.get_balance(wallet_name)
    return {
        "wallet_name": wallet_name,
        "balances": [b.to_dict() for b in balances],
    }


def lw_address(
    wallet_name: str = "default",
    index: int | None = None,
) -> dict[str, Any]:
    """
    Generate a receive address.

    Args:
        wallet_name: Name of the wallet. Default: "default"
        index: Specific address index. Default: next unused

    Returns:
        address: The Liquid address
        index: Address index
    """
    manager = get_manager()
    addr = manager.get_address(wallet_name, index)
    return addr.to_dict()


def lw_transactions(
    wallet_name: str = "default",
    limit: int | None = 10,
) -> dict[str, Any]:
    """
    Get transaction history.

    Args:
        wallet_name: Name of the wallet. Default: "default"
        limit: Maximum number of transactions. Default: 10

    Returns:
        transactions: List of transactions
        count: Number of transactions returned
    """
    manager = get_manager()
    txs = manager.get_transactions(wallet_name, limit)
    return {
        "wallet_name": wallet_name,
        "transactions": [tx.to_dict() for tx in txs],
        "count": len(txs),
    }


def lw_send(
    wallet_name: str,
    address: str,
    amount: int,
    password: str | None = None,
) -> dict[str, Any]:
    """
    Send L-BTC to an address.

    Args:
        wallet_name: Name of the wallet
        address: Destination Liquid address
        amount: Amount in satoshis
        password: Password to decrypt mnemonic (if encrypted at rest)

    Returns:
        txid: Transaction ID
        amount: Amount sent
        address: Destination address
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")
    manager = get_manager()
    txid = manager.send(wallet_name, address, amount, password=password)
    return {
        "txid": txid,
        "amount": amount,
        "address": address,
    }


def lw_send_asset(
    wallet_name: str,
    address: str,
    amount: int,
    asset_id: str,
    password: str | None = None,
) -> dict[str, Any]:
    """
    Send a Liquid asset to an address.

    Args:
        wallet_name: Name of the wallet
        address: Destination Liquid address
        amount: Amount in satoshis
        asset_id: Asset ID (hex string)
        password: Password to decrypt mnemonic (if encrypted at rest)

    Returns:
        txid: Transaction ID
        amount: Amount sent
        asset_id: Asset sent
        address: Destination address
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")
    manager = get_manager()
    txid = manager.send(wallet_name, address, amount, asset_id, password)
    ticker = resolve_asset_name(asset_id)
    return {
        "txid": txid,
        "amount": amount,
        "asset_id": asset_id,
        "ticker": ticker,
        "address": address,
    }


def _parse_tx_input(tx_input: str) -> tuple[str, str]:
    """Parse a txid or Blockstream URL into (txid, network)."""
    # Try to match a Blockstream URL
    match = re.match(
        r"https?://blockstream\.info/(liquidtestnet|liquid)/tx/([0-9a-fA-F]{64})",
        tx_input.strip(),
    )
    if match:
        network = "testnet" if match.group(1) == "liquidtestnet" else "mainnet"
        return match.group(2), network

    # Try raw txid
    txid = tx_input.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", txid):
        return txid, "mainnet"

    raise ValueError(
        f"Invalid input: expected a 64-char hex txid or a Blockstream URL, got: {tx_input}"
    )


def lw_tx_status(tx: str) -> dict[str, Any]:
    """
    Get the status of a Liquid transaction.

    Accepts a txid or a Blockstream explorer URL, e.g.:
    https://blockstream.info/liquid/tx/9763a7...

    Args:
        tx: Transaction ID (hex) or Blockstream URL

    Returns:
        txid, status (confirmed/unconfirmed), block_height, fee, amounts, explorer_url
    """
    txid, network = _parse_tx_input(tx)
    api_url = f"{ESPLORA_URLS[network]}/tx/{txid}"

    req = urllib.request.Request(api_url, headers={"User-Agent": "agentic-aqua"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"Transaction not found: {txid}")
        raise ValueError(f"Blockstream API error: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise ValueError(f"Could not reach Blockstream API: {e.reason}")

    status = data.get("status", {})
    confirmed = status.get("confirmed", False)
    block_height = status.get("block_height")
    block_time = status.get("block_time")
    fee = data.get("fee")

    # Summarize outputs with asset info
    outputs = []
    for vout in data.get("vout", []):
        entry = {}
        if vout.get("scriptpubkey_address"):
            entry["address"] = vout["scriptpubkey_address"]
        if vout.get("value") is not None:
            entry["value"] = vout["value"]
        if vout.get("asset"):
            asset_id = vout["asset"]
            entry["asset_id"] = asset_id
            entry["ticker"] = resolve_asset_name(asset_id)
        if entry:
            outputs.append(entry)

    result = {
        "txid": txid,
        "network": network,
        "status": "confirmed" if confirmed else "unconfirmed",
        "fee": fee,
        "outputs": outputs,
        "explorer_url": f"{EXPLORER_URLS[network]}/{txid}",
    }
    if confirmed:
        result["block_height"] = block_height
        if block_time:
            result["block_time"] = block_time
        # Fetch current tip to calculate confirmations
        tip_url = f"{ESPLORA_URLS[network]}/blocks/tip/height"
        tip_req = urllib.request.Request(tip_url, headers={"User-Agent": "agentic-aqua"})
        try:
            with urllib.request.urlopen(tip_req, timeout=15) as resp:
                tip_height = int(resp.read().decode().strip())
            result["confirmations"] = tip_height - block_height + 1
        except Exception as e:
            result["confirmations"] = None
            result["warning"] = (
                f"Could not fetch current block height to calculate confirmations: {e}"
            )
    else:
        result["confirmations"] = 0

    return result


# ---------------------------------------------------------------------------
# Bitcoin (btc_*) tools
# ---------------------------------------------------------------------------


def btc_balance(wallet_name: str = "default") -> dict[str, Any]:
    """
    Get Bitcoin wallet balance in satoshis.

    Args:
        wallet_name: Name of the wallet. Default: "default"

    Returns:
        wallet_name: Name of the wallet
        balance_sats: Balance in satoshis
        balance_btc: Human-readable balance in BTC
    """
    btc = get_btc_manager()
    balance_sats = btc.get_balance(wallet_name)
    return {
        "wallet_name": wallet_name,
        "balance_sats": balance_sats,
        "balance_btc": f"{balance_sats / 100_000_000:.8f}",
    }


def btc_address(
    wallet_name: str = "default",
    index: int | None = None,
) -> dict[str, Any]:
    """
    Generate a Bitcoin receive address (bc1...).

    Args:
        wallet_name: Name of the wallet. Default: "default"
        index: Specific address index. Default: next unused

    Returns:
        address: The Bitcoin address
        index: Address index
    """
    btc = get_btc_manager()
    addr = btc.get_address(wallet_name, index)
    return addr.to_dict()


def btc_transactions(
    wallet_name: str = "default",
    limit: int | None = 10,
) -> dict[str, Any]:
    """
    Get Bitcoin transaction history.

    Args:
        wallet_name: Name of the wallet. Default: "default"
        limit: Maximum number of transactions. Default: 10

    Returns:
        wallet_name: Name of the wallet
        transactions: List of transactions
        count: Number of transactions returned
    """
    btc = get_btc_manager()
    txs = btc.get_transactions(wallet_name, limit)
    return {
        "wallet_name": wallet_name,
        "transactions": [tx.to_dict() for tx in txs],
        "count": len(txs),
    }


def btc_import_descriptor(
    descriptor: str,
    wallet_name: str,
    network: str = "mainnet",
    change_descriptor: str | None = None,
) -> dict[str, Any]:
    """Import a watch-only BIP84 Bitcoin wallet. Liquid side must be imported separately."""
    btc = get_btc_manager()
    w = btc.import_descriptor(descriptor, wallet_name, network, change_descriptor)
    return {
        "wallet_name": w.name,
        "network": w.network,
        "btc_descriptor": w.btc_descriptor,
        "btc_change_descriptor": w.btc_change_descriptor,
        "watch_only": w.watch_only,
        "message": (
            "Bitcoin watch-only descriptor imported. To monitor the matching "
            "Liquid wallet from the same seed, import its CT descriptor "
            "separately with `lw_import_descriptor`. The Liquid descriptor "
            "is NOT derivable from the Bitcoin xpub (different derivation "
            "paths and SLIP-77 master blinding key required)."
        ),
    }


def btc_export_descriptor(wallet_name: str = "default") -> dict[str, Any]:
    """Export BIP84 descriptors and xpub metadata. Liquid CT descriptor requires lw_export_descriptor."""
    btc = get_btc_manager()
    data = btc.export_descriptor(wallet_name)
    data["note"] = (
        "This is the Bitcoin on-chain descriptor only. For the Liquid CT "
        "descriptor of the same wallet (different derivation path + "
        "SLIP-77 blinding key), call `lw_export_descriptor`."
    )
    return data


def btc_send(
    wallet_name: str,
    address: str,
    amount: int,
    fee_rate: int | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """
    Send BTC to an address.

    Args:
        wallet_name: Name of the wallet
        address: Destination Bitcoin address (bc1...)
        amount: Amount in satoshis
        fee_rate: Optional fee rate in sat/vB. Default: let BDK choose
        password: Password to decrypt mnemonic (if encrypted at rest)

    Returns:
        txid: Transaction ID
        amount: Amount sent
        address: Destination address
    """
    btc = get_btc_manager()
    txid = btc.send(wallet_name, address, amount, fee_rate, password)
    return {
        "txid": txid,
        "amount": amount,
        "address": address,
    }


def unified_balance(wallet_name: str = "default") -> dict[str, Any]:
    """
    Get balance for both Bitcoin and Liquid networks (unified wallet).

    Args:
        wallet_name: Name of the wallet. Default: "default"

    Returns:
        wallet_name: Name of the wallet
        bitcoin: { balance_sats, balance_btc } or null if no BTC descriptors
        bitcoin_error: Optional message when Bitcoin balance is unavailable (for agent to explain to user)
        liquid: { balances: [...] }
    """
    manager = get_manager()
    liquid_balances = manager.get_balance(wallet_name)
    btc_sats: int | None = None
    bitcoin_error: str | None = None
    try:
        btc = get_btc_manager()
        btc_sats = btc.get_balance(wallet_name)
    except ValueError as e:
        bitcoin_error = (
            str(e) or "This wallet has no Bitcoin descriptors (e.g. watch-only Liquid-only wallet)."
        )
        logger.info(
            "unified_balance: Bitcoin balance unavailable for %s: %s", wallet_name, bitcoin_error
        )
    except Exception as e:
        bitcoin_error = f"Could not fetch Bitcoin balance: {e}"
        logger.warning("unified_balance: %s", bitcoin_error, exc_info=True)

    result: dict[str, Any] = {
        "wallet_name": wallet_name,
        "bitcoin": (
            {
                "balance_sats": btc_sats,
                "balance_btc": f"{btc_sats / 100_000_000:.8f}" if btc_sats is not None else None,
            }
            if btc_sats is not None
            else None
        ),
        "liquid": {
            "balances": [b.to_dict() for b in liquid_balances],
        },
    }
    if bitcoin_error is not None:
        result["bitcoin_error"] = bitcoin_error
    return result


def lw_list_wallets() -> dict[str, Any]:
    """
    List all wallets.

    Returns:
        wallets: List of wallet names
        count: Number of wallets
    """
    manager = get_manager()
    wallets = manager.storage.list_wallets()
    return {
        "wallets": wallets,
        "count": len(wallets),
    }


def lw_list_assets(network: str = "mainnet") -> dict[str, Any]:
    """
    List known Liquid assets with their asset_id, ticker, name, and precision.

    Use this to discover asset IDs for lw_send_asset without needing a prior
    balance query. Tickers are the display name (e.g. "USDt", "DePix").

    Args:
        network: "mainnet" or "testnet". Default: "mainnet"

    Returns:
        network: Which registry was queried
        count: Number of known assets
        assets: List of {asset_id, ticker, name, precision}
    """
    if network not in ("mainnet", "testnet"):
        raise ValueError(f"Unknown network: {network}")
    registry = MAINNET_ASSETS if network == "mainnet" else TESTNET_ASSETS
    return {
        "network": network,
        "count": len(registry),
        "assets": [
            {
                "asset_id": info.asset_id,
                "ticker": info.ticker,
                "name": info.name,
                "precision": info.precision,
            }
            for info in registry.values()
        ],
    }


def delete_wallet(wallet_name: str) -> dict[str, Any]:
    """Delete a wallet and all its cached data.

    Args:
        wallet_name: Name of the wallet to delete.

    Returns:
        deleted: True if wallet was deleted.
        wallet_name: Name of the deleted wallet.
    """
    manager = get_manager()
    wallet_data = manager.storage.load_wallet(wallet_name)
    if wallet_data is None:
        raise ValueError(f"Wallet '{wallet_name}' not found")

    # Clear Liquid (LWK) manager caches
    manager._signers.pop(wallet_name, None)
    manager._wollets.pop(wallet_name, None)

    # Clear Bitcoin (BDK) manager caches
    btc = get_btc_manager()
    btc._wallets.pop(wallet_name, None)
    btc._persisters.pop(wallet_name, None)
    btc._networks.pop(wallet_name, None)

    # SideSwap peg records reference this wallet by name; delete them too so
    # the user doesn't keep stale entries pointing at a wallet that no
    # longer exists. Idempotent — silent if no records exist.
    pegs_removed = manager.storage.delete_sideswap_pegs_for_wallet(wallet_name)

    manager.storage.delete_wallet(wallet_name)
    return {
        "deleted": True,
        "wallet_name": wallet_name,
        "sideswap_pegs_removed": pegs_removed,
    }


# ---------------------------------------------------------------------------
# Lightning tools (unified interface)
# ---------------------------------------------------------------------------


def lightning_receive(
    amount: int,
    wallet_name: str = "default",
    password: str | None = None,
) -> dict[str, Any]:
    """Generate a Lightning invoice to receive L-BTC into a Liquid wallet.

    User pays this invoice externally; L-BTC arrives within 1-2 minutes.

    Args:
        amount: Amount in satoshis (100 – 25,000,000)
        wallet_name: Liquid wallet to receive into. Default: "default"
        password: Password to decrypt mnemonic (if encrypted at rest)

    Returns:
        swap_id, invoice, amount, wallet_name, message
    """
    manager = get_lightning_manager()
    swap = manager.create_receive_invoice(amount, wallet_name, password)

    # Count wallets to inform user which one receives
    all_wallets = get_manager().storage.list_wallets()
    wallet_note = f" in wallet '{wallet_name}'" if len(all_wallets) > 1 else ""

    return {
        "swap_id": swap.swap_id,
        "invoice": swap.invoice,
        "amount": amount,
        "wallet_name": wallet_name,
        "message": (
            f"Pay this Lightning invoice to receive {amount} satoshis of L-BTC{wallet_note}. "
            f"Usually takes 1–2 minutes to confirm on Liquid after Lightning payment confirms. "
            f"You can ask the agent to check status with swap_id: {swap.swap_id}"
        ),
    }


def lightning_send(
    invoice: str,
    wallet_name: str = "default",
    password: str | None = None,
) -> dict[str, Any]:
    """Pay a Lightning invoice using L-BTC from a Liquid wallet.

    Uses a submarine swap via Boltz. Fees: ~0.1% + miner fees.

    Args:
        invoice: BOLT11 Lightning invoice (lnbc... or lntb...)
        wallet_name: Liquid wallet to pay from. Default: "default"
        password: Password to decrypt mnemonic (if encrypted at rest)

    Returns:
        swap_id, lockup_txid, status, amount
    """
    manager = get_lightning_manager()
    swap = manager.pay_invoice(invoice, wallet_name, password)

    return {
        "swap_id": swap.swap_id,
        "lockup_txid": swap.lockup_txid,
        "status": swap.status,
        "amount": swap.amount,
    }


def lightning_transaction_status(swap_id: str) -> dict[str, Any]:
    """Check the status of a Lightning swap (send or receive).

    For receive swaps: auto-claims L-BTC when settled. For send swaps: checks
    Boltz status and retrieves preimage when claimed.

    Args:
        swap_id: Swap ID returned from lightning_receive or lightning_send

    Returns:
        swap_id, status, amount, wallet_name, invoice; for receive: optional preimage,
        warning, claim_warning; for send: optional boltz_status, lockup_txid, preimage,
        claim_txid, refund_info, warning
    """
    manager = get_lightning_manager()
    return manager.get_swap_status(swap_id)


# ---------------------------------------------------------------------------
# SideSwap (Liquid asset swaps + BTC ↔ L-BTC pegs)
# ---------------------------------------------------------------------------


def sideswap_server_status(network: str = "mainnet") -> dict[str, Any]:
    """Fetch SideSwap server status: live fees, minimum amounts, hot-wallet balance.

    Use this BEFORE recommending a peg or swap so values reflect current
    SideSwap state. Falls back to documented defaults if SideSwap is unreachable.

    Args:
        network: "mainnet" or "testnet". Default: "mainnet"

    Returns:
        elements_fee_rate, min_peg_in_amount, min_peg_out_amount,
        server_fee_percent_peg_in, server_fee_percent_peg_out,
        peg_in_wallet_balance, peg_out_wallet_balance, optional warning
    """
    manager = get_sideswap_peg_manager()
    return manager.get_server_status(network)


def sideswap_peg_quote(
    amount: int,
    peg_in: bool = True,
    network: str = "mainnet",
) -> dict[str, Any]:
    """Quote the receive amount for a peg (BTC ↔ L-BTC) at current fees.

    SideSwap charges 0.1% on the send amount + a small fixed second-chain fee
    (~286 sats for the Liquid claim tx on peg-in). The quote returns the exact
    amount the user will receive.

    Args:
        amount: Send amount in satoshis
        peg_in: True for BTC → L-BTC (peg-in); False for L-BTC → BTC (peg-out). Default: True
        network: "mainnet" or "testnet". Default: "mainnet"

    Returns:
        send_amount, recv_amount, fee_amount (send - recv), peg_in
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")
    manager = get_sideswap_peg_manager()
    return manager.quote_peg(amount, peg_in, network)


def sideswap_peg_in(
    wallet_name: str = "default",
    password: str | None = None,
) -> dict[str, Any]:
    """Initiate a SideSwap peg-in (BTC → L-BTC).

    Returns a Bitcoin deposit address. The user (or the agent via btc_send)
    must send BTC to this address. After 2 BTC confirmations (~20 min, hot
    wallet path) or 102 confs (~17 hours, cold wallet path for very large
    amounts), L-BTC arrives in the Liquid wallet.

    Fees: 0.1% + ~286 sats Liquid claim fee.

    Args:
        wallet_name: Liquid wallet to receive L-BTC. Default: "default"
        password: Password to decrypt mnemonic (used to derive the receive address)

    Returns:
        order_id, peg_addr (BTC deposit address), recv_addr (Liquid receive address),
        expected_recv (if known), expires_at, message
    """
    manager = get_sideswap_peg_manager()
    peg = manager.peg_in(wallet_name, password)
    return {
        "order_id": peg.order_id,
        "peg_addr": peg.peg_addr,
        "recv_addr": peg.recv_addr,
        "expected_recv": peg.expected_recv,
        "expires_at": peg.expires_at,
        "wallet_name": peg.wallet_name,
        "network": peg.network,
        "message": (
            f"Send BTC to {peg.peg_addr}. After 2 BTC confirmations "
            f"(~20 min for typical amounts; up to ~17 hours for very large peg-ins "
            f"that exceed SideSwap's hot-wallet liquidity), L-BTC will arrive at "
            f"{peg.recv_addr}. Track status with sideswap_peg_status using "
            f"order_id={peg.order_id!r}."
        ),
    }


def sideswap_peg_out(
    wallet_name: str,
    amount: int,
    btc_address: str,
    password: str | None = None,
) -> dict[str, Any]:
    """Initiate a SideSwap peg-out (L-BTC → BTC) and broadcast the L-BTC send.

    Sends `amount` sats of L-BTC from the local wallet to a SideSwap deposit
    address. After 2 Liquid confirmations (~2 min) the federation releases BTC
    to `btc_address` (total time usually 15–60 min).

    Fees: 0.1% + Bitcoin network fee (paid by the federation, deducted from payout).

    Args:
        wallet_name: Liquid wallet to send L-BTC from
        amount: Amount in satoshis to peg out
        btc_address: Destination Bitcoin address (bc1...)
        password: Password to decrypt mnemonic (if encrypted at rest)

    Returns:
        order_id, lockup_txid (L-BTC send txid), peg_addr (Liquid deposit addr),
        recv_addr (target BTC addr), amount, expected_recv (if known), expires_at, message
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")
    manager = get_sideswap_peg_manager()
    peg = manager.peg_out(wallet_name, amount, btc_address, password)
    return {
        "order_id": peg.order_id,
        "lockup_txid": peg.lockup_txid,
        "peg_addr": peg.peg_addr,
        "recv_addr": peg.recv_addr,
        "amount": peg.amount,
        "expected_recv": peg.expected_recv,
        "expires_at": peg.expires_at,
        "wallet_name": peg.wallet_name,
        "network": peg.network,
        "status": peg.status,
        "message": (
            f"L-BTC sent to SideSwap deposit address {peg.peg_addr} "
            f"(lockup_txid={peg.lockup_txid}). After 2 Liquid confirmations "
            f"(~2 min) and the federation BTC sweep (typically 15–60 min total), "
            f"BTC will arrive at {peg.recv_addr}. Track with sideswap_peg_status "
            f"using order_id={peg.order_id!r}."
        ),
    }


def sideswap_peg_status(order_id: str) -> dict[str, Any]:
    """Check the status of a SideSwap peg order (peg-in or peg-out).

    Args:
        order_id: Order ID from sideswap_peg_in or sideswap_peg_out

    Returns:
        order_id, peg_in, status (pending/processing/completed/failed),
        amount, expected_recv, peg_addr, recv_addr, optional tx_state,
        confirmations ("X/Y"), lockup_txid, payout_txid, warning
    """
    manager = get_sideswap_peg_manager()
    return manager.status(order_id)


def sideswap_recommend(
    amount: int,
    direction: str,
    network: str = "mainnet",
) -> dict[str, Any]:
    """Recommend a peg vs an instant swap-market trade for a BTC ↔ L-BTC conversion.

    Surfaces the trade-off (lower fee but slower) and warns when the amount
    exceeds SideSwap's hot-wallet liquidity (would trigger the 102-confirmation
    cold-wallet path on peg-in).

    Args:
        amount: Amount in satoshis to convert
        direction: "btc_to_lbtc" (BTC → L-BTC) or "lbtc_to_btc" (L-BTC → BTC)
        network: "mainnet" or "testnet". Default: "mainnet"

    Returns:
        recommendation ("peg" | "swap" | "either"), reason (human-readable),
        peg_pros, peg_cons, plus the live server_status snapshot.
    """
    from .sideswap import recommend_peg_or_swap

    server = get_sideswap_peg_manager().get_server_status(network)
    rec = recommend_peg_or_swap(amount, direction, server)
    rec["server_status"] = server
    rec["amount"] = amount
    rec["direction"] = direction
    return rec


def sideswap_list_assets(network: str = "mainnet") -> dict[str, Any]:
    """List Liquid assets that SideSwap supports for atomic swaps.

    Args:
        network: "mainnet" or "testnet". Default: "mainnet"

    Returns:
        network, count, assets (list of {asset_id, ticker, name, precision, instant_swaps, icon_url})
    """
    from .sideswap import fetch_assets

    assets = fetch_assets(network)
    return {
        "network": network,
        "count": len(assets),
        "assets": [a.to_dict() for a in assets],
    }


def sideswap_quote(
    asset_id: str,
    send_amount: int | None = None,
    recv_amount: int | None = None,
    send_bitcoins: bool = True,
    network: str = "mainnet",
) -> dict[str, Any]:
    """Get a read-only price quote for a SideSwap Liquid asset swap.

    Subscribes to the SideSwap price stream, captures one quote, then
    unsubscribes. Use this BEFORE calling sideswap_execute_swap so the user
    can confirm the price.

    Provide exactly one of `send_amount` or `recv_amount`.

    Args:
        asset_id: Liquid asset ID to swap with L-BTC
        send_amount: Amount the user is sending (in sats)
        recv_amount: Amount the user wants to receive (in sats)
        send_bitcoins: True if sending L-BTC for the asset; False if sending the asset for L-BTC
        network: "mainnet" or "testnet". Default: "mainnet"

    Returns:
        asset_id, send_bitcoins, send_amount, recv_amount, price, fixed_fee, optional error_msg.
    """
    from .sideswap import fetch_swap_quote

    quote = fetch_swap_quote(
        asset_id=asset_id,
        send_amount=send_amount,
        recv_amount=recv_amount,
        send_bitcoins=send_bitcoins,
        network=network,
    )
    return quote.to_dict()


def sideswap_execute_swap(
    asset_id: str,
    send_amount: int,
    wallet_name: str = "default",
    password: str | None = None,
    send_bitcoins: bool = True,
    flexible_small_amount: bool = False,
) -> dict[str, Any]:
    """Execute a Liquid atomic swap on SideSwap. Both directions are supported.

    Direction is controlled by `send_bitcoins`:

    - `send_bitcoins=True` (default): user sends L-BTC and receives `asset_id`
      (e.g. L-BTC → USDt). `send_amount` is in L-BTC sats.
    - `send_bitcoins=False`: user sends `asset_id` and receives L-BTC
      (e.g. USDt → L-BTC). `send_amount` is in `asset_id` sats.

    Flow (both directions, via SideSwap's mkt::* WebSocket protocol):
      1. Select confidential UTXOs of `send_asset` covering `send_amount`
      2. `market.list_markets` → find the market for our pair
      3. `market.start_quotes` with our UTXOs + receive/change addresses
      4. Wait for a `quote` notification with status=Success
      5. `market.get_quote {quote_id}` → returns the half-built PSET
      6. **Verify the PSET locally** against the agreed quote — refuses to
         sign if recv_asset balance ≠ recv_amount, send_asset is over-deducted,
         or any unrelated asset moves. The fee tolerance only applies to L-BTC,
         so the asset side is always checked at strict equality.
      7. Sign the PSET locally
      8. `market.taker_sign` — server merges and broadcasts; returns the txid

    The order is persisted at every step for crash recovery; check
    sideswap_swap_status with the returned order_id.

    Args:
        asset_id: The non-L-BTC Liquid asset (e.g. USDt). The L-BTC side is
            always the policy asset of the wallet's network.
        send_amount: Send amount in sats (L-BTC if send_bitcoins, else asset).
        wallet_name: Liquid wallet to sign with. Default: "default"
        password: Password to decrypt mnemonic (if encrypted at rest)
        send_bitcoins: True = L-BTC → asset; False = asset → L-BTC.
        flexible_small_amount: When True, accept dealer-rounded send_amount
            adjustments up to ±3000 sats. SideSwap's mkt::* dealer rounds
            internally; small swaps (<25k sats) often come back at e.g.
            5_050 sats when 5_000 was requested. Default False keeps the
            strict equality check that's safer for larger amounts.

    Returns:
        order_id, submit_id, send_asset, send_amount, recv_asset, recv_amount,
        price, txid, status, message
    """
    if send_amount <= 0:
        raise ValueError("send_amount must be positive")
    manager = get_sideswap_swap_manager()
    swap = manager.execute_swap(
        asset_id=asset_id,
        send_amount=send_amount,
        wallet_name=wallet_name,
        password=password,
        send_bitcoins=send_bitcoins,
        flexible_small_amount=flexible_small_amount,
    )
    return {
        "order_id": swap.order_id,
        "submit_id": swap.submit_id,
        "send_asset": swap.send_asset,
        "send_amount": swap.send_amount,
        "recv_asset": swap.recv_asset,
        "recv_amount": swap.recv_amount,
        "price": swap.price,
        "txid": swap.txid,
        "status": swap.status,
        "wallet_name": swap.wallet_name,
        "network": swap.network,
        "message": (
            f"Swap broadcast (txid={swap.txid}). Check confirmation status with "
            f"lw_tx_status. The PSET was verified locally against the quote — "
            f"the wallet receives exactly {swap.recv_amount} sats of recv_asset."
        ),
    }


def sideswap_swap_status(order_id: str) -> dict[str, Any]:
    """Get persisted status of a SideSwap atomic swap (asset swap).

    Asset swaps are atomic on Liquid; once the swap is broadcast the txid is
    final. To check on-chain confirmation, pass the txid to lw_tx_status.

    Args:
        order_id: Order ID returned from sideswap_execute_swap

    Returns:
        order_id, status, send/recv asset+amount, price, txid (if broadcast),
        last_error (if failed)
    """
    manager = get_sideswap_swap_manager()
    return manager.status(order_id)


# Tool registry for MCP
TOOLS = {
    "lw_generate_mnemonic": lw_generate_mnemonic,
    "lw_import_mnemonic": lw_import_mnemonic,
    "lw_import_descriptor": lw_import_descriptor,
    "lw_export_descriptor": lw_export_descriptor,
    "lw_balance": lw_balance,
    "lw_address": lw_address,
    "lw_transactions": lw_transactions,
    "lw_send": lw_send,
    "lw_send_asset": lw_send_asset,
    "lw_tx_status": lw_tx_status,
    "lw_list_wallets": lw_list_wallets,
    "lw_list_assets": lw_list_assets,
    "delete_wallet": delete_wallet,
    "btc_balance": btc_balance,
    "btc_address": btc_address,
    "btc_transactions": btc_transactions,
    "btc_send": btc_send,
    "btc_import_descriptor": btc_import_descriptor,
    "btc_export_descriptor": btc_export_descriptor,
    "unified_balance": unified_balance,
    "lightning_receive": lightning_receive,
    "lightning_send": lightning_send,
    "lightning_transaction_status": lightning_transaction_status,
    "sideswap_server_status": sideswap_server_status,
    "sideswap_peg_quote": sideswap_peg_quote,
    "sideswap_peg_in": sideswap_peg_in,
    "sideswap_peg_out": sideswap_peg_out,
    "sideswap_peg_status": sideswap_peg_status,
    "sideswap_recommend": sideswap_recommend,
    "sideswap_list_assets": sideswap_list_assets,
    "sideswap_quote": sideswap_quote,
    "sideswap_execute_swap": sideswap_execute_swap,
    "sideswap_swap_status": sideswap_swap_status,
}
