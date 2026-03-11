"""MCP tool definitions for AQUA."""

import json
import logging
import re
import urllib.request
import urllib.error
from typing import Any
from datetime import datetime, UTC

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

from .assets import resolve_asset_name
from .bitcoin import BitcoinWalletManager
from .boltz import (
    BoltzClient,
    MAX_SWAP_AMOUNT_SATS,
    MIN_SWAP_AMOUNT_SATS,
    SwapInfo,
    decode_bolt11_amount_sats,
    generate_keypair,
)
from .wallet import WalletManager


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
    passphrase: str | None = None,
) -> dict[str, Any]:
    """
    Import a wallet from a BIP39 mnemonic. Creates both Liquid (LWK) and Bitcoin (BDK)
    wallets from the same mnemonic (different derivation paths).

    Args:
        mnemonic: BIP39 mnemonic phrase
        wallet_name: Name for the wallet. Default: "default"
        network: "mainnet" or "testnet". Default: "mainnet"
        passphrase: Optional passphrase to encrypt the mnemonic at rest

    Returns:
        wallet_name: Name of the created wallet
        network: Network the wallet is on
        descriptor: CT descriptor (Liquid, can be shared for watch-only)
        btc_descriptor: BIP84 descriptor (Bitcoin)
        watch_only: False (this is a full wallet)
    """
    manager = get_manager()
    wallet = manager.import_mnemonic(mnemonic, wallet_name, network, passphrase)
    btc_manager = get_btc_manager()
    btc_manager.create_wallet(mnemonic, wallet_name, network, passphrase)
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
    passphrase: str | None = None,
) -> dict[str, Any]:
    """
    Send L-BTC to an address.
    
    Args:
        wallet_name: Name of the wallet
        address: Destination Liquid address
        amount: Amount in satoshis
        passphrase: Passphrase to decrypt mnemonic (if encrypted)
        
    Returns:
        txid: Transaction ID
        amount: Amount sent
        address: Destination address
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")
    manager = get_manager()
    txid = manager.send(wallet_name, address, amount, passphrase=passphrase)
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
    passphrase: str | None = None,
) -> dict[str, Any]:
    """
    Send a Liquid asset to an address.
    
    Args:
        wallet_name: Name of the wallet
        address: Destination Liquid address
        amount: Amount in satoshis
        asset_id: Asset ID (hex string)
        passphrase: Passphrase to decrypt mnemonic (if encrypted)
        
    Returns:
        txid: Transaction ID
        amount: Amount sent
        asset_id: Asset sent
        address: Destination address
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")
    manager = get_manager()
    txid = manager.send(wallet_name, address, amount, asset_id, passphrase)
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

    raise ValueError(f"Invalid input: expected a 64-char hex txid or a Blockstream URL, got: {tx_input}")


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

    req = urllib.request.Request(api_url, headers={"User-Agent": "aqua-mcp"})
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
        tip_req = urllib.request.Request(tip_url, headers={"User-Agent": "aqua-mcp"})
        try:
            with urllib.request.urlopen(tip_req, timeout=15) as resp:
                tip_height = int(resp.read().decode().strip())
            result["confirmations"] = tip_height - block_height + 1
        except Exception as e:
            result["confirmations"] = None
            result["warning"] = f"Could not fetch current block height to calculate confirmations: {e}"
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
        "balance_btc": round(balance_sats / 100_000_000, 8),
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


