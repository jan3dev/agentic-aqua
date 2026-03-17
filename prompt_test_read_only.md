# Read-Only Test Prompts

Manual test prompts for validating Aqua MCP read-only functionality using a real wallet. These prompts test operations that don't consume coins or require broadcasting transactions.

Use an agent with Haiku model to run these prompts

## Prerequisites

- A `.env` file must exist in the project root with the required variables.
- The following environment variable must be set:

| Variable | Description |
|----------|-------------|
| `SIGNER_MNEMONIC` | BIP39 mnemonic (12 words) for the test wallet |

## Test Prompts

### 1. Import Wallet

```
Import this wallet, name it prompt_wallet_<DATETIME> and tell me the general balance:
SIGNER_MNEMONIC=${SIGNER_MNEMONIC}
```

**Expected behavior:**
- Wallet is imported (or already exists) with name `default`
- `unified_balance` returns balances for both Bitcoin and Liquid networks
- Both L-BTC and BTC balances are displayed

---

### 2. Generate Receive Addresses

```
Give me a Liquid address and a Bitcoin on-chain address to receive funds.
```

**Expected behavior:**
- Liquid address starts with `lq1...` (mainnet) or `tex1...` / `tlq1...` (testnet)
- Bitcoin address starts with `bc1...` (mainnet) or `tb1...` (testnet)
- Both addresses belong to the `default` wallet

---

### 3. View Unified Balance

Show the balance for both Bitcoin and Liquid wallets.

```
Show me my unified balance for both Bitcoin and Liquid.
```

**Expected behavior:**
- Returns balances for both networks
- Bitcoin balance in satoshis and BTC
- Liquid balances for all assets (L-BTC and any other assets)

---

### 4. View Bitcoin Transaction History

Show the transaction history for the Bitcoin wallet.

```
Show me my Bitcoin transaction history.
```

**Expected behavior:**
- Returns a list of Bitcoin transactions (if any exist)
- Each transaction includes: txid, confirmation height, received/sent amounts, and fee
- Transactions are sorted by most recent first
- If no transactions exist, returns an empty list

---

### 5. View Liquid Transaction History

```
Show me my Liquid transaction history.
```

**Expected behavior:**
- Returns a list of Liquid transactions (if any exist)
- Each transaction includes: txid, height, timestamp, balance by asset, and fee
- Transactions are sorted by most recent first
- If no transactions exist, returns an empty list

---

### 6. List All Wallets

```
List all my wallets.
```

**Expected behavior:**
- Returns a list of all wallets
- Each wallet shows: name, network, type (watch-only or signing), and creation date
---

### 7. Export Descriptor (Watch-Only)

```
Export the CT descriptor for my latest imported wallet.
```

**Expected behavior:**
- Returns a CT descriptor string starting with `ct(slip77(...),elwpkh(...))`
- Descriptor can be imported on another device for watch-only access
- No private keys are exposed

---

### 8. Check Lightning Swap Status (that I paid)

```
Check the status of my Lightning swap wneeB76Iu5k2 at boltz
```

**Expected behavior:**
- Returns swap status from Boltz API (e.g. `transaction.claim.pending`, `transaction.claimed`)
- Shows `swap_id`, `status`, `lockup_txid`, `timeout_block_height`, and `network`
- If claimed, includes `preimage` and `claim_txid`
- If failed, includes `refund_info` with timeout block height


---

### 9. Generate Lightning Invoice (to receive)

```
Generate a Lightning invoice to receive 500 sats into my Liquid wallet.
```

**Expected behavior:**
- Returns a BOLT11 invoice string (`lnbc...`)
- Shows `swap_id`, `invoice`, `amount` (500 sats), and `receive_address` (lq1...)
- Swap status starts as `pending`
- Invoice is valid for ~24 hours

---

### 10. Check Lightning Swap Status (Receiving)

```
Check the status of my Lightning swap id 3882e4fd-c73a-4e72-abbe-18ac1ea3b2aa
```

**Expected behavior:**
- Returns swap status from Ankara API (e.g. `pending`, `claimed`, `settled`)
- Shows `swap_id`, `status`, `invoice`, `amount`, and `receive_address`
- If settled, includes `preimage` and auto-claims L-BTC to the Liquid wallet
- If failed, reports the error state

---

### 11. Delete Wallet

```
Import a wallet named delete_test_<DATETIME> using the mnemonic "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about" on mainnet. Then delete the wallet and verify it no longer appears in the wallet list.
```

**Expected behavior:**
- Wallet is imported successfully with name `delete_test_<DATETIME>`
- `delete_wallet` removes the wallet
- `lw_list_wallets` no longer shows the deleted wallet

---

### 12. Seed Backup & Restore with Passphrase

```
Create a new wallet named test_seed_restore_<DATETIME> with passphrase "test". Generate the first Bitcoin receiving address and remember it. Then delete the wallet. Re-import the wallet using the same seed and passphrase "test", generate the first Bitcoin receiving address again, and verify it matches the original.
```

**Expected behavior:**
- New mnemonic is generated and wallet is imported with passphrase "test"
- First BTC address is derived and stored in memory
- Wallet is successfully deleted
- Wallet is re-imported from the same mnemonic + passphrase "test"
- New BTC address matches the original (deterministic derivation)

---
