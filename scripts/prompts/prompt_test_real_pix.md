# Real Pix → DePix Test Prompts

Manual test prompts for validating Aqua MCP **Pix / DePix (Eulen)** receive functionality with a real wallet. Pix is Brazil's instant payment system; DePix is the BRL-pegged Liquid asset issued by Eulen.

Receive-only — there is no `pix_send` tool yet.

Use an agent with Sonnet model. The tester needs a Brazilian bank app to pay the Pix Copia-e-Cola string.

## Prerequisites

- A `.env` file must exist in the project root with the required variables.
- The following environment variable must be set:

| Variable | Description |
|----------|-------------|
| `SIGNER_MNEMONIC` | BIP39 mnemonic (12 words) for the test wallet |

- The MCP server must be running with `EULEN_API_TOKEN` set in its environment.
- The tester needs a Brazilian bank app capable of paying Pix Copia-e-Cola (or scanning a QR image).

## Test Prompts

### 1. Import Wallet

```
Import this wallet with the name prompt_wallet_<DATETIME>, then show my Liquid balance:
SIGNER_MNEMONIC=${SIGNER_MNEMONIC}
```

**Expected behavior:**
- Wallet imported (or reused) as `prompt_wallet_<DATETIME>`
- `lw_balance` returns Liquid balances (DePix may or may not be present yet)

---

### 2. Create a Pix Charge for R$5,00

```
Generate a Pix charge for R$5.00 (500 cents) so I can receive DePix into my Liquid wallet.
```

**Expected behavior:**
- Invokes `pix_receive(amount_cents=500, wallet_name="prompt_wallet_<DATETIME>")`
- Returns `swap_id`, `qr_copy_paste`, `qr_image_url`, `amount_cents=500`, `amount_brl="R$ 5,00"`, `depix_address` (lq1...), `expiration`, plus a `message` explaining how to pay
- The agent shows both `qr_copy_paste` and `qr_image_url` to the user

---

### 3. Poll the Charge Status

```
Check the status of Pix swap <SWAP_ID>.
```

**Expected behavior:**
- Invokes `pix_status(swap_id="<SWAP_ID>")`
- Status progresses: `pending` → `depix_sent`
- Once `depix_sent`, response includes `blockchain_txid` and (often) `payer_name`
- The agent should re-poll every ~30 s until terminal status

---

### 4. Verify DePix in Wallet

```
Show me my Liquid balance and recent transactions.
```

**Expected behavior:**
- `lw_balance` shows a DePix asset entry with the credited amount (≈ R$5.00 worth)
- `lw_transactions(limit=5)` includes the incoming DePix tx with the `blockchain_txid` from step 4

---

### 5. (Optional) Verify the DePix txid on the Explorer

```
What is the status of Liquid transaction <BLOCKCHAIN_TXID>?
```

**Expected behavior:**
- `lw_tx_status(tx="<BLOCKCHAIN_TXID>")` returns confirmed/unconfirmed plus the explorer URL
- The explorer URL (`https://blockstream.info/liquid/tx/<txid>`) opens to a confirmed transaction crediting the wallet's DePix address

## Notes

- **Amounts are in BRL cents**, not reais. `amount_cents=500` = R$5.00.
- Eulen pushes DePix automatically once the Pix payment settles — no claim step is required.

