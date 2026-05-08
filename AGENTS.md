# Agentic AQUA - Specification

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

## Tools (32 total)

Liquid tools use the `lw_` prefix; Bitcoin tools use the `btc_` prefix; unified tools are `unified_*`; Lightning tools are `lightning_*`; SideSwap tools are `sideswap_*`.

### Wallet Management

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_generate_mnemonic` | Generate a new BIP39 mnemonic | (default: 12 words) |
| `lw_import_mnemonic` | Import wallet from mnemonic; also creates Bitcoin wallet from same mnemonic (unified) | `mnemonic`: string, `wallet_name`: optional, `network`: mainnet/testnet, `password`: optional |
| `lw_export_descriptor` | Export CT descriptor (watch-only) | `wallet_name`: optional |
| `lw_import_descriptor` | Import watch-only wallet from CT descriptor | `descriptor`: string, `wallet_name`: string, `network`: optional |
| `lw_list_wallets` | List all wallets | (none) |
| `delete_wallet` | Delete a wallet and all its cached data. Agent MUST check balances and confirm with user before calling. Use the `delete_wallet` prompt for the safe workflow. | `wallet_name`: string |
| `btc_import_descriptor` | Import watch-only Bitcoin wallet from BIP84 descriptor. ONLY Bitcoin — for Liquid use `lw_import_descriptor`. | `descriptor`: string, `wallet_name`: string, `network`: optional, `change_descriptor`: optional |
| `btc_export_descriptor` | Export Bitcoin BIP84 descriptors + xpub. ONLY Bitcoin — for Liquid use `lw_export_descriptor`. | `wallet_name`: optional |

> ⚠️ The Bitcoin descriptor and the Liquid CT descriptor cannot be derived from each other. Bitcoin uses derivation path `m/84'/0'/0'`; Liquid uses `m/84'/1776'/0'` and additionally requires a SLIP-77 master blinding key derived from the seed. To monitor both networks watch-only, both descriptors must be imported.

### Wallet Operations (Liquid)

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_balance` | Get wallet balance (all assets) | `wallet_name`: optional |
| `lw_address` | Generate new receive address | `wallet_name`: optional, `index`: optional |
| `lw_transactions` | List transaction history | `wallet_name`: optional, `limit`: optional (default: 10) |

### Transactions (Liquid)

| Tool | Description | Parameters |
|------|-------------|------------|
| `lw_send` | Create, sign and broadcast L-BTC transaction | `wallet_name`, `address`, `amount` (sats), `password`: optional |
| `lw_send_asset` | Send a specific Liquid asset | `wallet_name`, `address`, `amount` (sats), `asset_id`, `password`: optional |
| `lw_tx_status` | Get transaction status (txid or Blockstream URL) | `tx`: string (64-char hex txid or blockstream.info URL) |

### Bitcoin (btc_*)

| Tool | Description | Parameters |
|------|-------------|------------|
| `btc_balance` | Get Bitcoin wallet balance in satoshis | `wallet_name`: optional |
| `btc_address` | Generate Bitcoin receive address (bc1...) | `wallet_name`: optional, `index`: optional |
| `btc_transactions` | List Bitcoin transaction history | `wallet_name`: optional, `limit`: optional (default: 10) |
| `btc_send` | Send BTC to an address | `wallet_name`, `address`, `amount` (sats), `fee_rate`: optional (sat/vB), `password`: optional |

### Unified

| Tool | Description | Parameters |
|------|-------------|------------|
| `unified_balance` | Get balance for both Bitcoin and Liquid | `wallet_name`: optional |

### Lightning (Unified Interface)

| Tool | Description | Parameters |
|------|-------------|------------|
| `lightning_receive` | Generate a Lightning invoice to receive L-BTC into Liquid wallet (~1-2 min after payment). Limits: 100 – 25,000,000 sats | `amount`: int (sats), `wallet_name`: optional (default: "default"), `password`: optional |
| `lightning_send` | Pay a Lightning invoice using L-BTC via Boltz submarine swap. Fees: ~0.1% + miner fees. Limits: 100 – 25,000,000 sats | `invoice`: BOLT11 string (lnbc... or lntb...), `wallet_name`: optional, `password`: optional |
| `lightning_transaction_status` | Check status of a Lightning swap (send or receive). For receive: auto-claims L-BTC when settled. For send: retrieves preimage when claimed. | `swap_id`: string |

