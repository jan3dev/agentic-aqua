#!/usr/bin/env python3
"""CLI smoke test: Send real BTC on Bitcoin mainnet.

Mirrors prompt_test_send_real_btc.md using the aqua-cli CLI.

Runs TWO suites end-to-end:
  1. no-password     — mnemonic stored in plaintext on disk.
  2. with-password   — AQUA_PASSWORD encrypts the mnemonic at rest
                       and is read again when signing the send.

Requirements:
    SIGNER_MNEMONIC  - BIP39 mnemonic for the test wallet
    BTC_DEST_ADDRESS - Bitcoin address to send to
    (Wallet must hold > 2 * SEND_AMOUNT sats because each suite sends once.)

Optional:
    AQUA_PASSWORD    - password used by suite 2 (default: "test").
                       Same env var the CLI reads in production.

Usage:
    uv run python scripts/cli_smoke_send_btc.py
"""

import json
import os
import sys
import time

from click.testing import CliRunner

from aqua_mcp.cli.main import cli

MNEMONIC = os.getenv("SIGNER_MNEMONIC")
DEST_ADDRESS = os.getenv("BTC_DEST_ADDRESS")
SMOKE_PASSWORD = os.getenv("AQUA_PASSWORD", "test")
SEND_AMOUNT = 1000  # sats

if not MNEMONIC or not DEST_ADDRESS:
    print("ERROR: Set SIGNER_MNEMONIC and BTC_DEST_ADDRESS in .env")
    sys.exit(1)

runner = CliRunner()
passed = 0
failed = 0


def run_cli(*args, env_extra=None):
    """Invoke CLI and return parsed JSON, or None on failure."""
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    result = runner.invoke(cli, ["--format", "json", *args], env=env)
    if result.exit_code != 0:
        print(f"  FAIL (exit {result.exit_code}): {result.output}")
        return None
    return json.loads(result.output)


def test(name, fn):
    global passed, failed
    print(f"\n{'=' * 60}")
    print(f"TEST: {name}")
    print(f"{'=' * 60}")
    try:
        fn()
        passed += 1
        print("  PASS")
    except Exception as e:
        failed += 1
        print(f"  FAIL: {e}")


def run_suite(label: str, password: str | None):
    """Run the full import → send → balance → history flow.

    When `password` is given, the wallet is imported with AQUA_PASSWORD (encrypted
    at rest) and the same env var is passed on send so the signer can decrypt.
    """
    wallet = f"btc_smoke_{'enc' if password else 'plain'}_{int(time.time())}"
    env = {"AQUA_MNEMONIC": MNEMONIC}
    if password:
        env["AQUA_PASSWORD"] = password

    print(f"\n{'#' * 60}")
    print(f"# SUITE: {label}  (wallet={wallet})")
    print(f"{'#' * 60}")

    def t_import_and_balance():
        result = run_cli("wallet", "import-mnemonic", "--wallet-name", wallet, env_extra=env)
        assert result is not None, "Import failed"
        assert result["wallet_name"] == wallet
        print(f"  Wallet '{wallet}' imported ({'encrypted' if password else 'plaintext'})")

        balance = run_cli("btc", "balance", "--wallet-name", wallet)
        assert balance is not None, "Balance check failed"
        print(f"  BTC balance: {balance['balance_sats']} sats ({balance['balance_btc']} BTC)")
        assert balance["balance_sats"] > SEND_AMOUNT, (
            f"Insufficient BTC: {balance['balance_sats']} < {SEND_AMOUNT}"
        )

    test(f"[{label}] 1. Import Wallet and Check BTC Balance", t_import_and_balance)

    state = {"txid": None}

    def t_send_btc():
        result = run_cli(
            "btc",
            "send",
            "--wallet-name",
            wallet,
            "--address",
            DEST_ADDRESS,
            "--amount",
            str(SEND_AMOUNT),
            env_extra=env,  # AQUA_PASSWORD needed here when wallet is encrypted
        )
        assert result is not None, "Send failed"
        state["txid"] = result["txid"]
        print(f"  Sent {SEND_AMOUNT} sats to {DEST_ADDRESS}")
        print(f"  TXID: {state['txid']}")
        assert len(state["txid"]) == 64, f"Invalid txid length: {len(state['txid'])}"

    test(f"[{label}] 2. Send BTC On-Chain", t_send_btc)

    def t_updated_balance():
        balance = run_cli("btc", "balance", "--wallet-name", wallet)
        assert balance is not None, "Balance check failed"
        print(
            f"  Updated BTC balance: {balance['balance_sats']} sats "
            f"({balance['balance_btc']} BTC)"
        )

    test(f"[{label}] 3. Verify Updated Balance", t_updated_balance)

    def t_tx_history():
        result = run_cli("btc", "transactions", "--wallet-name", wallet)
        assert result is not None, "Transactions failed"
        print(f"  Transaction count: {result['count']}")
        if result["transactions"]:
            latest = result["transactions"][0]
            print(f"  Latest txid: {latest.get('txid', 'N/A')}")

    test(f"[{label}] 4. View Transaction History", t_tx_history)

    print(f"\n--- Cleanup suite '{label}' ---")
    run_cli("wallet", "delete", "--wallet-name", wallet, "--yes")
    print(f"  Wallet '{wallet}' deleted")


run_suite("no-password", None)
run_suite("with-password (AQUA_PASSWORD)", SMOKE_PASSWORD)


print(f"\n{'=' * 60}")
print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed}")
print(f"{'=' * 60}")
sys.exit(1 if failed > 0 else 0)
