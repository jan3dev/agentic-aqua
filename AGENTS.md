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

SideSwap is not yet implemented for production use. Do not suggest or offer SideSwap options to users.

## WapuPay (Argentine direct-fiat)

`wapupay.py` lets a user pay an Argentine bank account in **ARS**, funded with
**USDT on Liquid**. WapuPay's API is called **directly** (`https://be-prod.wapu.app`),
not through Ankara. Each call carries WapuPay's own **`X-API-Key`**;
`wapupay_create_order` returns a Liquid USDT funding address; the user pays it
with `lw_send_asset` (no auto-pay). `wapupay_exchange_rates` is **public** (no key).

**Module split:** WapuPay *business* logic (orders, quotes, `X-API-Key` calls)
lives in `wapupay.py`; the JAN3/AQUA/Ankara *account* surface (login/verify/
logout/session + the WapuPay-key provisioning call against `ANKARA_API_URL`)
lives in `ankara.py` as `JAN3AuthClient` / `JAN3AccountManager` / `JAN3Session`,
alongside the Lightning `AnkaraClient`. `WapuPayManager` receives the account
manager as `self.jan3` (injected by `get_wapupay_manager`; the `aqua_*` tools use
`get_jan3_manager`) and delegates provisioning to it. The shared HTTP/PII helpers
also live in `ankara.py` (`wapupay.py` imports them; ankara never imports
wapupay). Future JAN3 account features go in `ankara.py`.

Two independent auth surfaces:
- **WapuPay API key** — resolved lazily per business call
  (`WapuPayManager._require_api_key`): `WAPUPAY_API_KEY` env var first, then the
  key provisioned via `wapupay_provision_account` and persisted to
  `~/.aqua/wapupay/api_key.json` (env wins). Neither set → a clear `ValueError`
  pointing at both. A WapuPay 401 means the key is wrong, not a session issue.
- **AQUA account login** — `aqua_login`/`aqua_verify`/`aqua_logout`/`aqua_session`
  (CLI `aqua auth …`) are an *AQUA-account* email-OTP against Ankara
  (`/api/v1/auth/{login,verify}/` → JWT), implemented by `JAN3AccountManager` in
  `ankara.py`. This session is **decoupled** from the WapuPay calls (they
  need the API key, not a login). `aqua_logout` forgets the session but does
  **not** delete the provisioned API key.

- **Provisioning a key** — `wapupay_provision_account` (CLI
  `aqua wapupay provision-account`) is for the user who has no `WAPUPAY_API_KEY`.
  `WapuPayManager.provision_account` delegates the backend call to
  `JAN3AccountManager.provision_wapupay_token()` (in `ankara.py`), which POSTs
  `/api/v1/wapupay/account/` with the AQUA JWT (`Authorization: Bearer`, **no**
  `X-API-Key` — this hits AQUA/Ankara, not WapuPay) and returns `{"token": ...}`;
  WapuPay then stores the key locally so every `wapupay_*` tool works.
  **Requires a prior `aqua_login`.** The raw key is never
  returned (masked preview only). The backend issues a **fresh key on every call
  and invalidates the previous one** (no grace period, verified), so the tool
  **only calls the backend when no key is configured yet** — if one already exists
  (env var or stored) it is a no-op (`already_configured`), so it never invalidates
  a working key.

- **Env vars:** `WAPUPAY_BASE_URL` (default `https://be-prod.wapu.app`; override
  for staging, e.g. `https://be-stage.wapu.app`), `WAPUPAY_API_KEY` (used for
  business calls if set), and `ANKARA_API_URL` (default `https://ankara.aquabtc.com`,
  shared with Lightning; used by the `aqua_*` login **and** the WapuPay
  provisioning endpoint — same backend, staging `https://test.aquabtc.com`).
- **Dark-launched OFF.** All 13 `aqua_*` / `wapupay_*` tools ship
  disabled-by-default (`features._SHIPPED_DISABLED`); opt in via
  `~/.aqua/config.json` `enabled_tools` (business calls also need a key — env or
  provisioned).
- **Rail pinned** to Liquid USDT; WapuPay rejects any other funding rail (400).
- The AQUA session (JWT) persists at `~/.aqua/jan3/session.json`; the provisioned
  API key and order records persist under `~/.aqua/wapupay/` — all at `0o600`.
  Bank PII + tokens + API key are never logged (see `ankara._redact` /
  `_SENSITIVE_LOG_FIELDS`, which includes `token`).
