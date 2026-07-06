<!-- Generated: 2026-05-20 | Updated: 2026-05-20 -->

# agentic-aqua

MCP server for managing **Liquid Network** and **Bitcoin** wallets through AI assistants
(part of AQUA). Liquid via LWK (Blockstream); Bitcoin via BDK. One BIP39 mnemonic backs both
networks (unified wallet).

## Architecture

```
AI Assistant ──MCP──▶ aqua.server ──▶ tools.py ──▶ wallet.py  (LWK ─ Electrum/Esplora)
                                              └─▶ bitcoin.py (BDK ─ Esplora)
                                              └─▶ lightning.py / sideshift /
                                                  changelly / pix / wapupay  (third-party clients)
```

No local node. Liquid: Blockstream Electrum/Esplora. Bitcoin: Esplora only.

## Layout

| Path | What lives there | Read its `AGENTS.md` |
|------|------------------|---------------------|
| `src/aqua/` | Python package: server, tools, wallets, swap clients | `src/aqua/AGENTS.md` |
| `src/aqua/cli/` | `aqua` Click CLI (mirrors MCP tool surface) | `src/aqua/cli/AGENTS.md` |
| `tests/` | pytest suite, fixtures, mock patterns | `tests/AGENTS.md` |
| `scripts/` | One-off dev/release helpers | — |
| `dist/` | Build artifacts (do not edit) | — |

## Entry points

- `aqua.server:main` — MCP stdio server (registered as `aqua-mcp` and `agentic-aqua`).
- `aqua.cli.main:cli` — Click CLI registered as `aqua`.

## Dev commands

```bash
uv sync --all-extras                       # install dev deps
uv run python -m pytest tests/             # run full suite
uv run python -m pytest tests/test_x.py    # single file
uv run ruff check src tests                # lint
uv run python -m aqua.server               # run MCP server locally (stdio)
uv run aqua --help                         # CLI surface
```

Python ≥ 3.13, package manager `uv` only (never pip/venv directly).

## Code invariants (must hold across the codebase)

1. **Amounts are integer satoshis** end-to-end. Decimal strings appear only on third-party
   wires (SideShift, Changelly). Convert at the boundary; never propagate floats inward.
2. **One mnemonic → two wallets.** BTC derivation `m/84'/0'/0'`, Liquid `m/84'/1776'/0'` + SLIP-77
   master blinding key. Descriptors are NOT inter-derivable; watch-only setups need both.
3. **TXIDs are returned big-endian (display format).** BDK uses little-endian internally; flip
   at the boundary in `bitcoin.py`.
4. **At-rest password ≠ BIP39 passphrase.** Encrypts the mnemonic file (PBKDF2 480k + Fernet);
   does NOT change derived keys. Same mnemonic restores in any BIP39 wallet without it.
5. **No silent fallbacks.** If signing, broadcasting, or PSET verification fails, raise
   `ValueError` (or a specific exception). Do not return a fake-success envelope. See
   `CLAUDE.md` "No lies rules".
6. **File perms.** `~/.aqua/` is `0o700`; wallet/swap files `0o600` (contain secrets or
   refund keys).
7. **Atomic writes.** Wallet/swap files are written via temp file + `os.replace`.

## Error envelope (tool layer)

Tools return either a success dict or:

```python
{"error": {"code": "INSUFFICIENT_FUNDS", "message": "...", "details": {...}}}
```

`ValueError` is the most common in-process exception; tool wrappers translate it.

## Codebase exploration

Prefer TLDR MCP tools over raw grep for navigation:

| Need | Tool |
|------|------|
| Find code by behavior | `mcp__tldr__semantic` |
| Function/class map | `mcp__tldr__structure` |
| Call graph from entry | `mcp__tldr__context` |
| Find callers before refactor | `mcp__tldr__impact` |
| Tests affected by a change | `mcp__tldr__change_impact` |

Use `grep`/`Glob` only for exact strings or config lookups.

## What is documented elsewhere (do NOT inline here)

- **MCP tool / prompt / resource schemas** — live in `src/aqua/server.py`. The MCP server is
  self-describing; don't duplicate the schema in markdown.
