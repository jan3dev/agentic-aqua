# Agentic AQUA

MCP server and CLI for managing **Bitcoin** and **Liquid Network** wallets through AI assistants like Claude. One seed backs both networks (unified wallet). Agentic AQUA can also operate on the Lightning Network.

## Features

- **Generate & Import** - Create new wallets or import existing seeds
- **Unified Wallet** - One seed (mnemonic) for Bitcoin and Liquid; `unified_balance` shows both
- **Bitcoin (onchain)** - BIP84 wallets, balance and send via `btc_*` tools (BDK)
- **Watch-Only** - Import CT descriptors for balance monitoring
- **Send & Receive** - Full transaction support (L-BTC, BTC, and Liquid assets like USDt)
- **Lightning** - Send and receive via Lightning using L-BTC
- **Assets** - Native support for L-BTC, USDt, and all Liquid assets
- **Swaps & Pegs** - Convert BTC ↔ L-BTC and swap Liquid/cross-chain assets via SideSwap, SideShift, and Changelly
- **JAN3 Account** - Login, Lightning Address, and WapuPay (pay ARS bank accounts with USDT) via your JAN3 account
- **Secure** - Encrypted storage, no remote servers for keys

## Installation

> **Quickest way:** just ask your AI agent directly:
>
> ```
> Install this MCP server: https://pypi.org/project/agentic-aqua/
> ```

> **Python 3.13 required.**

### Recommended (uv tool install)

If you don't have `uv` installed:

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Install Agentic AQUA:

```bash
uv tool install --python 3.13 agentic-aqua
```

This creates a permanent `agentic-aqua` executable. Find its full path with:

```bash
which agentic-aqua
# Example: /Users/yourname/.local/bin/agentic-aqua
```

Configure Claude Desktop (`~/.claude/claude_desktop_config.json`) using that path:

```json
{
  "mcpServers": {
    "agentic-aqua": {
      "command": "/full/path/to/agentic-aqua",
      "args": []
    }
  }
}
```

Restart Claude Desktop and you're ready to use Bitcoin and Liquid wallets.

> **Updating / removing:** `uv tool upgrade agentic-aqua` to update, `uv tool uninstall agentic-aqua` to remove.

### For Developers

Clone and install from source:

```bash
git clone https://github.com/jan3dev/agentic-aqua.git
cd agentic-aqua
uv python install 3.13
uv sync --python 3.13
```

Configure Claude Desktop using the full path to `uv` (find with `which uv`):

```json
{
  "mcpServers": {
    "agentic-aqua": {
      "command": "/full/path/to/uv",
      "args": ["run", "--python", "3.13", "--directory", "/absolute/path/to/agentic-aqua", "python", "-m", "aqua.server"]
    }
  }
}
```

## Quick Start

Once connected, you can ask Claude to:

- "Create a new wallet" (creates both Bitcoin and Liquid wallets from one seed)
- "Show my balance" / "What's my Bitcoin balance?"
- "Generate a receive address" (Liquid or Bitcoin)
- "Send 10,000 Sats to bc1..." / "Send 0.001 L-BTC to lq1..."
- "Pay this Lightning invoice: lnbc..."
- "Receive 50,000 Sats via Lightning"
- "Delete my wallet"

## Available Tools

**Wallet Management**

| Tool | Description |
|------|-------------|
| `lw_generate_mnemonic` | Generate new BIP39 seed |
| `lw_import_mnemonic` | Import wallet from seed (also creates Bitcoin wallet) |
| `lw_import_descriptor` | Import watch-only Liquid wallet from CT descriptor |
| `lw_export_descriptor` | Export Liquid CT descriptor for watch-only use |
| `btc_import_descriptor` | Import watch-only Bitcoin wallet from BIP84 descriptor |
| `btc_export_descriptor` | Export Bitcoin BIP84 descriptors + xpub |
| `lw_list_wallets` | List all wallets |
| `delete_wallet` | Delete a wallet and all its cached data |

