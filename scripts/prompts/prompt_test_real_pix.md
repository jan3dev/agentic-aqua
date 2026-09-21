# Real PIX → DePix Test Prompts (Ankara)

Manual test prompts for the Aqua MCP PIX on-ramp. All traffic must go to
`ANKARA_API_URL`. Never configure an Eulen API token, and never ask the user
for CPF, name, documents, EUID, or a claimed KYC status.

Use an agent with Sonnet. Give it only the prompt text (never share
"Expected behavior").

> Prefer **staging** (`ANKARA_API_URL=https://test.aquabtc.com`). Production
> spends real BRL. Creating a PIX charge and paying it moves real money.

## Prerequisites

- A logged-in JAN3 session for the test email (or the agent can run
  `jan3_login` → `jan3_verify`).
- A **mainnet** Liquid wallet already imported locally (PIX rejects testnet).
- Hosted KYC already `approved` + `VERIFIED` on that account, **or** Ankara's
  `eulen_kyc` switch enabled so the session/confirm tools work.
- PIX tools ship enabled; do not toggle them off in `~/.aqua/config.json`.

Test values:

```
PIX_TEST_EMAIL        = <jan3 account email>
PIX_TEST_WALLET       = default
PIX_TEST_AMOUNT_CENTS = 600          (R$6.00 gross; fee is dynamic)
```

---

## Section A — Session

### Inspect JAN3 session

```
Show my AQUA session status for PIX_TEST_EMAIL.
```

**Expected behavior:**
- `jan3_session_info(email=PIX_TEST_EMAIL)` reports an active session
- `base_url` matches `ANKARA_API_URL`

---

## Section B — Hosted KYC (skip if already VERIFIED)

### Start hosted KYC

```
Start Eulen hosted KYC for PIX_TEST_EMAIL. Do not ask me for CPF, name, or documents.
```

**Expected behavior:**
- `eulen_kyc_session(email=PIX_TEST_EMAIL)`
- Returns `session_id`, `operator_id`, `status`
- Agent shows those ids and waits; it must not invent a Noviuz URL
- If Ankara returns `EULEN_KYC_DISABLED`, stop this section and continue only
  if the account is already verified

### Confirm hosted KYC

```
I finished the hosted KYC UI. Confirm it for PIX_TEST_EMAIL with session_id <SESSION_ID>.
```

**Expected behavior:**
- `eulen_kyc_confirm(email=PIX_TEST_EMAIL, session_id="<SESSION_ID>")`
- Continue only when `session_status="approved"` and
  `verification_status="VERIFIED"`
- Body sent to Ankara is only `{session_id}`; no PII

---

## Section C — Create a charge

### Receive DePix via PIX

```
Create a PIX charge of 600 cents (R$6.00) to receive DePix in wallet PIX_TEST_WALLET
for PIX_TEST_EMAIL. Show me the Copia e Cola, fee, and net amount. Do not invent a QR URL.
```

**Expected behavior:**
- `pix_receive(email=PIX_TEST_EMAIL, amount_cents=600, wallet_name=PIX_TEST_WALLET)`
- Returns `swap_id` / `deposit_id` (Ankara pk), `qr_copy_paste`, `amount_cents`,
  `fee_cents`, `net_amount_cents`, `depix_address`, local `qr_code_path`
- Amounts are integer BRL cents, never a float
- The Liquid receive address is from the local mainnet wallet

Pay the PIX once in a bank app using `qr_copy_paste`. Then continue.

---

## Section D — List and status (Ankara is source of truth)

### List today's deposits

```
List the PIX deposits I made today for PIX_TEST_EMAIL.
```

**Expected behavior:**
- `pix_list(email=PIX_TEST_EMAIL, date_from="<YYYY-MM-DD>", date_to="<YYYY-MM-DD>")`
  with today's date in **UTC**, inclusive on both ends
- Does **not** call `/eulen/deposit/{id}/status/`
- Response includes `count` and `deposits[]` with `deposit_id`, `status`,
  `amount_cents`, `created_at`
- The new charge appears (status may still be `pending`)

### List last week

```
Show PIX deposits from last week for PIX_TEST_EMAIL.
```

**Expected behavior:**
- `pix_list` with inclusive UTC `date_from` / `date_to` covering last week
- Optional `status` filter is not required unless the user asked for one

### Refresh one deposit

```
What's the status of PIX deposit <DEPOSIT_ID> for PIX_TEST_EMAIL?
```

**Expected behavior:**
- `pix_status(swap_id="<DEPOSIT_ID>", email=PIX_TEST_EMAIL)`
- Email is required and must match the owning JAN3 account
- Internally uses `GET /eulen/deposits/?deposit_id=<id>` (Ankara DB), not the
  Eulen poll endpoint
- Poll until `depix_sent` or a terminal status (`canceled`, `error`,
  `refunded`, `expired`)
- `pending_pix2fa` is a deposit step, not identity KYC; report it, do not
  treat it as a KYC failure. There is no PIX-2FA tool in this surface
- On `depix_sent`, `blockchain_txid` is present when Ankara has it

---

## Section E — Wallet confirmation

```
Show the Liquid balances for PIX_TEST_WALLET, especially DePix.
```

**Expected behavior:**
- `lw_balance(wallet_name=PIX_TEST_WALLET)`
- After `depix_sent`, DePix increased by the **net** amount (not the gross PIX)

---

## Notes

- All MCP/CLI calls use a JAN3 JWT against `ANKARA_API_URL`. A request to
  `depix.eulen.app` (or any Eulen token) is a test failure.
- `pix_status` / `pix_list` overwrite local `~/.aqua/pix_swaps/{id}.json` with
  Ankara fields; `wallet_name` and fee cents are kept only if already local.
- CLI equivalents: `aqua eulen kyc-session|kyc-confirm|receive|list|status`
  (`status` and `list` always take `--email`).