- **Third-party protocol semantics** (Boltz status state machines, SideShift wire format,
  Changelly endpoints, Eulen Pix endpoints) — captured as docstrings
  and constants inside the relevant module.
- **On-disk JSON schemas** (wallet file, swap files) — the `@dataclass` definitions in
  `storage.py`, `lightning.py`, etc. are the source of truth. Read the
  dataclass to know the shape.
- **User-facing docs** — `README.md` and the MCP resources under `src/aqua/static/`.

## CLAUDE.md / project conventions

Project-level `CLAUDE.md` is symlinked from this file. Key user rules:
- Always use `uv` (never bare `pip` / `venv`).
- Never commit unless explicitly asked.
- Never use silent-fallback "fake success" patterns — raise or report.

<!-- MANUAL: Add project-specific notes below this line — preserved on regeneration. -->

## Branching / PR target

- `main` is the production branch.
- `develop` is the integration branch for code that is still pending broader testing.
- When opening code PRs by default, target `develop`, not `main`, unless the user explicitly asks otherwise.
- When comparing branch work for PR prep, prefer `git diff develop...HEAD` / `git log develop...HEAD --oneline` unless a different base branch is requested.

## SideSwap

sideswap.py

## Jan3/Aqua/Ankara account (Aqua backend)

**Future JAN3 account features go in `jan3_accounts.py`.**
The JAN3/AQUA/Ankara *account* surface (both login flows,
verify/logout/session, multi-account persistence + the WapuPay-key provisioning
call against `ANKARA_API_URL`) lives in **`jan3_accounts.py`** as
`Jan3AccountsClient` / `Jan3AccountsManager` / `Jan3Session`. `ankara.py` keeps
only the Lightning `AnkaraClient`/`AnkaraSwapInfo` plus the shared HTTP/PII/JWT
helpers (`_redact` / `_scrub_text` / `_mask` / `_extract_error_message` /
`_jwt_exp` / `_access_token_expired` / `SessionExpiredError`). `jan3_accounts.py`
imports those helpers from `ankara.py`; the dependency is one-way — `ankara.py`
never imports `jan3_accounts` or `wapupay`. `WapuPayManager` receives the account
manager as `self.jan3` (injected by `get_wapupay_manager` via `get_jan3_manager`,
which now builds a `Jan3AccountsManager`) and delegates provisioning to it.


