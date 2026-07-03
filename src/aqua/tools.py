"""MCP tool definitions for AQUA."""

import json
import logging
import re
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any

from .assets import MAINNET_ASSETS, TESTNET_ASSETS, resolve_asset_name, resolve_liquid_asset_id
from .bitcoin import BitcoinWalletManager
from .bolt11 import decode_bolt11_fields
from .qr import decode_qr, generate_qr
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
_pix_manager: "PixManager | None" = None
_changelly_manager: "ChangellyManager | None" = None
_sideshift_manager: "SideShiftManager | None" = None
_sideswap_peg_manager: "SideSwapPegManager | None" = None
_sideswap_swap_manager: "SideSwapSwapManager | None" = None
_wapupay_manager: "WapuPayManager | None" = None
_jan3_manager: "Jan3AccountsManager | None" = None


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


def get_pix_manager() -> "PixManager":
    """Get or create Pix manager (shares storage and wallet manager)."""
    global _pix_manager
    if _pix_manager is None:
        from .pix import PixManager

        _pix_manager = PixManager(
            storage=get_manager().storage,
            wallet_manager=get_manager(),
        )
    return _pix_manager


def get_changelly_manager() -> "ChangellyManager":
    """Get or create Changelly manager (shares storage + wallet manager)."""
    global _changelly_manager
    if _changelly_manager is None:
        from .changelly import ChangellyManager

        _changelly_manager = ChangellyManager(
            storage=get_manager().storage,
            wallet_manager=get_manager(),
        )
    return _changelly_manager


def get_sideshift_manager() -> "SideShiftManager":
    """Get or create SideShift manager (shares storage + wallet managers)."""
    global _sideshift_manager
    if _sideshift_manager is None:
        from .sideshift import SideShiftManager

        _sideshift_manager = SideShiftManager(
            storage=get_manager().storage,
            wallet_manager=get_manager(),
            btc_wallet_manager=get_btc_manager(),
        )
    return _sideshift_manager


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


def get_jan3_manager() -> "Jan3AccountsManager":
    """Get or create the JAN3 account manager (shares storage + wallet manager).

    Owns all JAN3/AQUA account management (the ``jan3_*`` tools): both login
    flows (free email-OTP + paid captchaless), multi-account session
    persistence, and the WapuPay-key provisioning call against the JAN3/AQUA
    backend (``ANKARA_API_URL``).
    """
    global _jan3_manager
    if _jan3_manager is None:
        from .jan3_accounts import Jan3AccountsManager

        _jan3_manager = Jan3AccountsManager(
            storage=get_manager().storage,
            wallet_manager=get_manager(),
        )
    return _jan3_manager


def get_wapupay_manager() -> "WapuPayManager":
    """Get or create the WapuPay manager (shares storage + wallet + JAN3 manager)."""
    global _wapupay_manager
    if _wapupay_manager is None:
        from .wapupay import WapuPayManager

        _wapupay_manager = WapuPayManager(
            storage=get_manager().storage,
            wallet_manager=get_manager(),
            jan3_manager=get_jan3_manager(),
        )
    return _wapupay_manager


# Tool implementations


def _attach_deposit_qr(
    result: dict[str, Any], data_key: str, *, encode_transform=None
) -> dict[str, Any]:
    """Generate a PNG QR for ``result[data_key]`` and attach its path.

    Adds ``qr_code_path`` (absolute path to the saved PNG) so the agent can
    display the QR to the user. QR rendering is auxiliary to the deposit flow:
    if generation fails the deposit address/invoice is still valid, so the
    error is reported as ``qr_error`` rather than failing the whole tool.

    ``encode_transform`` optionally maps the stored value to the form encoded
    into the QR (the stored field is left untouched). Used to uppercase BOLT11
    invoices, which are case-insensitive: an uppercase payload lets the QR use
    denser alphanumeric mode, producing a smaller, easier-to-scan code.
    """
    value = result.get(data_key)
    if not value:
        return result
    try:
        qr_dir = get_manager().storage.qr_dir
        payload = encode_transform(value) if encode_transform else value
        result["qr_code_path"] = generate_qr(payload, qr_dir)
    except Exception as exc:  # noqa: BLE001 - auxiliary feature, report don't crash
        result["qr_error"] = str(exc)
    return result


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
        index: Address index. If omitted, returns next unused address (read-only, idempotent).

    Returns:
        address: The Liquid address
        index: Address index
        qr_code_path: Path to a PNG QR of the address (or qr_error on failure)
    """
    manager = get_manager()
    addr = manager.peek_address(wallet_name, index)
    return _attach_deposit_qr(addr.to_dict(), "address")


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


def lw_sweep(
    wallet_name: str,
    address: str,
    asset_id: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """
    Sweep the entire L-BTC balance (or full balance of a Liquid asset) to one
    address. Fee paid from the inputs; 0 sats of the targeted balance remain.

    Args:
        wallet_name: Name of the wallet
        address: Destination Liquid address
        asset_id: Optional asset id (hex). Omit for an L-BTC sweep.
        password: Password to decrypt mnemonic (if encrypted at rest)

    Returns:
        txid: Transaction ID
        address: Destination address
        ticker: "L-BTC" or the resolved asset ticker
        asset_id: Only set for asset sweeps
    """
    manager = get_manager()
    txid = manager.sweep(wallet_name, address, asset_id, password)
    result: dict[str, Any] = {"txid": txid, "address": address}
    if asset_id:
        result["asset_id"] = asset_id
        result["ticker"] = resolve_asset_name(asset_id)
    else:
        result["ticker"] = "L-BTC"
    return result


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


def _validate_positive_decimal_string(value: str, field_name: str) -> None:
    """Ensure value strips to a valid Decimal > 0 (for Changelly decimal amounts)."""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must be a non-empty decimal string")
    try:
        amount = Decimal(stripped)
    except InvalidOperation:
        raise ValueError(
            f"{field_name} must be a valid decimal string, got {value!r}"
        ) from None
    if amount <= 0:
        raise ValueError(f"{field_name} must be positive")


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
        qr_code_path: Path to a PNG QR of the address (or qr_error on failure)
    """
    btc = get_btc_manager()
    addr = btc.get_address(wallet_name, index)
    return _attach_deposit_qr(addr.to_dict(), "address")


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