### SideSwap (BTC ↔ L-BTC Pegs and Liquid Asset Swaps)

| Tool | Description | Parameters |
|------|-------------|------------|
| `sideswap_server_status` | Fetch SideSwap server status: live fees, minimums, hot-wallet balances. Call BEFORE recommending or initiating a peg. | `network`: optional (mainnet/testnet) |
| `sideswap_peg_quote` | Quote receive amount for a peg at current fees (0.1% + ~286 sats Liquid claim fee on peg-in). | `amount`: sats, `peg_in`: optional (default: true), `network`: optional |
| `sideswap_peg_in` | Initiate a peg-in (BTC → L-BTC). Returns BTC deposit address. After 2 BTC confs (~20 min hot path; up to ~17 hours cold path for very large amounts) L-BTC arrives. Recommended for amounts ≥ ~0.01 BTC. | `wallet_name`: optional, `password`: optional |
| `sideswap_peg_out` | Initiate a peg-out (L-BTC → BTC) and broadcast the L-BTC send. After 2 Liquid confs and federation BTC sweep (~15-60 min total), BTC arrives. Standard path for L-BTC → BTC. | `wallet_name`, `amount` (sats), `btc_address`, `password`: optional |
| `sideswap_peg_status` | Check status of a peg order (peg-in or peg-out). Returns confs, tx_state, lockup_txid, payout_txid. | `order_id`: string |
| `sideswap_recommend` | Recommend peg vs swap-market for a BTC ↔ L-BTC conversion. Surfaces time-vs-fee trade-off and warns if amount exceeds hot-wallet liquidity. | `amount` (sats), `direction`: btc_to_lbtc/lbtc_to_btc, `network`: optional |
| `sideswap_list_assets` | List Liquid assets supported by SideSwap (USDt, EURx, MEX, DePix, etc.). | `network`: optional |
| `sideswap_quote` | Read-only price quote for a Liquid asset swap (e.g. L-BTC ↔ USDt). Use BEFORE `sideswap_execute_swap` to confirm price with user. | `asset_id`, `send_amount` (sats) OR `recv_amount` (sats), `send_bitcoins`: optional, `network`: optional |
| `sideswap_execute_swap` | Execute an atomic Liquid swap. Both directions supported via `send_bitcoins`: True = L-BTC → asset; False = asset → L-BTC. PSET verified locally against the agreed quote before signing; fee tolerance pinned to L-BTC so the asset side is always strict equality. | `asset_id`, `send_amount` (sats), `send_bitcoins`: optional (default true), `wallet_name`: optional, `password`: optional |
| `sideswap_swap_status` | Get persisted status of an atomic swap. Pass the txid to `lw_tx_status` for on-chain confirmation. | `order_id`: string |

> ⚠️ **Pegs vs swaps**: pegs charge 0.1% (vs 0.2% for instant swap-market trades) but require waiting for confirmations. Always call `sideswap_recommend` for amounts ≥ 0.01 BTC and surface the trade-off (and any 102-confirmation cold-wallet warning) before initiating a peg-in.

## Resources (3 total)

MCP resources provide static documentation to AI assistants.

| URI | Name | Description |
|-----|------|-------------|
| `aqua://docs/quickstart` | Quick Start Guide | Creating wallets, checking balance, receiving/sending funds |
| `aqua://docs/networks` | Network Reference | Bitcoin and Liquid network details, address formats, explorers, common assets |
| `aqua://docs/security` | Security Best Practices | Password usage, at-rest encryption, backup, watch-only wallets, recovery |

## Prompts (17 total)

MCP prompts provide pre-built conversation starters for common workflows.

