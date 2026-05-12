"""MCP server for AQUA."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env")

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)

from . import __version__
from .tools import TOOLS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


TOOL_SCHEMAS = {
    "lw_generate_mnemonic": {
        "description": "Generate a new BIP39 seed phrase for creating a Liquid wallet",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "lw_import_mnemonic": {
        "description": "Import a wallet from a BIP39 seed phrase (creates both Liquid and Bitcoin wallets from the same seed)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mnemonic": {
                    "type": "string",
                    "description": "BIP39 mnemonic phrase (12 words)",
                },
                "wallet_name": {
                    "type": "string",
                    "description": "Name for the wallet",
                    "default": "default",
                },
                "network": {
                    "type": "string",
                    "description": "Network to use",
                    "enum": ["mainnet", "testnet"],
                    "default": "mainnet",
                },
                "password": {
                    "type": "string",
                    "description": "Optional password to encrypt the mnemonic at rest (NOT a BIP39 passphrase)",
                },
            },
            "required": ["mnemonic"],
        },
    },
    "lw_import_descriptor": {
        "description": "Import a watch-only wallet from a CT descriptor",
        "inputSchema": {
            "type": "object",
            "properties": {
                "descriptor": {
                    "type": "string",
                    "description": "CT descriptor string",
                },
                "wallet_name": {
                    "type": "string",
                    "description": "Name for the wallet",
                },
                "network": {
                    "type": "string",
                    "description": "Network to use",
                    "enum": ["mainnet", "testnet"],
                    "default": "mainnet",
                },
            },
            "required": ["descriptor", "wallet_name"],
        },
    },
    "lw_export_descriptor": {
        "description": "Export the CT descriptor for a wallet (for watch-only import elsewhere)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                    "default": "default",
                },
            },
        },
    },
    "lw_balance": {
        "description": "Get wallet balance for all assets (L-BTC, USDT, etc.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                    "default": "default",
                },
            },
        },
    },
    "lw_address": {
        "description": "Generate a receive address",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                    "default": "default",
                },
                "index": {
                    "type": "integer",
                    "description": "Specific address index (optional)",
                },
            },
        },
    },
    "lw_transactions": {
        "description": "Get transaction history",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                    "default": "default",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of transactions",
                    "default": 10,
                },
            },
        },
    },
    "lw_send": {
        "description": "Send L-BTC to an address",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                },
                "address": {
                    "type": "string",
                    "description": "Destination Liquid address",
                },
                "amount": {
                    "type": "integer",
                    "description": "Amount in Satoshis",
                },
                "password": {
                    "type": "string",
                    "description": "Password to decrypt mnemonic (if encrypted at rest)",
                },
            },
            "required": ["wallet_name", "address", "amount"],
        },
    },
    "lw_send_asset": {
        "description": "Send a Liquid asset (USDT, etc.) to an address",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                },
                "address": {
                    "type": "string",
                    "description": "Destination Liquid address",
                },
                "amount": {
                    "type": "integer",
                    "description": "Amount in Satoshis",
                },
                "asset_id": {
                    "type": "string",
                    "description": "Asset ID (hex string)",
                },
                "password": {
                    "type": "string",
                    "description": "Password to decrypt mnemonic (if encrypted at rest)",
                },
            },
            "required": ["wallet_name", "address", "amount", "asset_id"],
        },
    },
    "lw_tx_status": {
        "description": "Get the status of a Liquid transaction. Accepts a txid or a Blockstream explorer URL (e.g. https://blockstream.info/liquid/tx/abc123...)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tx": {
                    "type": "string",
                    "description": "Transaction ID (64-char hex) or Blockstream explorer URL",
                },
            },
            "required": ["tx"],
        },
    },
    "lw_list_wallets": {
        "description": "List all wallets",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "lw_list_assets": {
        "description": (
            "List known Liquid assets (asset_id, ticker, name, precision). "
            "Use this to resolve asset IDs for lw_send_asset without a prior balance query."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "network": {
                    "type": "string",
                    "enum": ["mainnet", "testnet"],
                    "description": "Which asset registry to list",
                    "default": "mainnet",
                },
            },
        },
    },
    "delete_wallet": {
        "description": "Delete a wallet and all its cached data. IMPORTANT: The agent MUST check balances and ask for user confirmation before calling this tool. Use the 'delete_wallet' prompt for the safe workflow.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet to delete",
                },
            },
            "required": ["wallet_name"],
        },
    },
    "btc_balance": {
        "description": "Get Bitcoin wallet balance in Satoshis",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                    "default": "default",
                },
            },
        },
    },
    "btc_address": {
        "description": "Generate a Bitcoin receive address (bc1...)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                    "default": "default",
                },
                "index": {
                    "type": "integer",
                    "description": "Specific address index (optional)",
                },
            },
        },
    },
    "btc_transactions": {
        "description": "Get Bitcoin transaction history",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                    "default": "default",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of transactions",
                    "default": 10,
                },
            },
        },
    },
    "btc_send": {
        "description": "Send BTC to an address",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                },
                "address": {
                    "type": "string",
                    "description": "Destination Bitcoin address (bc1...)",
                },
                "amount": {
                    "type": "integer",
                    "description": "Amount in Satoshis",
                },
                "fee_rate": {
                    "type": "integer",
                    "description": "Optional fee rate in Sat/vB",
                },
                "password": {
                    "type": "string",
                    "description": "Password to decrypt mnemonic (if encrypted at rest)",
                },
            },
            "required": ["wallet_name", "address", "amount"],
        },
    },
    "btc_import_descriptor": {
        "description": (
            "Import a watch-only Bitcoin wallet from a BIP84 descriptor. "
            "ONLY imports Bitcoin — to monitor the same seed's Liquid wallet, "
            "the user must separately import its CT descriptor with "
            "lw_import_descriptor (different derivation path + SLIP-77 key)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "descriptor": {
                    "type": "string",
                    "description": "BIP84 external descriptor (with or without [fp/path] prefix)",
                },
                "wallet_name": {
                    "type": "string",
                    "description": "Wallet name (may add Bitcoin to an existing Liquid-only wallet)",
                },
                "network": {
                    "type": "string",
                    "enum": ["mainnet", "testnet"],
                    "default": "mainnet",
                    "description": "Network to use",
                },
                "change_descriptor": {
                    "type": "string",
                    "description": (
                        "Optional change descriptor; auto-derived from external "
                        "if omitted (replaces /0/* with /1/*)"
                    ),
                },
            },
            "required": ["descriptor", "wallet_name"],
        },
    },
    "btc_export_descriptor": {
        "description": (
            "Export the Bitcoin BIP84 descriptors + xpub for a wallet. "
            "ONLY returns Bitcoin — for the Liquid CT descriptor (different "
            "derivation path + SLIP-77 blinding key), use lw_export_descriptor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                    "default": "default",
                },
            },
        },
    },
    "unified_balance": {
        "description": "Get balance for both Bitcoin and Liquid networks (unified wallet)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Name of the wallet",
                    "default": "default",
                },
            },
        },
    },
    "lightning_receive": {
        "description": "Generate a Lightning invoice to receive L-BTC into a Liquid wallet (~1-2 min after payment). Limits: 100 – 25,000,000 Sats.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "integer",
                    "description": "Amount in Satoshis (100 – 25,000,000)",
                },
                "wallet_name": {
                    "type": "string",
                    "description": "Liquid wallet to receive into",
                    "default": "default",
                },
                "password": {
                    "type": "string",
                    "description": "Password to decrypt mnemonic (if encrypted at rest)",
                },
            },
            "required": ["amount"],
        },
    },
    "lightning_send": {
        "description": "Pay a Lightning invoice or Lightning Address using L-BTC from a Liquid wallet (reverse submarine swap). Fees: ~0.1% + miner fees. Limits: 100 – 25,000,000 Sats.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "invoice": {
                    "type": "string",
                    "description": "BOLT11 Lightning invoice (lnbc.../lntb...) OR Lightning Address (user@domain.com)",
                },
                "wallet_name": {
                    "type": "string",
                    "description": "Liquid wallet to pay from",
                    "default": "default",
                },
                "password": {
                    "type": "string",
                    "description": "Password to decrypt mnemonic (if encrypted at rest)",
                },
                "amount_sats": {
                    "type": "integer",
                    "description": "Amount in sats. Required when invoice is a Lightning Address; optional for BOLT11 (must match if supplied).",
                },
            },
            "required": ["invoice"],
        },
    },
    "lightning_transaction_status": {
        "description": "Check the status of a Lightning swap (send or receive). For receive: auto-claims L-BTC when settled. For send: checks Boltz status and retrieves preimage when claimed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "swap_id": {
                    "type": "string",
                    "description": "Swap ID returned from lightning_receive or lightning_send",
                },
            },
            "required": ["swap_id"],
        },
    },
    "pix_receive": {
        "description": "Mint a Pix charge (Brazil) that pays out DePix (BRL stablecoin on Liquid) to the named wallet's next address. Returns the Pix Copia e Cola string and a hosted QR image URL — the user pays from their banking app. Amount is in BRL CENTS (100 = R$1.00). Requires EULEN_API_TOKEN env var.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount_cents": {
                    "type": "integer",
                    "description": "Amount in BRL cents (100 = R$1.00). NOT reais — be precise about the unit.",
                },
                "wallet_name": {
                    "type": "string",
                    "description": "Liquid wallet to receive DePix into",
                    "default": "default",
                },
                "password": {
                    "type": "string",
                    "description": "Accepted for symmetry; not currently used by Pix receive (no signing needed).",
                },
            },
            "required": ["amount_cents"],
        },
    },
    "pix_status": {
        "description": "Check the status of a Pix → DePix deposit. Status values: pending, depix_sent, under_review, canceled, error, refunded, expired. Eulen delivers DePix automatically — no claim step.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "swap_id": {
                    "type": "string",
                    "description": "Swap ID returned from pix_receive",
                },
            },
            "required": ["swap_id"],
        },
    },
    "changelly_list_currencies": {
        "description": (
            "List the currencies Changelly supports (Changelly's own asset id format). "
            "Useful for discovery; the agentic-aqua surface only enables the curated "
            "USDt-Liquid ↔ USDt-on-{ethereum,tron,bsc,solana,polygon,ton} pairs for "
            "actual swaps."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    "changelly_quote": {
        "description": (
            "Get a fixed-rate Changelly quote for a USDt-Liquid ↔ USDt-on-X swap. "
            "Provide exactly one of deposit_amount or settle_amount as a decimal string. "
            "Use BEFORE changelly_send to confirm the price with the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "external_network": {
                    "type": "string",
                    "enum": ["ethereum", "tron", "bsc", "solana", "polygon", "ton"],
                    "description": "USDt network on the non-Liquid side",
                },
                "direction": {
                    "type": "string",
                    "enum": ["send", "receive"],
                    "default": "send",
                    "description": "'send' = deposit USDt-Liquid; 'receive' = deposit USDt on external chain",
                },
                "amount_from": {"type": "string", "description": "Deposit-side amount (decimal string)"},
                "amount_to": {"type": "string", "description": "Settle-side amount (decimal string)"},
            },
            "required": ["external_network"],
        },
    },
    "changelly_send": {
        "description": (
            "Send USDt-Liquid out via a Changelly fixed-rate swap. Gets a quote, "
            "creates the order, and broadcasts the deposit from the local wallet. "
            "Refund address is set automatically (the wallet's own Liquid address). "
            "ALWAYS call changelly_quote first and confirm the price with the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "external_network": {
                    "type": "string",
                    "enum": ["ethereum", "tron", "bsc", "solana", "polygon", "ton"],
                    "description": "Target USDt network",
                },
                "settle_address": {"type": "string", "description": "External chain address to receive USDt at"},
                "amount_from": {"type": "string", "description": "USDt-Liquid to send (decimal string, e.g. '100')"},
                "wallet_name": {"type": "string", "default": "default"},
                "password": {"type": "string", "description": "Password to decrypt mnemonic (if encrypted at rest)"},
                "rate_id": {"type": "string", "description": "Rate id from a prior changelly_quote call — pass this to lock the previewed rate and avoid drift"},
            },
            "required": ["external_network", "settle_address", "amount_from"],
        },
    },
    "changelly_receive": {
        "description": (
            "Receive USDt-Liquid via a Changelly variable-rate swap. Returns a "
            "deposit address on the source chain — the external sender pays to it. "
            "Settles to the wallet's Liquid address as USDt-Liquid. STRONGLY RECOMMEND "
            "passing external_refund_address."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "external_network": {
                    "type": "string",
                    "enum": ["ethereum", "tron", "bsc", "solana", "polygon", "ton"],
                    "description": "Source USDt network the external sender pays from",
                },
                "wallet_name": {"type": "string", "default": "default"},
                "external_refund_address": {
                    "type": "string",
                    "description": "Source-chain refund address (strongly recommended)",
                },
                "amount_from": {
                    "type": "string",
                    "description": "Amount the external sender will deposit (decimal string, e.g. '50')",
                },
            },
            "required": ["external_network", "amount_from"],
        },
    },
    "changelly_status": {
        "description": (
            "Check the status of a Changelly swap order. Returns is_final / "
            "is_success / is_failed booleans. State machine: new → waiting → "
            "confirming → exchanging → sending → finished. Failure: failed, "
            "refunded, expired, overdue."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "ID from changelly_send or changelly_receive"},
            },
            "required": ["order_id"],
        },
    },
    "sideswap_server_status": {
        "description": (
            "Fetch SideSwap server status: live fees, minimum amounts, and "
            "hot-wallet balances. Call this BEFORE recommending a peg or swap "
            "so values reflect current SideSwap state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "network": {
                    "type": "string",
                    "enum": ["mainnet", "testnet"],
                    "default": "mainnet",
                },
            },
        },
    },
    "sideswap_peg_quote": {
        "description": (
            "Quote the receive amount for a SideSwap peg (BTC ↔ L-BTC) at "
            "current fees (0.1% + ~286 sats Liquid claim fee on peg-in). "
            "Returns send_amount, recv_amount, fee_amount."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "integer",
                    "description": "Send amount in Satoshis",
                },
                "peg_in": {
                    "type": "boolean",
                    "description": "True for BTC → L-BTC, False for L-BTC → BTC",
                    "default": True,
                },
                "network": {
                    "type": "string",
                    "enum": ["mainnet", "testnet"],
                    "default": "mainnet",
                },
            },
            "required": ["amount"],
        },
    },
    "sideswap_peg_in": {
        "description": (
            "Initiate a SideSwap peg-in (BTC → L-BTC). Returns a Bitcoin deposit "
            "address; the user (or btc_send) must send BTC to it. After 2 BTC "
            "confirmations (~20 min hot path; up to ~17 hours cold path for "
            "very large amounts), L-BTC arrives in the Liquid wallet. "
            "Recommended over a swap-market trade for amounts ≥ ~0.01 BTC: "
            "lower fee (0.1% vs 0.2%) at the cost of waiting. "
            "ALWAYS call sideswap_recommend first for large amounts so the user "
            "understands the trade-off."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Liquid wallet to receive L-BTC",
                    "default": "default",
                },
                "password": {
                    "type": "string",
                    "description": "Password to decrypt mnemonic (if encrypted at rest)",
                },
            },
        },
    },
    "sideswap_peg_out": {
        "description": (
            "Initiate a SideSwap peg-out (L-BTC → BTC) and broadcast the L-BTC "
            "send. After 2 Liquid confirmations (~2 min) and the federation BTC "
            "sweep (typically 15–60 min total), BTC arrives at the user's "
            "Bitcoin address. Fees: 0.1% + Bitcoin network fee. Standard way to "
            "move L-BTC back to Bitcoin mainchain."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet_name": {
                    "type": "string",
                    "description": "Liquid wallet to send L-BTC from",
                },
                "amount": {
                    "type": "integer",
                    "description": "Amount in Satoshis to peg out",
                },
                "btc_address": {
                    "type": "string",
                    "description": "Destination Bitcoin address (bc1...)",
                },
                "password": {
                    "type": "string",
                    "description": "Password to decrypt mnemonic (if encrypted at rest)",
                },
            },
            "required": ["wallet_name", "amount", "btc_address"],
        },
    },
    "sideswap_peg_status": {
        "description": (
            "Check the status of a SideSwap peg order (peg-in or peg-out). "
            "Returns confirmations progress (X/Y), tx_state, lockup_txid, "
            "payout_txid when complete."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Order ID from sideswap_peg_in or sideswap_peg_out",
                },
            },
            "required": ["order_id"],
        },
    },
    "sideswap_recommend": {
        "description": (
            "Recommend a peg vs an instant swap-market trade for a BTC ↔ L-BTC "
            "conversion. Surfaces the trade-off (lower fee but slower) and "
            "warns when the amount exceeds SideSwap's hot-wallet liquidity. "
            "ALWAYS call this for large conversions before initiating a peg."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "integer",
                    "description": "Amount in Satoshis to convert",
                },
                "direction": {
                    "type": "string",
                    "enum": ["btc_to_lbtc", "lbtc_to_btc"],
                    "description": "Direction of conversion",
                },
                "network": {
                    "type": "string",
                    "enum": ["mainnet", "testnet"],
                    "default": "mainnet",
                },
            },
            "required": ["amount", "direction"],
        },
    },
    "sideswap_list_assets": {
        "description": (
            "List Liquid assets that SideSwap supports for atomic swaps "
            "(e.g. L-BTC, USDt, EURx, MEX, DePix)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "network": {
                    "type": "string",
                    "enum": ["mainnet", "testnet"],
                    "default": "mainnet",
                },
            },
        },
    },
    "sideswap_quote": {
        "description": (
            "Get a read-only price quote for a SideSwap Liquid asset swap "
            "(e.g. L-BTC ↔ USDt). Provide exactly one of send_amount or "
            "recv_amount. Use this BEFORE sideswap_execute_swap so the user "
            "can confirm the price."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {
                    "type": "string",
                    "description": "Liquid asset ID (hex) to swap with L-BTC",
                },
                "send_amount": {
                    "type": "integer",
                    "description": "Amount the user is sending (Satoshis)",
                },
                "recv_amount": {
                    "type": "integer",
                    "description": "Amount the user wants to receive (Satoshis)",
                },
                "send_bitcoins": {
                    "type": "boolean",
                    "description": "True if sending L-BTC for the asset; False if sending the asset for L-BTC",
                    "default": True,
                },
                "network": {
                    "type": "string",
                    "enum": ["mainnet", "testnet"],
                    "default": "mainnet",
                },
            },
            "required": ["asset_id"],
        },
    },
    "sideswap_execute_swap": {
        "description": (
            "Execute a Liquid atomic swap on SideSwap. Both directions are "
            "supported via send_bitcoins: True = L-BTC → asset (default), "
            "False = asset → L-BTC. The PSET returned by SideSwap is verified "
            "locally against the agreed quote BEFORE signing — the swap is "
            "aborted if the wallet's net balance change does not exactly match "
            "(refusing to sign protects against a hostile server). The fee "
            "tolerance is pinned to L-BTC, so on the asset → L-BTC direction "
            "the asset side is checked at strict equality. Order is persisted "
            "at every step for crash recovery. ALWAYS call sideswap_quote "
            "first and confirm the price with the user before invoking this tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {
                    "type": "string",
                    "description": "Non-L-BTC Liquid asset (e.g. USDt). The L-BTC side is always the policy asset.",
                },
                "send_amount": {
                    "type": "integer",
                    "description": "Send amount in sats (L-BTC if send_bitcoins, else asset)",
                },
                "send_bitcoins": {
                    "type": "boolean",
                    "description": "True = send L-BTC to receive asset; False = send asset to receive L-BTC",
                    "default": True,
                },
                "wallet_name": {
                    "type": "string",
                    "description": "Liquid wallet to sign with",
                    "default": "default",
                },
                "password": {
                    "type": "string",
                    "description": "Password to decrypt mnemonic (if encrypted at rest)",
                },
                "min_recv_amount": {
                    "type": "integer",
                    "description": (
                        "Optional floor on the dealer's recv_amount in sats. "
                        "Pass the recv_amount the user just confirmed in "
                        "sideswap_quote — if the rate moved between preview "
                        "and execution and the dealer offers less, the swap "
                        "is rejected before signing."
                    ),
                },
                "flexible_small_amount": {
                    "type": "boolean",
                    "description": (
                        "When True, accept dealer-rounded send_amount up to "
                        "±3000 sats from what was requested. SideSwap's mkt::* "
                        "dealer rounds internally; small swaps (e.g. 5k–25k "
                        "sats) often come back at a slightly different amount. "
                        "Off by default — strict equality is safer at scale."
                    ),
                    "default": False,
                },
            },
            "required": ["asset_id", "send_amount"],
        },
    },
    "sideswap_swap_status": {
        "description": (
            "Get persisted status of a SideSwap atomic asset swap. Once the "
            "swap is broadcast, pass the txid to lw_tx_status to track "
            "on-chain confirmations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Order ID returned from sideswap_execute_swap",
                },
            },
            "required": ["order_id"],
        },
    },
    "sideshift_list_coins": {
        "description": (
            "List the coins and networks SideShift supports for cross-chain swaps. "
            "Use to discover valid (coin, network) IDs for the other sideshift_* tools."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    "sideshift_pair_info": {
        "description": (
            "Get rate, min, and max for a SideShift pair (e.g. USDt-Liquid → USDt-Tron). "
            "Returns decimal-string rate / min / max in deposit-coin units."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_coin": {"type": "string", "description": "Deposit coin ticker (e.g. 'USDT')"},
                "from_network": {"type": "string", "description": "Deposit network (e.g. 'liquid', 'tron', 'ethereum')"},
                "to_coin": {"type": "string", "description": "Settle coin ticker"},
                "to_network": {"type": "string", "description": "Settle network"},
                "amount": {
                    "type": "string",
                    "description": "Optional reference amount in deposit-coin units (decimal string)",
                },
            },
            "required": ["from_coin", "from_network", "to_coin", "to_network"],
        },
    },
    "sideshift_quote": {
        "description": (
            "Request a fixed-rate SideShift quote (~15 min TTL). Provide exactly one "
            "of deposit_amount or settle_amount as a decimal string. Use BEFORE "
            "sideshift_send to confirm the quote with the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deposit_coin": {"type": "string"},
                "deposit_network": {"type": "string"},
                "settle_coin": {"type": "string"},
                "settle_network": {"type": "string"},
                "deposit_amount": {"type": "string", "description": "User sends this much (decimal string)"},
                "settle_amount": {"type": "string", "description": "User receives this much (decimal string)"},
            },
            "required": ["deposit_coin", "deposit_network", "settle_coin", "settle_network"],
        },
    },
    "sideshift_send": {
        "description": (
            "Send funds out via SideShift. Gets a fixed-rate quote, creates the shift, "
            "and broadcasts the deposit from the local wallet. Deposit chain MUST be "
            "'bitcoin' or 'liquid'. Both legs must be in the curated allowlist (USDt on "
            "ethereum/tron/bsc/solana/polygon/ton/liquid, or BTC on bitcoin) — mirrors "
            "AQUA Flutter's supported pairs. Set SIDESHIFT_ALLOW_ALL_NETWORKS=1 to bypass. "
            "A refund address is set automatically (the wallet's own deposit-chain "
            "address). For non-L-BTC Liquid assets (e.g. USDt-Liquid), pass liquid_asset_id. "
            "ALWAYS call sideshift_quote first and confirm the price with the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deposit_coin": {"type": "string", "description": "L-BTC: 'btc' (network='liquid'); BTC: 'btc'; USDt-Liquid: 'usdt'"},
                "deposit_network": {
                    "type": "string",
                    "enum": ["bitcoin", "liquid"],
                    "description": "Chain we sign on",
                },
                "settle_coin": {"type": "string"},
                "settle_network": {"type": "string"},
                "settle_address": {"type": "string", "description": "Where SideShift sends the converted asset"},
                "deposit_amount": {"type": "string"},
                "settle_amount": {"type": "string"},
                "wallet_name": {"type": "string", "default": "default"},
                "password": {"type": "string", "description": "Password to decrypt mnemonic (if encrypted)"},
                "liquid_asset_id": {
                    "type": "string",
                    "description": "Hex asset id; required when sending a non-L-BTC Liquid asset",
                },
                "settle_memo": {"type": "string", "description": "Required for memo networks (TON, BNB, etc.)"},
                "refund_memo": {"type": "string"},
                "quote_id": {
                    "type": "string",
                    "description": (
                        "Optional fixed-rate quote id from a prior sideshift_quote "
                        "call. Pass this after the user confirms the preview so the "
                        "shift executes at the same rate they saw — without it, the "
                        "tool fetches a fresh quote and the rate may have moved."
                    ),
                },
            },
            "required": [
                "deposit_coin", "deposit_network", "settle_coin",
                "settle_network", "settle_address",
            ],
        },
    },
    "sideshift_receive": {
        "description": (
            "Receive into the local wallet via a SideShift variable-rate shift. "
            "Returns a deposit address on the deposit chain — the user (or external "
            "sender) sends to it from any wallet. Settle chain MUST be 'bitcoin' or "
            "'liquid'. Both legs must be in the curated allowlist (USDt on "
            "ethereum/tron/bsc/solana/polygon/ton/liquid, or BTC on bitcoin) — mirrors "
            "AQUA Flutter's supported pairs. Set SIDESHIFT_ALLOW_ALL_NETWORKS=1 to bypass. "
            "STRONGLY RECOMMEND passing external_refund_address (the deposit-side "
            "sender's address) so a stuck shift can refund automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deposit_coin": {"type": "string"},
                "deposit_network": {"type": "string"},
                "settle_coin": {"type": "string", "description": "'btc' or 'usdt' typically"},
                "settle_network": {
                    "type": "string",
                    "enum": ["bitcoin", "liquid"],
                },
                "wallet_name": {"type": "string", "default": "default"},
                "external_refund_address": {
                    "type": "string",
                    "description": "Deposit-chain refund address (strongly recommended)",
                },
                "external_refund_memo": {"type": "string"},
                "settle_memo": {"type": "string"},
            },
            "required": ["deposit_coin", "deposit_network", "settle_coin", "settle_network"],
        },
    },
    "sideshift_status": {
        "description": (
            "Check the status of a SideShift shift order. Returns the shift record "
            "plus is_final / is_success / is_failed booleans."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "shift_id": {"type": "string", "description": "ID from sideshift_send or sideshift_receive"},
            },
            "required": ["shift_id"],
        },
    },
    "sideshift_recommend": {
        "description": (
            "Recommend SideSwap vs SideShift for a cross-asset conversion. "
            "SideSwap when both legs are on Bitcoin/Liquid (atomic, lower fees); "
            "SideShift when at least one leg is on a non-Liquid chain (custodial, "
            "covers 30+ chains)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_coin": {"type": "string"},
                "from_network": {"type": "string"},
                "to_coin": {"type": "string"},
                "to_network": {"type": "string"},
            },
            "required": ["from_coin", "from_network", "to_coin", "to_network"],
        },
    },
}


def create_server() -> Server:
    """Create and configure the MCP server."""
    server = Server(
        "agentic-aqua",
        instructions="""You are managing Bitcoin and Liquid Network cryptocurrency wallets.

