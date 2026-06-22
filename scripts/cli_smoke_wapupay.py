#!/usr/bin/env python3
"""CLI smoke script: WapuPay direct-fiat integration.

Runs the same aqua CLI as `uv run aqua wapupay …`.

Optional env vars:
    WAPUPAY_BASE_URL        override base URL (default: https://be-stage.wapu.app)
    WAPUPAY_API_KEY         override stored API key

Usage:
    uv run python scripts/cli_smoke_wapupay.py
    WAPUPAY_BASE_URL=https://be-stage.wapu.app uv run python scripts/cli_smoke_wapupay.py
"""

import json
import os
import re
import sys
import time

# Load the API key into os.environ BEFORE importing any aqua module.
# Importing aqua.wapupay first (even with a lazy key read) interacts badly
# with CliRunner and causes 401s; key-first ordering avoids it.
_KEY_FILE = os.path.expanduser("~/.aqua/wapupay/api_key.json")
if not os.environ.get("WAPUPAY_API_KEY") and os.path.isfile(_KEY_FILE):
    with open(_KEY_FILE) as _f:
        _stored = json.load(_f)
    if _stored.get("token"):
        os.environ["WAPUPAY_API_KEY"] = _stored["token"]

if not os.environ.get("WAPUPAY_API_KEY"):
    print("ERROR: WAPUPAY_API_KEY not set and no key found in ~/.aqua/wapupay/api_key.json")
    sys.exit(1)

from aqua.cli.main import cli  # noqa: E402 — must come after env setup
from click.testing import CliRunner

WAPUPAY_TEST_ALIAS = "test.alias.mp"
WAPUPAY_TEST_AMOUNT_ARS = "10000"
WAPUPAY_TEST_RECEIVER_NAME = "Test Receiver"

_runner = CliRunner()

_TRANSIENT = re.compile(r"unreachable|failed \(5\d\d ", re.IGNORECASE)
_ATTEMPTS = 4
_RETRY_DELAY_S = 3

passed = 0
failed = 0


def run_cli(*args):
    """Invoke the aqua CLI with the real OS environment and return parsed JSON."""
    last = ""
    for attempt in range(_ATTEMPTS):
        result = _runner.invoke(cli, ["--format", "json", *args], env={**os.environ})
        if result.exit_code == 0:
            data = json.loads(result.stdout)
            if "error" in data:
                print(f"  FAIL (tool error): {data}")
                return None
            return data
        last = f"{result.stdout!r} {result.stderr!r}"
        if not _TRANSIENT.search(last):
            break
        if attempt < _ATTEMPTS - 1:
            print(f"  Transient error, retrying ({attempt + 1}/{_ATTEMPTS})…")
            time.sleep(_RETRY_DELAY_S)

    if _TRANSIENT.search(last):
        print(f"  SKIP: WapuPay staging unreachable after retries. Last: {last[:400]}")
        return None
    print(f"  FAIL (exit non-zero): {last}")
    return None


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


def _is_staging() -> bool:
    url = os.environ.get("WAPUPAY_BASE_URL", "https://be-stage.wapu.app")
    return "stage" in url.lower()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def t_rates():
    result = run_cli("wapupay", "rates")
    assert result is not None, "rates call failed"
    assert isinstance(result, dict) and result, f"unexpected payload: {result}"
    for entry in result.get("rates", []):
        print(f"  Buy: {entry.get('buy')}  Sell: {entry.get('sell')}  Pair: {entry.get('pair')}")


def t_quote():
    result = run_cli("wapupay", "quote", "--amount-ars", WAPUPAY_TEST_AMOUNT_ARS)
    assert result is not None, "quote call failed"
    assert any(k in result for k in ("usdt_amount", "total_amount", "exchange_rate")), (
        f"unexpected quote payload: {result}"
    )
    print(f"  quote: {json.dumps(result, indent=2)}")


def t_quote_with_alias():
    if not WAPUPAY_TEST_ALIAS:
        print("  SKIP: WAPUPAY_TEST_ALIAS not set")
        return
    result = run_cli(
        "wapupay", "quote",
        "--amount-ars", WAPUPAY_TEST_AMOUNT_ARS,
        "--alias", WAPUPAY_TEST_ALIAS,
    )
    assert result is not None, "quote-with-alias call failed"
    assert "valid_cbu_alias" in result, f"missing valid_cbu_alias in: {result}"
    print(f"  valid_cbu_alias: {result['valid_cbu_alias']}")


def t_spending_limit():
    result = run_cli("wapupay", "spending-limit")
    assert result is not None, "spending-limit call failed"
    assert isinstance(result, dict) and result, f"unexpected payload: {result}"
    print(f"  spending limit: {json.dumps(result, indent=2)}")


def t_transactions():
    result = run_cli("wapupay", "transactions")
    assert result is not None, "transactions call failed"
    assert "transactions" in result, f"missing 'transactions' key: {result}"
    assert isinstance(result["transactions"], list)
    print(f"  transaction count: {len(result['transactions'])}")


def t_orders():
    result = run_cli("wapupay", "orders")
    assert result is not None, "orders call failed"
    assert "orders" in result, f"missing 'orders' key: {result}"
    assert isinstance(result["orders"], list)
    print(f"  order count: {len(result['orders'])}")


_tentative_id = None


def t_create_order():
    global _tentative_id
    if not _is_staging():
        base_url = os.environ.get("WAPUPAY_BASE_URL", "https://be-stage.wapu.app")
        print(
            f"  SKIP: WAPUPAY_BASE_URL is not staging ({base_url})"
            " — refusing to create an order outside staging"
        )
        return
    if not WAPUPAY_TEST_ALIAS:
        print("  SKIP: WAPUPAY_TEST_ALIAS not set")
        return

    args = [
        "wapupay", "create-order",
        "--amount-ars", WAPUPAY_TEST_AMOUNT_ARS,
        "--alias", WAPUPAY_TEST_ALIAS,
        "--yes",
    ]
    if WAPUPAY_TEST_RECEIVER_NAME:
        args += ["--receiver-name", WAPUPAY_TEST_RECEIVER_NAME]

    result = run_cli(*args)
    assert result is not None, "create-order call failed"
    assert "tentative_id" in result, f"missing tentative_id in: {result}"
    if result.get("funded"):
        assert result["address_destination"].startswith(("lq1", "ex1", "VJL")), (
            f"unexpected Liquid address: {result['address_destination']}"
        )
    _tentative_id = result["tentative_id"]
    print(f"  tentative_id: {_tentative_id}")
    print("  (order created but NOT funded — no USDT deposit sent)")


def t_order_status():
    if _tentative_id is None:
        print("  SKIP: no tentative_id from create-order")
        return
    result = run_cli("wapupay", "order-status", "--tentative-id", _tentative_id)
    assert result is not None, "order-status call failed"
    assert "status" in result, f"missing 'status' key: {result}"
    print(f"  status: {result['status']}")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

test("1. Exchange rates (public)", t_rates)
test("2. Quote", t_quote)
test("3. Quote with alias", t_quote_with_alias)
test("4. Spending limit", t_spending_limit)
test("5. Transactions", t_transactions)
test("6. Orders (local)", t_orders)
test("7. Create order (staging only)", t_create_order)
test("8. Order status", t_order_status)

print(f"\n{'=' * 60}")
print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed}")
print(f"{'=' * 60}")
sys.exit(1 if failed > 0 else 0)
