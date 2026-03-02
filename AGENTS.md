# AQUA MCP - Specification

## Overview

MCP (Model Context Protocol) server for interacting with the Liquid Network. Enables AI assistants to manage Liquid wallets through AQUA.

Built on top of **LWK (Liquid Wallet Kit)** Python bindings from Blockstream.

## Architecture

```
AI Assistant ←→ MCP Server (Python) ←→ LWK Bindings ←→ Liquid Network
                                              ↓
                                    Electrum/Esplora (Blockstream public servers)
```

No local server required. Uses Blockstream's public infrastructure for blockchain sync.

## Tools

All tools use the `lw_` prefix.

### Wallet Management

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_generate_mnemonic` | Generate a new BIP39 mnemonic | `words`: 12 or 24 (default: 12) |
| `lw_import_mnemonic` | Import wallet from mnemonic | `mnemonic`: string, `network`: mainnet/testnet |
| `lw_export_descriptor` | Export CT descriptor (watch-only) | `wallet_name`: string |
| `lw_import_descriptor` | Import watch-only wallet from CT descriptor | `descriptor`: string, `wallet_name`: string |

### Wallet Operations

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_balance` | Get wallet balance | `wallet_name`: string |
| `lw_address` | Generate new receive address | `wallet_name`: string, `index`: optional |
| `lw_transactions` | List transaction history | `wallet_name`: string, `limit`: optional |
| `lw_utxos` | List unspent outputs | `wallet_name`: string |

### Transactions

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_send` | Create, sign and broadcast transaction | `wallet_name`: string, `address`: string, `amount`: satoshis, `asset`: optional (default: L-BTC) |
| `lw_send_asset` | Send a specific Liquid asset | `wallet_name`: string, `address`: string, `amount`: satoshis, `asset_id`: string |

### Assets

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_assets` | List assets in wallet | `wallet_name`: string |
| `lw_asset_details` | Get asset metadata | `asset_id`: string |

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
  "encrypted_mnemonic": "...",  // Optional, if full wallet
  "watch_only": false,
  "created_at": "2026-02-20T12:00:00Z"
}
```

## Security Considerations

1. **Mnemonic Storage**: Mnemonics are encrypted at rest using a passphrase
2. **Watch-Only Mode**: Supports CT descriptors for balance checking without signing capability
3. **No Server**: All operations are local + public Electrum/Esplora servers
4. **Network Isolation**: Mainnet/testnet wallets are kept separate

## Networks

| Network | Electrum Server | Esplora |
|---------|-----------------|---------|
| Mainnet | `blockstream.info:995` | `https://blockstream.info/liquid/api` |
| Testnet | `blockstream.info:465` | `https://blockstream.info/liquidtestnet/api` |

## Dependencies

- `lwk` - Liquid Wallet Kit Python bindings
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

### Create New Wallet

```
1. lw_generate_mnemonic(words=12)
   → "abandon abandon abandon..."

2. lw_import_mnemonic(mnemonic="...", network="mainnet")
   → { "wallet_name": "default", "descriptor": "ct(...)" }

3. lw_address(wallet_name="default")
   → { "address": "lq1...", "index": 0 }
```

### Check Balance & Send

```
1. lw_balance(wallet_name="default")
   → { "L-BTC": 100000, "USDT": 5000000 }

2. lw_send(wallet_name="default", address="lq1...", amount=50000)
   → { "txid": "abc123...", "fee": 250 }
```

### Watch-Only Import

```
1. lw_import_descriptor(descriptor="ct(slip77(...),elwpkh(...))", wallet_name="cold")
   → { "wallet_name": "cold", "watch_only": true }

2. lw_balance(wallet_name="cold")
   → { "L-BTC": 500000 }
```

## Development

### Project Structure

```
aqua-mcp/
├── AGENTS.md           # This file (specs)
├── claude.md           # Symlink to AGENTS.md
├── README.md           # User documentation
├── pyproject.toml      # Python package config
├── src/
│   └── aqua_mcp/
│       ├── __init__.py
│       ├── server.py   # MCP server entry point
│       ├── tools.py    # Tool implementations
│       ├── wallet.py   # Wallet management
│       └── storage.py  # Persistence layer
└── tests/
    └── test_wallet.py
```

### Running Tests

```bash
pytest tests/
```

### Local Development

```bash
pip install -e ".[dev]"
python -m aqua_mcp.server
```

## Roadmap

- [ ] v0.1 - Basic wallet operations (generate, import, balance, address)
- [ ] v0.2 - Send transactions
- [ ] v0.3 - Asset management
- [ ] v0.4 - Multi-wallet support
- [ ] v0.5 - Hardware wallet integration (Jade)

---

*Last updated: 2026-02-20*