STARTUP BEHAVIOR:
- FIRST ACTION: Always check existing wallets with lw_list_wallets
- Show user what wallets are already available locally
- This prevents re-importing seeds every session
- If wallet is encrypted with a password, ask user for it when needed (signing transactions)

DEFAULTS:
- Network: MAINNET (unless user explicitly requests testnet)
- Balance queries: Use unified_balance (both networks) unless user specifies bitcoin or liquid only
- New wallets: Encourage encrypting the seed at rest with a strong password
- Password format: Use memorable but strong passwords (e.g. "Wild-red-dolphin-386")

CRITICAL SAFETY RULES:
- Amounts are in SATOSHIS (1 BTC = 100,000,000 Sats)
- Always verify network: mainnet vs testnet
- Confirm transactions before broadcasting
- Show explorer links after sending
- STRONGLY recommend encrypting the seed with a password, but allow user choice

NETWORK IDENTIFIERS:
- Bitcoin mainnet: bc1... addresses
- Bitcoin testnet: tb1... addresses
- Liquid mainnet: lq1... addresses
- Liquid testnet: tex1... addresses

WORKFLOW:
1. Check balance first (unified_balance by default)
2. Verify destination address
3. Confirm amount with user
4. Broadcast and provide txid

WHEN GENERATING NEW SEEDS:
1. Generate seed with lw_generate_mnemonic
2. ASK user if they want to encrypt the seed on disk with a password (STRONGLY RECOMMENDED)
3. If yes: ASK user for their password
   - Give example: "Wild-red-dolphin-386" (Word1-word2-word3-###)
   - Wait for user to provide their chosen password
4. Import wallet with seed + user's password (or no password if declined)
5. Show user the seed (and remind them of their password if used)
6. Emphasize importance of backing up the seed securely. The password only
   protects the local file — it is NOT a BIP39 passphrase, so the seed alone
   is enough to restore the wallet in any other software.

PASSWORD HANDLING (encryption at rest):
- Wallets with encrypted seeds require the password to decrypt for signing
- Ask user for the password when calling btc_send, lw_send, lw_send_asset, lightning_send
- If operation fails with decryption error, the wallet likely has a password
- IMPORTANT: the password is NOT a BIP39 passphrase. It does not alter the
  derived keys. The seed alone fully restores the same descriptors on Liquid
  and Bitcoin in any BIP39-compliant wallet.

LIGHTNING:
- Use lightning_receive to generate an invoice for receiving L-BTC from Lightning
  Fees: ~0.1%, Limits: 100 - 25,000,000 Sats, Time: ~1-2 min after payment
- Use lightning_send to pay a BOLT11 invoice OR a Lightning Address (user@domain.com)
  using L-BTC (submarine swap via Boltz). Lightning Addresses require amount_sats.
  Fees: ~0.1% + miner fees, Limits: 100 - 25,000,000 Sats
- Use lightning_transaction_status to check status of any Lightning swap (send or receive)

PIX → DEPIX (Brazilian Real on-ramp via Eulen):
- Use pix_receive to mint a Pix charge that pays out DePix (BRL stablecoin on Liquid)
  Amount is in BRL CENTS (100 = R$1.00). Be very explicit about the unit when asking the user.
  Requires the EULEN_API_TOKEN environment variable; if missing, tell the user to set it
  (token comes from https://depix.info/#partners).
- The tool returns a `qr_copy_paste` string (EMV BR-Code) and a `qr_image_url`. Show BOTH
  to the user and explain they can either paste the string into their banking app's
  "Pix Copia e Cola" field, or open the URL on their phone and scan the QR.
- After the user pays, call pix_status with the swap_id until status="depix_sent".
  Eulen pushes DePix automatically — no claim step.
- First-time users on Eulen typically have a low limit (around R$500); limits scale up
  with usage. If a deposit fails with an amount error, suggest a smaller amount.

CHANGELLY (custodial USDt cross-chain swaps via AQUA's Ankara proxy):
- Use changelly_send when the user wants to send USDt-Liquid OUT to USDt on
  another chain (Ethereum, Tron, BSC, Solana, Polygon, TON).
- Use changelly_receive when the user wants to receive USDt-Liquid IN from
  USDt on another chain. Returns a deposit address on the source chain.
- ALWAYS call changelly_quote first for sends so the user can confirm the
  rate before signing. Quotes are fixed-rate with a short TTL.
- ALWAYS encourage providing external_refund_address on receives — without
  it, a stuck order requires manual intervention via Changelly's web UI.
- Use changelly_status to poll an order; the response includes is_final /
  is_success / is_failed booleans.
- TRUST MODEL: Changelly is custodial — they take the deposit and send the
  converted asset from their hot wallet. Different from SideSwap (atomic on
  Liquid) and Lightning (Boltz submarine, atomic). Communicate the trade-off.
- SCOPE: USDt-Liquid ↔ USDt on the 6 supported chains only. For BTC ↔ X,
  L-BTC ↔ X, or anything non-USDt, use SideSwap or SideShift instead.
- SideSwap vs Changelly vs SideShift for similar flows:
  - L-BTC ↔ USDt-Liquid: SideSwap (atomic, lower fees)
  - USDt-Liquid ↔ USDt-Tron / USDt-Ethereum / etc.: Changelly OR SideShift
    (both custodial; Changelly proxies through AQUA backend; SideShift uses
    a public affiliate ID). Pick whichever is configured / has better rates.

SIDESWAP (BTC ↔ L-BTC pegs and Liquid asset swaps):
- Pegs are the canonical way to move funds between Bitcoin mainchain and Liquid.
- Peg-in (BTC → L-BTC): user sends BTC to a SideSwap deposit address; after 2
  BTC confirmations (~20 min), L-BTC arrives in their Liquid wallet.
- Peg-out (L-BTC → BTC): user sends L-BTC to a SideSwap deposit address; after
  2 Liquid confs and the federation sweep (~15-60 min total), BTC arrives.
- Fees: 0.1% on each peg + a small second-chain fee (~286 sats on peg-in).
- BEFORE initiating a peg for ≥ 0.01 BTC (1,000,000 sats), call
  sideswap_recommend to surface the time-vs-fee trade-off and warn the user.
- For VERY LARGE peg-ins that exceed SideSwap's hot-wallet balance, expect the
  cold-wallet path: 102 BTC confirmations (~17 hours). Always check
  sideswap_server_status first and warn the user when this applies.
- For Liquid asset swaps (e.g. L-BTC ↔ USDt), sideswap_quote returns a quote
  and sideswap_execute_swap performs the swap. Both directions are supported
  via the send_bitcoins flag. The PSET returned by SideSwap is verified
  LOCALLY against the agreed quote before signing — refusing to sign if the
  recv balance does not match exactly. The fee tolerance is pinned to L-BTC,
  so the non-L-BTC asset side is always checked at strict equality.

WHEN TO RECOMMEND A PEG:
- "I want to move my BTC to Liquid" → if amount ≥ 0.01 BTC, recommend peg-in.
  Below that, instant atomic swaps may be preferable for speed.
- "I want to move my L-BTC to Bitcoin" → recommend peg-out (it is the standard
  path; swap-market liquidity for L-BTC → BTC is shallow).
- ALWAYS explain the time trade-off and ask the user to confirm they want to
  wait the expected duration before broadcasting.

SIDESHIFT (custodial cross-chain swaps):
- Use sideshift_send when the user wants to send funds OUT of Liquid/Bitcoin to
  another chain (e.g. USDt-Liquid → USDt-Tron, L-BTC → ETH, BTC → SOL).
- Use sideshift_receive when the user wants to receive funds INTO Liquid/Bitcoin
  from another chain (e.g. USDt-Tron → USDt-Liquid, ETH → L-BTC).
- ALWAYS call sideshift_quote first for sends so the user can confirm the
  rate before signing. Quotes expire in ~15 minutes.
- ALWAYS encourage providing external_refund_address on receives — without it,
  a stuck shift requires manual intervention via the SideShift web UI.
- Use sideshift_status to poll a shift; the response includes is_final /
  is_success / is_failed booleans so you don't have to memorise the state machine.
- TRUST MODEL: SideShift is custodial. They take the deposit and send from
  their hot wallet. This is different from SideSwap (atomic on Liquid) and
  Lightning (Boltz submarine, atomic). Communicate this trade-off to the user.
- Memo networks (TON, BNB Beacon, Stellar, etc.) require a memo on either
  the deposit or settle side — pass settle_memo / refund_memo when prompted.

WATCH-ONLY WALLETS:
- For a Bitcoin-only watch wallet: btc_import_descriptor (BIP84 wpkh xpub).
- For a Liquid-only watch wallet: lw_import_descriptor (CT descriptor).
- Bitcoin and Liquid descriptors are NOT interchangeable: Bitcoin uses path
  m/84'/0'/0' and Liquid uses m/84'/1776'/0'; Liquid also requires the SLIP-77
  master blinding key from the seed. If the user wants both networks watch-only,
  both descriptors must be imported separately.
- btc_export_descriptor / lw_export_descriptor return the public descriptors that
  can be re-imported elsewhere as watch-only.

WALLET DELETION:
- ALWAYS use the delete_wallet prompt workflow (check balances, remind about seed backup, confirm)
- NEVER call delete_wallet directly without first checking balances and getting user confirmation
- Remind user to backup their seed before deletion""",
    )

    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        """List available prompt templates."""
        return [
            # Wallet creation
            Prompt(
                name="create_new_wallet",
                description="Create a new wallet with seed and optional at-rest password",
                arguments=[
                    PromptArgument(
                        name="wallet_name", description="Name for the wallet", required=False
                    ),
                    PromptArgument(
                        name="network", description="mainnet or testnet", required=False
                    ),
                ],
            ),
            Prompt(
                name="import_seed",
                description="Import an existing wallet from a seed phrase",
                arguments=[
                    PromptArgument(
                        name="wallet_name", description="Name for the wallet", required=False
                    ),
                ],
            ),
            # Balance queries
            Prompt(
                name="show_balance",
                description="Show my wallet balance (both networks by default)",
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            Prompt(
                name="bitcoin_balance",
                description="Show only Bitcoin balance",
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            Prompt(
                name="liquid_balance",
                description="Show only Liquid balance (all assets)",
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            # Addresses
            Prompt(
                name="generate_address",
                description="Generate an address to receive funds",
                arguments=[
                    PromptArgument(name="network", description="bitcoin or liquid", required=True),
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            # Transactions
            Prompt(
                name="show_transactions",
                description="View transaction history",
                arguments=[
                    PromptArgument(name="network", description="bitcoin or liquid", required=False),
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            Prompt(
                name="send_bitcoin",
                description="Send Bitcoin to an address",
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            Prompt(
                name="send_liquid",
                description="Send L-BTC or other Liquid asset",
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            Prompt(
                name="transaction_status",
                description="Check transaction status",
                arguments=[
                    PromptArgument(name="network", description="bitcoin or liquid", required=False),
                ],
            ),
            # Management
            Prompt(
                name="list_wallets",
                description="Show all my wallets",
                arguments=[],
            ),
            Prompt(
                name="export_descriptor",
                description="Export descriptor for watch-only wallet",
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            Prompt(
                name="delete_wallet",
                description="Safely delete a wallet with balance check and seed backup reminder",
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=True),
                ],
            ),
            # Lightning
            Prompt(
                name="pay_lightning",
                description="Pay a Lightning invoice using Liquid Bitcoin (via Boltz submarine swap)",
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            # Pix → DePix
            Prompt(
                name="receive_via_pix",
                description="Receive DePix (BRL stablecoin on Liquid) by paying a Pix charge in your Brazilian banking app",
                arguments=[
                    PromptArgument(
                        name="wallet_name", description="Wallet name", required=False
                    ),
                ],
            ),
            # Changelly (cross-chain USDt swaps)
            Prompt(
                name="usdt_cross_chain_send",
                description=(
                    "Send USDt-Liquid out to USDt on another chain via Changelly "
                    "(e.g. USDt-Liquid → USDt-Tron). Walks through quote, "
                    "confirmation, and broadcast."
                ),
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            Prompt(
                name="usdt_cross_chain_receive",
                description=(
                    "Receive USDt-Liquid from USDt on another chain via Changelly "
                    "(e.g. USDt-Tron → USDt-Liquid). Returns a deposit address for "
                    "the external sender."
                ),
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            # SideSwap
            Prompt(
                name="peg_in",
                description="Move BTC to Liquid (BTC → L-BTC) via SideSwap peg-in",
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            Prompt(
                name="peg_out",
                description="Move L-BTC to Bitcoin (L-BTC → BTC) via SideSwap peg-out",
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            Prompt(
                name="swap_assets",
                description="Quote a Liquid asset swap (e.g. L-BTC ↔ USDt) via SideSwap (read-only)",
                arguments=[],
            ),
            # SideShift (cross-chain)
            Prompt(
                name="cross_chain_send",
                description=(
                    "Send funds from Liquid or Bitcoin to another chain via SideShift "
                    "(e.g. USDt-Liquid → USDt-Tron, L-BTC → ETH)"
                ),
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
            Prompt(
                name="cross_chain_receive",
                description=(
                    "Receive funds from another chain into Liquid or Bitcoin via SideShift "
                    "(e.g. USDt-Tron → USDt-Liquid, ETH → L-BTC)"
                ),
                arguments=[
                    PromptArgument(name="wallet_name", description="Wallet name", required=False),
                ],
            ),
        ]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
        """Get a specific prompt template."""
        wallet_name = arguments.get("wallet_name", "default") if arguments else "default"
        network = arguments.get("network", "mainnet") if arguments else "mainnet"

        # Wallet creation
        if name == "create_new_wallet":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to create a new wallet named '{wallet_name}' on {network}.

Please:
1. Generate a new 12-word seed with lw_generate_mnemonic
2. Show me the seed
3. Ask me: "Do you want to encrypt the seed on disk with a password? (STRONGLY RECOMMENDED)"
   - Clarify: this password protects the local file only. It is NOT a BIP39
     passphrase, so the seed phrase alone restores the same addresses elsewhere.
4. If I say yes:
   - Ask me: "Please provide your password. Example format: 'Wild-red-dolphin-386' (Word1-word2-word3-###)"
   - Wait for me to give you my chosen password
   - Import wallet with my password
5. If I say no:
   - Warn me that the seed will only be base64-encoded (less secure at rest)
   - Ask for confirmation
   - Import wallet without password
6. Confirm wallet creation for both Bitcoin and Liquid
7. Remind me to backup the seed phrase securely (losing the password only blocks
   this local file — the mnemonic alone still restores my funds elsewhere)
8. Generate a receive address for Bitcoin and another for Liquid""",
                        ),
                    )
                ]
            )

        elif name == "import_seed":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to import an existing mnemonic or seed phrase.