| Prompt | Description | Arguments |
|--------|-------------|-----------|
| `create_new_wallet` | Create a new wallet with mnemonic and optional at-rest password | `wallet_name`: optional, `network`: optional |
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
| `delete_wallet` | Safely delete a wallet with balance check and seed backup reminder | `wallet_name`: required |
| `pay_lightning` | Pay a Lightning invoice using Liquid Bitcoin | `wallet_name`: optional |
| `peg_in` | Move BTC to Liquid (BTC → L-BTC) via SideSwap peg-in, with quote, recommendation, and time warning | `wallet_name`: optional |
| `peg_out` | Move L-BTC to Bitcoin (L-BTC → BTC) via SideSwap peg-out, with quote and time estimate | `wallet_name`: optional |
| `swap_assets` | Quote a Liquid asset swap (e.g. L-BTC ↔ USDt) via SideSwap (read-only; execution requires AQUA mobile or sideswap.io) | (none) |

## Data Storage

Wallet data stored in `~/.aqua/`:
```
~/.aqua/
├── config.json          # Network settings, defaults
├── wallets/
│   ├── default.json     # Encrypted wallet data
│   └── work.json
├── swaps/               # Boltz submarine swap data (for refund recovery)
│   └── {swap_id}.json   # Contains swap details + refund private key
├── ankara_swaps/        # Ankara Lightning receive swap data (legacy)
│   └── {swap_id}.json   # Contains swap details + preimage when settled
├── lightning_swaps/     # Unified Lightning swap data (send & receive)
│   └── {swap_id}.json   # Contains swap details + status + optional preimage
├── sideswap_pegs/       # SideSwap peg orders (peg-in and peg-out)
│   └── {order_id}.json  # Contains order, addresses, status, tx_state, payout_txid
├── sideswap_swaps/      # SideSwap atomic asset swap orders (L-BTC → asset)
│   └── {order_id}.json  # Contains quote, submit_id, status, txid, optional last_error
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

### Swap File Structure (Boltz)

```json
{
  "swap_id": "abc123",
  "address": "lq1...",
  "expected_amount": 50069,
  "claim_public_key": "03...",
  "swap_tree": {...},
  "timeout_block_height": 2500000,
  "refund_private_key": "...",
  "refund_public_key": "...",
  "invoice": "lnbc...",
  "status": "transaction.claimed",
  "network": "mainnet",
  "created_at": "2026-03-06T12:00:00Z",
  "lockup_txid": "...",
  "preimage": "...",
  "claim_txid": "..."
}
```

File permissions: `0o600` (contains private refund key for recovery).

### Ankara Swap File Structure

```json
{
  "swap_id": "ankara_uuid_123",
  "boltz_swap_id": "boltz_abc_456",
  "invoice": "lnbc...",
  "address": "lq1...",
  "amount": 100000,
  "wallet_name": "default",
  "status": "pending",
  "created_at": "2026-03-12T12:00:00Z",
  "preimage": null
}
```

File permissions: `0o600`. Status progresses: `pending` → `claimed` → `settled`. Preimage populated when settled.

### Lightning Swap File Structure (Unified)

Unified Lightning swap storage for both send (Boltz) and receive (Ankara) operations:

```json
{
  "swap_id": "ankara_uuid_123",
  "swap_type": "receive",
  "provider": "ankara",
  "invoice": "lnbc...",
  "amount": 100000,
  "wallet_name": "default",
  "status": "pending",
  "network": "mainnet",
  "created_at": "2026-03-12T12:00:00Z",
  "receive_address": "lq1...",
  "preimage": null
}
```

Or for send swaps (Boltz):

```json
{
  "swap_id": "boltz_swap_456",
  "swap_type": "send",
  "provider": "boltz",
  "invoice": "lnbc...",
  "amount": 50069,
  "wallet_name": "default",
  "status": "processing",
  "network": "mainnet",
  "created_at": "2026-03-12T12:00:00Z",
  "lockup_txid": "abc123...",
  "timeout_block_height": 2500000,
  "refund_private_key": "..."
}
```

File permissions: `0o600`. Status values: `pending` | `processing` | `completed` | `failed`. The `lightning_transaction_status` tool auto-claims settled receive swaps.

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

1. **Mnemonic Storage**: When a password is provided, it encrypts the mnemonic at rest (PBKDF2 480k iterations + Fernet). Without password, the mnemonic is stored as base64 (not encrypted). NOTE: this password is NOT a BIP39 passphrase — derived keys depend solely on the mnemonic, so the same mnemonic restores identical descriptors in any BIP39-compliant wallet.
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
- `coincurve` - secp256k1 for Boltz swap keypair generation
- `websockets` - WebSocket client for SideSwap JSON-RPC (>=12.0)

## Ankara Integration

Ankara backend (`test.aquabtc.com`) provides Lightning → L-BTC swaps (receive side).

**API Endpoint**: Configurable via `ANKARA_API_URL` environment variable (defaults to `https://test.aquabtc.com`)

