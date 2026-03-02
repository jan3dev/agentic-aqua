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

## Tools

Liquid tools use the `lw_` prefix; Bitcoin tools use the `btc_` prefix.

### Wallet Management (Liquid)

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_generate_mnemonic` | Generate a new BIP39 mnemonic | (default: 12 words) |
| `lw_import_mnemonic` | Import wallet from mnemonic; also creates Bitcoin wallet from same mnemonic (unified) | `mnemonic`: string, `wallet_name`: optional, `network`: mainnet/testnet, `passphrase`: optional |
| `lw_export_descriptor` | Export CT descriptor (watch-only) | `wallet_name`: optional |
| `lw_import_descriptor` | Import watch-only wallet from CT descriptor | `descriptor`: string, `wallet_name`: string, `network`: optional |

### Wallet Operations (Liquid)

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_balance` | Get wallet balance (all assets) | `wallet_name`: optional |
| `lw_address` | Generate new receive address | `wallet_name`: optional, `index`: optional |
| `lw_transactions` | List transaction history | `wallet_name`: optional, `limit`: optional |
| `lw_list_wallets` | List all wallets | (none) |

### Transactions (Liquid)

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_send` | Create, sign and broadcast L-BTC transaction | `wallet_name`, `address`, `amount` (sats), `passphrase`: optional |
| `lw_send_asset` | Send a specific Liquid asset | `wallet_name`, `address`, `amount`, `asset_id`, `passphrase`: optional |
| `lw_tx_status` | Get transaction status (txid or Blockstream URL) | `tx`: string |

### Bitcoin (btc_*)

| Tool | Description | Parameters |
|------|-------------|------------|
| `btc_balance` | Get Bitcoin wallet balance in satoshis | `wallet_name`: optional |
| `btc_address` | Generate Bitcoin receive address (bc1...) | `wallet_name`: optional, `index`: optional |
| `btc_transactions` | List Bitcoin transaction history | `wallet_name`: optional, `limit`: optional |
| `btc_send` | Send BTC to an address | `wallet_name`, `address`, `amount` (sats), `fee_rate`: optional, `passphrase`: optional |

### Unified

| Tool | Description | Parameters |
|------|-------------|------------|
| `unified_balance` | Get balance for both Bitcoin and Liquid | `wallet_name`: optional |

## Data Storage

Wallet data stored in `~/.aqua-mcp/`:
```
~/.aqua-mcp/
├── config.json          # Network settings, defaults
├── wallets/
│   ├── default.json     # Encrypted wallet data
│   └── work.json
└── cache/               # Blockchain sync cache
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

## Security Considerations

1. **Mnemonic Storage**: Mnemonics are encrypted at rest using a passphrase
2. **Watch-Only Mode**: Supports CT descriptors for balance checking without signing capability
3. **No Server**: All operations are local + public Electrum/Esplora servers
4. **Network Isolation**: Mainnet/testnet wallets are kept separate

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
- `cryptography` - For mnemonic encryption

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
│       ├── server.py   # MCP server entry point
│       ├── tools.py    # Tool implementations
│       ├── wallet.py   # Liquid wallet (LWK)
│       ├── bitcoin.py  # Bitcoin wallet (BDK)
│       ├── assets.py   # Asset registry
│       └── storage.py  # Persistence layer
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

*Last updated: 2026-02-26*