Please ask me:
1. The seed (12 or 24 words)
2. If I want to encrypt the seed on disk with a password (STRONGLY RECOMMENDED)
   - If yes: ask for the password
   - If no: warn me it will be less secure at rest (base64 only)
   - Note: the password only protects the local file. It is NOT a BIP39
     passphrase — derived addresses depend solely on the seed.
3. Network: mainnet or testnet (default: mainnet)
4. Wallet name (default: '{wallet_name}')

Then import and confirm that both Bitcoin and Liquid wallets were created.""",
                        ),
                    )
                ]
            )

        # Balance queries
        elif name == "show_balance":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""Show me the balance of my '{wallet_name}' wallet.

Use unified_balance to display:
- Bitcoin balance (in BTC and Sats)
- Liquid balance (L-BTC and other assets if any)
- User-friendly format with BTC values, not just Satoshis""",
                        ),
                    )
                ]
            )

        elif name == "bitcoin_balance":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""Show me only the Bitcoin balance of my '{wallet_name}' wallet.

Use btc_balance and display result in both BTC and Satoshis.""",
                        ),
                    )
                ]
            )

        elif name == "liquid_balance":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""Show me the Liquid balance of my '{wallet_name}' wallet.

Use lw_balance and display all assets with their tickers and amounts.""",
                        ),
                    )
                ]
            )

        # Addresses
        elif name == "generate_address":
            network_arg = arguments.get("network", "bitcoin") if arguments else "bitcoin"
            tool = "btc_address" if network_arg == "bitcoin" else "lw_address"
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""Generate an address to receive {network_arg.upper()} in my '{wallet_name}' wallet.

