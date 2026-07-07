# Read-Only Integration Test Prompts

Manual test prompts for validating Aqua MCP **integration tools** (SideSwap, SideShift, ChangeChangelly) without spending coins or broadcasting transactions. Covers read-only flows: server status, asset listings, quotes, pair info, and recommendations.

Use an agent with Haiku model to run these prompts.

## Prerequisites

- A `.env` file must exist in the project root with the required variables.
- The following environment variable must be set:

| Variable | Description |
|----------|-------------|
| `SIGNER_MNEMONIC` | BIP39 mnemonic (12 words) for the test wallet |

- Network defaults to mainnet for all prompts below.

## Test Prompts

### 1. Import Wallet

```
Import this wallet, name it prompt_wallet_<DATETIME> and tell me the general balance:
SIGNER_MNEMONIC=${SIGNER_MNEMONIC}
```

**Expected behavior:**
- Wallet is imported (or already exists) with name `prompt_wallet_<DATETIME>`
- `unified_balance` returns Bitcoin and Liquid balances

---

### 2. SideSwap Server Status

```
What is the current SideSwap server status on mainnet? Show fees, minimum peg amounts, and hot-wallet balances.
```

**Expected behavior:**
- Invokes `sideswap_server_status` with `network="mainnet"`
- Returns `elements_fee_rate`, `min_peg_in_amount`, `min_peg_out_amount`, `server_fee_percent_peg_in`, `server_fee_percent_peg_out`, `peg_in_wallet_balance`, `peg_out_wallet_balance`
- Includes a `warning` field if the SideSwap API is unreachable and defaults are used

---

### 3. SideSwap Supported Assets

```
Show me the Liquid assets SideSwap supports for atomic swaps on mainnet.
```

**Expected behavior:**
- Invokes `sideswap_list_assets`
- Returns at least L-BTC, USDt, and likely DePix, EURx, MEX
- Each asset includes `asset_id`, `ticker`, `name`, `precision`, `instant_swaps`

---

### 4. SideSwap Recommendation: BTC → L-BTC (small amount)

```
I want to convert 200,000 sats from BTC to L-BTC. Should I use a peg or an instant swap?
```

**Expected behavior:**
- Invokes `sideswap_recommend` with `amount=200000`, `direction="btc_to_lbtc"`
- Returns `recommendation` ("peg" | "swap" | "either"), `reason`, `peg_pros`, `peg_cons`
- Includes the live `server_status` snapshot

---

### 5. SideSwap Peg Quote (peg-in)

```
Quote a peg-in for 500,000 sats. How much L-BTC would I receive after fees?
```

**Expected behavior:**
- Invokes `sideswap_peg_quote` with `amount=500000`, `peg_in=True`
- Returns `send_amount`, `recv_amount`, `fee_amount`, `peg_in=true`
- `fee_amount` ≈ 0.1% of send + ~286 sats Liquid claim fee

---

### 6. SideSwap L-BTC → USDt Price Quote

```
Give me a read-only price quote on SideSwap to swap 100,000 sats of L-BTC into USDt.
```

**Expected behavior:**
- Invokes `sideswap_quote` with the USDt asset_id, `send_amount=100000`, `send_bitcoins=True`
- Returns `send_amount`, `recv_amount`, `price`, `fixed_fee`
- No transaction is broadcast

---

### 7. SideShift Coin/Network Discovery

```
List the coins and networks SideShift supports.
```

**Expected behavior:**
- Invokes `sideshift_list_coins`
- Returns a list with `coin`, `name`, `networks`, `hasMemo`, `fixedOnly`/`variableOnly`
- Includes BTC, USDT (multiple networks), ETH, TRX, etc.

---

### 8. SideShift Pair Info

```
What is the SideShift rate for USDt on Tron to USDt on Liquid for around 50 USDt?
```

**Expected behavior:**
- Invokes `sideshift_pair_info` with `from_coin="USDT"`, `from_network="tron"`, `to_coin="USDT"`, `to_network="liquid"`, `amount="51"`
- Returns `rate`, `min`, `max`, plus deposit/settle coin and network fields

---

### 9. SideShift Fixed-Rate Quote (read-only)

```
Quote a fixed-rate SideShift to send 20 USDT from Liquid USDT to USDT on Ethereum.
```

**Expected behavior:**
- Invokes `sideshift_quote` with `deposit_coin="USDT"`, `deposit_network="liquid"`, `settle_coin="USDT"`, `settle_network="ethereum"`, `deposit_amount="20"`
- Returns SideShift quote payload: `id`, `expiresAt`, `depositAmount`, `settleAmount`, `rate`
- No shift is created

---

### 10. SideShift vs SideSwap Recommendation

```
I want to convert L-BTC to BTC. Should I use SideSwap or SideShift?
```

**Expected behavior:**
- Invokes `sideshift_recommend` with `from_coin="btc"`, `from_network="liquid"`, `to_coin="btc"`, `to_network="bitcoin"`
- Returns `recommendation="sideswap"` (both legs on Bitcoin/Liquid), with explanatory `reason`

---

### 11. Changelly Currencies

```
List the currencies Changelly supports.
```

**Expected behavior:**
- Invokes `changelly_list_currencies`
- Returns a list of currency identifiers and a `count`
- Note: agentic-aqua only enables USDt-Liquid ↔ USDt-on-X for actual swaps; this listing is unrestricted

---

### 12. Changelly Quote — Send (Liquid → Solana)

```
Quote me sending 50 L-USDt to USDt on Solana via Changelly.
```

**Expected behavior:**
- Invokes `changelly_quote` with `external_network="solana"`, `direction="send"`, `amount_from="51"`
- Returns Changelly response with `id` (rate_id), `result`, `amountFrom`, `amountTo`, `networkFee`, `min`, `max`, `expiredAt`
- ⚠️ Issue #51: Changelly may return `Validation failed (400)` depending on region. If this happens, document and continue with the remaining prompts.

---

### 13. Changelly Quote — Receive (Solana → Liquid)

```
Quote me receiving 50 L-USDt from sending USDt on Solana via Changelly.
```

**Expected behavior:**
- Invokes `changelly_quote` with `external_network="solana"`, `direction="receive"`, `amount_to="51"` (or `amount_from="51"`)
- Same response shape as prompt #12

---