def btc_sweep(
    wallet_name: str,
    address: str,
    fee_rate: int | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """
    Sweep the entire Bitcoin balance to one address. Fee paid from the inputs;
    0 sats remain.

    Args:
        wallet_name: Name of the wallet
        address: Destination Bitcoin address (bc1...)
        fee_rate: Optional fee rate in sat/vB. Default: let BDK choose.
        password: Password to decrypt mnemonic (if encrypted at rest)

    Returns:
        txid: Transaction ID
        address: Destination address
    """
    btc = get_btc_manager()
    txid = btc.sweep(wallet_name, address, fee_rate, password)
    return {"txid": txid, "address": address}


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
        swap_id, invoice, amount, wallet_name, message, qr_code_path
        (qr_code_path is a PNG QR of the invoice, or qr_error on failure)
    """
    manager = get_lightning_manager()
    swap = manager.create_receive_invoice(amount, wallet_name, password)

    # Count wallets to inform user which one receives
    all_wallets = get_manager().storage.list_wallets()
    wallet_note = f" in wallet '{wallet_name}'" if len(all_wallets) > 1 else ""

    return _attach_deposit_qr(
        {
            "swap_id": swap.swap_id,
            "invoice": swap.invoice,
            "amount": amount,
            "wallet_name": wallet_name,
            "message": (
                f"Pay this Lightning invoice to receive {amount} satoshis of L-BTC{wallet_note}. "
                f"Usually takes 1–2 minutes to confirm on Liquid after Lightning payment confirms. "
                f"You can ask the agent to check status with swap_id: {swap.swap_id}"
            ),
        },
        "invoice",
        encode_transform=str.upper,
    )


def lightning_send(
    invoice: str,
    wallet_name: str = "default",
    password: str | None = None,
    amount_sats: int | None = None,
) -> dict[str, Any]:
    """Pay a Lightning invoice or Lightning Address using L-BTC from a Liquid wallet.

    Uses a submarine swap via Boltz. Fees: ~0.1% + miner fees.

    Args:
        invoice: BOLT11 Lightning invoice (lnbc.../lntb...) OR Lightning Address
            (user@domain.com). For LN addresses, the server resolves to a BOLT11
            via LUD-16 (https://{domain}/.well-known/lnurlp/{user}).
        wallet_name: Liquid wallet to pay from. Default: "default"
        password: Password to decrypt mnemonic (if encrypted at rest)
        amount_sats: Amount in sats. Required when `invoice` is a Lightning Address.
            Optional for BOLT11 (must match the encoded amount if supplied).

    Returns:
        swap_id, lockup_txid, status, amount
    """
    if amount_sats is not None:
        if type(amount_sats) is not int:
            raise ValueError("amount_sats must be a positive integer")
        if amount_sats <= 0:
            raise ValueError("Amount must be positive")

    manager = get_lightning_manager()
    swap = manager.pay_invoice(
        invoice, wallet_name, password=password, amount_sats=amount_sats
    )

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


def lightning_decode(invoice: str) -> dict[str, Any]:
    """Decode a BOLT11 Lightning invoice without paying it.

    Returns the amount in satoshis, description/message, and expiry.
    """
    fields = decode_bolt11_fields(invoice)
    return {
        "amount_sats": fields["amount_sats"],
        "description": fields["description"],
        "expiry_seconds": fields["expiry"],
    }


# ---------------------------------------------------------------------------
# Pix → DePix tools (Brazilian Real on-ramp via Eulen)
# ---------------------------------------------------------------------------


def pix_receive(
    amount_cents: int,
    wallet_name: str = "default",
    password: str | None = None,
) -> dict[str, Any]:
    """Mint a Pix charge that pays out DePix to your Liquid wallet.

    Pix is Brazil's instant payment system; DePix is a BRL-pegged Liquid asset
    issued by Eulen. The user pays the returned `qr_copy_paste` string in their
    banking app's "Pix Copia e Cola" field (or scans `qr_image_url` from a
    second device); Eulen credits DePix to the wallet's next address.

    Requires the EULEN_API_TOKEN environment variable.

    Args:
        amount_cents: Amount in BRL cents (100 = R$1.00). NOT reais.
        wallet_name: Liquid wallet to receive DePix into. Default: "default".
        password: Accepted for symmetry; receiving DePix needs only an address.

    Returns:
        swap_id, qr_copy_paste, qr_image_url, amount_cents, amount_brl,
        depix_address, expiration, message.
    """
    manager = get_pix_manager()
    swap = manager.create_deposit(amount_cents, wallet_name, password)

    from .pix import EULEN_FEE_CENTS, format_brl

    amount_brl = format_brl(swap.amount_cents)
    fee_cents = EULEN_FEE_CENTS
    fee_brl = format_brl(fee_cents)
    net_amount_cents = max(swap.amount_cents - fee_cents, 0)
    net_amount_brl = format_brl(net_amount_cents)
    all_wallets = get_manager().storage.list_wallets()
    wallet_note = f" in wallet '{wallet_name}'" if len(all_wallets) > 1 else ""
    return {
        "swap_id": swap.swap_id,
        "qr_copy_paste": swap.qr_copy_paste,
        "qr_image_url": swap.qr_image_url,
        "amount_cents": swap.amount_cents,
        "amount_brl": amount_brl,
        "fee_cents": fee_cents,
        "fee_brl": fee_brl,
        "net_amount_cents": net_amount_cents,
        "net_amount_brl": net_amount_brl,
        "depix_address": swap.depix_address,
        "expiration": swap.expiration,
        "wallet_name": wallet_name,
        "message": (
            f"Pay {amount_brl} via Pix to receive DePix{wallet_note}. "
            "Paste qr_copy_paste into your banking app's 'Pix Copia e Cola' field, "
            "or open qr_image_url on your phone and scan with your bank app. "
            f"Note: Eulen deducts a flat fee of {fee_brl} per deposit, so you will "
            f"receive {net_amount_brl} in DePix. "
            f"Check status with swap_id: {swap.swap_id}"
        ),
    }


# ---------------------------------------------------------------------------
# Changelly (USDt cross-chain swaps via AQUA's Ankara proxy)
# ---------------------------------------------------------------------------


def changelly_list_currencies() -> dict[str, Any]:
    """List the Changelly currencies enabled for swaps in agentic-aqua.

    Filtered server-side to the curated USDt-Liquid ↔ USDt-on-external-chain
    set: `lusdt` (Liquid) plus the 5 external USDt variants (usdt20/usdtrx/
    usdtbsc/usdtsol/usdtpolygon). Other Changelly assets aren't
    exposed because we don't offer swaps for them. Set
    `CHANGELLY_ALLOW_ALL_PAIRS=1` to bypass the filter.

    Returns:
        currencies: list of asset id strings (≤ 7 entries)
        count: number of entries
    """
    currencies = get_changelly_manager().list_currencies()
    return {"currencies": currencies, "count": len(currencies)}


def changelly_quote(
    external_network: str,
    direction: str = "send",
    amount_from: str | None = None,
    amount_to: str | None = None,
) -> dict[str, Any]:
    """Get a fixed-rate Changelly quote for a USDt-Liquid ↔ USDt-on-X swap.

    Provide exactly one of `amount_from` or `amount_to` (decimal strings).
    Direction implies which leg is the deposit:
      - "send": deposit USDt-Liquid, receive USDt on `external_network`
      - "receive": deposit USDt on `external_network`, receive USDt-Liquid

    Args:
        external_network: USDt network (one of: ethereum, tron, bsc, solana,
            polygon).
        direction: "send" or "receive". Default: "send".
        amount_from: amount the deposit side sends (decimal string).
        amount_to: amount the settle side receives (decimal string).

    Returns:
        Changelly's quote response: {id, result, amountFrom, amountTo,
        networkFee, min, max, expiredAt, ...}
    """
    if (amount_from is None) == (amount_to is None):
        raise ValueError(
            "Provide exactly one of amount_from or amount_to — not both, not neither."
        )
    from .changelly import LIQUID_USDT_ID, network_to_asset_id

    ext = network_to_asset_id(external_network)
    if direction == "send":
        from_asset, to_asset = LIQUID_USDT_ID, ext
    elif direction == "receive":
        from_asset, to_asset = ext, LIQUID_USDT_ID
    else:
        raise ValueError("direction must be 'send' or 'receive'")
    return get_changelly_manager().fixed_quote(
        from_asset, to_asset, amount_from=amount_from, amount_to=amount_to,
    )


def changelly_send(
    external_network: str,
    settle_address: str,
    amount_from: str | None = None,
    amount_to: str | None = None,
    wallet_name: str = "default",
    password: str | None = None,
    rate_id: str | None = None,
) -> dict[str, Any]:
    """Send USDt-Liquid out via a Changelly fixed-rate swap.

    Flow:
      1. Get a fixed-rate quote (skipped if `rate_id` supplied from a prior
         changelly_quote call).
      2. Create the fixed order; Changelly returns a Liquid deposit address.
      3. Broadcast the USDt-Liquid deposit from the local wallet.

    A refund address is set automatically — the wallet's own Liquid address,
    so a stuck order refunds back to source.

    Provide exactly one of `amount_from` or `amount_to`:
    - `amount_from`: USDt-Liquid to send; fees deducted from what recipient gets.
    - `amount_to`: what the recipient should receive; fees paid by the sender on top.

    Args:
        external_network: target USDt network (ethereum, tron, bsc, solana,
            polygon).
        settle_address: external chain address where the user receives.
        amount_from: USDt-Liquid to send (decimal string, e.g. "100").
        amount_to: amount recipient receives (decimal string). Mutually exclusive
            with amount_from.
        wallet_name: Liquid wallet to sign with.
        password: mnemonic decryption password (if encrypted at rest).
        rate_id: rate id from a prior changelly_quote call. Pass this to lock
            the previewed rate and avoid drift between quote and execution.

    Returns:
        order_id, deposit_hash (txid we broadcast), deposit_address,
        amount_from, amount_to, status, expires_at, track_url
    """
    if (amount_from is None) == (amount_to is None):
        raise ValueError("Provide exactly one of amount_from or amount_to")
    if amount_from is not None:
        _validate_positive_decimal_string(amount_from, "amount_from")
    if amount_to is not None:
        _validate_positive_decimal_string(amount_to, "amount_to")
    if not settle_address or not settle_address.strip():
        raise ValueError("settle_address cannot be empty")
    swap = get_changelly_manager().send_swap(
        external_network=external_network,
        amount_from=amount_from,
        amount_to=amount_to,
        settle_address=settle_address,
        wallet_name=wallet_name,
        password=password,
        rate_id=rate_id,
    )
    return swap.to_dict()


def changelly_receive(
    external_network: str,
    wallet_name: str = "default",
    external_refund_address: str | None = None,
    amount_from: str | None = None,
    amount_to: str | None = None,
) -> dict[str, Any]:
    """Receive USDt-Liquid via a Changelly variable-rate swap.

    Returns a deposit address on `external_network`. The external sender
    pays to it from any USDt-supporting wallet on that network; rate is set
    when the deposit confirms; Changelly settles to the wallet's Liquid
    address as USDt-Liquid.

    Provide exactly one of `amount_from` or `amount_to`:
    - `amount_from`: amount the external sender will deposit.
    - `amount_to`: amount to receive in the Liquid wallet.

    Args:
        external_network: source USDt network (ethereum, tron, bsc, solana,
            polygon).
        wallet_name: Liquid wallet to receive into.
        external_refund_address: STRONGLY RECOMMENDED — the deposit-chain
            address to refund to if the order fails. Without one a stuck
            order requires manual web UI intervention.
        amount_from: amount the external sender will deposit (decimal string,
            e.g. "50"). Mutually exclusive with amount_to.
        amount_to: amount to receive in the wallet (decimal string).
            Mutually exclusive with amount_from.

    Returns:
        order_id, deposit_address, settle_address, amount_from, status, track_url,
        qr_code_path (PNG QR of deposit_address, or qr_error on failure)
    """
    if (amount_from is None) == (amount_to is None):
        raise ValueError("Provide exactly one of amount_from or amount_to")
    if amount_from is not None:
        _validate_positive_decimal_string(amount_from, "amount_from")
    if amount_to is not None:
        _validate_positive_decimal_string(amount_to, "amount_to")
    swap = get_changelly_manager().receive_swap(
        external_network=external_network,
        wallet_name=wallet_name,
        external_refund_address=external_refund_address,
        amount_from=amount_from,
        amount_to=amount_to,
    )
    return _attach_deposit_qr(swap.to_dict(), "deposit_address")


def changelly_status(order_id: str) -> dict[str, Any]:
    """Check the status of a Changelly swap order.

    Returns the persisted record plus is_final / is_success / is_failed
    booleans so callers don't have to memorise the state machine. The
    Changelly state machine: new → waiting → confirming → exchanging →
    sending → finished (success). Failure terminals: failed, refunded,
    expired, overdue.

    Args:
        order_id: ID returned from changelly_send or changelly_receive.
    """
    return get_changelly_manager().status(order_id)


# ---------------------------------------------------------------------------
# WapuPay (Argentine direct-fiat payments)
# ---------------------------------------------------------------------------

def wapupay_exchange_rates() -> dict[str, Any]:
    """Get WapuPay's current exchange rates (e.g. USDT/ARS). Public — no login or key."""
    return get_wapupay_manager().exchange_rates()