- **JAN3 account login (multi-account, one session per email)** — two flows, both
  ending at `/api/v1/auth/verify/` → JWT, implemented by `Jan3AccountsManager` in
  `jan3_accounts.py`:
  - **Default (free email-OTP):** `jan3_login` → `jan3_verify`
    (`POST /api/v1/auth/login/`).
  - **Fallback (paid captchaless):** `jan3_login_start` → `jan3_login_complete`
    (`POST /api/v2/auth/login/`, funded with a signed L-BTC tx to AQUA's vault).
  - `jan3_session_info <email>` / `jan3_list_sessions` / `jan3_logout <email>`
    inspect/forget sessions. CLI: `aqua jan3 …`. The two login tools are
    independently toggleable via `config.enabled_tools` (disable `jan3_login` to
    leave only the captchaless fallback). Sessions are **decoupled** from WapuPay
   calls (those need the API key, not a login); `jan3_logout` forgets a
    session but does **not** delete the provisioned API key.

- **Lightning Address (LN-address delivery)** — also on `Jan3AccountsManager`
  (per-email JWT, either login flow). A user's Lightning Address is
  `ln_username@<domain>`; the backend returns the full address in the
  `ln_username` field — surface it verbatim, never append a domain. Four tools:
  - `jan3_purchase_ln_username <email> <ln_username>` — buys/updates the username
    on-chain: creates an `LN_USERNAME_UPDATE` payment request and funds it with a
    signed L-BTC tx (`wallet_manager.craft_raw_tx` → `submit_raw_tx`). Check first
    with `jan3_ln_check_username`.
  - `jan3_enable_lightning_address <email> --enable/--disable` — opts the account in/out
    of LN-address delivery. **On enable it immediately populates the pool** of
    unused Liquid receive addresses (best-effort, reported under
    `ln_address_pool`); received BTC lands on those stored addresses.
  - `jan3_user_info <email>` — account profile; when LN-address is active it
    **auto-tops-up the pool** (best-effort, never fails the read).
  - `register_ln_addresses` / `ensure_ln_pool` are **internal manager methods,
    NOT MCP tools** — the pool self-heals via toggle-on + `jan3_user_info`. They
    rely on `WalletManager.reserve_addresses` (batched minting) and
    `WalletManager.fingerprint` (account↔wallet binding); the no-arg
    `get_address` advances a persisted `WalletData.next_address_index` counter so
    off-chain handouts never reuse an index.
  - `next_address_index` caveats: the counter lives **only in the local wallet
    JSON**. A seed reimport (or deleted `~/.aqua`) resets it to 0 while the
    server still delivers to the previously registered pool; the first
    successful pool registration repairs it (`ensure_counter_covers` matches
    the server's unused pool against derivations and bumps the counter), but
    until then no-arg `get_address` can re-hand pool indices. **Downgrade
    break**: releases ≤ v0.4.2 construct `WalletData` strictly and crash on
    wallet files containing this key — call it out in release notes.

## WapuPay (Argentine direct-fiat)

`wapupay.py` lets a user pay an Argentine bank account in **ARS**, funded with
**USDT on Liquid**. WapuPay's API is called **directly** (`https://be-prod.wapu.app`),
not through Ankara. Each call carries WapuPay's own **`X-API-Key`**;
`wapupay_create_order` returns a Liquid USDT funding address; the user pays it
with `lw_send_asset` (no auto-pay). `wapupay_exchange_rates` is **public** (no key).

WapuPay logic (orders, quotes, `X-API-Key` calls)
lives in `wapupay.py`.
- **WapuPay API key** — resolved lazily per business call
  (`WapuPayManager._require_api_key`): `WAPUPAY_API_KEY` env var first, then the
  key provisioned via `wapupay_provision_account` and persisted to
  `~/.aqua/wapupay/api_key.json` (env wins). Neither set → a clear `ValueError`
  pointing at both. A WapuPay 401 means the key is wrong, not a session issue.


- **Provisioning a key** — `wapupay_provision_account <email>` (CLI
  `aqua wapupay provision-account --email …`) is for the user who has no
  `WAPUPAY_API_KEY`. `WapuPayManager.provision_account(email)` delegates the
  backend call to `Jan3AccountsManager.provision_wapupay_token(email)` (in
  `jan3_accounts.py`), which POSTs `/api/v1/wapupay/account/` with that account's
  AQUA JWT (`Authorization: Bearer`, **no** `X-API-Key` — this hits AQUA/Ankara,
  not WapuPay) and returns `{"token": ...}`; WapuPay then stores the key locally
  so every `wapupay_*` tool works. **Requires a prior `jan3_login`/`jan3_verify`
  (or the captchaless flow) for that email** — provisioning works with a session
  from *either* flow (it only needs the JWT). The raw key is never returned
  (masked preview only). The backend issues a **fresh key on every call and
  invalidates the previous one** (no grace period, verified), so the tool **only
  calls the backend when no key is configured yet** — if one already exists (env
  var or stored) it is a no-op (`already_configured`), so it never invalidates a
  working key.

- **Env vars:** `WAPUPAY_BASE_URL` (default `https://be-prod.wapu.app`; override
  for staging, e.g. `https://be-stage.wapu.app`), `WAPUPAY_API_KEY` (used for
  business calls if set), and `ANKARA_API_URL` (default `https://ankara.aquabtc.com`,
  shared with Lightning; used by the `jan3_*` login **and** the WapuPay
  provisioning endpoint — same backend, staging `https://test.aquabtc.com`).
- **Enabled by default.** All `jan3_*` / `wapupay_*` tools ship enabled (not in
  `features._SHIPPED_DISABLED`). Business calls still need a key — env var or
  provisioned via `wapupay_provision_account`.
- **Rail pinned** to Liquid USDT; WapuPay rejects any other funding rail (400).
- JAN3 sessions persist per-email at `~/.aqua/jan3/{email}.json`; the
  provisioned API key and order records persist under `~/.aqua/wapupay/` — all at
  `0o600`. Bank PII + tokens + API key are never logged (see `ankara._redact` /
  `_SENSITIVE_LOG_FIELDS`, which includes `token`).
