# AQUA MCP - Specification

## Overview

MCP (Model Context Protocol) server for interacting with the **Liquid Network** and **Bitcoin**. Enables AI assistants to manage Liquid and Bitcoin wallets through AQUA. One mnemonic can back both networks (unified wallet).

Built on **LWK (Liquid Wallet Kit)** Python bindings from Blockstream and **BDK (Bitcoin Development Kit)** Python bindings for Bitcoin.

## Architecture

```
AI Assistant ←→ MCP Server (Python) ←→ LWK (Liquid) ──→ Electrum/Esplora (Blockstream)
                        │
                        └──→ BDK (Bitcoin) ──→ Esplora (Blockstream)
```

No local server required. Liquid uses Electrum/Esplora; Bitcoin uses Esplora only. All via Blockstream's public infrastructure.

## Tools (16 total)

Liquid tools use the `lw_` prefix; Bitcoin tools use the `btc_` prefix.

### Wallet Management (Liquid)

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_generate_mnemonic` | Generate a new BIP39 mnemonic | (default: 12 words) |
| `lw_import_mnemonic` | Import wallet from mnemonic; also creates Bitcoin wallet from same mnemonic (unified) | `mnemonic`: string, `wallet_name`: optional, `network`: mainnet/testnet, `passphrase`: optional |
| `lw_export_descriptor` | Export CT descriptor (watch-only) | `wallet_name`: optional |
| `lw_import_descriptor` | Import watch-only wallet from CT descriptor | `descriptor`: string, `wallet_name`: string, `network`: optional |
| `lw_list_wallets` | List all wallets | (none) |

### Wallet Operations (Liquid)

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_balance` | Get wallet balance (all assets) | `wallet_name`: optional |
| `lw_address` | Generate new receive address | `wallet_name`: optional, `index`: optional |
| `lw_transactions` | List transaction history | `wallet_name`: optional, `limit`: optional (default: 10) |

### Transactions (Liquid)

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_send` | Create, sign and broadcast L-BTC transaction | `wallet_name`, `address`, `amount` (sats), `passphrase`: optional |
| `lw_send_asset` | Send a specific Liquid asset | `wallet_name`, `address`, `amount` (sats), `asset_id`, `passphrase`: optional |
| `lw_tx_status` | Get transaction status (txid or Blockstream URL) | `tx`: string (64-char hex txid or blockstream.info URL) |

### Bitcoin (btc_*)

| Tool | Description | Parameters |
|------|-------------|------------|
| `btc_balance` | Get Bitcoin wallet balance in satoshis | `wallet_name`: optional |
| `btc_address` | Generate Bitcoin receive address (bc1...) | `wallet_name`: optional, `index`: optional |
| `btc_transactions` | List Bitcoin transaction history | `wallet_name`: optional, `limit`: optional (default: 10) |
| `btc_send` | Send BTC to an address | `wallet_name`, `address`, `amount` (sats), `fee_rate`: optional (sat/vB), `passphrase`: optional |

### Unified

| Tool | Description | Parameters |
|------|-------------|------------|
| `unified_balance` | Get balance for both Bitcoin and Liquid | `wallet_name`: optional |

## Resources (3 total)

MCP resources provide static documentation to AI assistants.

| URI | Name | Description |
|-----|------|-------------|
| `aqua://docs/quickstart` | Quick Start Guide | Creating wallets, checking balance, receiving/sending funds |
| `aqua://docs/networks` | Network Reference | Bitcoin and Liquid network details, address formats, explorers, common assets |
| `aqua://docs/security` | Security Best Practices | Passphrase usage, encryption, backup, watch-only wallets, recovery |

## Prompts (12 total)

MCP prompts provide pre-built conversation starters for common workflows.