def wapupay_quote(
    amount_ars: str,
    type: str = "fast_fiat_transfer",
    alias: str | None = None,
) -> dict[str, Any]:
    """Preview the USDT cost, fee, and rate for an ARS payment (no order created).

    Call this BEFORE `wapupay_create_order` to confirm the price with the user.
    If you pass the recipient `alias`, the response's `valid_cbu_alias` tells you
    whether the bank alias/CBU/CVU is valid before you commit to an order.

    Args:
        amount_ars: amount to pay in Argentine pesos (decimal string, e.g. "10000").
        type: transfer speed (default "fast_fiat_transfer"). Explain the trade-off
            to the user:
            - "fast_fiat_transfer" (default, higher fee): prioritized payment.
              Completes in ~10 minutes to 1 hour during daytime. NOT instant.
            - "fiat_transfer" (standard, lower fee): takes 3 to 12 hours. Recommend
              this ONLY when there is no rush, or when paying at night or on
              weekends — a payer will pick up the transaction the next
              day anyway, so the speed premium buys nothing.
        alias: recipient bank alias / CBU / CVU (optional; enables alias validation).

    Returns:
        usdt_amount, fee, total_amount, exchange_rate, valid_cbu_alias.
    """
    return get_wapupay_manager().quote(amount_ars, type, alias=alias)


