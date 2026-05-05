#!/usr/bin/env python3
"""CLI smoke test: Send real L-BTC on Liquid mainnet.

Mirrors prompt_test_send_real_lbtc.md using the aqua-cli CLI.

Runs TWO suites end-to-end:
  1. no-password     — mnemonic stored in plaintext on disk.
  2. with-password   — AQUA_PASSWORD encrypts the mnemonic at rest
                       and is read again when signing the send.

Requirements:
    SIGNER_MNEMONIC      - BIP39 mnemonic for the test wallet
    LIQUID_DEST_ADDRESS  - Liquid address to send to
    (Wallet must hold > 2 * SEND_AMOUNT sats of L-BTC because each suite sends once.)

Optional:
    AQUA_PASSWORD        - password used by suite 2 (default: "test").
                           Same env var the CLI reads in production.

Usage:
    uv run python scripts/cli_smoke_send_lbtc.py
"""

import json
import os
import sys
import time

from click.testing import CliRunner

from aqua.cli.main import cli

MNEMONIC = os.getenv("SIGNER_MNEMONIC")
DEST_ADDRESS = os.getenv("LIQUID_DEST_ADDRESS")
SMOKE_PASSWORD = os.getenv("AQUA_PASSWORD", "test")
SEND_AMOUNT = 500  # sats
USDT_SEND_SATS = 50_000_000  # 0.5 USDt (precision 8)

if not MNEMONIC or not DEST_ADDRESS:
    print("ERROR: Set SIGNER_MNEMONIC and LIQUID_DEST_ADDRESS in .env")
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
    """Run the full L-BTC import → send → status → history flow.

    When `password` is given, the wallet is imported with AQUA_PASSWORD (encrypted
    at rest) and the same env var is passed on send so the signer can decrypt.
    """
    wallet = f"lbtc_smoke_{'enc' if password else 'plain'}_{int(time.time())}"
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

        balance = run_cli("liquid", "balance", "--wallet-name", wallet)
        assert balance is not None, "Balance check failed"
        print(f"  Liquid balances: {json.dumps(balance['balances'], indent=2)}")

        lbtc_sats = 0
        for b in balance["balances"]:
            if b.get("ticker") == "L-BTC":
                lbtc_sats = b.get("amount_sats", b.get("value", 0))
        print(f"  L-BTC balance: {lbtc_sats} sats")
        assert lbtc_sats > SEND_AMOUNT, f"Insufficient L-BTC: {lbtc_sats} < {SEND_AMOUNT}"

    test(f"[{label}] 1. Import Wallet and Check L-BTC Balance", t_import_and_balance)

    state = {"txid": None}

    def t_send_lbtc():
        result = run_cli(
            "liquid",
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

    test(f"[{label}] 2. Send L-BTC", t_send_lbtc)

    def t_tx_status():
        assert state["txid"] is not None, "No txid from send test"
        result = None
        for attempt in range(5):
            result = run_cli("liquid", "tx-status", "--tx", state["txid"])
            if result is not None:
                break
            print(f"  Indexer not ready yet (attempt {attempt + 1}/5), retrying in 3s...")
            time.sleep(3)
        assert result is not None, "Status check failed after retries"
        print(f"  Status: {result.get('status')}")
        print(f"  Explorer: {result.get('explorer_url')}")
        assert result["txid"] == state["txid"]

    test(f"[{label}] 3. Check Transaction Status", t_tx_status)

    def t_updated_balance():
        balance = run_cli("liquid", "balance", "--wallet-name", wallet)
        assert balance is not None, "Balance check failed"
        for b in balance["balances"]:
            if b.get("ticker") == "L-BTC":
                print(f"  Updated L-BTC balance: {b.get('amount_sats', b.get('value', 0))} sats")

    test(f"[{label}] 4. Verify Updated Balance", t_updated_balance)

    def t_tx_history():
        result = run_cli("liquid", "transactions", "--wallet-name", wallet)
        assert result is not None, "Transactions failed"
        print(f"  Transaction count: {result['count']}")
        if result["transactions"]:
            latest = result["transactions"][0]
            print(f"  Latest txid: {latest.get('txid', 'N/A')}")

    test(f"[{label}] 5. View Transaction History", t_tx_history)

    def t_send_usdt_by_ticker():
        """Send 0.5 USDt to a fresh self-address using --asset-ticker."""
        balance = run_cli("liquid", "balance", "--wallet-name", wallet)
        assert balance is not None, "Balance check failed"
        usdt_sats = 0
        for b in balance["balances"]:
            if b.get("ticker", "").lower() == "usdt":
                usdt_sats = b.get("amount_sats", b.get("value", 0))
        print(f"  USDt balance: {usdt_sats} sats")
        if usdt_sats < USDT_SEND_SATS:
            print(f"  SKIP: wallet holds {usdt_sats} sats USDt, needs >= {USDT_SEND_SATS}")
            return

        self_addr = run_cli("liquid", "address", "--wallet-name", wallet)
        assert self_addr is not None, "Could not derive receive address"
        dest = self_addr["address"]
        print(f"  Self-send destination: {dest}")

        result = run_cli(
            "liquid",
            "send-asset",
            "--wallet-name",
            wallet,
            "--address",
            dest,
            "--amount",
            str(USDT_SEND_SATS),
            "--asset-ticker",
            "usdt",
            env_extra=env,  # AQUA_PASSWORD needed here when wallet is encrypted
        )
        assert result is not None, "send-asset via --asset-ticker failed"
        assert "txid" in result, f"No txid in response: {result}"
        assert len(result["txid"]) == 64, f"Invalid txid length: {len(result['txid'])}"
        print(f"  Sent 0.5 USDt, TXID: {result['txid']}")

    test(f"[{label}] 6. Send 0.5 USDt by ticker", t_send_usdt_by_ticker)

    print(f"\n--- Cleanup suite '{label}' ---")
    run_cli("wallet", "delete", "--wallet-name", wallet, "--yes")
    print(f"  Wallet '{wallet}' deleted")


run_suite("no-password", None)
run_suite("with-password (AQUA_PASSWORD)", SMOKE_PASSWORD)


print(f"\n{'=' * 60}")
print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed}")
print(f"{'=' * 60}")
sys.exit(1 if failed > 0 else 0)