> ⚠️ Bitcoin and Liquid descriptors cannot be derived from each other (different BIP84 paths + Liquid's SLIP-77 blinding key). To watch a unified wallet, import both descriptors separately.

**Liquid (lw_*)**

| Tool | Description |
|------|-------------|
| `lw_balance` | Get wallet balances (all assets) |
| `lw_address` | Generate Liquid receive address (lq1...) |
| `lw_send` | Send L-BTC |
| `lw_send_asset` | Send any Liquid asset (USDt, etc.) |
| `lw_sweep` | Sweep the entire L-BTC (or one asset) balance to one address |
| `lw_list_assets` | List known Liquid assets (asset_id, ticker, name, precision) |
| `lw_transactions` | Transaction history |
| `lw_tx_status` | Get transaction status (txid or explorer URL) |

**Bitcoin (btc_*)**

| Tool | Description |
|------|-------------|
| `btc_balance` | Get Bitcoin balance (sats) |
| `btc_address` | Generate Bitcoin receive address (bc1...) |
| `btc_transactions` | Bitcoin transaction history |
| `btc_send` | Send BTC |
| `btc_sweep` | Sweep the entire Bitcoin balance to one address |

**Unified**

| Tool | Description |
|------|-------------|
| `unified_balance` | Get balance for both Bitcoin and Liquid |

**Lightning**

| Tool | Description |
|------|-------------|
| `lightning_receive` | Generate a Lightning invoice to receive L-BTC (100–25,000,000 Sats) |
| `lightning_send` | Pay a Lightning invoice using L-BTC via Boltz (~0.1% fee) |
| `lightning_transaction_status` | Check status of a Lightning swap (send or receive) |
| `lightning_decode` | Decode a BOLT11 invoice without paying it |

**Swaps — SideSwap (`sideswap_*`)** — BTC ↔ L-BTC pegs and atomic Liquid asset swaps

| Tool | Description |
|------|-------------|
| `sideswap_server_status` | Live fees, peg minimums, hot-wallet balance |
| `sideswap_recommend` | Recommend a peg vs an instant swap for a BTC ↔ L-BTC conversion |
| `sideswap_peg_quote` | Quote the receive amount for a peg at current fees |
| `sideswap_peg_in` | Peg BTC → L-BTC |
| `sideswap_peg_out` | Peg L-BTC → BTC |
| `sideswap_peg_status` | Check status of a peg order |
| `sideswap_list_assets` | List Liquid assets SideSwap supports for atomic swaps |
| `sideswap_quote` | Read-only price quote for a Liquid asset swap |
| `sideswap_execute_swap` | Execute an atomic Liquid asset swap (L-BTC ↔ USDt, etc.) |
| `sideswap_swap_status` | Check status of an atomic asset swap |

**Swaps — SideShift (`sideshift_*`)** — custodial cross-chain swaps (USDt across chains, BTC ↔ USDt-on-X)

| Tool | Description |
|------|-------------|
| `sideshift_list_coins` | List supported coins and networks |
| `sideshift_pair_info` | Rate / min / max for a pair |
| `sideshift_quote` | Fixed-rate quote (~15 min TTL) |
| `sideshift_recommend` | Recommend SideSwap vs SideShift for a pair |
| `sideshift_send` | Send funds from the wallet via a fixed-rate shift |
| `sideshift_receive` | Receive into the wallet via a variable-rate shift |
| `sideshift_status` | Check status of a shift order |

**Swaps — Changelly (`changelly_*`)** — USDt-Liquid ↔ USDt on Ethereum/Tron/BSC/Solana/Polygon

| Tool | Description |
|------|-------------|
| `changelly_list_currencies` | List currencies Changelly supports |
| `changelly_quote` | Fixed-rate quote for a USDt-Liquid ↔ USDt-on-X swap |
| `changelly_send` | Send USDt-Liquid out to USDt on another chain |
| `changelly_receive` | Receive USDt-Liquid from USDt on another chain |
| `changelly_status` | Check status of a swap order |

**WapuPay (`wapupay_*`)** — pay Argentine bank accounts in ARS, funded with USDT on Liquid

| Tool | Description |
|------|-------------|
| `wapupay_exchange_rates` | Current exchange rates (e.g. USDT/ARS); public, no key needed |
| `wapupay_quote` | Preview USDT cost, fee, and rate for an ARS payment |
| `wapupay_create_order` | Create a direct-fiat order; returns a Liquid USDT funding address |
| `wapupay_fund_order` | Re-issue funding instructions for an existing order |
| `wapupay_order_status` | Check a direct-fiat order's status |
| `wapupay_orders` | List locally-tracked orders |
| `wapupay_transactions` | List WapuPay transactions |
| `wapupay_transaction` | Get a single transaction by id |
| `wapupay_spending_limit` | Monthly spending limit (USDT) for the account/key |
| `wapupay_provision_account` | Provision a WapuPay API key via your JAN3 account |

**JAN3 Account (`jan3_*`)** — login, sessions, and Lightning Address for your JAN3 account

| Tool | Description |
|------|-------------|
| `jan3_login` / `jan3_verify` | Default login flow: email OTP → verify, saves the session |
| `jan3_login_start` / `jan3_login_complete` | Fallback paid captchaless login flow |
| `jan3_session_info` | Status/metadata for a persisted session |
| `jan3_list_sessions` | List all persisted sessions |
| `jan3_logout` | Delete a persisted session |
| `jan3_user_info` | Account profile + Lightning Address status |
| `jan3_enable_lightning_address` | Enable/disable the Lightning Address |
| `jan3_rebind_wallet` | Re-bind Lightning Address delivery to a different wallet (destructive) |
| `jan3_ln_check_username` | Check if a Lightning username is available |
| `jan3_purchase_ln_username` | Buy/update the Lightning username (on-chain payment) |

**Utilities**

| Tool | Description |
|------|-------------|
| `qr_generate` | Generate a PNG QR code for any content (address, invoice, URI) |
| `qr_decode` | Decode a QR code from an image file |
| `doctor` | Diagnose (and optionally repair) the `~/.aqua/config.json` config file |

## CLI

Agentic AQUA also ships with a Click-based CLI (`aqua`) for direct, scriptable wallet operations. It exposes the same operations as the MCP tools.

```bash
# Discover commands
aqua --help
aqua wallet --help
aqua btc --help
aqua liquid --help
aqua lightning --help
aqua sideswap --help
aqua sideshift --help
aqua changelly --help
aqua wapupay --help
aqua jan3 --help
aqua qr --help

# Wallet management
aqua wallet generate-mnemonic
aqua wallet import-mnemonic --mnemonic-stdin --wallet-name default --network mainnet --password-stdin
aqua wallet list
aqua wallet delete --wallet-name old

# Watch-only descriptors (Bitcoin and Liquid are separate)
aqua btc export-descriptor    --wallet-name default
aqua btc import-descriptor    --wallet-name cold --descriptor "wpkh([fp/84h/0h/0h]xpub.../0/*)#cs"
aqua liquid export-descriptor --wallet-name default
aqua liquid import-descriptor --wallet-name cold --descriptor "ct(slip77(...),elwpkh(...))"

# Balances
aqua balance                              # unified (BTC + Liquid)
aqua btc balance --wallet-name default
aqua liquid balance --wallet-name default

# Receive addresses
aqua btc address
aqua liquid address

# Send (--wallet-name is required for on-chain sends)
aqua btc send    --wallet-name default --address bc1... --amount 10000
aqua liquid send --wallet-name default --address lq1... --amount 50000
aqua liquid send-asset --wallet-name default --address lq1... --amount 1000000 --asset-id <asset_id>
# (or use --asset-ticker USDt instead of --asset-id)

# Transaction history & status
aqua btc transactions
aqua liquid transactions
aqua liquid tx-status --tx <txid|explorer_url>

# Lightning (L-BTC via Boltz / Ankara)
aqua lightning receive --amount 50000
aqua lightning send --invoice lnbc...
aqua lightning status --swap-id <id>
aqua lightning decode --invoice lnbc...

# Swaps & pegs
aqua sideswap peg-in --wallet-name default                       # BTC -> L-BTC
aqua sideswap peg-out --amount 50000 --btc-address bc1... --wallet-name default
aqua sideswap swap --asset-ticker USDt --amount 50000 --wallet-name default
aqua sideshift send --deposit-coin btc --deposit-network liquid --settle-coin usdt --settle-network tron \
  --settle-address T... --deposit-amount 0.001 --wallet-name default
aqua changelly send --external-network tron --settle-address T... --amount-from 100 --wallet-name default

# WapuPay (pay ARS bank accounts, funded with USDT on Liquid)
aqua wapupay quote --amount-ars 10000 --alias some.alias
aqua wapupay create-order --amount-ars 10000 --alias some.alias --wallet-name default
# then fund the returned address:
aqua liquid send-asset --wallet-name default --address <funding_address> --amount <amount> --asset-ticker USDt

# QR
aqua qr decode ./invoice-qr.png

# Run as MCP stdio server
aqua serve       # recommended
aqua-mcp         # direct MCP entrypoint
```

Output defaults to a human-readable table on the terminal and JSON when piped. Force a format with `--format json` or `--format pretty`.

### Loading seeds safely

Avoid pasting seeds into the chat with your agent. Because it will persist in logs and will be sent to the AI provider, agent transcripts may persist them. The recommended workflow is to use this command that hides the text input:

```bash
aqua wallet import-mnemonic --mnemonic-stdin --wallet-name defaultx --network mainnet --password-stdin
```

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
- After importing a wallet, the seed is no longer needed for day-to-day operations; only `AQUA_PASSWORD` is used to sign transactions.

### JAN3 account login

Two login flows, both multi-account (one session per email), ending with a locally saved session:

```bash
# Default (free email-OTP)
aqua jan3 login --email you@example.com          # emails an OTP
aqua jan3 verify --email you@example.com --otp 123456

# Fallback (paid captchaless), for accounts that can't use the free flow
aqua jan3 login-start --email you@example.com --wallet-name default --password-stdin
aqua jan3 login-complete --email you@example.com --otp 123456
```

Once logged in:

```bash
aqua jan3 list-sessions
aqua jan3 user-info --email you@example.com
aqua jan3 logout --email you@example.com
```

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

Seeds are encrypted at rest using a password (PBKDF2 + Fernet). Without a password, the seed is stored base64-encoded only — use a password for real funds. **Note:** this password is NOT a BIP39 passphrase; the derived Liquid/Bitcoin keys depend solely on the seed, so the same seed restores identical descriptors in any BIP39-compliant wallet (AQUA, Blockstream App, Jade, etc.).

For maximum security you can:
1. Generate wallet on an air-gapped device
2. Export the CT descriptor
3. Import as watch-only on your daily machine

All private key operations happen locally. Only blockchain sync uses Blockstream's public servers.

## Development

```bash
# Install with dev dependencies
uv python install 3.13
uv sync --python 3.13 --all-extras

# Run tests
uv run --python 3.13 python -m pytest tests/

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