def wapupay_create_order(
    amount_ars: str,
    alias: str,
    type: str = "fast_fiat_transfer",
    receiver_name: str | None = None,
    refund_address: str | None = None,
    wallet_name: str = "default",
) -> dict[str, Any]:
    """Create a WapuPay direct-fiat order and get a Liquid USDT funding address.

    Creates the payment tentative (freezing the quote) and immediately issues
    funding instructions. The order is persisted before funding, so if funding
    fails you get the order back with `funded=False` and can retry via
    `wapupay_fund_order` — no silent failure.

    The result includes `address_destination` (a Liquid address), `asset_id`
    (USDT on Liquid), `funding_amount_usdt`, `total_amount_usdt`,
    `total_funding_amount_base_units`, and `funding_expires_at`. Pay the TOTAL
    with `lw_send_asset` (amount = `total_funding_amount_base_units`, the exact
    total_amount_usdt in USDT base units; asset_id from the response); WapuPay
    then settles `amount_ars` ARS to the bank account. This tool never
    broadcasts a payment itself.

    Args:
        amount_ars: amount to pay in Argentine pesos (decimal string, e.g. "10000").
        alias: recipient bank alias / CBU / CVU.
        type: transfer speed (default "fast_fiat_transfer"). Explain the trade-off
            to the user before creating the order:
            - "fast_fiat_transfer" (default, higher fee): prioritized payment.
              Completes in ~10 minutes to 1 hour during daytime. NOT instant.
            - "fiat_transfer" (standard, lower fee): takes 3 to 12 hours. Recommend
              this ONLY when there is no rush, or when paying at night or on
              weekends — a payer will pick up the transaction the next
              day anyway, so the speed premium buys nothing.
        receiver_name: recipient name (optional).
        refund_address: Liquid mainnet address (lq1…/ex1…/VJL…) for a refund if funding
            cannot execute (optional); validated before the order is created.
        wallet_name: wallet you intend to fund from (recorded for tracking).

    Returns:
        The order record incl. tentative_id, status, address_destination,
        asset_id, funding_amount_usdt, total_amount_usdt,
        total_funding_amount_base_units, funding_expires_at, funded,
        pay_instructions, and qr_code_path (QR of the funding address).
    """
    result = get_wapupay_manager().create_order(
        amount_ars=amount_ars,
        alias=alias,
        transfer_type=type,
        receiver_name=receiver_name,
        refund_address=refund_address,
        wallet_name=wallet_name,
    )
    return _attach_deposit_qr(result, "address_destination")


def wapupay_fund_order(tentative_id: str) -> dict[str, Any]:
    """Issue (or re-issue) Liquid USDT funding instructions for an existing order.

    Use this to recover an order created without funding, or to re-fetch the
    funding address before it expires.

    Args:
        tentative_id: the order id from `wapupay_create_order`.

    Returns:
        Order record with address_destination, asset_id, funding amounts,
        funded, pay_instructions, and qr_code_path.
    """
    result = get_wapupay_manager().fund_order(tentative_id)
    return _attach_deposit_qr(result, "address_destination")


def wapupay_order_status(tentative_id: str) -> dict[str, Any]:
    """Check a WapuPay direct-fiat order's status (re-read from WapuPay).

    Returns is_final / is_success / is_failed booleans. Status machine:
    CREATED → FUNDING_ISSUED → EXECUTED (success). Terminals: EXPIRED,
    SETTLED_TO_BALANCE (USDT credited to WapuPay balance, payout not made),
    FAILED.

    Treat `is_final and not is_success` as NEEDS ATTENTION — SETTLED_TO_BALANCE
    is final but neither success nor failed (the user funded but the ARS payout
    didn't happen), so don't rely on is_failed alone to decide something went wrong.

    Args:
        tentative_id: the order id from `wapupay_create_order`.
    """
    return get_wapupay_manager().order_status(tentative_id)


