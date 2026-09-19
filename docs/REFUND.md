# Refunding a failed Lightning send

A Lightning send (`lightning_send` / `aqua lightning send`) is a Boltz v2
submarine swap: AQUA locks L-BTC into a taproot output, the provider pays the
invoice off that lockup, and the provider claims the locked coins. When the
invoice cannot be paid — no route, a destination node that rejects the payment —
the swap ends in `invoice.failedToPay` and **the L-BTC stays locked on chain**.
`lightning_refund` is what gets it back.

```bash
aqua lightning refund --swap-id <id>                 # back to the swap's own wallet
aqua lightning refund --swap-id <id> --dry-run       # build and sign, do not broadcast
```

`lightning_transaction_status` reports `refund_info.refundable` for swaps that
are still waiting for one.

## The two paths

The lockup output's taproot tree has a key path and two leaves:

| Path | When | What signs it |
|------|------|---------------|
| **Cooperative** (key path) | Immediately, while the provider cosigns | MuSig2 over `[claimPublicKey, refundPublicKey]` |
| **Unilateral** (script path) | Only after the timeout block | The refund key alone, against the refund leaf |

`lightning_refund` tries the cooperative path first. It is the cheap one: a
key-path spend is a single 64-byte signature, so the transaction is small and
nothing depends on waiting. The provider can switch cooperative refunds off (it
answers HTTP 400), and then the only option is the unilateral path — which the
refund leaf's `OP_CHECKLOCKTIMEVERIFY` gates until the chain reaches the swap's
`timeoutBlockHeight`. Until that block, a refused cooperative refund means
waiting; the error names the block and how far away it is (Liquid produces
roughly one block per minute).

## What the swap record must contain

A refund is reconstructed entirely from the swap's own data — there is no
server-side recovery. `~/.aqua/lightning_swaps/<id>.json` needs:

| Field | Where it comes from | Why |
|-------|--------------------|-----|
| `refund_private_key` | generated locally at swap creation | signs the refund |
| `timeout_block_height` | provider | in the refund leaf, and gates the unilateral path |
| `claim_public_key` | provider | in the claim leaf and in the MuSig2 aggregate |
| `blinding_key` | provider | unblinds the confidential lockup output |
| `lockup_txid` | the lockup broadcast | locates the output |

`invoice` supplies the payment hash, which the claim leaf commits to. The
`swap_tree` the provider returns is stored verbatim for auditing, but the tree
is always rebuilt from those primitives rather than trusted as given — that is
what makes swaps created before these fields were persisted recoverable at all.

**Legacy swaps.** Records written before this feature landed hold only the
refund key and the timeout. Ask the provider for the swap's `claimPublicKey` and
blinding key and pass them explicitly:

```bash
aqua lightning refund --swap-id <id> \
  --claim-public-key <hex> --blinding-key <hex>
```

Nothing has to be trusted about those values: the tree they produce is compared
against the scriptPubKey the lockup output actually carries on chain, and a
mismatch aborts before anything is signed.

**Losing the file loses the coins.** The refund key is random, not derived from
the wallet's seed, so restoring the mnemonic elsewhere does not recover a stuck
lockup. `~/.aqua/lightning_swaps/*.json` is the only copy, and it stores that
key and the blinding key in the clear at `0o600`. Encrypting them at rest is not
implemented.

## Implementation notes

`src/aqua/boltz_refund.py` builds, signs and broadcasts the transaction.

* **Swap tree.** Liquid uses tapleaf version `0xc4` and the `TapLeaf/elements`,
  `TapBranch/elements` and `TapTweak/elements` tagged-hash domains. The MuSig2
  aggregate takes the keys in the fixed order `[claim, refund]` — the protocol
  never key-sorts them, and the reverse order yields a different address.
* **MuSig2** comes from `src/aqua/_bip327.py`, the BIP-327 reference
  implementation vendored under BSD-3-Clause. It is not constant-time, which is
  acceptable only because refund keys are ephemeral and per-swap.
* **Sighash.** libwally computes the Elements key-path sighash, but hardcodes
  Bitcoin's tapleaf version inside its own script-path variant, so the script
  path needs the hand-rolled `elements_taproot_sighash`. A test pins its
  key-path output against libwally so the two cannot drift.
* **Outputs.** The lockup is confidential, so the destination output is blinded
  too (the blinding factors have to balance) and the fee is an explicit Elements
  output. The fee uses the discounted vsize Liquid applies to confidential
  transactions, and is capped — a refund that wants an implausible fee is a bug,
  and providers reject overpaying transactions.
* **Guards before broadcasting.** The reconstructed scriptPubKey must match the
  lockup output; the unblinded asset must be L-BTC and the value must match the
  swap; the provider's partial signature is verified before aggregation; and the
  aggregate signature is verified against the tweaked output key.