Use {tool} and show me the address in a clear format.""",
                        ),
                    )
                ]
            )

        # Transactions
        elif name == "show_transactions":
            if arguments and "network" in arguments:
                net = arguments["network"]
                tool = "btc_transactions" if net == "bitcoin" else "lw_transactions"
                return GetPromptResult(
                    messages=[
                        PromptMessage(
                            role="user",
                            content=TextContent(
                                type="text",
                                text=f"""Show me the recent {net.upper()} transactions from my '{wallet_name}' wallet.

Use {tool} with limit=10 and display in readable format with dates, amounts, and txids.""",
                            ),
                        )
                    ]
                )
            else:
                return GetPromptResult(
                    messages=[
                        PromptMessage(
                            role="user",
                            content=TextContent(
                                type="text",
                                text=f"""Show me the transactions from my '{wallet_name}' wallet.

Display transactions from BOTH networks (Bitcoin and Liquid) in chronological order.""",
                            ),
                        )
                    ]
                )

        elif name == "send_bitcoin":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to send Bitcoin from my '{wallet_name}' wallet.

Please:
1. Show my current Bitcoin balance
2. Ask me for:
   - Destination address (bc1...)
   - Amount (accept in BTC, convert to Satoshis)
   - Fee rate (optional, suggest: 2-10 Sat/vB based on urgency)
3. Verify the address is valid and mainnet
4. Show me a summary BEFORE sending:
   - Amount: X BTC (Y Sats)
   - Estimated fees
   - Destination address
5. Ask for explicit confirmation
6. If wallet is password-encrypted, ask me for the password
7. Send with btc_send
8. Show txid and explorer link""",
                        ),
                    )
                ]
            )

        elif name == "send_liquid":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to send from Liquid (wallet '{wallet_name}').

