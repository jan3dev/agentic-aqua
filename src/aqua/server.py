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
        "description": "Pay a Lightning invoice using L-BTC from a Liquid wallet (reverse submarine swap). Fees: ~0.1% + miner fees. Limits: 100 – 25,000,000 Sats.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "invoice": {
                    "type": "string",
                    "description": "BOLT11 Lightning invoice (lnbc... or lntb...)",
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
- Use lightning_send to pay a BOLT11 invoice using L-BTC (submarine swap via Boltz)
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

        elif name == "pay_lightning":
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=f"""I want to pay a Lightning invoice using my Liquid wallet '{wallet_name}'.

Please:
1. Show my L-BTC balance first (lw_balance)
2. Ask me for the Lightning invoice (BOLT11 format, starts with lnbc...)
3. Explain the fee structure:
   - Boltz fee: ~0.1% of amount
   - Miner fee: ~19 Sats
   - Limits: 1,000 - 25,000,000 Sats
4. Show total cost (invoice amount + fees) and ask for confirmation
5. Use lightning_send to execute the swap
6. Wait for completion (may take 1-3 minutes)
7. Show the result:
   - Swap ID for reference
   - Preimage (proof of payment)
   - Explorer link for lockup transaction
8. If swap fails, explain that L-BTC is locked until timeout and can be refunded""",
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
