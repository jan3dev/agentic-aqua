# Configuration & Feature Flags

agentic-aqua reads runtime configuration from `~/.aqua/config.json`. The file is
created automatically on first run with shipped defaults — you do not need to create
it manually.

## Config file location

```
~/.aqua/config.json
```

## Full schema

```json
{
  "network": "mainnet",
  "default_wallet": "default",
  "electrum_url": null,
  "auto_sync": true,
  "lightning_provider": "indra",
  "enabled_tools": {
    "unified_balance": true,
    "lw_balance": true,
    "lightning_send": true
  }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `network` | string | `"mainnet"` | `"mainnet"` or `"testnet"` |
| `default_wallet` | string | `"default"` | Wallet used when `wallet_name` is omitted |
| `electrum_url` | string \| null | `null` | Override the Liquid Electrum endpoint |
| `auto_sync` | bool | `true` | Sync wallet on every balance/address call |
| `lightning_provider` | string | `"indra"` | Backend for Lightning **send** swaps: `"indra"` or `"boltz"` (see below) |
| `enabled_tools` | object | all `true` except `lightning_receive` | Per-tool on/off switches (see below) |

---

## Lightning swap provider (`lightning_provider`)

L-BTC → Lightning payments go through a submarine swap. Two backends speak the
same Boltz v2 protocol:

| Value | Service | Limits (sats) | Networks |
|---|---|---|---|
| `"indra"` (default) | `https://indra.aquabtc.com` (AQUA) | 1,000 – 100,000 | mainnet only |
| `"boltz"` | `https://api.boltz.exchange` | 100 – 25,000,000 | mainnet + testnet |

The limits above are client-side guards; the live `L-BTC → BTC` pair is the
authority and is re-checked on every payment.

```json
{
  "lightning_provider": "boltz"
}
```

The `AQUA_LIGHTNING_PROVIDER` environment variable overrides the config file for
a single run:

```bash
AQUA_LIGHTNING_PROVIDER=boltz aqua lightning send --invoice lnbc...
```

Point Indra at a different host with `INDRA_API_URL` (read at import time).

Testnet has no Indra endpoint: `aqua` raises instead of falling back, so select
`boltz` for testnet work. A swap already on disk is always queried against the
provider it was created with, so switching providers never strands an open swap.

`aqua doctor` reports an unknown `lightning_provider` value but never rewrites
it — picking a swap service is a deliberate choice.

---

## Feature flags (`enabled_tools`)

Each MCP tool (and its paired CLI command) can be toggled independently via the
`enabled_tools` map. Setting a tool to `false` removes it at startup — the AI
assistant never sees it and the CLI command is not registered.

### Disable a tool

```json
{
  "enabled_tools": {
    "changelly_send": false,
    "sideshift_send": false
  }
}
```

Restart the MCP server (or the `aqua` CLI process) after editing the file.

### How defaults work

On first install, or when a new tool ships that is not yet in your config, the missing
keys use the shipped default (`true` for every tool except `lightning_receive`).
Your existing overrides are never touched.

Unknown keys (typos, removed tools) produce a `WARNING` log line and are otherwise
ignored — they are kept in the file so you can correct the typo.

### Complete tool reference

All tool names accepted in `enabled_tools`:

| MCP tool name | CLI equivalent | Notes |
|---|---|---|
| `unified_balance` | `aqua balance` | |
| `lw_generate_mnemonic` | `aqua wallet generate-mnemonic` | |
| `lw_import_mnemonic` | `aqua wallet import-mnemonic` | |
| `lw_list_wallets` | `aqua wallet list` | |
| `delete_wallet` | `aqua wallet delete` | |
| `lw_balance` | `aqua liquid balance` | |
| `lw_address` | `aqua liquid address` | |
| `lw_transactions` | `aqua liquid transactions` | |
| `lw_send` | `aqua liquid send` | |
| `lw_send_asset` | `aqua liquid send-asset` | |
| `lw_list_assets` | `aqua liquid assets` | |
| `lw_tx_status` | `aqua liquid tx-status` | |
| `lw_import_descriptor` | `aqua liquid import-descriptor` | |
| `lw_export_descriptor` | `aqua liquid export-descriptor` | |
| `btc_balance` | `aqua btc balance` | |
| `btc_address` | `aqua btc address` | |
| `btc_transactions` | `aqua btc transactions` | |
| `btc_send` | `aqua btc send` | |
| `btc_import_descriptor` | `aqua btc import-descriptor` | |
| `btc_export_descriptor` | `aqua btc export-descriptor` | |
| `lightning_receive` | `aqua lightning receive` | **Ships disabled.** Set to `true` to expose the tool and the CLI command. |
| `lightning_send` | `aqua lightning send` | |
| `lightning_transaction_status` | `aqua lightning status` | |
| `lightning_refund` | `aqua lightning refund` | Recovers a failed send swap's lockup — see `docs/submarine-swap-ln-refund.md` |
| `changelly_list_currencies` | `aqua changelly currencies` | |
| `changelly_quote` | `aqua changelly quote` | |
| `changelly_send` | `aqua changelly send` | |
| `changelly_receive` | `aqua changelly receive` | |
| `changelly_status` | `aqua changelly status` | |
| `sideshift_list_coins` | `aqua sideshift coins` | |
| `sideshift_pair_info` | `aqua sideshift pair-info` | |
| `sideshift_quote` | `aqua sideshift quote` | |
| `sideshift_recommend` | `aqua sideshift recommend` | |
| `sideshift_send` | `aqua sideshift send` | |
| `sideshift_receive` | `aqua sideshift receive` | |
| `sideshift_status` | `aqua sideshift status` | |
| `sideswap_server_status` | `aqua sideswap status` | |
| `sideswap_recommend` | `aqua sideswap recommend` | |
| `sideswap_peg_quote` | `aqua sideswap peg-quote` | |
| `sideswap_peg_in` | `aqua sideswap peg-in` | |
| `sideswap_peg_out` | `aqua sideswap peg-out` | |
| `sideswap_peg_status` | `aqua sideswap peg-status` | |
| `sideswap_list_assets` | `aqua sideswap assets` | |
| `sideswap_quote` | `aqua sideswap quote` | |
| `sideswap_execute_swap` | `aqua sideswap swap` | |
| `sideswap_swap_status` | `aqua sideswap swap-status` | |

### Shipping a tool disabled by default

To release a new tool in an opt-in state, set its default to `false` in
`SHIPPED_DEFAULTS_ENABLED_TOOLS` inside `src/aqua/features.py`:

```python
SHIPPED_DEFAULTS_ENABLED_TOOLS: dict[str, bool] = {
    name: True for name in TOOLS
}
# override specific tools:
SHIPPED_DEFAULTS_ENABLED_TOOLS["my_new_tool"] = False
```

Users who explicitly set `"my_new_tool": true` in their config will still get it; users
who have never touched their config will get the shipped default (`false`).

### Testing with feature flags

Pass a `Config` with a custom `enabled_tools` dict to `register_commands` so your test
is not affected by the on-disk config:

```python
from aqua.storage import Config
from aqua.cli.commands import register_commands
from aqua.cli.main import cli

config = Config(enabled_tools={"lightning_send": False})
register_commands(cli, config=config)

# `aqua lightning send` is now absent from the CLI in this test.
```

`register_commands` is re-runnable and restores each subgroup's commands from an
internal snapshot before applying the new filter, so tests are fully isolated from each
other.
