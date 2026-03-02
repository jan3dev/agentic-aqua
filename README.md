# 💧 AQUA MCP

MCP server for managing **Liquid Network** and **Bitcoin** wallets through AI assistants like Claude. One mnemonic can back both networks (unified wallet).

## Features

- 🔑 **Generate & Import** - Create new wallets or import existing mnemonics
- 🔗 **Unified Wallet** - One mnemonic for Liquid and Bitcoin; `unified_balance` shows both
- ₿ **Bitcoin (onchain)** - BIP84 wallets, balance and send via `btc_*` tools (BDK)
- 👀 **Watch-Only** - Import CT descriptors for balance monitoring
- 💸 **Send & Receive** - Full transaction support (L-BTC, BTC, and Liquid assets)
- 🪙 **Assets** - Native support for L-BTC, USDT, and all Liquid assets
- 🔒 **Secure** - Encrypted storage, no remote servers for keys

## Installation

### For End Users (Easiest!)

If you don't have `uvx` installed:

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then configure Claude Desktop (`~/.claude/claude_desktop_config.json`):

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

**Important:** You can use the full path to `uvx` because Claude Desktop (macOS GUI app) doesn't inherit your shell's PATH. Find it with:

```bash
which uvx
# Example output: /Users/yourname/.local/bin/uvx
```

Restart Claude Desktop and you're ready to use Liquid wallets.

### For Developers

Clone and install from source:

```bash
git clone https://github.com/jan3dev/aqua-mcp.git
cd aqua-mcp
uv sync
```

Configure Claude Desktop. Use the **full path** to `uv` (e.g. `which uv`) so the app finds it:

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

### Use with Claude

Once connected, you can ask Claude to:

- "Create a new Liquid wallet" (also creates Bitcoin wallet from same mnemonic)
- "Show me my L-BTC balance" / "What's my Bitcoin balance?"
- "Show my unified balance (Bitcoin and Liquid)"
- "Generate a new receive address" (Liquid or Bitcoin)
- "Send 0.001 L-BTC to lq1..." / "Send 10,000 sats to bc1..."

## Usage Examples

### Create a New Wallet

```
User: Create a new Liquid wallet for me

Claude: I'll generate a new wallet for you.
[Uses lw_generate_mnemonic → lw_import_mnemonic]

Your new wallet has been created!
Mnemonic (SAVE THIS SECURELY):
  abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about

⚠️ Write this down and store it safely. Anyone with these words can access your funds.
```

### Check Balance

```
User: What's my Liquid balance?

Claude: [Uses lw_balance]

Your balances:
- L-BTC: 0.00100000 (100,000 sats)
- USDT: 50.00
```

### Send Transaction

```
User: Send 50,000 sats to lq1qqw8...

Claude: [Uses lw_send]

Transaction sent!
TXID: 7f3a8b2c...
Fee: 250 sats
```

## Available Tools

**Liquid (lw_*)**

| Tool | Description |
|------|-------------|
| `lw_generate_mnemonic` | Generate new BIP39 mnemonic |
| `lw_import_mnemonic` | Import wallet from mnemonic (also creates Bitcoin wallet) |
| `lw_import_descriptor` | Import watch-only wallet |
| `lw_export_descriptor` | Export CT descriptor |
| `lw_balance` | Get wallet balances (all assets) |
| `lw_address` | Generate Liquid receive address |
| `lw_send` | Send L-BTC |
| `lw_send_asset` | Send any Liquid asset |
| `lw_transactions` | Transaction history |
| `lw_tx_status` | Get transaction status (txid or explorer URL) |
| `lw_list_wallets` | List all wallets |

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

### Networks

- **Liquid**: `mainnet` (real funds), `testnet` (test funds) — Electrum/Esplora (Blockstream)
- **Bitcoin**: `mainnet`, `testnet` — Esplora (Blockstream)

## Security

### Mnemonic Storage

Mnemonics are encrypted at rest using a passphrase. On first use, you'll be prompted to set a passphrase.

### Watch-Only Mode

For maximum security, you can:
1. Generate wallet on an air-gapped device
2. Export the CT descriptor
3. Import as watch-only on your daily machine
4. Sign transactions on the air-gapped device

### No Remote Keys

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
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  AI Assistant   │────▶│   MCP Server    │────▶│   LWK (Liquid)  │───▶ Electrum/Esplora
│  (Claude, etc)  │     │   (Python)      │     └─────────────────┘
└─────────────────┘     └────────┬────────┘     ┌─────────────────┐
                                 │              │   BDK (Bitcoin) │───▶ Esplora
                                 └──────────────└─────────────────┘     (Blockstream)
```

## Credits

Built with:
- [LWK](https://github.com/Blockstream/lwk) - Liquid Wallet Kit by Blockstream
- [BDK](https://github.com/bitcoindevkit/bdk-python) - Bitcoin Development Kit
- [MCP](https://modelcontextprotocol.io/) - Model Context Protocol

