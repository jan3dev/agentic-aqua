"""MCP server for Liquid Wallet."""

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
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
}


def create_server() -> Server:
    """Create and configure the MCP server."""
    server = Server("liquid-wallet")

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
            # Get the tool function
            tool_fn = TOOLS[name]
            
            # Call the tool
            result = tool_fn(**arguments)
            
            # Format result
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
        logger.info(f"Liquid Wallet MCP v{__version__} starting...")
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