Please:
1. Show my Liquid balance (all assets)
2. Ask me for:
   - Which asset to send (L-BTC, USDt, etc.)
   - Destination address (lq1...)
   - Amount
3. Determine the correct asset_id
4. Verify valid mainnet address
5. Show summary BEFORE sending
6. Ask for confirmation and the encryption password if applicable
7. Send with lw_send or lw_send_asset
8. Show txid and explorer link""",
                        ),
                    )
                ]
            )

        elif name == "transaction_status":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text="""I want to check the status of a transaction.

Please ask me for:
- The txid or explorer URL
- Which network (bitcoin or liquid)

Then use lw_tx_status (for Liquid) or check Bitcoin explorer and show:
- Status (confirmed/pending)
- Number of confirmations
- Amount
- Explorer link""",
                        ),
                    )
                ]
            )

        # Management
        elif name == "list_wallets":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text="""Show me all my wallets.

Use lw_list_wallets and display in table format with:
- Name
- Network (mainnet/testnet)
- Type (full/watch-only)
- Whether the seed is password-encrypted at rest""",
                        ),
                    )
                ]
            )

        elif name == "export_descriptor":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""Export the descriptor from my '{wallet_name}' wallet for watch-only use.

Use lw_export_descriptor and explain:
- What the descriptor is for
- How to import it in another wallet as watch-only
- That it does NOT provide access to sign transactions""",
                        ),
                    )
                ]
            )

        elif name == "delete_wallet":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to delete wallet '{wallet_name}'.

