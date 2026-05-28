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
                                                  changelly / pix  (third-party clients)
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
