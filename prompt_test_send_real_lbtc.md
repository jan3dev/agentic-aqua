# Real L-BTC Transaction Test Prompts

Manual test prompts for validating Aqua MCP Liquid on-chain functionality using a real wallet. These prompts test sending L-BTC on the Liquid Network.

## Prerequisites

- A `.env` file must exist in the project root with the required variables.
- The following environment variables must be set:

| Variable | Description |
|----------|-------------|
| `SIGNER_MNEMONIC` | BIP39 mnemonic (12 words) for the test wallet |
| `LIQUID_DEST_ADDRESS` | Liquid address to use as destination for send tests |

**IMPORTANT:** The wallet must have sufficient L-BTC balance to cover the send amount plus fees.

## Test Prompts

### 1. Import Wallet and Check Balance

Import the wallet and verify there's sufficient L-BTC balance.

```
Import this wallet and show me my Liquid balance:
SIGNER_MNEMONIC=${SIGNER_MNEMONIC}
```

**Expected behavior:**
- Wallet is imported with name `default`
- L-BTC balance is displayed in satoshis and BTC
- Balance should be > 1000 sats to perform the send test

---

### 2. Send L-BTC

Send a small amount of L-BTC to the destination address.

```
Send 1000 sats of L-BTC to this Liquid address: ${LIQUID_DEST_ADDRESS}
```

**Expected behavior:**
- Transaction is created, signed, and broadcast successfully
- A valid TXID is returned
- The TXID can be verified on [blockstream.info/liquid](https://blockstream.info/liquid)

---

### 3. Check Transaction Status

Verify the transaction was broadcast by checking its status.

```
What is the status of that Liquid transaction?
```

**Expected behavior:**
- Transaction appears on the Liquid network
- Status can be: "in mempool" (unconfirmed) or "confirmed" with block height
- Use `lw_tx_status` with the TXID or Blockstream URL

---

### 4. Verify Updated Balance

Check that the wallet balance reflects the sent amount plus fees.

```
Show me my updated Liquid balance.
```

**Expected behavior:**
- L-BTC balance is reduced by send amount + fees
- Balance change matches the transaction output

---

### 5. View Transaction History

Confirm the sent transaction appears in the transaction history.

```
Show me my recent Liquid transaction history.
```

**Expected behavior:**
- The sent transaction appears at the top of the list
- Transaction details include: txid, sent amount, fee, and confirmation status

---
