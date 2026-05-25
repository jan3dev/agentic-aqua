# Real Changelly Transaction Test Prompts

Manual test prompts for validating Aqua MCP **Changelly** USDt cross-chain swap functionality with a real wallet.

> ⚠️ **Known limitation (issue #51):** Changelly may return `Validation failed (400)` across all networks depending on the geographic region of the API caller. If every prompt below fails with that error, document the result and skip — the bug is upstream / configuration-related, not a regression in agentic-aqua.

Use an agent with Sonnet model.

## Prerequisites

- A `.env` file must exist in the project root with the required variables.
- The following environment variables must be set:

| Variable | Description |
|----------|-------------|
| `SIGNER_MNEMONIC` | BIP39 mnemonic (12 words) for the test wallet |
| `CHANGELLY_SOL_DEST` | USDt-Solana address to receive in the send test |
| `CHANGELLY_SOL_REFUND` | USDt-Solana refund address used in the receive test |

**IMPORTANT:**
- Wallet must hold ≥ 51 USDt on Liquid for Section A.
- Minimum for lusdt→usdtsol pair is ~50.05 USDT; 51 is used throughout these tests.
- Each order is trackable via the `track_url` returned by Changelly.
- ⚠️ Issue #54: fees may be deducted from the receive side (inconsistent with the Flutter APK). Capture the discrepancy in your test notes.

## Test Prompts

### 1. Import Wallet and Verify USDt-Liquid Balance

```
Import this wallet with the name prompt_wallet_<DATETIME>, then show my Liquid balances:
SIGNER_MNEMONIC=${SIGNER_MNEMONIC}
```

**Expected behavior:**
- Wallet imported (or reused) as `prompt_wallet_<DATETIME>`
- `lw_balance` shows L-BTC and USDt
- USDt-Liquid balance is ≥ 51

---

## Section A — Send USDt-Liquid → USDt on Solana

### 2. Get a Send Quote

```
Quote me sending 51 L-USDt to USDt on Solana via Changelly.
```

**Expected behavior:**
- Invokes `changelly_quote(external_network="solana", direction="send", amount_from="51")`
- Returns Changelly payload including `id` (we will reuse as `rate_id`), `amountFrom`, `amountTo`, `networkFee`, `min`, `max`, `expiredAt`
- ⚠️ If `Validation failed (400)`, see issue #51 and stop here.

---

### 3. Execute the Send (locked rate)

```
Execute the Changelly send to ${CHANGELLY_SOL_DEST} using the quote id from the previous step.
```

**Expected behavior:**
- Invokes `changelly_send(external_network="solana", settle_address="${CHANGELLY_SOL_DEST}", amount_from="51", wallet_name="prompt_wallet_<DATETIME>", rate_id="<RATE_ID>")`
- Internally fetches the Liquid deposit address from Changelly and broadcasts USDt-Liquid
- Returns `order_id`, `deposit_hash` (Liquid txid), `deposit_address`, `amount_from`, `amount_to`, `status`, `expires_at`, `track_url`
- `deposit_hash` is verifiable on `https://blockstream.info/liquid/tx/<txid>`

---

### 4. Track Order Status

```
Check the status of Changelly order <ORDER_ID>.
```

**Expected behavior:**
- Invokes `changelly_status(order_id="<ORDER_ID>")`
- State machine: new → waiting → confirming → exchanging → sending → finished
- Returns `is_final`, `is_success`, `is_failed`
- ⚠️ Compare `amount_to` actually received vs `amount_to` from the quote. Issue #54 says fees may be silently subtracted from the receive — note any discrepancy.

---

## Section B — Receive USDt-Solana → USDt-Liquid

> ⚠️ Issue #53: `changelly_receive` only warns when `external_refund_address` is missing. **Always pass it** to avoid stuck orders.

### 5. Get a Receive Quote

```
Quote me receiving 51 L-USDt from sending USDt on Solana via Changelly.
```

**Expected behavior:**
- Invokes `changelly_quote(external_network="solana", direction="receive", amount_to="51")`
- Returns the Changelly quote payload

---

### 6. Create Receive Order with Refund Address

```
Create a Changelly receive order so I can deposit 51 USDt-Solana and have it settle to my Liquid wallet. Refund address on Solana is ${CHANGELLY_SOL_REFUND}.
```

**Expected behavior:**
- Invokes `changelly_receive(external_network="solana", wallet_name="prompt_wallet_<DATETIME>", external_refund_address="${CHANGELLY_SOL_REFUND}", amount_from="51")`
- Returns `order_id`, `deposit_address` (Solana), `settle_address` (lq1...), `amount_from`, `status`, `track_url`
- ⚠️ Confirms refund address was set; if the agent omits it, surface the warning explicitly (issue #53)

---

### 7. (Manual) Send USDt-Solana to the Deposit Address

The tester now sends exactly `amount_from` USDt-Solana from an external wallet to the returned `deposit_address`. No MCP action required.

---

### 8. Track Receive Order to Completion

```
What is the status of Changelly order <ORDER_ID>?
```

**Expected behavior:**
- `changelly_status` progresses to `finished`
- `lw_balance` shows the settled USDt-Liquid credited to the wallet
- ⚠️ Verify the received amount against `amountTo` from the original quote (issue #54)

---

## Notes / Known Issues

- **Issue #51**: Changelly returns `Validation failed (400)` in some regions. Document and abort if all prompts fail with this error.
- **Issue #53**: `external_refund_address` is only warned about, not enforced. The prompts pass it explicitly.
- **Issue #54**: fees may be silently subtracted from receive — capture the actual received amount and compare with the quote.
- **Issue #41** tracks this manual QA pass.
