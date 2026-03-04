# Real Bitcoin Transaction Test Prompts

Manual test prompts for validating Aqua MCP Bitcoin on-chain functionality using a real wallet. These prompts test sending BTC on the Bitcoin network.

## Prerequisites

- A `.env` file must exist in the project root with the required variables.
- The following environment variables must be set:

| Variable | Description |
|----------|-------------|
| `SIGNER_MNEMONIC` | BIP39 mnemonic (12 words) for the test wallet |
| `BTC_DEST_ADDRESS` | Bitcoin address to use as destination for send tests |

**IMPORTANT:** The wallet must have sufficient BTC balance to cover the send amount plus fees.

## Test Prompts

### 1. Import Wallet and Check Balance

Import the wallet and verify there's sufficient BTC balance.

```
Import this wallet and show me my Bitcoin balance:
SIGNER_MNEMONIC=${SIGNER_MNEMONIC}
```

**Expected behavior:**
- Wallet is imported with name `default`
- BTC balance is displayed in satoshis and BTC
- Balance should be > 1000 sats to perform the send test

---

### 2. Send BTC On-Chain

Send a small amount of satoshis to the destination address.

```
Send 1000 sats to this Bitcoin address: ${BTC_DEST_ADDRESS}
```

**Expected behavior:**
- Transaction is created, signed, and broadcast successfully
- A valid TXID is returned in **display format** (big-endian, matching block explorers)
- The TXID can be verified on [blockstream.info](https://blockstream.info)

---

### 3. Verify Transaction on Block Explorer

Check the transaction on a block explorer to confirm it was broadcast.

```
Can you give me the Blockstream URL for that transaction?
```

**Expected behavior:**
- Returns a valid Blockstream.info URL for the transaction
- URL format: `https://blockstream.info/tx/<txid>` (mainnet) or `https://blockstream.info/testnet/tx/<txid>` (testnet)
- Opening the URL in a browser shows the transaction details

---

### 4. Verify Updated Balance

Check that the wallet balance reflects the sent amount plus fees.

```
Show me my updated Bitcoin balance.
```

**Expected behavior:**
- BTC balance is reduced by send amount + mining fees
- Balance change matches the transaction output

---

### 5. View Transaction History

Confirm the sent transaction appears in the transaction history.

```
Show me my recent Bitcoin transaction history.
```

**Expected behavior:**
- The sent transaction appears at the top of the list
- Transaction details include: txid, confirmation height, sent amount, and fee
- If unconfirmed, height may be null or show as "pending"

---

### 6. Check Transaction Confirmation Status

After a few minutes, check if the transaction has been confirmed.

```
Has my Bitcoin transaction been confirmed yet?
```

**Expected behavior:**
- If confirmed: shows block height and number of confirmations
- If unconfirmed: indicates the transaction is still in the mempool
- Transaction typically confirms within 10-60 minutes depending on fee rate

---
