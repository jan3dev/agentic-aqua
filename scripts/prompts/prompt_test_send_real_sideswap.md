# Real SideSwap Transaction Test Prompts

Manual test prompts for validating Aqua MCP **SideSwap** functionality with a real wallet. Covers BTC ↔ L-BTC pegs (peg-in / peg-out) and Liquid asset swaps (L-BTC ↔ USDt).

Use an agent with Sonnet model — these flows are multi-step and require careful confirmation before broadcasting.

## Prerequisites

- A `.env` file must exist in the project root with the required variables.
- The following environment variables must be set:

| Variable | Description |
|----------|-------------|
| `SIGNER_MNEMONIC` | BIP39 mnemonic (12 words) for the test wallet |
| `BTC_DEST_ADDRESS` | Bitcoin destination address (used as the peg-out target) |
| `SIDESWAP_PEG_AMOUNT_SATS` | Optional. Sats to peg. Default 200,000. Must be ≥ `min_peg_in_amount` returned by `sideswap_server_status` |
| `SIDESWAP_USDT_AMOUNT_SATS` | Optional. L-BTC sats for the asset swap. Default 5,000 |

**IMPORTANT:**
- BTC and L-BTC balances must cover the test amounts plus network and SideSwap fees.
- All test order IDs should be tracked on `https://sideswap.io` (peg monitor for pegs; the same dashboard for swap orders).
- Small asset swaps may need `flexible_small_amount=True` due to issue #55.

## Test Prompts

### 1. Import Wallet and Check Both Balances

```
Import this wallet with the name prompt_wallet_<DATETIME>, then show my unified balance:
SIGNER_MNEMONIC=${SIGNER_MNEMONIC}
```

**Expected behavior:**
- Wallet imported (or already exists) as `prompt_wallet_<DATETIME>`
- `unified_balance` returns Bitcoin and Liquid balances
- Both BTC and L-BTC balances are displayed and sufficient for the tests below

---

## Section A — Peg-In (BTC → L-BTC)

### 2. Quote a Peg-In

```
Quote a SideSwap peg-in for ${SIDESWAP_PEG_AMOUNT_SATS} sats.
```

**Expected behavior:**
- Invokes `sideswap_peg_quote(amount=${SIDESWAP_PEG_AMOUNT_SATS}, peg_in=True)`
- Returns `send_amount`, `recv_amount`, `fee_amount`

---

### 3. Initiate Peg-In and Send BTC

```
Start a SideSwap peg-in to my Liquid wallet, then send ${SIDESWAP_PEG_AMOUNT_SATS} sats from my Bitcoin wallet to the peg deposit address.
```

**Expected behavior:**
- Invokes `sideswap_peg_in(wallet_name="prompt_wallet_<DATETIME>")` and returns `order_id`, `peg_addr` (bc1...), `recv_addr` (lq1...)
- Confirms intent with the user, then invokes `btc_send` with `address=peg_addr` and `amount=${SIDESWAP_PEG_AMOUNT_SATS}`
- Returns the broadcast `txid`

---

### 4. Track Peg-In Status

```
Check the status of SideSwap peg order <ORDER_ID>.
```

**Expected behavior:**
- Invokes `sideswap_peg_status(order_id="<ORDER_ID>")`
- Returns `peg_in=true`, `status` progressing through pending → processing → completed
- After 2 BTC confirmations (~20 min) L-BTC arrives at `recv_addr`; verify with `lw_balance` on the same wallet

---

## Section B — Peg-Out (L-BTC → BTC)

### 5. Quote a Peg-Out

```
Quote a SideSwap peg-out for ${SIDESWAP_PEG_AMOUNT_SATS} sats.
```

**Expected behavior:**
- Invokes `sideswap_peg_quote(amount=${SIDESWAP_PEG_AMOUNT_SATS}, peg_in=False)`
- Returns the peg-out send/recv/fee

---

### 6. Execute Peg-Out

```
Peg out ${SIDESWAP_PEG_AMOUNT_SATS} sats of L-BTC to this Bitcoin address: ${BTC_DEST_ADDRESS}
```

**Expected behavior:**
- Invokes `sideswap_peg_out(wallet_name=..., amount=${SIDESWAP_PEG_AMOUNT_SATS}, btc_address="${BTC_DEST_ADDRESS}")`
- L-BTC is signed and broadcast; returns `order_id`, `lockup_txid`, `peg_addr`, `recv_addr`
- The `lockup_txid` is verifiable on `https://blockstream.info/liquid/tx/<lockup_txid>`

---

### 7. Track Peg-Out Status

```
What is the status of SideSwap peg order <ORDER_ID>?
```

**Expected behavior:**
- Invokes `sideswap_peg_status`
- Status progresses pending → processing → completed (~15–60 min total)
- After completion the destination BTC address receives the funds

---

## Section C — Asset Swap L-BTC → USDt

### 8. Quote the L-BTC → USDt Swap

```
Quote on SideSwap how much USDt I would get by sending ${SIDESWAP_USDT_AMOUNT_SATS} sats of L-BTC.
```

**Expected behavior:**
- Invokes `sideswap_list_assets` to resolve the USDt asset_id (if needed), then `sideswap_quote(asset_id=<USDT>, send_amount=${SIDESWAP_USDT_AMOUNT_SATS}, send_bitcoins=True)`
- Returns `send_amount`, `recv_amount`, `price`, `fixed_fee`

---

### 9. Execute the L-BTC → USDt Swap

```
Execute the swap now. Use flexible_small_amount=true so dealer rounding doesn't fail it.
```

**Expected behavior:**
- Invokes `sideswap_execute_swap(asset_id=<USDT>, send_amount=${SIDESWAP_USDT_AMOUNT_SATS}, send_bitcoins=True, flexible_small_amount=True, min_recv_amount=<from previous quote>)`
- Returns `order_id`, `txid`, `recv_amount`
- The `txid` is verifiable on `https://blockstream.info/liquid/tx/<txid>`

---

### 10. Verify Swap Settled

```
What is the status of SideSwap order <ORDER_ID> and its on-chain confirmation?
```

**Expected behavior:**
- Invokes `sideswap_swap_status(order_id="<ORDER_ID>")` → returns persisted status + `txid`
- Invokes `lw_tx_status(tx="<TXID>")` → confirmations and explorer URL
- `lw_balance` shows the USDt credited

---

## Section D — Asset Swap USDt → L-BTC

### 11. Quote and Execute the Reverse Swap

```
Quote me selling ${SIDESWAP_USDT_AMOUNT_SATS} sats of USDt for L-BTC on SideSwap, then execute the swap if the price looks reasonable.
```

**Expected behavior:**
- Invokes `sideswap_quote(asset_id=<USDT>, send_amount=${SIDESWAP_USDT_AMOUNT_SATS}, send_bitcoins=False)` → preview
- After confirmation, invokes `sideswap_execute_swap(send_bitcoins=False, ...)` with `min_recv_amount` from the quote
- Returns `txid` and `recv_amount` in L-BTC sats

---

### 12. Check Final Balance

```
Show me my updated Liquid balance for L-BTC and USDt.
```

**Expected behavior:**
- `lw_balance` reflects all swaps and pegs from this session
- USDt balance reflects sections C and D netting out (modulo fees)

---

## Notes / Known Issues

- **Issue #55**: small asset swaps fail without `flexible_small_amount=True`. The prompts above set it explicitly.
- **Issue #56**: peg-in/peg-out does not expose `--fee-rate`; BDK uses 1 sat/vB by default which may delay confirmation. Track on the BTC explorer if the order stays pending unusually long.
- **Issue #41** tracks this manual QA pass.
