# Real SideShift Transaction Test Prompts

Manual test prompts for validating Aqua MCP **SideShift** cross-chain swap functionality with a real wallet.

**Scope:** SideShift in agentic-aqua is intentionally scoped to **USDt-Liquid ↔ USDt-other-network** swaps (see `ALLOWED_PAIRS` in `src/aqua/sideshift.py`). L-BTC ↔ BTC mainchain peg-in/peg-out is **not** SideShift territory — that lives in `prompt_test_send_real_sideswap.md` (`sideswap_peg_in` / `sideswap_peg_out`). This prompt exercises USDt-Liquid ↔ USDt-Polygon in both directions.

Use an agent with Sonnet model.

## Prerequisites

- A `.env` file must exist in the project root with the required variables.
- The following environment variables must be set:

| Variable | Description |
|----------|-------------|
| `SIGNER_MNEMONIC` | BIP39 mnemonic (12 words) for the test wallet |
| `SIDESHIFT_USDT_POLYGON_DEST` | USDt-Polygon (0x…) address to receive in the send test |
| `EXTERNAL_USDT_POLYGON_REFUND` | USDt-Polygon (0x…) refund address for the receive test |
| `SIDESHIFT_USDT_AMOUNT` | Optional. Decimal amount of USDt-Liquid to swap. Default `"5"` (pair min is 1) |

**IMPORTANT:**
- Wallet must have sufficient USDt-Liquid balance plus L-BTC for the network fee on the Liquid send.
- Each shift can be tracked on `https://sideshift.ai/orders/<shift_id>`.
- SideShift labels the Polygon settle asset `USDT0` (LayerZero's canonical USDT on Polygon); the deposit side from Liquid is reported as `USDT`. This is expected.

## Test Prompts

### 1. Import Wallet and Verify Balances

```
Import this wallet with the name prompt_wallet_<DATETIME>, then show my unified balance:
SIGNER_MNEMONIC=${SIGNER_MNEMONIC}
```

**Expected behavior:**
- Wallet imported as `prompt_wallet_<DATETIME>`
- `unified_balance` shows L-BTC and USDt-Liquid balances sufficient for the test

---

## Section A — Send USDt-Liquid → USDt-Polygon

### 2. Pair Info and Quote

```
Get the SideShift pair info and a fixed-rate quote to send ${SIDESHIFT_USDT_AMOUNT} USDt from Liquid to USDt on Polygon.
```

**Expected behavior:**
- Invokes `sideshift_pair_info(from_coin="USDT", from_network="liquid", to_coin="USDT", to_network="polygon", amount="${SIDESHIFT_USDT_AMOUNT}")`
- Invokes `sideshift_quote(deposit_coin="USDT", deposit_network="liquid", settle_coin="USDT", settle_network="polygon", deposit_amount="${SIDESHIFT_USDT_AMOUNT}")`
- Returns the SideShift quote `id` (we will reuse it as `<QUOTE_ID>`)

---

### 3. Execute the Send

```
Execute the SideShift send to ${SIDESHIFT_USDT_POLYGON_DEST} using the previous quote id.
```

**Expected behavior:**
- Invokes `sideshift_send(deposit_coin="USDT", deposit_network="liquid", settle_coin="USDT", settle_network="polygon", settle_address="${SIDESHIFT_USDT_POLYGON_DEST}", deposit_amount="${SIDESHIFT_USDT_AMOUNT}", wallet_name="prompt_wallet_<DATETIME>", quote_id="<QUOTE_ID>")` — **no `liquid_asset_id` needed**, auto-resolved from `deposit_coin`
- Returns `shift_id`, `deposit_hash` (Liquid txid), `deposit_address`, `deposit_amount`, `settle_amount`, `rate`, `status`
- Liquid `deposit_hash` is verifiable on `https://blockstream.info/liquid/tx/<txid>`

---

### 4. Track Shift Status

```
Check the status of SideShift shift <SHIFT_ID>.
```

**Expected behavior:**
- Invokes `sideshift_status(shift_id="<SHIFT_ID>")`
- Status progresses waiting → pending → processing → settling → settled
- Returns `is_final`, `is_success`, `is_failed`
- The destination 0x address on Polygon receives the settled USDT; verify on `https://polygonscan.com/address/${SIDESHIFT_USDT_POLYGON_DEST}`

---

## Section B — Receive USDt-Polygon → USDt-Liquid

### 5. Create Receive Shift

```
Create a SideShift to receive USDt-Liquid into my wallet from an external USDt-Polygon sender. Use ${EXTERNAL_USDT_POLYGON_REFUND} as the external refund address.
```

**Expected behavior:**
- Invokes `sideshift_receive(deposit_coin="USDT", deposit_network="polygon", settle_coin="USDT", settle_network="liquid", wallet_name="prompt_wallet_<DATETIME>", external_refund_address="${EXTERNAL_USDT_POLYGON_REFUND}")`
- Returns `shift_id`, `deposit_address` (Polygon 0x…), `deposit_min`, `deposit_max`, `settle_address` (lq1…), `status`

---

### 6. (Manual) Send USDt-Polygon to the Deposit Address

The tester now sends a small amount of USDt-Polygon (within `deposit_min`/`deposit_max`) from an external wallet to the returned `deposit_address`. No MCP action required for this step.

---

### 7. Confirm Receive Settled

```
What is the status of shift <SHIFT_ID>?
```

**Expected behavior:**
- `sideshift_status` progresses to `settled`
- The wallet's Liquid USDt balance increases by the settled amount (verify with `lw_balance`)

---

## Notes / Known Issues

- **Out of scope here**: L-BTC ↔ BTC mainchain (use SideSwap peg-in/peg-out) and any BTC ↔ USDt flows (also fall outside the curated SideShift scope for AQUA — the allowlist permits them but they are not the supported product path).