Please follow this safety workflow:
1. Check if the wallet exists with lw_list_wallets
2. Check balances on BOTH networks using unified_balance for '{wallet_name}'
3. If there are any funds (BTC or L-BTC > 0):
   - WARN me clearly about the remaining funds
   - Show me exactly how much is in each network
4. REMIND me: "Make sure you have backed up your seed phrase (mnemonic) before proceeding. Without it, you will permanently lose access to any funds associated with this wallet. The at-rest encryption password, if any, protects only this local file — the seed alone is enough to restore funds elsewhere."
5. Ask me for EXPLICIT confirmation: "Are you sure you want to delete wallet '{wallet_name}'? This cannot be undone."
6. Only after I explicitly confirm, call delete_wallet with wallet_name='{wallet_name}'
7. Confirm deletion was successful""",
                        ),
                    )
                ]
            )

        elif name == "peg_in":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to peg in (move BTC into L-BTC) using my '{wallet_name}' wallet.

Please:
1. Ask me how much BTC I want to peg in (in BTC or Sats)
2. If I haven't given a clear amount yet, also show my current Bitcoin balance
   (btc_balance) so I have context
3. Call sideswap_server_status to fetch live fees, minimums, and hot-wallet balance
4. If the amount is >= 0.01 BTC (1,000,000 sats), call sideswap_recommend with
   direction="btc_to_lbtc" and the amount to confirm peg-in is appropriate,
   and surface the trade-off:
   - Lower fee (0.1% vs ~0.2% on instant swaps)
   - Slower: usually 20–40 min for 2 BTC confirmations
   - For very large amounts: may require 102 confs (~17 hours) if it exceeds
     SideSwap's hot-wallet liquidity. WARN clearly if this applies.
5. Call sideswap_peg_quote to show the exact receive amount after fees
6. Show me a summary BEFORE proceeding:
   - Send amount (BTC) → Receive amount (L-BTC)
   - Fee breakdown
   - Expected time (and any 102-conf warning)
7. Ask for explicit confirmation
8. Call sideswap_peg_in to get the BTC deposit address (peg_addr)
9. Ask me whether I want to fund it from my local Bitcoin wallet (btc_send) or
   send manually from another wallet
10. If from local: ask for password (if encrypted), then btc_send to peg_addr
11. Show me the order_id and tell me to use sideswap_peg_status to track progress""",
                        ),
                    )
                ]
            )

        elif name == "peg_out":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to peg out (move L-BTC into Bitcoin) from my '{wallet_name}' wallet.

Please:
1. Show my current L-BTC balance (lw_balance)
2. Ask me:
   - How much L-BTC to peg out (Sats)
   - Destination Bitcoin address (bc1...)
3. Call sideswap_server_status to fetch live minimums and fees
4. Call sideswap_recommend with direction="lbtc_to_btc" — peg-out is the
   standard path for L-BTC → BTC and you should communicate that
5. Call sideswap_peg_quote (peg_in=false) to show the exact receive amount
6. Show me a summary BEFORE proceeding:
   - Send: X L-BTC → Receive: Y BTC at {{btc_address}}
   - Fee breakdown (0.1% + Bitcoin network fee, deducted from payout)
   - Expected time: usually 15–60 minutes
7. Ask for explicit confirmation
8. If wallet is password-encrypted, ask for the password
9. Call sideswap_peg_out — this broadcasts the L-BTC send to the SideSwap
   deposit address. Show the order_id and lockup_txid
10. Tell me to track progress with sideswap_peg_status""",
                        ),
                    )
                ]
            )

        elif name == "swap_assets":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text="""I want to swap Liquid assets (e.g. L-BTC ↔ USDt) via SideSwap.

Please:
1. Call sideswap_list_assets to show what's tradeable on SideSwap right now
2. Ask me what I want to swap and which direction:
   - L-BTC → asset (send_bitcoins=true): I send L-BTC, receive an asset
   - asset → L-BTC (send_bitcoins=false): I send an asset, receive L-BTC
3. Ask me for the send_amount in the corresponding sats (L-BTC sats if
   sending L-BTC; asset sats otherwise). For L-BTC, accept input in BTC
   and convert.
4. Show me my current balance for the send asset (lw_balance) so I have context
5. Call sideswap_quote with the right send_bitcoins flag to get a price quote
6. Show me a summary clearly:
   - Send: X sats of [send asset]
   - Receive: Y sats of [recv asset]
   - Price + fixed_fee
   - Net effective rate
7. Ask for explicit confirmation
8. If wallet is password-encrypted, ask me for the password
9. Call sideswap_execute_swap with the same asset_id, send_amount, and
   send_bitcoins flag. ALSO pass min_recv_amount=<recv_amount from the
   quote> so the swap aborts if the rate has drifted between the preview
   I just confirmed and the mkt::* quote that actually executes.
   The tool will: capture a fresh quote (price may have moved by a few
   percent), request the PSET via SideSwap's market.get_quote, VERIFY it
   locally against the quote, sign it, and submit via market.taker_sign.
   If the verification fails the tool aborts WITHOUT signing — that's a
   safety feature, not a bug; relay the error message to me.