| Prompt | Description | Arguments |
|--------|-------------|-----------|
| `create_new_wallet` | Create a new wallet with mnemonic and passphrase | `wallet_name`: optional, `network`: optional |
| `import_seed` | Import an existing wallet from a mnemonic | `wallet_name`: optional |
| `show_balance` | Show wallet balance (both networks by default) | `wallet_name`: optional |
| `bitcoin_balance` | Show only Bitcoin balance | `wallet_name`: optional |
| `liquid_balance` | Show only Liquid balance (all assets) | `wallet_name`: optional |
| `generate_address` | Generate an address to receive funds | `network`: required (bitcoin/liquid), `wallet_name`: optional |
| `show_transactions` | View transaction history | `network`: optional (bitcoin/liquid), `wallet_name`: optional |
| `send_bitcoin` | Send Bitcoin to an address | `wallet_name`: optional |
| `send_liquid` | Send L-BTC or other Liquid asset | `wallet_name`: optional |
| `transaction_status` | Check transaction status | `network`: optional (bitcoin/liquid) |
| `list_wallets` | Show all wallets | (none) |
| `export_descriptor` | Export descriptor for watch-only wallet | `wallet_name`: optional |

## Data Storage

Wallet data stored in `~/.aqua-mcp/`:
```
~/.aqua-mcp/
├── config.json          # Network settings, defaults
├── wallets/
│   ├── default.json     # Encrypted wallet data
│   └── work.json
└── cache/
    └── <wallet_name>/
        └── btc/
            └── bdk.sqlite  # BDK persistence (Bitcoin)
```

### Wallet File Structure

```json
{
  "name": "default",
  "network": "mainnet",
  "descriptor": "ct(slip77(...),elwpkh(...))",
  "btc_descriptor": "wpkh([...]/0/*)#...",
  "btc_change_descriptor": "wpkh([...]/1/*)#...",
  "encrypted_mnemonic": "...",
  "watch_only": false,
  "created_at": "2026-02-20T12:00:00Z"
}
```

`btc_descriptor` and `btc_change_descriptor` (BIP84) are set when the wallet is imported from mnemonic (unified wallet). Omitted for watch-only or descriptor-only imports.

### Config Structure

```json
{
  "network": "mainnet",
  "default_wallet": "default",
  "electrum_url": null,
  "auto_sync": true
}
```

## Security Considerations

1. **Mnemonic Storage**: When a passphrase is provided, it is used as the password to encrypt the mnemonic at rest (PBKDF2 480k iterations + Fernet). Without passphrase, the mnemonic is stored as base64 (not encrypted)
2. **Watch-Only Mode**: Supports CT descriptors for balance checking without signing capability
3. **No Server**: All operations are local + public Electrum/Esplora servers
4. **Network Isolation**: Mainnet/testnet wallets are kept separate
5. **File Permissions**: Wallet directory created with `0o700`, files with `0o600`
6. **Atomic Writes**: Wallet files written via temp files to prevent corruption

## Networks

**Liquid**

| Network | Electrum Server | Esplora |
|---------|-----------------|---------|
| Mainnet | `blockstream.info:995` | `https://blockstream.info/liquid/api` |
| Testnet | `blockstream.info:465` | `https://blockstream.info/liquidtestnet/api` |

**Bitcoin**

| Network | Esplora |
|---------|---------|
| Mainnet | `https://blockstream.info/api` |
| Testnet | `https://blockstream.info/testnet/api` |

## Dependencies

- `lwk` - Liquid Wallet Kit Python bindings
- `bdkpython` - Bitcoin Development Kit Python bindings (>=2.2.0)
- `mcp` - Model Context Protocol SDK
- `cryptography` - For mnemonic encryption (PBKDF2 + Fernet)

## Bitcoin Implementation Details

### BDK Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `STOP_GAP` | 20 | Max consecutive unused addresses to scan before stopping |
| `PARALLEL_REQUESTS` | 3 | Concurrent Esplora API requests during full_scan |

### BDK Send Flow

