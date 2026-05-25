# Real SideShift Transaction Test Prompts

Manual test prompts for validating Aqua MCP **SideShift** cross-chain swap functionality with a real wallet. Covers fixed-rate sends (BTC/L-USDt → external) and variable-rate receives (external → Liquid).

Use an agent with Sonnet model.

## Prerequisites

- A `.env` file must exist in the project root with the required variables.
- The following environment variables must be set:

| Variable | Description |
|----------|-------------|
| `SIGNER_MNEMONIC` | BIP39 mnemonic (12 words) for the test wallet |
| `SIDESHIFT_USDT_TRON_DEST` | USDt-Tron address to receive in send tests |
| `SIDESHIFT_BTC_DEST` | Bitcoin address to receive in BTC send test |
| `EXTERNAL_USDT_TRON_REFUND` | USDt-Tron refund address for the receive test |
| `SIDESHIFT_BTC_AMOUNT` | Optional. BTC sats to swap. Default 50,000 |
| `SIDESHIFT_USDT_AMOUNT` | Optional. Decimal amount of USDt-Liquid to swap. Default "5" |

**IMPORTANT:**
- Wallet must have sufficient L-BTC, BTC, and (for Section B) USDt-Liquid balance.
- Each shift can be tracked on `https://sideshift.ai/orders/<shift_id>`.

## Test Prompts

### 1. Import Wallet and Verify Balances

```
Import this wallet with the name prompt_wallet_<DATETIME>, then show my unified balance:
SIGNER_MNEMONIC=${SIGNER_MNEMONIC}
```

**Expected behavior:**
- Wallet imported as `prompt_wallet_<DATETIME>`
- `unified_balance` shows BTC and Liquid balances (including USDt-Liquid if any)

---

## Section A — Send BTC → USDt on Tron

### 2. Pair Info and Quote

```
Get the SideShift pair info and a fixed-rate quote to send ${SIDESHIFT_BTC_AMOUNT} sats of BTC to USDt on Tron.
```

**Expected behavior:**
- Invokes `sideshift_pair_info(from_coin="BTC", from_network="bitcoin", to_coin="USDT", to_network="tron", amount="<btc decimal>")`
- Invokes `sideshift_quote(deposit_coin="BTC", deposit_network="bitcoin", settle_coin="USDT", settle_network="tron", deposit_amount="<btc decimal>")`
- Returns the SideShift quote `id` (we will reuse it as `quote_id`)

---

### 3. Execute the Send

```
Execute the SideShift send to ${SIDESHIFT_USDT_TRON_DEST} using the previous quote id.
```

**Expected behavior:**
- Invokes `sideshift_send(deposit_coin="BTC", deposit_network="bitcoin", settle_coin="USDT", settle_network="tron", settle_address="${SIDESHIFT_USDT_TRON_DEST}", deposit_amount="<btc decimal>", wallet_name="prompt_wallet_<DATETIME>", quote_id="<QUOTE_ID>")`
- Returns `shift_id`, `deposit_hash` (the BTC txid we broadcast), `deposit_address`, `deposit_amount`, `settle_amount`, `rate`, `status`
- BTC `deposit_hash` is verifiable on `https://blockstream.info/tx/<txid>`

---

### 4. Track Shift Status

```
Check the status of SideShift shift <SHIFT_ID>.
```

**Expected behavior:**
- Invokes `sideshift_status(shift_id="<SHIFT_ID>")`
- Status progresses waiting → pending → processing → settling → settled
- Returns `is_final`, `is_success`, `is_failed`

---

## Section B — Send USDt-Liquid → BTC on Bitcoin

> ⚠️ Issue #50: SideShift send on non-L-BTC Liquid assets needs the asset ID passed explicitly. The prompt below makes that explicit.

### 5. Resolve the Liquid USDt Asset ID

```
What is the asset_id of USDt on Liquid mainnet?
```

**Expected behavior:**
- Invokes `lw_list_assets(network="mainnet")` and surfaces the USDt asset_id

---

### 6. Quote and Send USDt-Liquid → BTC

```
Send ${SIDESHIFT_USDT_AMOUNT} USDt from my Liquid wallet to ${SIDESHIFT_BTC_DEST} via SideShift. Use the Liquid USDt asset_id I just got. Quote first, then send.
```

**Expected behavior:**
- Invokes `sideshift_quote(deposit_coin="USDT", deposit_network="liquid", settle_coin="BTC", settle_network="bitcoin", deposit_amount="${SIDESHIFT_USDT_AMOUNT}")`
- Confirms with user, then invokes `sideshift_send(deposit_coin="USDT", deposit_network="liquid", settle_coin="BTC", settle_network="bitcoin", settle_address="${SIDESHIFT_BTC_DEST}", deposit_amount="${SIDESHIFT_USDT_AMOUNT}", liquid_asset_id="<USDT_LIQUID_ID>", wallet_name="prompt_wallet_<DATETIME>", quote_id=...)`
- Returns `shift_id`, `deposit_hash` (Liquid txid)
- If the agent forgets `liquid_asset_id`, the call raises `ValueError` (issue #50). Re-invoke with the asset ID.

---

### 7. Confirm Settlement

```
What is the status of shift <SHIFT_ID>?
```

**Expected behavior:**
- `sideshift_status` returns `settled` and the destination BTC address has been credited
- Verify externally on `https://blockstream.info/address/${SIDESHIFT_BTC_DEST}`

---

## Section C — Receive USDt-Tron → USDt-Liquid

### 8. Create Receive Shift

```
Create a SideShift to receive USDt-Liquid into my wallet from an external USDt-Tron sender. Use ${EXTERNAL_USDT_TRON_REFUND} as the external refund address.
```

**Expected behavior:**
- Invokes `sideshift_receive(deposit_coin="USDT", deposit_network="tron", settle_coin="USDT", settle_network="liquid", wallet_name="prompt_wallet_<DATETIME>", external_refund_address="${EXTERNAL_USDT_TRON_REFUND}")`
- Returns `shift_id`, `deposit_address` (Tron), `deposit_min`, `deposit_max`, `settle_address` (lq1...), `status`

---

### 9. (Manual) Send USDt-Tron to the Deposit Address

The tester now sends a small amount of USDt-Tron (within `deposit_min`/`deposit_max`) from an external wallet to the returned `deposit_address`. No MCP action required for this step.

---

### 10. Confirm Receive Settled

```
What is the status of shift <SHIFT_ID>?
```

**Expected behavior:**
- `sideshift_status` progresses to `settled`
- The wallet's Liquid balance increases by the settled USDt amount (verify with `lw_balance`)

---

## Notes / Known Issues

- **Issue #50**: `sideshift_send` does not auto-resolve `liquid_asset_id` for non-L-BTC Liquid assets. Prompt #6 documents the workaround.
- **Issue #41** tracks this manual QA pass.
