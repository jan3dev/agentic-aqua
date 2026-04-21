#!/usr/bin/env python3
"""CLI smoke test: Send real BTC on Bitcoin mainnet.

Mirrors prompt_test_send_real_btc.md using the aqua-cli CLI.

Requirements:
    SIGNER_MNEMONIC  - BIP39 mnemonic for the test wallet
    BTC_DEST_ADDRESS - Bitcoin address to send to

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
SEND_AMOUNT = 1000  # sats

if not MNEMONIC or not DEST_ADDRESS:
    print("ERROR: Set SIGNER_MNEMONIC and BTC_DEST_ADDRESS in .env")
    sys.exit(1)

runner = CliRunner()
WALLET = f"btc_smoke_{int(time.time())}"
passed = 0
failed = 0


def run_cli(*args):
    """Invoke CLI and return parsed JSON, or exit on failure."""
    result = runner.invoke(cli, ["--format", "json", *args])
    if result.exit_code != 0:
        print(f"  FAIL (exit {result.exit_code}): {result.output}")
        return None
    return json.loads(result.output)


def test(name, fn):
    global passed, failed
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    try:
        fn()
        passed += 1
        print(f"  PASS")
    except Exception as e:
        failed += 1
        print(f"  FAIL: {e}")


# ---------------------------------------------------------------------------
# 1. Import Wallet and Check Balance
# ---------------------------------------------------------------------------

def test_import_and_balance():
    result = run_cli(
        "wallet", "import-mnemonic",
        "--mnemonic", MNEMONIC,
        "--wallet-name", WALLET,
    )
    assert result is not None, "Import failed"
    assert result["wallet_name"] == WALLET
    print(f"  Wallet '{WALLET}' imported")

    balance = run_cli("btc", "balance", "--wallet-name", WALLET)
    assert balance is not None, "Balance check failed"
    print(f"  BTC balance: {balance['balance_sats']} sats ({balance['balance_btc']} BTC)")
    assert balance["balance_sats"] > SEND_AMOUNT, (
        f"Insufficient BTC: {balance['balance_sats']} < {SEND_AMOUNT}"
    )

test("1. Import Wallet and Check BTC Balance", test_import_and_balance)

# ---------------------------------------------------------------------------
# 2. Send BTC On-Chain
# ---------------------------------------------------------------------------

txid = None

def test_send_btc():
    global txid
    result = run_cli(
        "btc", "send",
        "--wallet-name", WALLET,
        "--address", DEST_ADDRESS,
        "--amount", str(SEND_AMOUNT),
    )
    assert result is not None, "Send failed"
    txid = result["txid"]
    print(f"  Sent {SEND_AMOUNT} sats to {DEST_ADDRESS}")
    print(f"  TXID: {txid}")
    assert len(txid) == 64, f"Invalid txid length: {len(txid)}"

test("2. Send BTC On-Chain", test_send_btc)

# ---------------------------------------------------------------------------
# 3. Verify Updated Balance
# ---------------------------------------------------------------------------

def test_updated_balance():
    balance = run_cli("btc", "balance", "--wallet-name", WALLET)
    assert balance is not None, "Balance check failed"
    print(f"  Updated BTC balance: {balance['balance_sats']} sats ({balance['balance_btc']} BTC)")

test("3. Verify Updated Balance", test_updated_balance)

# ---------------------------------------------------------------------------
# 4. View Transaction History
# ---------------------------------------------------------------------------

def test_tx_history():
    result = run_cli("btc", "transactions", "--wallet-name", WALLET)
    assert result is not None, "Transactions failed"
    print(f"  Transaction count: {result['count']}")
    if result["transactions"]:
        latest = result["transactions"][0]
        print(f"  Latest txid: {latest.get('txid', 'N/A')}")

test("4. View Transaction History", test_tx_history)

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

print(f"\n{'='*60}")
print("CLEANUP: Deleting smoke wallet")
run_cli("wallet", "delete", "--wallet-name", WALLET, "--yes")
print(f"  Wallet '{WALLET}' deleted")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n{'='*60}")
print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed}")
print(f"{'='*60}")
sys.exit(1 if failed > 0 else 0)