def wapupay_orders() -> dict[str, Any]:
    """List locally-tracked WapuPay direct-fiat orders.

    These are local recovery records of orders this device created — distinct
    from `wapupay_transactions` (WapuPay's server-side view).
    Each record carries the involved txids
    (`funding_transaction_id`, `executed_transaction_id`) plus `status` /
    `tentative_id` so each stage can be tracked locally.
    """
    return {"orders": get_wapupay_manager().list_orders()}


def wapupay_transactions() -> dict[str, Any]:
    """List WapuPay transactions (scoped to the WapuPay account/key)."""
    result = get_wapupay_manager().transactions()
    return result if isinstance(result, dict) else {"transactions": result}


def wapupay_transaction(id: str) -> dict[str, Any]:
    """Get a single WapuPay transaction by id (UUID or numeric).

    Args:
        id: WapuPay transaction id (uuid or numeric).
    """
    return get_wapupay_manager().transaction(id)


def wapupay_spending_limit() -> dict[str, Any]:
    """Get the WapuPay account/key's monthly spending limit (USDT), based on KYC tier.

    Passes WapuPay's response through unchanged (typically the KYC tier plus the
    monthly limit and amount used, in USDT — exact field names per WapuPay).
    """
    return get_wapupay_manager().spending_limit()


def wapupay_provision_account(email: str) -> dict[str, Any]:
    """Provision a WapuPay API key via your JAN3 account, so the WapuPay tools work.

    Use this when the user wants to use WapuPay but has no WapuPay API key set.
    It calls the AQUA backend (authorized with the JAN3 login session for
    `email`) to create the user's WapuPay sub-user and obtain their API key, then
    stores the key locally (0o600) so every other `wapupay_*` tool can use it
    automatically. The raw key is never returned — only a masked preview.

    Requires a prior JAN3 login for `email` (either flow): call `jan3_login` then
    `jan3_verify`, or `jan3_login_start` then `jan3_login_complete`.

    Args:
        email: the logged-in JAN3 account whose session authorizes the call.

    The AQUA backend issues a fresh key on EVERY call and invalidates any key
    previously issued for the account (no grace period). So this only calls the
    backend when no key is configured yet: if one already exists (env var or
    stored) it returns `already_configured` without touching the backend, so a
    stray call can never invalidate a working key.

    Returns:
        When the backend was called: provisioned=True, key_preview, created_at,
        message, and a `warning` noting the backend always rotates. When a key was
        already configured: already_configured=True, source ("env"|"stored"),
        key_preview, message.
    """
    return get_wapupay_manager().provision_account(email)


# ---------------------------------------------------------------------------
# SideShift (custodial cross-chain swaps via sideshift.ai)
# ---------------------------------------------------------------------------


def sideshift_list_coins() -> dict[str, Any]:
    """List the SideShift coin/network identifiers enabled for swaps.

    Filtered server-side to the curated allowlist (USDt across
    ethereum/tron/bsc/solana/polygon/liquid, plus mainchain BTC) so the
    response stays small and only surfaces pairs we actually support. Each
    kept entry has `coin`, `name`, `networks` (intersected with the
    allowlist), `hasMemo`, `fixedOnly`/`variableOnly`, and a pruned
    `tokenDetails`. Set `SIDESHIFT_ALLOW_ALL_NETWORKS=1` to bypass the
    filter.

    Returns:
        coins: list of {coin, name, networks, hasMemo, ...}
        count: number of entries
    """
    coins = get_sideshift_manager().list_coins()
    return {"coins": coins, "count": len(coins)}


def sideshift_pair_info(
    from_coin: str,
    from_network: str,
    to_coin: str,
    to_network: str,
    amount: str | None = None,
) -> dict[str, Any]:
    """Get rate / min / max for a SideShift pair.

    Args:
        from_coin: Deposit coin ticker (case-insensitive, e.g. "USDT")
        from_network: Deposit network (case-insensitive, e.g. "tron", "liquid", "bitcoin", "ethereum")
        to_coin: Settle coin ticker
        to_network: Settle network
        amount: Optional reference amount in deposit-coin units (decimal string).
            Default reference is approximately $500 USD if omitted.

    Returns:
        rate (string), min (string), max (string), depositCoin, settleCoin,
        depositNetwork, settleNetwork
    """
    return get_sideshift_manager().pair_info(
        from_coin, from_network, to_coin, to_network, amount=amount
    )


def sideshift_quote(
    deposit_coin: str,
    deposit_network: str,
    settle_coin: str,
    settle_network: str,
    deposit_amount: str | None = None,
    settle_amount: str | None = None,
) -> dict[str, Any]:
    """Request a fixed-rate quote (~15 minute TTL).

    Provide exactly one of `deposit_amount` (user is sending X) or
    `settle_amount` (user wants to receive exactly X). Amounts are decimal
    strings to preserve precision.

    Returns:
        SideShift's quote response: {id, expiresAt, depositAmount,
        settleAmount, rate, ...}.

    Use this BEFORE `sideshift_send` to confirm the quote with the user.
    """
    return get_sideshift_manager().quote(
        deposit_coin=deposit_coin,
        deposit_network=deposit_network,
        settle_coin=settle_coin,
        settle_network=settle_network,
        deposit_amount=deposit_amount,
        settle_amount=settle_amount,
    )