10. On success show me txid + the explorer link
11. Tell me to use sideswap_swap_status with the order_id to recall details
    later, and lw_tx_status with the txid to check on-chain confirmation""",
                        ),
                    )
                ]
            )

        elif name == "pay_lightning":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to pay a Lightning invoice or Lightning Address using my Liquid wallet '{wallet_name}'.

Please:
1. Show my L-BTC balance first (lw_balance)
2. Ask me for either:
   - A BOLT11 Lightning invoice (starts with lnbc.../lntb...), OR
   - A Lightning Address (user@domain.com). If a Lightning Address, also ask for
     the amount in sats — Lightning Addresses don't encode the amount.
3. If a Lightning Address, confirm the resolved amount and metadata before sending.
4. Explain the fee structure:
   - Boltz fee: ~0.1% of amount
   - Miner fee: ~19 Sats
   - Limits: 100 - 25,000,000 Sats
5. Show total cost (invoice amount + fees) and ask for confirmation
6. Use lightning_send to execute the swap (pass amount_sats for Lightning Addresses)
7. Wait for completion (may take 1-3 minutes)
8. Show the result:
   - Swap ID for reference
   - Preimage (proof of payment)
   - Explorer link for lockup transaction
9. If swap fails, explain that L-BTC is locked until timeout and can be refunded""",
                        ),
                    )
                ]
            )

        elif name == "receive_via_pix":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to receive DePix (BRL stablecoin on Liquid) into my wallet '{wallet_name}' by paying via Pix.

Please:
1. Verify the EULEN_API_TOKEN environment variable is set. If not, tell me to obtain one from https://depix.info/#partners and stop here.
2. Ask me how much I want to deposit, IN REAIS (e.g. "R$50"). Convert to cents (R$50 → 5000 cents) before calling pix_receive. Be explicit about the unit so I do not get a 100× error.
3. Mention the practical first-time limit on Eulen is around R$500; offer to use a smaller amount if mine is higher.
4. Call pix_receive(amount_cents=…, wallet_name='{wallet_name}').
5. Show me BOTH:
   - The `qr_copy_paste` string (EMV BR-Code) — I can paste this into my banking app's "Pix Copia e Cola" field.
   - The `qr_image_url` — I can open this on my phone and scan the QR with my bank app.
   Explain I only need to do ONE of those, not both.
6. After I confirm I have paid, call pix_status(swap_id=…) and report the status. Re-check on request until status="depix_sent" (DePix delivered) or a terminal failure.
7. When delivered, show the `blockchain_txid` (Liquid txid) so I can verify on a block explorer.""",
                        ),
                    )
                ]
            )

        elif name == "usdt_cross_chain_send":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to send USDt-Liquid out to USDt on another chain via Changelly, from wallet '{wallet_name}'.

Changelly is a custodial cross-chain swap service routed through AQUA's
Ankara backend. They take the USDt-Liquid deposit and send USDt on the
target chain from their hot wallet. Trust model: trust the company, not
on-chain — make sure I understand this trade-off.

Please:
1. Show my Liquid balance (lw_balance) so I can see how much USDt-Liquid I have
2. Ask me which target USDt chain (ethereum, tron, bsc, solana, polygon, ton)
3. Ask me for:
   - The destination address on that chain
   - The amount of USDt-Liquid to send (decimal, e.g. "100")
4. Call changelly_quote with direction='send' to show the rate, network fee,
   and exact amount the recipient will get
5. Show me a clear summary BEFORE proceeding:
   - Send: X USDt on Liquid
   - Receive: Y USDt on [chain] at [destination address]
   - Network fee + Changelly fee
6. Ask for explicit confirmation
7. If wallet is password-encrypted, ask for the password
8. Call changelly_send — this gets a fresh quote, creates the order, and
   broadcasts the deposit from my Liquid wallet
9. Show me order_id + deposit_hash + the Changelly tracking URL
10. Tell me to use changelly_status to track progress""",
                        ),
                    )
                ]
            )

        elif name == "usdt_cross_chain_receive":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to receive USDt-Liquid in my '{wallet_name}' wallet by having someone send USDt from another chain via Changelly.

Changelly is a custodial cross-chain swap service. They take the deposit
on the source chain from the external sender and send USDt-Liquid to my
Liquid address from their hot wallet. Trust model: trust the company.

Please:
1. Ask me which source USDt chain the external sender will pay from
   (ethereum, tron, bsc, solana, polygon, ton)
2. STRONGLY recommend providing an external_refund_address — the source
   chain address the external sender controls. Without it, a stuck order
   requires manual web UI intervention. Ask for it.
3. Ask the user for the deposit amount in source-chain USDt (decimal string,
   e.g. "50"). This is REQUIRED — the Ankara backend serialiser rejects
   the request without it — and the changelly_receive tool will refuse an
   empty value. Pass it as amount_from.
4. Call changelly_receive — this creates a variable-rate order
5. Show me clearly:
   - The deposit address on the source chain (this is what the external
     sender pays to)
   - The Changelly tracking URL
   - Where the funds will arrive in my wallet (USDt-Liquid)
6. Tell me to use changelly_status with the order_id to poll progress""",
                        ),
                    )
                ]
            )

        elif name == "cross_chain_send":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to send funds out to another chain via SideShift, from wallet '{wallet_name}'.

SideShift is a custodial cross-chain swap service — they take the deposit and
send the converted asset from their hot wallet. The trust model is "trust
SideShift the company" rather than "trust an on-chain protocol." Make sure I
understand this trade-off.

Please:
1. Ask me what I want to send (e.g. USDt-Liquid, L-BTC, BTC) and to which
   coin/network on the receive side (USDt-Tron, ETH-Ethereum, etc.)
2. If both legs are on Bitcoin or Liquid, suggest using SideSwap instead
   (atomic, lower fees) — call sideshift_recommend to confirm
3. Ask me for:
   - The destination address on the settle network (must belong to me or
     someone I trust)
   - The amount: either how much to send (deposit_amount) or how much to
     receive (settle_amount), as a DECIMAL STRING (e.g. "0.0005", "100")
4. Call sideshift_pair_info to show me the rate, min, max for the pair
5. Validate my amount against min/max
6. Call sideshift_quote to get a fixed-rate quote
7. Show me a clear summary BEFORE proceeding:
   - Send: X [deposit_coin] on [deposit_network]
   - Receive: Y [settle_coin] at [settle_address] on [settle_network]
   - Quote rate, expires in ~15 min
8. Ask for explicit confirmation
9. If wallet is password-encrypted, ask me for the password
10. For non-L-BTC Liquid assets (USDt-Liquid, etc.), look up the asset_id
    from lw_list_assets and pass liquid_asset_id when calling sideshift_send
11. Call sideshift_send with the same parameters AND pass quote_id from the
    quote you just showed me so the shift executes at the rate I confirmed
    (omit quote_id and a fresh quote is fetched, which may move). This
    creates the shift and broadcasts the deposit from my wallet
12. Show me shift_id + deposit_hash + the SideShift order URL
    (https://sideshift.ai/orders/<shift_id>)
13. Tell me to use sideshift_status with the shift_id to track progress""",
                        ),
                    )
                ]
            )

        elif name == "cross_chain_receive":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to receive funds from another chain into my '{wallet_name}' wallet via SideShift.

SideShift is a custodial cross-chain swap service — they take the deposit
on the source chain and send to my Liquid or Bitcoin address from their
hot wallet. The trust model is "trust SideShift the company." Make sure I
understand this trade-off.

Please:
1. Ask me which coin/network the deposit is coming from (USDt-Tron, ETH-Ethereum,
   USDt-on-Ethereum, etc.) and which Liquid/Bitcoin asset to settle into
   (USDt-Liquid, L-BTC via coin='btc' network='liquid', BTC mainchain via
   coin='btc' network='bitcoin')
2. Strongly recommend providing an external_refund_address — the address on
   the deposit chain that the external sender controls. Without this, a stuck
   shift requires manual intervention via the SideShift web UI. Ask for it.
3. Call sideshift_pair_info so I see the rate / min / max
4. Call sideshift_receive — this creates a variable-rate shift; the rate is
   set when the deposit confirms on-chain
5. Show me clearly:
   - The deposit address on the source chain (this is what the external
     sender pays to)
   - deposit_min and deposit_max
   - deposit_memo IF PRESENT (the source chain requires a memo, e.g. TON,
     Stellar, BNB Beacon — the sender MUST include it)
   - Where the funds will arrive in my wallet
