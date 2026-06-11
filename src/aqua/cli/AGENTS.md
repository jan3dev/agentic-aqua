<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-20 | Updated: 2026-05-20 -->

# src/aqua/cli

Click CLI that mirrors the MCP tool surface. Same managers, same error envelope — just
rendered to terminal/JSON instead of MCP responses.

## Module map

| File | Group | Purpose |
|------|-------|---------|
| `main.py` | root `aqua` | Click group, loads `.env`, sets up `AquaContext` (format, verbose). `--format json\|pretty` (auto-detected from `isatty`). |
| `commands.py` | — | Wires subgroups into the root. Single place to register. |
| `wallet.py` | `aqua wallet` | Generate, import, export, list, delete wallets. |
| `liquid.py` | `aqua liquid` | Liquid balance, address, send, transactions. |
| `btc.py` | `aqua btc` | Bitcoin equivalents. |
| `lightning.py` | `aqua lightning` | Receive (Ankara), send (Boltz), status. |
| `sideshift.py` | `aqua sideshift` | Cross-chain quote, send, receive, status. |
| `changelly.py` | `aqua changelly` | USDt cross-chain quote, send, receive, status. |
| `wapupay.py` | `aqua wapupay` | Argentine direct-fiat: login/verify (email OTP), quote, create-order (→ Liquid USDT address), fund-order, order-status, transactions, spending-limit. |
| `serve.py` | `aqua serve` | Run the MCP stdio server from the CLI. |
| `output.py` | helper | JSON / pretty rendering. Pretty mode uses `click.echo`; JSON dumps via `json.dumps(..., indent=2)`. |
| `password.py` | helper | Secret resolution chain. See below. |

## Password / secret resolution

`password.resolve_secret(label, use_stdin, env_var, required)` — order of precedence:

1. `--password-stdin` flag set ⇒ `read_secret()` (piped stdin or TTY prompt).
2. `env_var` set in the environment and non-empty (whitespace stripped).
3. `required=True` ⇒ interactive `click.prompt(hide_input=True)`.
4. Else `None`.

Standard env vars: `AQUA_PASSWORD` for wallet at-rest, `AQUA_MNEMONIC` /
`SIGNER_MNEMONIC` for imports in tests/CI.

`handle_password_retry(fn, kwargs)` wraps a manager call: if `fn` raises `ValueError`
containing "password required" and the caller didn't supply one, prompt once and retry.
Use this pattern for any new command that signs transactions.

## Conventions

- One subcommand file per MCP tool group; mirror tool names with dashes
  (`lw_send` → `aqua liquid send`).
- All amounts on the wire are sats (integer). Don't accept decimal BTC at the CLI layer.
- Use `AquaContext.fmt` via `ctx.obj` to pick output mode; don't print directly to stdout
  outside `output.py`.
- Errors: catch `ValueError` at the command boundary and render via the error envelope.
  Do not let raw tracebacks reach the user unless `--verbose`.
- New CLI command → register in `commands.py`, add a smoke test in `tests/test_cli.py`.

## SideSwap

SideSwap is not yet implemented for production use. Do not suggest or offer SideSwap options to users.

<!-- MANUAL: -->