def sideshift_send(
    deposit_coin: str,
    deposit_network: str,
    settle_coin: str,
    settle_network: str,
    settle_address: str,
    deposit_amount: str | None = None,
    settle_amount: str | None = None,
    wallet_name: str = "default",
    password: str | None = None,
    liquid_asset_id: str | None = None,
    settle_memo: str | None = None,
    refund_memo: str | None = None,
    quote_id: str | None = None,
) -> dict[str, Any]:
    """Send funds from our wallet via a SideShift fixed-rate shift.

    Flow:
      1. Get a fixed-rate quote (matches the agreed amounts).
      2. Create the shift; SideShift returns a deposit address on the deposit chain.
      3. Broadcast the deposit from the local wallet (via lw_send / btc_send / lw_send_asset).

    The deposit chain MUST be one of {bitcoin, liquid} — those are the only
    chains we can sign on. Both legs (deposit + settle) must also be in the
    curated pair allowlist mirroring AQUA Flutter: USDt on
    {ethereum, tron, bsc, solana, polygon, liquid} or BTC on bitcoin.
    L-BTC (btc-liquid) is excluded — use SideSwap for L-BTC ↔ external.
    Set `SIDESHIFT_ALLOW_ALL_NETWORKS=1` to bypass.

    A refund address is always set automatically: the wallet's own deposit-
    chain address, so a stuck shift refunds back to the source.

    Args:
        deposit_coin: e.g. "btc" (for L-BTC use coin="btc", network="liquid")
        deposit_network: "bitcoin" | "liquid"
        settle_coin: any SideShift coin ticker
        settle_network: any SideShift network
        settle_address: where SideShift sends the converted asset
        deposit_amount / settle_amount: provide exactly one, decimal strings
        wallet_name: local wallet to sign with
        password: mnemonic decryption password (if encrypted)
        liquid_asset_id: optional hex asset id override. When omitted and
            depositing a non-L-BTC Liquid asset, auto-resolved from
            `deposit_coin` against the known Liquid asset registry
            (USDt, DePix, JPYS, EURx, MEX). Pass to override the registry.
        settle_memo / refund_memo: required for memo networks (TON, BNB, etc.)
        quote_id: optional fixed-rate quote id from a prior `sideshift_quote`
            call. Pass `preview["id"]` after the user confirms the preview to
            ensure the shift executes at the rate the user just saw. Without
            it, sideshift_send fetches a fresh quote — fine for non-interactive
            flows, but the rate may have moved since any earlier preview.

    Returns:
        shift_id, deposit_hash (txid we broadcast), deposit_address,
        deposit_amount, settle_amount, rate, status, expires_at
    """
    resolved_asset_id = resolve_liquid_asset_id(
        deposit_coin, deposit_network, explicit_id=liquid_asset_id,
    )
    shift = get_sideshift_manager().send_shift(
        deposit_coin=deposit_coin,
        deposit_network=deposit_network,
        settle_coin=settle_coin,
        settle_network=settle_network,
        settle_address=settle_address,
        deposit_amount=deposit_amount,
        settle_amount=settle_amount,
        wallet_name=wallet_name,
        password=password,
        liquid_asset_id=resolved_asset_id,
        settle_memo=settle_memo,
        refund_memo=refund_memo,
        quote_id=quote_id,
    )
    return shift.to_dict()


def sideshift_receive(
    deposit_coin: str,
    deposit_network: str,
    settle_coin: str,
    settle_network: str,
    wallet_name: str = "default",
    external_refund_address: str | None = None,
    external_refund_memo: str | None = None,
    settle_memo: str | None = None,
) -> dict[str, Any]:
    """Receive into our wallet via a SideShift variable-rate shift.

    SideShift returns a deposit address on the deposit chain. The user (or
    external sender) sends to that address from any wallet/chain. The rate
    is set when the deposit confirms; SideShift settles to the wallet's
    Liquid or Bitcoin address.

    The settle chain MUST be one of {bitcoin, liquid} — those are the only
    chains we hold addresses for. Both legs (deposit + settle) must also be
    in the curated pair allowlist mirroring AQUA Flutter: USDt on
    {ethereum, tron, bsc, solana, polygon, liquid} or BTC on bitcoin.
    Set `SIDESHIFT_ALLOW_ALL_NETWORKS=1` to bypass.

    Args:
        deposit_coin: any SideShift coin (e.g. "USDT")
        deposit_network: any SideShift network (e.g. "tron", "ethereum")
        settle_coin: "btc" or "usdt" (for Liquid: settle_network="liquid"; for Bitcoin mainchain: settle_network="bitcoin")
        settle_network: "bitcoin" | "liquid"
        wallet_name: local wallet to receive into
        external_refund_address: STRONGLY RECOMMENDED — where SideShift
            refunds if the deposit fails. Without one a stuck shift requires
            manual web UI intervention.

    Returns:
        shift_id, deposit_address, deposit_min, deposit_max, deposit_memo
        (if applicable), settle_address, status, expires_at, qr_code_path
        (qr_code_path is a PNG QR of deposit_address, or qr_error on failure)
    """
    shift = get_sideshift_manager().receive_shift(
        deposit_coin=deposit_coin,
        deposit_network=deposit_network,
        settle_coin=settle_coin,
        settle_network=settle_network,
        wallet_name=wallet_name,
        external_refund_address=external_refund_address,
        external_refund_memo=external_refund_memo,
        settle_memo=settle_memo,
    )
    result = _attach_deposit_qr(shift.to_dict(), "deposit_address")
    # Memo-based chains (e.g. BNB) require a deposit memo/tag alongside the
    # address. The QR encodes ONLY the address, so scanning it would silently
    # drop the memo — sending without it can permanently lose funds. Flag it.
    if result.get("deposit_memo") and "qr_code_path" in result:
        result["qr_warning"] = (
            "QR encodes the deposit address only. This network also requires a "
            f"deposit memo ({result['deposit_memo']}) that the QR does NOT include — "
            "it must be entered manually when sending. Sending without the memo can "
            "cause permanent loss of funds."
        )
    return result


def sideshift_status(shift_id: str) -> dict[str, Any]:
    """Check the status of a SideShift shift order.

    Pings SideShift, refreshes the persisted record, and returns the latest
    state. Status values (lowercase): waiting, pending, processing, settling,
    settled, refund, refunding, refunded, expired, review, multiple.

    Returns the full shift record plus `is_final`, `is_success`, `is_failed`
    so callers don't need to memorise the state machine.

    Args:
        shift_id: ID returned from sideshift_send or sideshift_receive
    """
    return get_sideshift_manager().status(shift_id)


def sideshift_recommend(
    from_coin: str,
    from_network: str,
    to_coin: str,
    to_network: str,
) -> dict[str, Any]:
    """Recommend SideSwap vs SideShift for a cross-asset conversion.

    SideSwap is preferred when both legs are on Bitcoin or Liquid (atomic /
    near-trustless, lower fees). SideShift is the fallback when at least one
    leg is on a non-Liquid chain (Ethereum, Tron, etc.).

    Args:
        from_coin: deposit coin ticker (case-insensitive)
        from_network: deposit network (e.g. "tron", "liquid")
        to_coin: settle coin ticker
        to_network: settle network

    Returns:
        recommendation ("sideswap" | "sideshift" | "none"), reason, plus the
        input fields. "none" is returned when both legs are the same
        (coin, network) — there's nothing to swap.
    """
    from .sideshift import recommend_shift_or_swap

    return recommend_shift_or_swap(from_coin, from_network, to_coin, to_network)
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
    if network not in ("mainnet", "testnet"):
        raise ValueError(f"Unknown network: {network}")
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


