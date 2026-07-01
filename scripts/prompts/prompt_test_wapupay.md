# Real WapuPay Direct-Fiat Test Prompts

Use a sub agent with Haiku model. Provide only the minimum context to it to work (never share the "Expected behavior")

> ⚠️ **Run against STAGING.** Set `WAPUPAY_BASE_URL=https://be-stage.wapu.app` so
> `wapupay_create_order` never creates a real production order. The flow below stops
> **before** funding (no `lw_send_asset`), so no USDT ever leaves the wallet — the order
> is left at `FUNDING_ISSUED` and expires on its own.

Test values used throughout:

```
WAPUPAY_TEST_ALIAS         = test.alias.mp
WAPUPAY_TEST_AMOUNT_ARS    = 10000          (the minimum)
WAPUPAY_TEST_RECEIVER_NAME = Test Receiver
WAPUPAY_TEST_REFUND_ADDR   = lq1qqw4fd5njdy2faru0p4dspqcp6nzvkgptaf3drukhrx7gp456srvzryvtstr7q3r4j25w3u0adc6dta8yxmgnklhd9zwtk6lng
WAPUPAY_TEST_TX_ID         = 7d977c86-23d0-4505-8078-c1e7362eebac
```

---

## Section A — AQUA Account Session (`aqua_*`)

> Run this section only if you want to exercise login + key provisioning. If you already
> have `WAPUPAY_API_KEY` set, skip to Section C.


### Inspect Aqua Session

```
Show my AQUA session status.
```

**Expected behavior:**
- `jan3_session_info(email)` reports whether a session is active (and its expiry)

---

## Provision a WapuPay Key (`wapupay_provision_account`)

Create for me a Wapupay account throug Aqua

**Expected behavior:**
- Invokes `wapupay_provision_account()`
- If no key configured: `provisioned=True`, masked `key_preview`, `created_at`, a `warning`
  about rotation. Key stored at `~/.aqua/wapupay/api_key.json` (0o600).
- If a key already exists (env or stored): `already_configured=True`, `source`, `key_preview`
  — **no backend call** (so a working key is never invalidated)

---

### Exchange Rates (public, no key)

```
What are WapuPay's current exchange rates?
```

**Expected behavior:**
- Invokes `wapupay_exchange_rates()` — public endpoint, works even with no key
- Returns a non-empty rates payload (e.g. USDT/ARS)

---

### Spending Limit

```
What's my WapuPay monthly spending limit?
```

**Expected behavior:**
- Invokes `wapupay_spending_limit()`
- Returns the account's KYC tier plus monthly limit / amount used (in USDT)

---

### Quote (no alias)

```
Quote me how much USDT it costs to pay 10000 ARS via WapuPay.
```

**Expected behavior:**
- Invokes `wapupay_quote(amount_ars="10000")`
- Returns `usdt_amount`, `fee`, `total_amount`, `exchange_rate` — **no order created**

---

### Quote with Alias Validation

```
Quote 10000 ARS to bank alias test.alias.mp, and tell me if the alias is valid.
```

**Expected behavior:**
- Invokes `wapupay_quote(amount_ars="10000", alias="test.alias.mp")`
- Response includes `valid_cbu_alias` confirming the alias/CBU/CVU is valid before committing

---

### List Transactions (WapuPay server-side view)

```
Show my WapuPay transactions.
```

**Expected behavior:**
- Invokes `wapupay_transactions()` → `{"transactions": [...]}` (may be empty on a fresh key)

---

### Get a Single Transaction

```
Show me WapuPay transaction 7d977c86-23d0-4505-8078-c1e7362eebac
```

**Expected behavior:**
- Invokes `wapupay_transaction(id="7d977c86-23d0-4505-8078-c1e7362eebac")`
- Returns that transaction's detail (status, amounts, timestamps)

---

### List Local Orders

```
List my WapuPay orders.
```

**Expected behavior:**
- Invokes `wapupay_orders()` → `{"orders": [...]}` (local recovery records this device created)
- Each carries `tentative_id`, `status`, and any `funding_transaction_id` / `executed_transaction_id`

---

## Section D — Create & Track an Order (STAGING ONLY, never funded)

### Create the Order

```
Create a WapuPay order to pay 10000 ARS to alias test.alias.mp for "Test Receiver",
using lq1qqw4fd5njdy2faru0p4dspqcp6nzvkgptaf3drukhrx7gp456srvzryvtstr7q3r4j25w3u0adc6dta8yxmgnklhd9zwtk6lng
as the refund address. Show me the funding instructions but DO NOT pay yet.
```

**Expected behavior:**
- Invokes `wapupay_create_order(amount_ars="10000", alias="test.alias.mp", receiver_name="Test Receiver", refund_address="lq1qqw4...k6lng")`
- Returns `tentative_id`, `status` (`FUNDING_ISSUED`), `address_destination` (Liquid `lq1…/ex1…/VJL…`),
  `asset_id` (USDT on Liquid), `funding_amount_usdt`, `total_amount_usdt`,
  `total_funding_amount_base_units`, `funding_expires_at`, `pay_instructions`, `qr_code_path`
- ⚠️ The agent shows the funding address/QR **but must NOT call `lw_send_asset`** — keep this dry

---

### Re-issue Funding Instructions

```
Fetch the funding instructions for that order before they expire.
```

**Expected behavior:**
- Invokes `wapupay_fund_order(tentative_id="<TENTATIVE_ID>")`
- Returns the same `address_destination`, `asset_id`, funding amounts, `pay_instructions`, `qr_code_path`
- Still **no payment** — this only re-issues instructions

---

### Check Order Status

```
What's the status of WapuPay order 030de9a6-5bc0-4ed8-971a-150663283161?
```

**Expected behavior:**
- Invokes `wapupay_order_status(tentative_id="<TENTATIVE_ID>")`
- Returns `status` plus `is_final` / `is_success` / `is_failed`
- Since we never funded, it stays at `FUNDING_ISSUED` until it `EXPIRED`s

---

## Notes

- **The real payment step is deliberately omitted.** A live transfer would be:
  `wapupay_create_order` → pay `total_funding_amount_base_units` of `asset_id` to
  `address_destination` with `lw_send_asset` → `wapupay_order_status` until `EXECUTED`.
  This test never performs that send.
- **Rail is pinned to Liquid USDT.** WapuPay rejects any other funding rail (400).
- **Amounts:** `amount_ars` is a decimal ARS string (`"10000"`); funding amounts are USDT.
  Pay `total_funding_amount_base_units` (USDT base units), not `amount_ars`.
- Bank PII, tokens, and the API key are never logged (`ankara._redact`).
