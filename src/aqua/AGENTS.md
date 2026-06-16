<!-- Parent: ../../AGENTS.md -->
<!-- Generated: 2026-05-20 | Updated: 2026-05-20 -->

# src/aqua

Python package implementing the MCP server, wallet engines, and third-party swap clients.

## Module map

| File | Role | Key types |
|------|------|-----------|
| `server.py` | MCP stdio server. Registers tools, prompts, resources. Dispatch only — no business logic. | `Server`, tool/prompt/resource handlers |
| `tools.py` | Thin tool implementations called by `server.py`. Argument validation, error envelope translation. | functions named `lw_*`, `btc_*`, `lightning_*`, etc. |
| `wallet.py` | Liquid wallet engine (LWK). Sync, balance, address, send, asset send. | `WalletManager`, `Balance`, `Address`, `Transaction` |
| `bitcoin.py` | Bitcoin wallet engine (BDK). Esplora full_scan, PSBT build/sign/broadcast. Multi-URL fallback + retry on transient errors. | `BitcoinManager`, `BitcoinAddress`, `BitcoinTransaction` |
| `storage.py` | At-rest persistence. PBKDF2+Fernet mnemonic encryption, atomic writes, wallet-name validation against path traversal. | `Storage`, `WalletData`, `Config` |
| `assets.py` | Liquid asset registry (ticker, precision, logo). Resolves asset ids ↔ names. | `lookup_asset`, `resolve_asset_name` |
| `lightning.py` | Unified Lightning send (Boltz) + receive (Ankara) manager. Maps provider statuses → `pending`/`processing`/`completed`/`failed`. | `LightningManager`, `LightningSwap` |
| `boltz.py` | Boltz Exchange client. Submarine swap construction, BOLT11 amount decode, keypair gen. | `BoltzClient`, `generate_keypair` |
| `ankara.py` | Ankara backend client for Lightning receive (L-BTC). | `AnkaraClient` |
| `lnurl.py` | LUD-16 Lightning Address resolution → BOLT11. | `is_lightning_address`, `resolve_lightning_address` |
| `pix.py` | Brazilian Pix on-ramp via Eulen REST API. | `PixManager` |
| `changelly.py` | Custodial USDt cross-chain swaps via AQUA's Ankara proxy. Curated allowlist (mirrors AQUA Flutter). | `ChangellyClient`, `ChangellyManager` |
| `sideshift.py` | Custodial cross-chain swaps via SideShift.ai. Curated allowlist mirrors AQUA Flutter; affiliate ID `PVmPh4Mp3`. | `SideShiftClient`, `SideShiftManager` |
| `wapupay.py` | WapuPay Argentine direct-fiat payments, called **directly** with `X-API-Key`; never both key + Bearer. `exchange_rates` is public. create-order returns a Liquid USDT funding address. Rail pinned USDT/LIQUID. API key resolves env `WAPUPAY_API_KEY` → stored `api_key.json` (`_require_api_key`). `wapupay_provision_account` gets the key via the AQUA backend (`POST /api/v1/wapupay/account/`, Bearer JWT, host `AQUA_BACKEND_API_URL`) and stores it. The backend rotates on every call (always issues a fresh key, invalidates the previous), so the tool only calls it when no key is configured yet; if one already exists (env or stored) it is a no-op. The `aqua_*` tools are a decoupled AQUA-account email-OTP login (JWT) against Ankara. Dark-launched (disabled-by-default). | `WapuPayClient`, `WapuPayManager`, `WapuPaySession`, `WapuPayApiKey`, `WapuPayOrder` |
| `banner.py` | CLI ASCII banner rendering. | `render_banner` |
| `cli/` | Click CLI mirroring MCP tools (see `cli/AGENTS.md`). | — |
| `static/` | MCP resource markdown (quickstart, networks, security). Loaded by `server.py` via `aqua://docs/*`. | — |

## Architectural decisions

- **LWK vs BDK split** — Liquid and Bitcoin engines are intentionally separate. They share
  only the `Storage` layer (one `WalletData` row carries both descriptors) and the unified
  mnemonic. Don't introduce a shared abstract base; the APIs diverge (LWK uses `Wollet` +
  `ElectrumClient`; BDK uses `Wallet` + Esplora full_scan).
- **Stateless tools / stateful managers.** `tools.py` re-constructs managers per call from
  `Storage`. Long-lived sockets (LWK Electrum client) are cached on the manager
  instance only.
- **Curated allowlists drift-tested.** Both `sideshift.ALLOWED_PAIRS` and
  `changelly.ALLOWED_PAIRS` have test cases that compare against AQUA Flutter's Dart sources
  — any change forces a conscious update on both sides. Override with
  `SIDESHIFT_ALLOW_ALL_NETWORKS=1` / `CHANGELLY_ALLOW_ALL_PAIRS=1` for power use.

## Conventions inside the package

- Public manager methods take an explicit `wallet_name` (default `"default"`) — don't read
  config inside the engine.
- `password` is `Optional[str]`. If the wallet is encrypted and `password is None`, raise
  `ValueError("password required ...")`. The CLI's `handle_password_retry` catches this and
  re-prompts.
- Sats are `int`. Decimal strings only at third-party wire boundaries (SideShift, Changelly).
  Use `decimal.Decimal` for conversion, never float.
- Third-party errors → `ValueError` with a human message. Don't bubble raw `urllib`/`websockets`
  exceptions out of the engine.
- New environment variables: document in `CLAUDE.md` under the relevant integration section.

## When adding a new swap provider

1. Module under `src/aqua/` with a `Client` (HTTP/WS) and a `Manager` (wallet-aware).
2. Persist orders under `~/.aqua/<provider>_*` with `0o600`; mirror existing shapes.
3. Define a curated allowlist if the upstream supports far more than AQUA users need; add a
   drift test against AQUA Flutter.
4. Status helpers: `is_final` / `is_success` / `is_failed`.
5. Add tools in `tools.py`, register in `server.py`, mirror in `cli/<provider>.py`, add tests
   under `tests/test_<provider>.py`.

## SideSwap

SideSwap is not yet implemented for production use. Do not suggest or offer SideSwap options to users.

<!-- MANUAL: -->