1. **Validate** amount > 0, fee_rate > 0 (if provided), wallet has mnemonic
2. **Decrypt** mnemonic using passphrase (if encrypted)
3. **Load** wallet with signing capability (`_get_wallet_with_signer`)
4. **Sync** wallet via Esplora `full_scan` to get latest UTXOs
5. **Build** PSBT with `TxBuilder`, set recipient and optional fee rate
6. **Sign** with `trust_witness_utxo=True`, `try_finalize=True`
7. **Broadcast** via Esplora client
8. **Return** txid in display format (big-endian, matching block explorers)

### TXID Format

Transaction IDs are returned in **big-endian display format** (byte-reversed hex), matching what block explorers show. BDK internally uses little-endian.

## Error Handling

All tools return structured errors:

```json
{
  "error": {
    "code": "INSUFFICIENT_FUNDS",
    "message": "Not enough L-BTC to complete transaction",
    "details": {
      "required": 10000,
      "available": 5000
    }
  }
}
```

Common error codes: `ValueError`, `INSUFFICIENT_FUNDS`, `Generic`.

## Example Flows

### Create New Wallet (Unified)

```
1. lw_generate_mnemonic()
   → { "mnemonic": "abandon abandon ...", "words": 12 }

2. lw_import_mnemonic(mnemonic="...", network="mainnet")
   → { "wallet_name": "default", "descriptor": "ct(...)", "btc_descriptor": "wpkh(...)" }

3. lw_address(wallet_name="default")   → Liquid address (lq1...)
   btc_address(wallet_name="default")   → Bitcoin address (bc1...)
```

### Check Balance & Send

```
1. lw_balance(wallet_name="default")
   → { "balances": [{ "ticker": "L-BTC", "amount_sats": 100000 }, ...] }

2. unified_balance(wallet_name="default")
   → { "bitcoin": { "balance_sats": 50000 }, "liquid": { "balances": [...] } }

3. lw_send(wallet_name="default", address="lq1...", amount=50000)
   → { "txid": "abc123...", "amount": 50000 }

4. btc_balance(wallet_name="default")  → { "balance_sats": 0, "balance_btc": 0 }
   btc_send(wallet_name="default", address="bc1...", amount=10000, passphrase="...")
   → { "txid": "...", "amount": 10000 }
```

### Watch-Only Import

```
1. lw_import_descriptor(descriptor="ct(slip77(...),elwpkh(...))", wallet_name="cold")
   → { "wallet_name": "cold", "watch_only": true }

2. lw_balance(wallet_name="cold")
   → { "balances": [...] }
```

### Check Transaction Status

```
1. lw_tx_status(tx="abc123...")
   → { "txid": "abc123...", "status": "confirmed", "confirmations": 5, "explorer_url": "https://..." }

2. lw_tx_status(tx="https://blockstream.info/liquid/tx/abc123...")
   → { "txid": "abc123...", "network": "mainnet", "status": "unconfirmed", ... }
```
## Development Environment

This is a Python/uv project. Always use `uv` commands (uv sync, uv run, uvx) instead of pip, venv, or other Python package managers.

## Development

### Project Structure

```
aqua-mcp/
├── AGENTS.md           # This file (specs)
├── README.md           # User documentation
├── pyproject.toml      # Python package config
├── src/
│   └── aqua_mcp/
│       ├── __init__.py
│       ├── server.py   # MCP server entry point (tools, resources, prompts)
│       ├── tools.py    # Tool implementations (lw_*, btc_*, unified_*)
│       ├── wallet.py   # Liquid wallet (LWK)
│       ├── bitcoin.py  # Bitcoin wallet (BDK)
│       ├── assets.py   # Asset registry
│       └── storage.py  # Persistence layer (encryption, config, wallet data)
└── tests/
    ├── test_tools.py
    ├── test_storage.py
    └── test_bitcoin.py
```

### Running Tests

```bash
uv sync --all-extras
uv run python -m pytest tests/
```

### Local Development

```bash
uv sync
uv run python -m aqua_mcp.server
```

---

*Last updated: 2026-03-03*