def pix_status(swap_id: str) -> dict[str, Any]:
    """Check the status of a Pix → DePix deposit.

    Eulen pushes DePix automatically once the Pix payment settles, so there is
    no claim step. Status values from the upstream API: pending, depix_sent,
    under_review, canceled, error, refunded, expired.

    Args:
        swap_id: Swap ID returned from pix_receive.

    Returns:
        swap_id, status, amount_cents, amount_brl, wallet_name, depix_address,
        network, message; optionally blockchain_txid, payer_name, expiration,
        warning.
    """
    manager = get_pix_manager()
    return manager.get_deposit_status(swap_id)


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
    min_recv_amount: int | None = None,
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
        min_recv_amount: Optional floor on the dealer's recv_amount, in sats.
            When set, the swap is rejected before signing if the mkt::*
            quote returns a recv_amount strictly less than this value. The
            CLI passes the recv_amount the user just confirmed in the
            preview, so a rate move between preview and execution can no
            longer surprise the user with a worse settlement.
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
        min_recv_amount=min_recv_amount,
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


def qr_decode(image_path: str) -> dict[str, Any]:
    """Decode a QR code from an image file and return the raw string content."""
    content = decode_qr(image_path)
    return {"content": content}


# ---------------------------------------------------------------------------
# JAN3 account management (multi-account login + sessions + paid captchaless login)
# ---------------------------------------------------------------------------


def jan3_login(email: str, language: str = "en") -> dict[str, Any]:
    """Default JAN3 login: the backend emails a one-time code (free, email-OTP).

    The user authenticates with their JAN3 account email; the backend sends a
    6-digit OTP to that address. Follow up with `jan3_verify` passing the code.
    This is the preferred login path; use `jan3_login_start` (paid captchaless)
    only as a fallback when this flow isn't available for the account.

    Args:
        email: the user's JAN3 account email address.
        language: OTP email language (en/es/pt). Default: en.

    Returns:
        email, message, otp_sent_to, next_step (and otp_code only on non-prod).
    """
    return get_jan3_manager().login(email, language=language)