**Endpoints**:
- `POST /api/v1/lightning/swaps/create/` - Create receive invoice
- `POST /api/v1/lightning/swaps/{swap_id}/claim/` - Claim settled swap
- `GET /api/v1/lightning/lnurlp/verify/{swap_id}` - Verify settlement status

**Amount Limits**: 100 – 25,000,000 sats (no authentication required)

## SideSwap Integration

SideSwap (`sideswap.io`) provides BTC ↔ L-BTC pegs and Liquid asset swaps via WebSocket JSON-RPC.

**WebSocket endpoints**:
- Mainnet: `wss://api.sideswap.io/json-rpc-ws`
- Testnet: `wss://api-testnet.sideswap.io/json-rpc-ws`

**Wire format** (mirrors AQUA Flutter wallet):
```json
// Request
{"id": <int>, "method": "<snake_case>", "params": {...}}
// Response
{"id": <int>, "method": "<method>", "result": {...}}
{"id": <int>, "error": {"code": <int>, "message": "<str>"}}
// Notification (no id)
{"method": "<method>", "params": {...}}
```

**Methods used**:
- `login_client` (anonymous, `user_agent: "agentic-aqua"`)
- `server_status` — fees, mins, hot-wallet balances
- `peg_fee`, `peg`, `peg_status` — peg flow
- `assets`, `subscribe_price_stream`, `unsubscribe_price_stream` — asset swap quoting
- `market.list_markets`, `market.start_quotes`, `market.get_quote`, `market.taker_sign` — atomic asset swap execution (the modern `mkt::*` flow). Wire format wraps the inner variant in a single-key object: `{"id": N, "method": "market", "params": {"<variant_in_snake_case>": {...}}}`. `AssetType` and `TradeDir` are PascalCase on the wire (`"Base"|"Quote"`, `"Buy"|"Sell"`).

**Fees**:
- Pegs: 0.1% on send amount + small second-chain fee (~286 sats Liquid claim on peg-in)
- Swap-market taker: 0.2% (or 500 sats minimum, whichever higher)

**Peg minimums** (read live values from `server_status`):
- Peg-in: 1,286 sats (~0.00001286 BTC)
- Peg-out: 100,000 sats (0.001 BTC) on the SideSwap server, 25,000 sats in the AQUA app

**Peg timing**:
- Peg-in: 2 BTC confs (~20 min) hot-wallet path; 102 BTC confs (~17 hours) if amount exceeds `PegInWalletBalance`
- Peg-out: 2 Liquid confs + federation BTC sweep (typically 15–60 min total)

**Asset swap execution** (`sideswap_execute_swap`) uses SideSwap's modern `mkt::*` flow over WebSocket only (no HTTP dance) and supports **both directions**:

- **L-BTC → asset** (`send_bitcoins=True`): user's L-BTC change pays the network fee. Wallet net effect: `L-BTC: -(send_amount + fee)`, `asset: +recv_amount`.
- **asset → L-BTC** (`send_bitcoins=False`): SideSwap dealer absorbs the network fee from their L-BTC contribution. Wallet net effect: `asset: -send_amount` (exact), `L-BTC: +recv_amount` (exact).

**mkt::* flow steps**:
1. `market.list_markets` — fetch available pairs and find one matching ours
2. Resolve `(asset_type, trade_dir)` via `resolve_market` — always `Sell` with the asset_type matching the side we're sending
3. `market.start_quotes` with our UTXOs + receive/change addresses + `instant_swap=true`
4. Wait for a `quote` notification with `status=Success`; `parse_quote_status` raises on `LowBalance` / `Error`
5. `market.get_quote {quote_id}` → returns the half-built PSET
6. **Verify** with `wollet.pset_details(pset)` against the agreed quote — refuses to sign on mismatch
7. `signer.sign(pset)` locally
8. `market.taker_sign {quote_id, pset}` → server merges & broadcasts; returns the txid

