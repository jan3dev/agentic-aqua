# Read-Only Test Prompts

Manual test prompts for validating Aqua MCP read-only functionality using a real wallet. These prompts test operations that don't consume coins or require broadcasting transactions.

## Prerequisites

- A `.env` file must exist in the project root with the required variables.
- The following environment variable must be set:

| Variable | Description |
|----------|-------------|
| `SIGNER_MNEMONIC` | BIP39 mnemonic (12 words) for the test wallet |

## Test Prompts

### 1. Import Wallet

Import the mnemonic and confirm the wallet is created on both networks (Liquid + Bitcoin).

```
Import this wallet and tell me the general balance:
SIGNER_MNEMONIC=${SIGNER_MNEMONIC}
```

**Expected behavior:**
- Wallet is imported (or already exists) with name `default`
- `unified_balance` returns balances for both Bitcoin and Liquid networks
- Both L-BTC and BTC balances are displayed

---

### 2. Generate Receive Addresses

Request a Liquid address and a Bitcoin on-chain address for receiving funds.

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

Show the transaction history for the Liquid wallet.

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

Show all wallets currently stored in the system.

```
List all my wallets.
```

**Expected behavior:**
- Returns a list of all wallets
- Each wallet shows: name, network, type (watch-only or signing), and creation date

---

### 7. Export Descriptor (Watch-Only)

Export the Confidential Transactions descriptor for watch-only usage.

```
Export the CT descriptor for my default wallet.
```

**Expected behavior:**
- Returns a CT descriptor string starting with `ct(slip77(...),elwpkh(...))`
- Descriptor can be imported on another device for watch-only access
- No private keys are exposed

---
