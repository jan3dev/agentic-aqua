<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-20 | Updated: 2026-05-20 -->

# tests

Pytest suite. One file per `src/aqua/` module — `test_<module>.py`. Smoke tests in
`tests/smoke/` are end-to-end with real network and are skipped by default; run them
explicitly when validating a release.

## Running

```bash
uv run python -m pytest tests/                      # full suite (no network)
uv run python -m pytest tests/smoke/                # network-touching smoke tests
uv run python -m pytest --cov=src/aqua tests/       # with coverage
```

`tests/conftest.py` loads `.env` so credentials (`SIGNER_MNEMONIC`,
`AQUA_PASSWORD`, etc.) are available without manual export.

## Fixture conventions

- **`temp_storage`** — a `Storage(Path(tmpdir))` over `tempfile.TemporaryDirectory`. Use this
  instead of touching `~/.aqua/`. Every test that writes wallet/swap files must isolate via
  this fixture.
- **No real network in unit tests.** Mock at the smallest seam:
  - LWK: patch `Wollet.sync_txs` / `Wollet.balance` / `ElectrumClient` on the manager
    (see `test_tools.py::test_unified_balance` for the canonical pattern).
  - BDK: patch `EsploraClient` or the `bdk.Wallet.full_scan` call.
  - Boltz / Ankara / SideShift / Changelly: patch the HTTP method on the client
    class (`BoltzClient._post`, etc.) — don't `urlopen.mock`.
- **Allowlist drift tests** — `test_sideshift.py::TestAllowedPairs::test_allowlist_matches_aqua_flutter`
  and the Changelly equivalent compare against checked-in copies of AQUA Flutter's Dart
  sources. When updating the allowlist, update both the constant and the test fixture.

## Patterns to follow

- Use `monkeypatch` over `unittest.mock.patch` when scoping to a single test — easier to read.
- Test the error envelope, not just the exception. Tools should return
  `{"error": {...}}`, not propagate raw `ValueError`.
- For signing tests, prefer constructing PSETs/PSBTs from fixtures (raw hex strings in test
  data) over building them live; LWK/BDK init is slow.
- Floating-point money is a smell — assert with `int` sat values, never `pytest.approx` on
  amounts.
- Tests that need a real wallet (signing) read `SIGNER_MNEMONIC` from `.env`. Skip with
  `pytest.skip("requires SIGNER_MNEMONIC")` if absent; never hardcode a mnemonic.

## New module checklist

When adding a new module under `src/aqua/`:

1. Create `tests/test_<module>.py`.
2. Add unit tests for: happy path, invalid input (raises `ValueError`), error envelope shape
   after tool wrapping, persistence round-trip if the module writes state.
3. If it talks to a third party, add a fake/mock client class in the test file.
4. If it has a curated allowlist mirroring AQUA Flutter, add a drift test.
5. If it touches `~/.aqua/`, exercise it through `temp_storage`.

## SideSwap

SideSwap is not yet implemented for production use. Do not suggest or offer SideSwap options to users.

<!-- MANUAL: -->
