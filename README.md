# AQUA MCP

MCP server for managing **Liquid Network** and **Bitcoin** wallets through AI assistants like Claude. One mnemonic backs both networks (unified wallet).

## Features

- **Generate & Import** - Create new wallets or import existing mnemonics
- **Unified Wallet** - One mnemonic for Liquid and Bitcoin; `unified_balance` shows both
- **Bitcoin (onchain)** - BIP84 wallets, balance and send via `btc_*` tools (BDK)
- **Watch-Only** - Import CT descriptors for balance monitoring
- **Send & Receive** - Full transaction support (L-BTC, BTC, and Liquid assets)
- **Lightning** - Send and receive via Lightning using L-BTC (via Boltz & Ankara)
- **Assets** - Native support for L-BTC, USDT, and all Liquid assets
- **Secure** - Encrypted storage, no remote servers for keys

## Installation

### Recommended (uvx)

If you don't have `uvx` installed:

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Configure Claude Desktop (`~/.claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "aqua-mcp": {
      "command": "/full/path/to/uvx",
      "args": ["aqua-mcp"]
    }
  }
}
```

Find the full path to `uvx` with:

```bash
which uvx
# Example: /Users/yourname/.local/bin/uvx
```

Restart Claude Desktop and you're ready to use Bitcoin and Liquid wallets.

### For Developers

Clone and install from source:

```bash
git clone https://github.com/jan3dev/aqua-mcp.git
cd aqua-mcp
uv sync
```

Configure Claude Desktop using the full path to `uv` (find with `which uv`):

```json
{
  "mcpServers": {
    "aqua-mcp": {
      "command": "/full/path/to/uv",
      "args": ["run", "--directory", "/absolute/path/to/aqua-mcp", "python", "-m", "aqua_mcp.server"]
    }
  }
}
```

## Quick Start

Once connected, you can ask Claude to:

- "Create a new wallet" (creates both Bitcoin and Liquid wallets from one mnemonic)
- "Show my balance" / "What's my Bitcoin balance?"
- "Generate a receive address" (Liquid or Bitcoin)
- "Send 10,000 sats to bc1..." / "Send 0.001 L-BTC to lq1..."
- "Pay this Lightning invoice: lnbc..."
- "Receive 50,000 sats via Lightning"
- "Delete my wallet"

## Available Tools

**Wallet Management**

| Tool | Description |
|------|-------------|
| `lw_generate_mnemonic` | Generate new BIP39 mnemonic |
| `lw_import_mnemonic` | Import wallet from mnemonic (also creates Bitcoin wallet) |
| `lw_import_descriptor` | Import watch-only wallet from CT descriptor |
| `lw_export_descriptor` | Export CT descriptor for watch-only use |
| `lw_list_wallets` | List all wallets |
| `delete_wallet` | Delete a wallet and all its cached data |

**Liquid (lw_*)**

| Tool | Description |
|------|-------------|
| `lw_balance` | Get wallet balances (all assets) |
| `lw_address` | Generate Liquid receive address (lq1...) |
| `lw_send` | Send L-BTC |
| `lw_send_asset` | Send any Liquid asset (USDT, etc.) |
| `lw_transactions` | Transaction history |
| `lw_tx_status` | Get transaction status (txid or explorer URL) |

**Bitcoin (btc_*)**

| Tool | Description |
|------|-------------|
| `btc_balance` | Get Bitcoin balance (sats) |
| `btc_address` | Generate Bitcoin receive address (bc1...) |
| `btc_transactions` | Bitcoin transaction history |
| `btc_send` | Send BTC |

**Unified**

| Tool | Description |
|------|-------------|
| `unified_balance` | Get balance for both Bitcoin and Liquid |

**Lightning**

| Tool | Description |
|------|-------------|
| `lightning_receive` | Generate a Lightning invoice to receive L-BTC (100–25,000,000 sats) |
| `lightning_send` | Pay a Lightning invoice using L-BTC via Boltz (~0.1% fee) |
| `lightning_transaction_status` | Check status of a Lightning swap (send or receive) |

## Configuration

Default config location: `~/.aqua-mcp/config.json`

```json
{
  "network": "mainnet",
  "default_wallet": "default",
  "electrum_url": null,
  "auto_sync": true
}
```

## Security

Mnemonics are encrypted at rest using a passphrase (PBKDF2 + Fernet). Without a passphrase, the mnemonic is stored base64-encoded only — use a passphrase for real funds.

For maximum security you can:
1. Generate wallet on an air-gapped device
2. Export the CT descriptor
3. Import as watch-only on your daily machine

All private key operations happen locally. Only blockchain sync uses Blockstream's public servers.

## Development

```bash
# Install with dev dependencies
uv sync --all-extras

# Run tests
uv run python -m pytest tests/

# Format code
uv run black src/
uv run ruff check src/
```

## Architecture

```
AI Assistant ←→ MCP Server (Python) ←→ LWK (Liquid) ──→ Electrum/Esplora
                       │
                       ├──→ BDK (Bitcoin) ──→ Esplora (Blockstream)
                       │
                       └──→ Boltz / Ankara ──→ Lightning
```

## Credits

Built with:
- [LWK](https://github.com/Blockstream/lwk) - Liquid Wallet Kit by Blockstream
- [BDK](https://github.com/bitcoindevkit/bdk-python) - Bitcoin Development Kit
- [MCP](https://modelcontextprotocol.io/) - Model Context Protocol
- [Boltz](https://boltz.exchange/) - Submarine swaps for Lightning
