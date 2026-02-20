# 🌊 Liquid Wallet MCP

MCP server for managing Liquid Network wallets through AI assistants like Claude.

## Features

- 🔑 **Generate & Import** - Create new wallets or import existing mnemonics
- 👀 **Watch-Only** - Import CT descriptors for balance monitoring
- 💸 **Send & Receive** - Full transaction support with signing
- 🪙 **Assets** - Native support for L-BTC, USDT, and all Liquid assets
- 🔒 **Secure** - Encrypted storage, no remote servers for keys

## Installation

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Clone and install

```bash
git clone https://github.com/jan3dev/mcp-liquid-wallet.git
cd mcp-liquid-wallet
uv sync
```

This creates a virtual environment and installs all dependencies.

## Quick Start

### 1. Run the MCP server

```bash
# Using uv
uv run python -m liquid_wallet.server

# Or activate venv first
source .venv/bin/activate
python -m liquid_wallet.server
```

### 2. Configure your MCP client

Add to your MCP client configuration (e.g., Claude Desktop `~/.claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "liquid-wallet": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-liquid-wallet", "python", "-m", "liquid_wallet.server"]
    }
  }
}
```

### 3. Use with Claude

Once connected, you can ask Claude to:

- "Create a new Liquid wallet"
- "Show me my L-BTC balance"
- "Generate a new receive address"
- "Send 0.001 L-BTC to lq1..."

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

| Tool | Description |
|------|-------------|
| `lw_generate_mnemonic` | Generate new BIP39 mnemonic |
| `lw_import_mnemonic` | Import wallet from mnemonic |
| `lw_import_descriptor` | Import watch-only wallet |
| `lw_export_descriptor` | Export CT descriptor |
| `lw_balance` | Get wallet balances |
| `lw_address` | Generate receive address |
| `lw_send` | Send L-BTC |
| `lw_send_asset` | Send any Liquid asset |
| `lw_transactions` | Transaction history |
| `lw_list_wallets` | List all wallets |

## Configuration

Default config location: `~/.liquid-wallet/config.json`

```json
{
  "network": "mainnet",
  "default_wallet": "default",
  "electrum_url": null,
  "auto_sync": true
}
```

### Networks

- `mainnet` - Liquid mainnet (real funds)
- `testnet` - Liquid testnet (test funds)

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
uv sync --dev

# Run tests
uv run pytest

# Format code
uv run black src/
uv run ruff check src/
```

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  AI Assistant   │────▶│   MCP Server    │────▶│   LWK (Rust)    │
│  (Claude, etc)  │     │   (Python)      │     │   via bindings  │
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                                                         ▼
                                                ┌─────────────────┐
                                                │ Electrum/Esplora│
                                                │  (Blockstream)  │
                                                └─────────────────┘
```

## Contributing

PRs welcome! Please read `AGENTS.md` for specs and conventions.

## License

MIT

## Credits

Built with:
- [LWK](https://github.com/Blockstream/lwk) - Liquid Wallet Kit by Blockstream
- [MCP](https://modelcontextprotocol.io/) - Model Context Protocol

---

⚡ Built for the Liquid Network