6. Tell me to use sideshift_status with the shift_id to poll progress""",
                        ),
                    )
                ]
            )

        raise ValueError(f"Unknown prompt: {name}")

    @server.list_resources()
    async def list_resources() -> list[Resource]:
        """List available documentation resources."""
        return [
            Resource(
                uri="aqua://docs/quickstart",
                name="Quick Start Guide",
                description="Getting started with Agentic AQUA wallet management",
                mimeType="text/markdown",
            ),
            Resource(
                uri="aqua://docs/networks",
                name="Network Reference",
                description="Bitcoin and Liquid network details, address formats, and differences",
                mimeType="text/markdown",
            ),
            Resource(
                uri="aqua://docs/security",
                name="Security Best Practices",
                description="How to safely manage wallets and private keys",
                mimeType="text/markdown",
            ),
        ]

    @server.read_resource()
    async def read_resource(uri: str) -> str:
        """Read a documentation resource."""
        if uri == "aqua://docs/quickstart":
            return """# Agentic AQUA Quick Start

## Creating a New Wallet (Recommended Method)

1. Generate a mnemonic: `lw_generate_mnemonic()`
2. Choose a strong password to encrypt the seed on disk
3. Import it: `lw_import_mnemonic(mnemonic="your 12 words", network="mainnet", password="your-password")`
4. This creates BOTH a Liquid and Bitcoin wallet from the same seed
5. **BACKUP THE MNEMONIC**: Save it securely offline. The password only protects
   the local file — it is NOT a BIP39 passphrase, so the seed alone is
   enough to restore the same addresses in any BIP39-compliant wallet.

## Creating Without a Password (Not Recommended)

If you need convenience over security:
- `lw_import_mnemonic(mnemonic="your words", network="mainnet")`
- ⚠️ **WARNING**: Seed stored with only base64 encoding (not encrypted)
- Only use for testing/small amounts

## Checking Balance

**Default (recommended)**: `unified_balance(wallet_name="default")`
- Shows both Bitcoin and Liquid balances in one call

**Network-specific**:
- Bitcoin only: `btc_balance(wallet_name="default")`
- Liquid only: `lw_balance(wallet_name="default")`

## Receiving Funds

- Bitcoin: `btc_address(wallet_name="default")` → bc1... (mainnet)
- Liquid: `lw_address(wallet_name="default")` → lq1... (mainnet)

## Sending Funds

- Bitcoin: `btc_send(wallet_name="default", address="bc1...", amount=10000, password="secret")`
- Liquid (L-BTC): `lw_send(wallet_name="default", address="lq1...", amount=10000, password="secret")`
- Liquid (other assets): `lw_send_asset(..., asset_id="ce091c99...")`

Note: If wallet mnemonic is not encrypted, omit the password parameter
Note: mnemonic is sinonym of seed or seed phrase

## Watch-Only Wallets

Watch-only wallets monitor balances and generate addresses without exposing
private keys. Bitcoin and Liquid descriptors are NOT interchangeable — to
monitor both networks for the same seed, you must import each side separately.

```python
# Bitcoin watch-only (BIP84 wpkh xpub on m/84'/0'/0')
btc_import_descriptor(
    descriptor="wpkh([fp/84'/0'/0']xpub.../0/*)#cs",
    wallet_name="cold",
)

# Liquid watch-only (CT descriptor on m/84'/1776'/0' + SLIP-77 blinding key)
lw_import_descriptor(
    descriptor="ct(slip77(...),elwpkh([fp/84'/1776'/0']xpub.../0/*))",
    wallet_name="cold",
)

# Export the public descriptors of an existing wallet for re-import elsewhere
btc_export_descriptor(wallet_name="cold")
lw_export_descriptor(wallet_name="cold")
```"""

        elif uri == "aqua://docs/networks":
            return """# Network Reference

## Bitcoin

**Mainnet**
- Address prefix: `bc1` (native segwit)
- Explorer: https://blockstream.info/
- Esplora API: https://blockstream.info/api

**Testnet**
- Address prefix: `tb1`
- Explorer: https://blockstream.info/testnet/
- Get test coins: https://testnet-faucet.com/btc-testnet/

## Liquid Network

**Mainnet**
- Address prefix: `lq1` (confidential)
- Native asset: L-BTC (same value as BTC, but on Liquid)
- Explorer: https://blockstream.info/liquid/
- Common assets:
  - L-BTC: 6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d
  - USDt: ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2

**Testnet**
- Address prefix: `tex1`
- Explorer: https://blockstream.info/liquidtestnet/

## Key Differences

| Feature | Bitcoin | Liquid |
|---------|---------|--------|
| Block time | ~10 min | 1 min |
| Finality | 6 blocks | 2 blocks |
| Assets | BTC only | Multiple assets |
| Privacy | Public amounts | Confidential amounts |
| Fees | Dynamic (Sat/vB) | Fixed (~33 Sats) |"""

        elif uri == "aqua://docs/security":
            return """# Security Best Practices

## ⚠️ IMPORTANT: Seed Storage

**At-rest encryption with a password is STRONGLY RECOMMENDED but optional**.

The `password` parameter is used ONLY to encrypt the seed on disk (PBKDF2 +
Fernet). It is **NOT** a BIP39 passphrase: the derived Liquid and Bitcoin keys
depend solely on the seed, so the same seed restores the same
descriptors in any BIP39-compliant wallet (AQUA, Blockstream Green, Jade, etc.).

```python
# ⚠️ NOT RECOMMENDED - stores seed with base64 only (not encrypted)
# Use only for testing or small amounts
lw_import_mnemonic(mnemonic="abandon abandon...")

# ✅ RECOMMENDED - encrypts seed on disk with strong encryption
lw_import_mnemonic(
    mnemonic="abandon abandon...",
    password="strong-password-here"
)
```

**Trade-offs**:
- **With password**: Local file is secure against casual filesystem access, but
  you must remember the password to sign transactions.
- **Without password**: Convenient but only protected by filesystem permissions
  (base64 encoding).

## Password Requirements (encryption at rest)

- **Strong**: Use 12+ characters, mix letters/numbers/symbols
- **Unique**: Don't reuse passwords from other services
- **Required for signing**: You'll need it every time you send funds
- **NOT required for recovery**: Losing the password only blocks this local
  file. The seed alone still restores the wallet in any other software.

## Wallet Security Checklist

- ✅ Always encrypt the local seed with a password
- ✅ Back up the seed offline (paper, metal, encrypted USB)
- ✅ Verify addresses before sending (double-check network)
- ✅ Start with small test transactions
- ✅ Use watch-only wallets for monitoring (export with `lw_export_descriptor`)
- ✅ Keep mainnet and testnet wallets separate

## What NOT to Do

- ❌ Store seeds in cloud services (Dropbox, Google Drive)
- ❌ Share seeds in chat/email/screenshots
- ❌ Leave seeds unencrypted on shared machines
- ❌ Send mainnet funds to testnet addresses
- ❌ Ignore address network prefixes

## Watch-Only Wallets

For monitoring without signing risk:

1. Export descriptors from a full wallet:
   - Liquid: `lw_export_descriptor(wallet_name="main")`
   - Bitcoin: `btc_export_descriptor(wallet_name="main")`
2. Import them as watch-only on another instance:
   - Liquid: `lw_import_descriptor(descriptor="ct(...)", wallet_name="monitor")`
   - Bitcoin: `btc_import_descriptor(descriptor="wpkh(...)", wallet_name="monitor")`
3. Monitor balances without exposing private keys

⚠️ The Bitcoin descriptor and the Liquid CT descriptor cannot be derived from
each other — Bitcoin uses derivation path `m/84'/0'/0'`, Liquid uses
`m/84'/1776'/0'` and additionally requires a SLIP-77 master blinding key. To
monitor both networks watch-only, both descriptors must be imported separately.

## Recovery

If you have:
- ✅ Mnemonic (encrypted or plaintext) → Full recovery possible
- ✅ Descriptor only → Watch-only monitoring
- ❌ Nothing → **Funds are permanently lost**"""

        raise ValueError(f"Unknown resource: {uri}")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available tools."""
        tools = []
        for name, schema in TOOL_SCHEMAS.items():
            tools.append(
                Tool(
                    name=name,
                    description=schema["description"],
                    inputSchema=schema["inputSchema"],
                )
            )
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        """Execute a tool."""
        if name not in TOOLS:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        try:
            tool_fn = TOOLS[name]
            result = tool_fn(**arguments)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2),
                )
            ]
        except Exception as e:
            logger.exception(f"Error calling tool {name}")
            error_result = {
                "error": {
                    "code": type(e).__name__,
                    "message": str(e),
                }
            }
            return [
                TextContent(
                    type="text",
                    text=json.dumps(error_result, indent=2),
                )
            ]

    return server


async def run_server():
    """Run the MCP server."""
    server = create_server()

    async with stdio_server() as (read_stream, write_stream):
        logger.info(f"Agentic AQUA v{__version__} starting...")
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main():
    """Entry point."""
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