def jan3_verify(
    email: str,
    otp_code: str,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Verify the OTP from `jan3_login` and persist the JAN3 session for `email`.

    On success the JWT session is persisted locally per-email (0o600) so
    subsequent JAN3/WapuPay calls don't re-prompt.

    Args:
        email: the same email used in `jan3_login`.
        otp_code: the 6-digit code from the email.
        fingerprint: optional device fingerprint string.

    Returns:
        email, logged_in, captcha_exempt (False for this free flow), message,
        access_token_preview, and next_step — which cues you to offer the user
        the Lightning Address opt-in (jan3_enable_lightning_address).
    """
    return get_jan3_manager().verify(
        email, otp_code, fingerprint=fingerprint, captcha_exempt=False
    )


def jan3_login_start(
    email: str,
    wallet_name: str = "default",
    password: str | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Fallback JAN3 login (paid captchaless), step 1 — for accounts that can't
    use the free `jan3_login` email-OTP flow.

    Fetches the AQUA vault payment address and the CAPTCHALESS_LOGIN price,
    crafts a signed L-BTC tx funding the vault for that exact amount, and
    POSTs it to /api/v2/auth/login/. The server broadcasts the tx and emails
    an OTP to ``email``. After receiving the OTP, call ``jan3_login_complete``.

    Args:
        email: JAN3 account email (will receive the OTP).
        wallet_name: Liquid wallet used to fund the captchaless login payment.
        password: Decrypts the wallet mnemonic if encrypted at rest.
        language: 2-letter language code for the OTP email (default "en").

    Returns:
        message, payment_address, amount_sats, asset_ticker, otp_sent_to,
        otp_code (only when server has EMAIL_BASED_OTP off — dev/test),
        next_step.
    """
    return get_jan3_manager().request_login(
        email=email,
        wallet_name=wallet_name,
        password=password,
        language=language,
    )


def jan3_login_complete(
    email: str,
    otp_code: str,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Step 2 of the paid captchaless login. Exchanges the OTP for JWT tokens and
    persists the per-email session to ``~/.aqua/jan3/{email}.json`` (0o600).

    Args:
        email: JAN3 account email (must match the one used in jan3_login_start).
        otp_code: The 6-digit OTP from the verification email.
        fingerprint: Optional device fingerprint string.

    Returns:
        email, logged_in, captcha_exempt (True for this paid flow), message,
        access_token_preview, and next_step — which cues you to offer the user
        the Lightning Address opt-in (jan3_enable_lightning_address). Full tokens are
        NEVER echoed.
    """
    return get_jan3_manager().verify(
        email, otp_code, fingerprint=fingerprint, captcha_exempt=True
    )


def jan3_session_info(email: str) -> dict[str, Any]:
    """Return status + non-sensitive info about the persisted JAN3 session for
    `email` (refreshes the token if expired). Does NOT echo full tokens.
    """
    return get_jan3_manager().session_status(email)


def jan3_list_sessions() -> dict[str, Any]:
    """List all persisted JAN3 sessions (metadata only — no tokens)."""
    manager = get_jan3_manager()
    sessions = manager.list_sessions()
    return {
        "sessions": [
            {
                "email": s.email,
                "base_url": s.base_url,
                "created_at": s.created_at,
                "refreshed_at": s.refreshed_at,
                "captcha_exempt": s.captcha_exempt,
            }
            for s in sessions
        ]
    }


def jan3_logout(email: str) -> dict[str, Any]:
    """Delete the persisted JAN3 session for `email`. Idempotent."""
    return get_jan3_manager().logout(email)


def jan3_user_info(
    email: str,
    wallet_name: str = "default",
) -> dict[str, Any]:
    """Get the AQUA account profile for `email` (Lightning Address status included).

    ``ln_username`` is the full Lightning Address verbatim from the backend.
    Auto-tops-up the LN-address pool when active (result under ``ln_address_pool``).
    The top-up derives Liquid addresses from the wallet descriptor, so no password
    is needed even for a wallet encrypted at rest.

    Args:
        email: the JAN3 account email.
        wallet_name: Liquid wallet whose addresses back the LN-address pool.
    """
    return get_jan3_manager().get_user(email, wallet_name=wallet_name)


def jan3_enable_lightning_address(
    email: str,
    enabled: bool,
    wallet_name: str = "default",
) -> dict[str, Any]:
    """Enable or disable the user's Lightning Address for `email`.

    On enable, registers Liquid receive addresses and populates the pool
    (result under ``ln_address_pool``). Addresses are derived from the wallet descriptor.

    Args:
        email: the JAN3 account email.
        enabled: True to opt in (and populate the pool), False to opt out.
        wallet_name: Liquid wallet whose addresses back the pool.
    """
    return get_jan3_manager().ln_address_toggle(
        email, enabled, wallet_name=wallet_name
    )


def jan3_rebind_wallet(
    email: str,
    wallet_name: str = "default",
    confirm: bool = False,
) -> dict[str, Any]:
    """Re-bind Lightning Address delivery to a different local wallet (DESTRUCTIVE).

    Two-step: ``confirm=False`` returns a preview (``ln_username``, fingerprint diff,
    ``warning``); ``confirm=True`` executes after user consent.

    Args:
        email: the JAN3 account email (identifies the account/session).
        wallet_name: the local Liquid wallet to bind delivery to.
        confirm: False (default) previews without mutating; True executes.
    """
    return get_jan3_manager().rebind_wallet(
        email, wallet_name=wallet_name, confirm=confirm
    )


def jan3_ln_check_username(email: str, ln_username: str) -> dict[str, Any]:
    """Check whether a Lightning username is free before buying it.

    Args:
        email: the JAN3 account email (session used to authenticate the check).
        ln_username: the desired username (local part, before the @domain).
    """
    return get_jan3_manager().ln_username_available(email, ln_username)


def jan3_purchase_ln_username(
    email: str,
    ln_username: str,
    wallet_name: str = "default",
    password: str | None = None,
    asset: str = "L-BTC",
    confirm: bool = False,
) -> dict[str, Any]:
    """Purchase / update the Lightning username for a JAN3 account (on-chain).

    Two-step so the user approves the price before any spend:

    * ``confirm=False`` (default) returns a quote — ``requires_confirmation``,
      ``display_amount`` (e.g. ``"2000 Sats"`` or ``"1.50 USDT"``),
      ``amount_base_units``, ``amount``, ``expires_at`` — WITHOUT signing.
    * ``confirm=True`` funds and submits the on-chain payment in ``asset``.

    Args:
        email: the JAN3 account email.
        ln_username: the desired username (local part, before the @domain).
        wallet_name: Liquid wallet used to fund the purchase.
        password: decrypts the wallet mnemonic if encrypted at rest (confirm only).
        asset: funding asset ticker — "L-BTC" or "USDt" (default L-BTC).
        confirm: False previews the price; True pays.

    Returns:
        Quote (confirm=False): requires_confirmation, ln_username, asset_ticker,
        amount_base_units, amount, display_amount, expires_at, address, message.
        Receipt (confirm=True): payment_id, status, txid, plus the same amount
        fields.
    """
    return get_jan3_manager().purchase_ln_username(
        email,
        ln_username,
        wallet_name=wallet_name,
        password=password,
        asset=asset,
        confirm=confirm,
    )


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
    "lw_sweep": lw_sweep,
    "lw_tx_status": lw_tx_status,
    "lw_list_wallets": lw_list_wallets,
    "lw_list_assets": lw_list_assets,
    "delete_wallet": delete_wallet,
    "btc_balance": btc_balance,
    "btc_address": btc_address,
    "btc_transactions": btc_transactions,
    "btc_send": btc_send,
    "btc_sweep": btc_sweep,
    "btc_import_descriptor": btc_import_descriptor,
    "btc_export_descriptor": btc_export_descriptor,
    "unified_balance": unified_balance,
    "lightning_receive": lightning_receive,
    "lightning_send": lightning_send,
    "lightning_transaction_status": lightning_transaction_status,
    "lightning_decode": lightning_decode,
    "pix_receive": pix_receive,
    "pix_status": pix_status,
    "changelly_list_currencies": changelly_list_currencies,
    "changelly_quote": changelly_quote,
    "changelly_send": changelly_send,
    "changelly_receive": changelly_receive,
    "changelly_status": changelly_status,
    "sideshift_list_coins": sideshift_list_coins,
    "sideshift_pair_info": sideshift_pair_info,
    "sideshift_quote": sideshift_quote,
    "sideshift_send": sideshift_send,
    "sideshift_receive": sideshift_receive,
    "sideshift_status": sideshift_status,
    "sideshift_recommend": sideshift_recommend,
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
    "wapupay_exchange_rates": wapupay_exchange_rates,
    "wapupay_quote": wapupay_quote,
    "wapupay_create_order": wapupay_create_order,
    "wapupay_fund_order": wapupay_fund_order,
    "wapupay_order_status": wapupay_order_status,
    "wapupay_orders": wapupay_orders,
    "wapupay_transactions": wapupay_transactions,
    "wapupay_transaction": wapupay_transaction,
    "wapupay_spending_limit": wapupay_spending_limit,
    "wapupay_provision_account": wapupay_provision_account,
    "jan3_login": jan3_login,
    "jan3_verify": jan3_verify,
    "jan3_login_start": jan3_login_start,
    "jan3_login_complete": jan3_login_complete,
    "jan3_session_info": jan3_session_info,
    "jan3_list_sessions": jan3_list_sessions,
    "jan3_logout": jan3_logout,
    "jan3_user_info": jan3_user_info,
    "jan3_enable_lightning_address": jan3_enable_lightning_address,
    "jan3_rebind_wallet": jan3_rebind_wallet,
    "jan3_ln_check_username": jan3_ln_check_username,
    "jan3_purchase_ln_username": jan3_purchase_ln_username,
    "qr_decode": qr_decode,
}