The legacy `start_swap_web` + HTTP `swap_start`/`swap_sign` path is no longer the live execution path. `SideSwapHTTPClient` and `start_swap_web` are kept in the module for compatibility / debugging only.

**Verification rules** (`verify_pset_balances` in `src/aqua/sideswap.py`):
1. Wallet must gain *exactly* `recv_amount` of `recv_asset`.
2. Wallet must lose at most `send_amount + fee_tolerance_sats` (default 1000) of `send_asset` *if* `send_asset == fee_asset`; otherwise strict equality.
3. No other asset may have a non-zero balance change.

The manager always passes `fee_asset = policy_asset` (L-BTC) regardless of direction, so the fee tolerance only relaxes constraints on the L-BTC side — never on a non-L-BTC asset, which would otherwise be a siphon vector on the reverse path.

If any rule fails, `PsetVerificationError` is raised and signing is aborted — the order is persisted as `failed` for forensics. The order is also persisted at every flow step (`pending` → `verified` → `signed` → `broadcast`) for crash recovery.

UTXO selection (`select_swap_utxos`): confidential (asset_bf and value_bf both non-zero), holding the requested send_asset, sorted descending by value, accumulated to cover `send_amount`. wpkh-only (matching the wallet's BIP84 m/84'/1776'/0' descriptor). No separate L-BTC fee inputs are required on either direction (mirroring AQUA Flutter's `swap_provider.dart`).

## Bitcoin Implementation Details

### BDK Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `STOP_GAP` | 20 | Max consecutive unused addresses to scan before stopping |
| `PARALLEL_REQUESTS` | 3 | Concurrent Esplora API requests during full_scan |

### BDK Send Flow

1. **Validate** amount > 0, fee_rate > 0 (if provided), wallet has mnemonic
2. **Decrypt** mnemonic using password (if encrypted at rest)
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
   btc_send(wallet_name="default", address="bc1...", amount=10000, password="...")
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
agentic-aqua/
├── AGENTS.md           # This file (specs)
├── README.md           # User documentation
├── pyproject.toml      # Python package config
├── src/
│   └── aqua/
│       ├── __init__.py
│       ├── server.py   # MCP server entry point (tools, resources, prompts)
│       ├── tools.py    # Tool implementations (lw_*, btc_*, unified_*, lightning_*, sideswap_*)
│       ├── wallet.py   # Liquid wallet (LWK)
│       ├── bitcoin.py  # Bitcoin wallet (BDK)
│       ├── lightning.py # Lightning abstraction layer (unified send/receive manager)
│       ├── boltz.py    # Boltz Exchange integration (submarine swaps, send)
│       ├── ankara.py   # Ankara backend integration (Lightning receive)
│       ├── sideswap.py # SideSwap WS+HTTP client, peg manager, swap quoting
│       ├── assets.py   # Asset registry
│       └── storage.py  # Persistence layer (encryption, config, wallet data)
└── tests/
    ├── test_tools.py
    ├── test_lightning.py
    ├── test_storage.py
    ├── test_bitcoin.py
    ├── test_boltz.py
    ├── test_ankara.py
    ├── test_sideswap.py
    └── test_server.py
```

### Running Tests

```bash
uv sync --all-extras
uv run python -m pytest tests/
```

### Local Development

```bash
uv sync
uv run python -m aqua.server
```

---

## TLDR Integration

For code exploration, PREFER these TLDR tools over raw Grep/Glob:

| Tool | When to use |
|------|-------------|
| `mcp__tldr__semantic` | Find code by behavior ("validate tokens", "handle errors") |
| `mcp__tldr__structure` | Get function/class map of project |
| `mcp__tldr__context` | Get call graph from entry point (95% token savings) |
| `mcp__tldr__impact` | Before refactoring, find all callers |
| `mcp__tldr__arch` | Detect architectural layers |
| `mcp__tldr__change_impact` | Find tests affected by changes |

Use standard Grep/Glob only for: exact string matches, simple file lookups, config/env searches.

---

*Last updated: 2026-03-17*
