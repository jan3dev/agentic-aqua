# Agentic AQUA

MCP server and CLI for managing **Liquid Network** and **Bitcoin** wallets through AI assistants like Claude. One mnemonic backs both networks (unified wallet). Also can operates on Lightning network via Boltz swaps.

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

> **Quickest way:** just ask your AI agent directly:
>
> ```
> Install this MCP server: https://github.com/jan3dev/agentic-aqua
> ```

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
    "agentic-aqua": {
      "command": "/full/path/to/uvx",
      "args": ["agentic-aqua"]
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
git clone https://github.com/jan3dev/agentic-aqua.git
cd agentic-aqua
uv sync
```

Configure Claude Desktop using the full path to `uv` (find with `which uv`):

```json
{
  "mcpServers": {
    "agentic-aqua": {
      "command": "/full/path/to/uv",
      "args": ["run", "--directory", "/absolute/path/to/agentic-aqua", "python", "-m", "aqua.server"]
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

## CLI

Agentic AQUA also ships with a Click-based CLI (`aqua-cli`) for direct, scriptable wallet operations. It exposes the same operations as the MCP tools.

```bash
# Discover commands
aqua-cli --help
aqua-cli wallet --help
aqua-cli btc --help
aqua-cli liquid --help
aqua-cli lightning --help

# Wallet management
aqua-cli wallet generate-mnemonic
aqua-cli wallet import-mnemonic --wallet-name default --network mainnet
aqua-cli wallet list
aqua-cli wallet export-descriptor --wallet-name default
aqua-cli wallet delete --wallet-name old

# Balances
aqua-cli balance                              # unified (BTC + Liquid)
aqua-cli btc balance --wallet-name default
aqua-cli liquid balance --wallet-name default

# Receive addresses
aqua-cli btc address
aqua-cli liquid address

# Send (--wallet-name is required for on-chain sends)
aqua-cli btc send    --wallet-name default --address bc1... --amount 10000
aqua-cli liquid send --wallet-name default --address lq1... --amount 50000
aqua-cli liquid send-asset --wallet-name default --address lq1... --amount 1000000 --asset-id <asset_id>
# (or use --asset-ticker USDt instead of --asset-id)

# Transaction history & status
aqua-cli btc transactions
aqua-cli liquid transactions
aqua-cli liquid tx-status --tx <txid|explorer_url>

# Lightning (L-BTC via Boltz / Ankara)
aqua-cli lightning receive --amount 50000
aqua-cli lightning send --invoice lnbc...
aqua-cli lightning status --swap-id <id>

# Run as MCP stdio server
aqua-cli serve   # equivalent to `aqua`
```

Output defaults to a human-readable table on the terminal and JSON when piped. Force a format with `--format json` or `--format pretty`.

### Loading mnemonics safely (env vars from a text file)

Avoid pasting mnemonics into shell prompts or chat with an AI agent — both shell history and agent transcripts may persist them. The recommended workflow is to keep secrets in a local text file with restricted permissions and load them as environment variables.

1. Create `~/.aqua/secrets.env` (or any path you prefer) and lock it down:

   ```bash
   mkdir -p ~/.aqua
   cat > ~/.aqua/secrets.env <<'EOF'
   AQUA_MNEMONIC="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
   AQUA_PASSWORD="Wild-red-dolphin-386"
   EOF
   chmod 600 ~/.aqua/secrets.env
   ```

2. Source it before running CLI commands and clear it afterwards:

   ```bash
   set -a; . ~/.aqua/secrets.env; set +a
   aqua-cli wallet import-mnemonic --wallet-name default --network mainnet
   unset AQUA_MNEMONIC AQUA_PASSWORD
   ```

   `aqua-cli` also auto-loads a `.env` file from the project root via `python-dotenv` if you prefer a per-project file.

The CLI honors these variables out of the box:

| Variable | Used by |
|----------|---------|
| `AQUA_MNEMONIC` | `wallet import-mnemonic` |
| `AQUA_PASSWORD` | `wallet import-mnemonic`, `btc send`, `liquid send`, `liquid send-asset`, `lightning send`, `lightning receive` |
| `AQUA_<OPTION>` | Any CLI option (Click `auto_envvar_prefix="AQUA"`) — e.g. `AQUA_WALLET_NAME=default` |

If you would rather pipe secrets from a password manager, every secret-bearing command also accepts `--mnemonic-stdin` / `--password-stdin`:

```bash
pass show crypto/aqua-mnemonic | aqua-cli wallet import-mnemonic --mnemonic-stdin
```

Tips:
- Never commit `.env` or `secrets.env` files (the project's `.gitignore` already excludes them).
- Prefer `set -a; . file; set +a` over `export $(cat file)` — the former tolerates spaces and quotes inside values.
- After importing a wallet, the mnemonic is no longer needed for day-to-day operations; only `AQUA_PASSWORD` is used to sign transactions.

## Configuration

Default config location: `~/.aqua/config.json`

> **Migrating from `aqua-mcp`?** The config dir moved from `~/.aqua-mcp` to `~/.aqua`. There is no automatic migration. To carry over your wallets, run once:
>
> ```bash
> mv ~/.aqua-mcp ~/.aqua
> ```

```json
{
  "network": "mainnet",
  "default_wallet": "default",
  "electrum_url": null,
  "auto_sync": true
}
```

## Security

Mnemonics are encrypted at rest using a password (PBKDF2 + Fernet). Without a password, the mnemonic is stored base64-encoded only — use a password for real funds. **Note:** this password is NOT a BIP39 passphrase; the derived Liquid/Bitcoin keys depend solely on the mnemonic, so the same mnemonic restores identical descriptors in any BIP39-compliant wallet (AQUA, Blockstream Green, Jade, etc.).

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
