"""MCP server for AQUA."""

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    Prompt,
    PromptMessage,
    PromptArgument,
    GetPromptResult,
    Resource,
    ResourceTemplate,
)

from . import __version__
from .tools import TOOLS

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Tool schemas for MCP
TOOL_SCHEMAS = {
    "lw_generate_mnemonic": {
        "description": "Generate a new BIP39 mnemonic phrase for creating a Liquid wallet",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "lw_import_mnemonic": {
        "description": "Import a wallet from a BIP39 mnemonic phrase",
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
                "passphrase": {
                    "type": "string",
                    "description": "Optional passphrase to encrypt the mnemonic at rest",
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
                    "description": "Amount in satoshis",
                },
                "passphrase": {
                    "type": "string",
                    "description": "Passphrase to decrypt mnemonic (if encrypted)",
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
                    "description": "Amount in satoshis",
                },
                "asset_id": {
                    "type": "string",
                    "description": "Asset ID (hex string)",
                },
                "passphrase": {
                    "type": "string",
                    "description": "Passphrase to decrypt mnemonic (if encrypted)",
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
    "btc_balance": {
        "description": "Get Bitcoin wallet balance in satoshis",
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
                    "description": "Amount in satoshis",
                },
                "fee_rate": {
                    "type": "integer",
                    "description": "Optional fee rate in sat/vB",
                },
                "passphrase": {
                    "type": "string",
                    "description": "Passphrase to decrypt mnemonic (if encrypted)",
                },
            },
            "required": ["wallet_name", "address", "amount"],
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
}


def create_server() -> Server:
    """Create and configure the MCP server."""
    server = Server(
        "aqua-mcp",
        instructions="""You are managing Bitcoin and Liquid Network cryptocurrency wallets.

STARTUP BEHAVIOR:
- FIRST ACTION: Always check existing wallets with lw_list_wallets
- Show user what wallets are already available locally
- This prevents re-importing seeds every session
- If wallet has passphrase, ask user for it when needed (signing transactions)

DEFAULTS:
- Network: MAINNET (unless user explicitly requests testnet)
- Balance queries: Use unified_balance (both networks) unless user specifies bitcoin or liquid only
- New wallets: Generate with passphrase by default for security
- Passphrase format: Use word-based passphrases like "Wild-red-dolphin-386" (memorable + secure)

CRITICAL SAFETY RULES:
- Amounts are in SATOSHIS (1 BTC = 100,000,000 sats)
- Always verify network: mainnet vs testnet
- Confirm transactions before broadcasting
- Show explorer links after sending
- STRONGLY recommend passphrases for wallet security, but allow user choice

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
1. Generate mnemonic with lw_generate_mnemonic
2. ASK user if they want to use a passphrase (STRONGLY RECOMMENDED)
3. If yes: ASK user for their passphrase
   - Give example: "Wild-red-dolphin-386" (Word1-word2-word3-###)
   - Wait for user to provide their chosen passphrase
4. Import wallet with mnemonic + user's passphrase (or no passphrase if declined)
5. Show user the mnemonic (and remind them of their passphrase if used)
6. Emphasize importance of backing up both securely

PASSPHRASE HANDLING:
- Wallets with encrypted mnemonics require passphrase for signing
- Ask user for passphrase when calling btc_send, lw_send, lw_send_asset
- If operation fails with decryption error, wallet likely has passphrase""",
    )

    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        """List available prompt templates."""
        return [
            # Wallet creation
            Prompt(
                name="create_new_wallet",
                description="Create a new wallet with mnemonic and passphrase",
                arguments=[
                    PromptArgument(name="wallet_name", description="Name for the wallet", required=False),
                    PromptArgument(name="network", description="mainnet or testnet", required=False),
                ],
            ),
            Prompt(
                name="import_seed",
                description="Import an existing wallet from a mnemonic",
                arguments=[
                    PromptArgument(name="wallet_name", description="Name for the wallet", required=False),
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
        ]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
        """Get a specific prompt template."""
        wallet_name = arguments.get("wallet_name", "default") if arguments else "default"
        network = arguments.get("network", "mainnet") if arguments else "mainnet"

        # Wallet creation
        if name == "create_new_wallet":
            return GetPromptResult(messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""I want to create a new wallet named '{wallet_name}' on {network}.

Please:
1. Generate a new 12-word mnemonic with lw_generate_mnemonic
2. Show me the mnemonic
3. Ask me: "Do you want to use a passphrase? (STRONGLY RECOMMENDED for security)"
4. If I say yes:
   - Ask me: "Please provide your passphrase. Example format: 'Wild-red-dolphin-386' (Word1-word2-word3-###)"
   - Wait for me to give you my chosen passphrase
   - Import wallet with my passphrase
5. If I say no:
   - Warn me that the mnemonic will only be base64-encoded (less secure)
   - Ask for confirmation
   - Import wallet without passphrase
6. Confirm wallet creation for both Bitcoin and Liquid
7. Remind me to backup the mnemonic (and passphrase if used) securely
8. Generate a receive address for Bitcoin and another for Liquid""",
                ),
            )])

        elif name == "import_seed":
            return GetPromptResult(messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""I want to import an existing mnemonic.

Please ask me:
1. The mnemonic (12 or 24 words)
2. If I want to use a passphrase (STRONGLY RECOMMENDED for security)
   - If yes: ask for the passphrase
   - If no: warn me it will be less secure (base64 only)
3. Network: mainnet or testnet (default: mainnet)
4. Wallet name (default: '{wallet_name}')

Then import and confirm that both Bitcoin and Liquid wallets were created.""",
                ),
            )])

        # Balance queries
        elif name == "show_balance":
            return GetPromptResult(messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""Show me the balance of my '{wallet_name}' wallet.

Use unified_balance to display:
- Bitcoin balance (in BTC and sats)
- Liquid balance (L-BTC and other assets if any)
- User-friendly format with BTC values, not just satoshis""",
                ),
            )])

        elif name == "bitcoin_balance":
            return GetPromptResult(messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""Show me only the Bitcoin balance of my '{wallet_name}' wallet.

Use btc_balance and display result in both BTC and satoshis.""",
                ),
            )])

        elif name == "liquid_balance":
            return GetPromptResult(messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""Show me the Liquid balance of my '{wallet_name}' wallet.

Use lw_balance and display all assets with their tickers and amounts.""",
                ),
            )])

        # Addresses
        elif name == "generate_address":
            network_arg = arguments.get("network", "bitcoin") if arguments else "bitcoin"
            tool = "btc_address" if network_arg == "bitcoin" else "lw_address"
            return GetPromptResult(messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""Generate an address to receive {network_arg.upper()} in my '{wallet_name}' wallet.

Use {tool} and show me the address in a clear format.""",
                ),
            )])

        # Transactions
        elif name == "show_transactions":
            if arguments and "network" in arguments:
                net = arguments["network"]
                tool = "btc_transactions" if net == "bitcoin" else "lw_transactions"
                return GetPromptResult(messages=[PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"""Show me the recent {net.upper()} transactions from my '{wallet_name}' wallet.

Use {tool} with limit=10 and display in readable format with dates, amounts, and txids.""",
                    ),
                )])
            else:
                return GetPromptResult(messages=[PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"""Show me the transactions from my '{wallet_name}' wallet.

Display transactions from BOTH networks (Bitcoin and Liquid) in chronological order.""",
                    ),
                )])

        elif name == "send_bitcoin":
            return GetPromptResult(messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""I want to send Bitcoin from my '{wallet_name}' wallet.

Please:
1. Show my current Bitcoin balance
2. Ask me for:
   - Destination address (bc1...)
   - Amount (accept in BTC, convert to satoshis)
   - Fee rate (optional, suggest: 2-10 sat/vB based on urgency)
3. Verify the address is valid and mainnet
4. Show me a summary BEFORE sending:
   - Amount: X BTC (Y sats)
   - Estimated fees
   - Destination address
5. Ask for explicit confirmation
6. If wallet has passphrase, ask me for it
7. Send with btc_send
8. Show txid and explorer link""",
                ),
            )])

        elif name == "send_liquid":
            return GetPromptResult(messages=[PromptMessage(
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
6. Ask for confirmation and passphrase if applicable
7. Send with lw_send or lw_send_asset
8. Show txid and explorer link""",
                ),
            )])

        elif name == "transaction_status":
            return GetPromptResult(messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""I want to check the status of a transaction.

Please ask me for:
- The txid or explorer URL
- Which network (bitcoin or liquid)

Then use lw_tx_status (for Liquid) or check Bitcoin explorer and show:
- Status (confirmed/pending)
- Number of confirmations
- Amount
- Explorer link""",
                ),
            )])

        # Management
        elif name == "list_wallets":
            return GetPromptResult(messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text="""Show me all my wallets.

Use lw_list_wallets and display in table format with:
- Name
- Network (mainnet/testnet)
- Type (full/watch-only)
- Whether it has passphrase (encrypted)""",
                ),
            )])

        elif name == "export_descriptor":
            return GetPromptResult(messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""Export the descriptor from my '{wallet_name}' wallet for watch-only use.

Use lw_export_descriptor and explain:
- What the descriptor is for
- How to import it in another wallet as watch-only
- That it does NOT provide access to sign transactions""",
                ),
            )])

        raise ValueError(f"Unknown prompt: {name}")

    @server.list_resources()
    async def list_resources() -> list[Resource]:
        """List available documentation resources."""
        return [
            Resource(
                uri="aqua://docs/quickstart",
                name="Quick Start Guide",
                description="Getting started with AQUA MCP wallet management",
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
            return """# AQUA MCP Quick Start

## Creating a New Wallet (Recommended Method)

1. Generate a mnemonic: `lw_generate_mnemonic()`
2. Generate a strong passphrase (or let the system generate one)
3. Import it: `lw_import_mnemonic(mnemonic="your 12 words", network="mainnet", passphrase="your-passphrase")`
4. This creates BOTH a Liquid and Bitcoin wallet from the same mnemonic
5. **BACKUP BOTH**: Ask user to save mnemonic + passphrase securely offline

## Creating Without Passphrase (Not Recommended)

If you need convenience over security:
- `lw_import_mnemonic(mnemonic="your words", network="mainnet")`
- ⚠️ **WARNING**: Mnemonic stored with only base64 encoding (not encrypted)
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

- Bitcoin: `btc_send(wallet_name="default", address="bc1...", amount=10000, passphrase="secret")`
- Liquid (L-BTC): `lw_send(wallet_name="default", address="lq1...", amount=10000, passphrase="secret")`
- Liquid (other assets): `lw_send_asset(..., asset_id="ce091c99...")`

Note: If wallet has no passphrase, omit the passphrase parameter"""

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
| Fees | Dynamic (sat/vB) | Fixed (~33 sats) |"""

        elif uri == "aqua://docs/security":
            return """# Security Best Practices

## ⚠️ IMPORTANT: Mnemonic Storage

**Passphrase usage is STRONGLY RECOMMENDED but optional**:

```python
# ⚠️ NOT RECOMMENDED - stores mnemonic with base64 only (not encrypted)
# Use only for testing or small amounts
lw_import_mnemonic(mnemonic="abandon abandon...")

# ✅ RECOMMENDED - encrypts mnemonic on disk with strong encryption
lw_import_mnemonic(
    mnemonic="abandon abandon...",
    passphrase="strong-password-here"
)
```

**Trade-offs**:
- **With passphrase**: Secure against malware/file access, but you must remember it
- **Without passphrase**: Convenient but only protected by filesystem permissions (base64 encoding)

## Passphrase Requirements

- **Strong**: Use 12+ characters, mix letters/numbers/symbols
- **Unique**: Don't reuse passwords from other services
- **Backed up**: Store securely offline (password manager, paper backup)
- **Required for signing**: You'll need it every time you send funds

## Wallet Security Checklist

- ✅ Always encrypt mnemonics with passphrases
- ✅ Back up mnemonics offline (paper, metal, encrypted USB)
- ✅ Verify addresses before sending (double-check network)
- ✅ Start with small test transactions
- ✅ Use watch-only wallets for monitoring (export with `lw_export_descriptor`)
- ✅ Keep mainnet and testnet wallets separate

## What NOT to Do

- ❌ Store mnemonics in cloud services (Dropbox, Google Drive)
- ❌ Share mnemonics in chat/email/screenshots
- ❌ Import wallets without passphrases
- ❌ Send mainnet funds to testnet addresses
- ❌ Ignore address network prefixes

## Watch-Only Wallets

For monitoring without signing risk:

1. Export descriptor: `lw_export_descriptor(wallet_name="main")`
2. Import as watch-only: `lw_import_descriptor(descriptor="ct(...)", wallet_name="monitor")`
3. Monitor balance without exposing private keys

## Recovery

If you have:
- ✅ Mnemonic + passphrase → Full recovery possible
- ✅ Mnemonic only (if stored plaintext) → Full recovery possible
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
        logger.info(f"AQUA MCP v{__version__} starting...")
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