def btc_send(
    wallet_name: str,
    address: str,
    amount: int,
    fee_rate: int | None = None,
    passphrase: str | None = None,
) -> dict[str, Any]:
    """
    Send BTC to an address.

    Args:
        wallet_name: Name of the wallet
        address: Destination Bitcoin address (bc1...)
        amount: Amount in satoshis
        fee_rate: Optional fee rate in sat/vB. Default: let BDK choose
        passphrase: Passphrase to decrypt mnemonic (if encrypted)

    Returns:
        txid: Transaction ID
        amount: Amount sent
        address: Destination address
    """
    btc = get_btc_manager()
    txid = btc.send(wallet_name, address, amount, fee_rate, passphrase)
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
        bitcoin_error = str(e) or "This wallet has no Bitcoin descriptors (e.g. watch-only Liquid-only wallet)."
        logger.info("unified_balance: Bitcoin balance unavailable for %s: %s", wallet_name, bitcoin_error)
    except Exception as e:
        bitcoin_error = f"Could not fetch Bitcoin balance: {e}"
        logger.warning("unified_balance: %s", bitcoin_error, exc_info=True)

    result: dict[str, Any] = {
        "wallet_name": wallet_name,
        "bitcoin": {
            "balance_sats": btc_sats,
            "balance_btc": round(btc_sats / 100_000_000, 8) if btc_sats is not None else None,
        } if btc_sats is not None else None,
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


# ---------------------------------------------------------------------------
# Lightning (Boltz submarine swap) tools
# ---------------------------------------------------------------------------


def lbtc_pay_lightning_invoice(
    invoice: str,
    wallet_name: str = "default",
    passphrase: str | None = None,
) -> dict[str, Any]:
    """Pay a Lightning invoice using L-BTC via Boltz submarine swap.

    Args:
        invoice: BOLT11 Lightning invoice (lnbc...)
        wallet_name: Liquid wallet to pay from. Default: "default"
        passphrase: Passphrase to decrypt mnemonic (if encrypted)

    Returns:
        swap_id, lockup_txid, status, expected_amount, timeout_block_height
    """
    # Step 1: Validate invoice format
    valid_prefixes = ("lnbc", "lntb")
    if not invoice or not any(invoice.startswith(p) for p in valid_prefixes):
        raise ValueError("Invalid invoice: must be a BOLT11 Lightning invoice starting with 'lnbc' (mainnet) or 'lntb' (testnet)")

    manager = get_manager()

    # Verify wallet exists and is usable
    wallet_data = manager.storage.load_wallet(wallet_name)
    if not wallet_data:
        raise ValueError(f"Wallet '{wallet_name}' not found")
    if wallet_data.watch_only:
        raise ValueError("Watch-only wallet cannot sign transactions")
    if wallet_data.encrypted_mnemonic and manager.storage.is_mnemonic_encrypted(
        wallet_data.encrypted_mnemonic
    ):
        if not passphrase:
            raise ValueError("Passphrase required to decrypt mnemonic")

    network = wallet_data.network

    # Step 2: Pre-flight amount check (avoids creating an orphan swap on Boltz)
    invoice_amount = decode_bolt11_amount_sats(invoice)
    if invoice_amount is not None:
        if invoice_amount < MIN_SWAP_AMOUNT_SATS:
            raise ValueError(
                f"Invoice amount {invoice_amount} sats is below the minimum ({MIN_SWAP_AMOUNT_SATS} sats)"
            )
        if invoice_amount > MAX_SWAP_AMOUNT_SATS:
            raise ValueError(
                f"Invoice amount {invoice_amount} sats exceeds the maximum ({MAX_SWAP_AMOUNT_SATS} sats)"
            )

    # Step 3: Verify the L-BTC/BTC pair is available on Boltz
    client = BoltzClient(network=network)
    pairs = client.get_submarine_pairs()

    pair = pairs.get("L-BTC", {}).get("BTC")
    if not pair:
        raise ValueError("L-BTC/BTC pair not available on Boltz")

    # Step 4: Generate ephemeral keypair
    refund_privkey, refund_pubkey = generate_keypair()  # Next PR will change how this works

    # Step 5: Create submarine swap
    swap_resp = client.create_submarine_swap(invoice, refund_pubkey)

    expected_amount = swap_resp["expectedAmount"]

    # Step 6: Build SwapInfo and persist BEFORE sending

    swap = SwapInfo(
        swap_id=swap_resp["id"],
        address=swap_resp["address"],
        expected_amount=expected_amount,
        claim_public_key=swap_resp["claimPublicKey"],
        swap_tree=swap_resp["swapTree"],
        timeout_block_height=swap_resp["timeoutBlockHeight"],
        refund_private_key=refund_privkey,
        refund_public_key=refund_pubkey,
        invoice=invoice,
        status="swap.created",
        network=network,
        created_at=datetime.now(UTC).isoformat(),
    )
    manager.storage.save_swap(swap)

    # Step 7: Send L-BTC to lockup address (send() validates balance internally)
    lockup_txid = manager.send(
        wallet_name, swap.address, expected_amount, passphrase=passphrase
    )

    # Update swap with lockup txid
    swap.lockup_txid = lockup_txid
    swap.status = "transaction.mempool"
    manager.storage.save_swap(swap)

    return {
        "swap_id": swap.swap_id,
        "lockup_txid": lockup_txid,
        "status": swap.status,
        "expected_amount": expected_amount,
        "timeout_block_height": swap.timeout_block_height,
    }


def lbtc_swap_lightning_status(swap_id: str) -> dict[str, Any]:
    """Check the status of a Boltz submarine swap.

    Args:
        swap_id: Boltz swap ID

    Returns:
        swap_id, status, lockup_txid, timeout_block_height, network
    """
    manager = get_manager()
    swap = manager.storage.load_swap(swap_id)
    if not swap:
        raise ValueError(f"Swap not found: {swap_id}")

    # Try to fetch remote status from Boltz
    client = BoltzClient(network=swap.network)
    warning = None
    try:
        remote = client.get_swap_status(swap_id)
        swap.status = remote["status"]
        manager.storage.save_swap(swap)
    except Exception as e:
        warning = f"Could not fetch remote status: {e}"

    result: dict[str, Any] = {
        "swap_id": swap.swap_id,
        "status": swap.status,
        "lockup_txid": swap.lockup_txid,
        "timeout_block_height": swap.timeout_block_height,
        "network": swap.network,
    }

    # If claimed, fetch preimage
    if swap.status == "transaction.claimed":
        try:
            claim = client.get_claim_details(swap_id)
            result["preimage"] = claim.get("preimage")
            result["claim_txid"] = claim.get("transactionHash")
        except Exception as e:
            result["claim_details_warning"] = f"Could not fetch claim details: {e}"

    # If failed, provide refund info
    FAILURE_STATUSES = {"invoice.failedToPay", "swap.expired", "transaction.lockupFailed"}
    if swap.status in FAILURE_STATUSES:
        result["refund_info"] = (
            f"Swap failed. Refund available after block {swap.timeout_block_height}. "
            f"Swap ID: {swap.swap_id}"
        )

    if warning:
        result["warning"] = warning

    return result


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
    "btc_balance": btc_balance,
    "btc_address": btc_address,
    "btc_transactions": btc_transactions,
    "btc_send": btc_send,
    "unified_balance": unified_balance,
    "lbtc_pay_lightning_invoice": lbtc_pay_lightning_invoice,
    "lbtc_swap_lightning_status": lbtc_swap_lightning_status,
}
