# Release notes

Drafts for the next GitHub Release. Releases are published by tagging `main`
(see [PUBLISHING.md](PUBLISHING.md)); copy the relevant section into the
release body when cutting the tag.

## Unreleased — Indra becomes the default Lightning swap provider

L-BTC → Lightning payments now go through **Indra**
(`https://indra.aquabtc.com`), AQUA's Boltz-compatible swap service. Boltz stays
available as a one-line config switch.

### Breaking changes

**1. `lightning_transaction_status` renames `boltz_status` → `provider_status`.**
Send-swap results also gain a `provider` field (`"indra"` or `"boltz"`). A client
that reads `boltz_status` gets nothing back — the key is absent, not an error, so
the failure is silent. Update any integration that consumes it.

**2. Send limits drop from 100 – 25,000,000 sats to 1,000 – 100,000 sats.**
Those are Indra's limits for the live `L-BTC → BTC` pair. A payment that used to
succeed at, say, 5,000,000 sats is now rejected before any HTTP call. Restore the
old ceiling by setting `"lightning_provider": "boltz"` in `~/.aqua/config.json`
(or `AQUA_LIGHTNING_PROVIDER=boltz` for one run). Indra is **mainnet only**;
testnet requires `boltz`.

**3. `lightning_receive` now ships disabled.** The tool disappears from the MCP
listing and `aqua lightning receive` is no longer registered. Re-enable it with:

```json
{ "enabled_tools": { "lightning_receive": true } }
```

Note for anyone who already had it on: a previous `aqua doctor --fix` run pruned
`"lightning_receive": true` from `config.json` because it matched the shipped
default at the time. Those users are silently downgraded to disabled on upgrade
and must add the key back by hand.

### Compatibility

- Swaps already on disk are always queried against the provider they were created
  with, so open Boltz swaps keep resolving against Boltz after the switch.
- The at-rest wallet format is unchanged; `lightning_provider` is a new optional
  key in `config.json` that older releases ignore.
