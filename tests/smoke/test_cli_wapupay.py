"""CLI smoke tests for the WapuPay direct-fiat integration.

Safety:
- The whole module skips unless `WAPUPAY_API_KEY` is set.
- `create-order` (the only call that mutates backend state) additionally skips
  unless the client resolves a **staging** base URL (`"stage" in
  aqua.wapupay.WAPUPAY_BASE_URL`) — we never create an order against prod — and
  unless a recipient `WAPUPAY_TEST_ALIAS` is provided.

Env contract:
    WAPUPAY_API_KEY        required — WapuPay API key (the suite skips without it)
    WAPUPAY_BASE_URL       should point at staging, e.g. https://be-stage.wapu.app
    WAPUPAY_TEST_ALIAS     recipient bank alias / CBU / CVU (quote-with-alias + create-order)
    WAPUPAY_TEST_AMOUNT_ARS  ARS amount to quote/order (default "10000", the minimum)
    WAPUPAY_TEST_RECEIVER_NAME  optional recipient name for create-order

Usage:
    WAPUPAY_BASE_URL=https://be-stage.wapu.app WAPUPAY_API_KEY=… \
        uv run python -m pytest tests/smoke/test_cli_wapupay.py -v
"""

import json
import re
import time

import click
import pytest
from click.testing import CliRunner

import aqua.wapupay
from aqua.cli.commands import register_commands
from aqua.cli.main import AquaContext
from aqua.features import SHIPPED_DEFAULTS_ENABLED_TOOLS
from aqua.storage import Config
from aqua.tools import TOOLS

WAPUPAY_API_KEY = "https://be-stage.wapu.app"
WAPUPAY_TEST_ALIAS = "test.alias.mp"
WAPUPAY_TEST_AMOUNT_ARS = "10000"
WAPUPAY_TEST_RECEIVER_NAME = "Test Receiver"

# No API key → nothing to test. Skip the whole module rather than fail.
pytestmark = pytest.mark.skipif(
    not WAPUPAY_API_KEY,
    reason="WAPUPAY_API_KEY not set — WapuPay smoke tests need a real (staging) key",
)


def _is_staging() -> bool:
    """True when the client resolves a staging base URL (captured at import)."""
    return "stage" in aqua.wapupay.WAPUPAY_BASE_URL.lower()


# Retry 5xx/unreachable (transient); fail on 400/401 (real error).
_TRANSIENT = re.compile(r"unreachable|failed \(5\d\d ", re.IGNORECASE)

_ATTEMPTS = 4
_RETRY_DELAY_S = 3


@pytest.fixture(scope="module")
def cli_runner():
    """Invoke `aqua` with WapuPay tools force-enabled and return parsed JSON.

    The WapuPay surface ships dark-launched OFF, so we register a fresh root
    group with an enabled `Config` instead of mutating the import-time `cli`
    singleton (which would leak gating state into other smoke tests).
    Retries transient 5xx/unreachable errors; skips if the backend stays down.
    """
    enabled = dict(SHIPPED_DEFAULTS_ENABLED_TOOLS)
    enabled.update({name: True for name in TOOLS if name.startswith("wapupay_")})

    @click.group()
    @click.pass_context
    def root(ctx):
        # Mirror `aqua.cli.main.cli`: commands use `@pass_obj` → AquaContext.
        ctx.obj = AquaContext(fmt="json")

    register_commands(root, config=Config(enabled_tools=enabled))
    runner = CliRunner()

    def run(*args):
        last = ""
        for attempt in range(_ATTEMPTS):
            result = runner.invoke(root, list(args))
            if result.exit_code == 0:
                data = json.loads(result.stdout)
                assert "error" not in data, f"tool returned an error envelope: {data}"
                return data
            last = f"{result.stdout!r} {result.stderr!r}"
            if not _TRANSIENT.search(last):
                break
            if attempt < _ATTEMPTS - 1:
                time.sleep(_RETRY_DELAY_S)

        if _TRANSIENT.search(last):
            pytest.skip(f"WapuPay staging unreachable after retries. Last: {last[:400]}")
        raise AssertionError(f"CLI failed: {last}")

    return run


class TestSmokeWapupayRates:
    def test_rates(self, cli_runner):
        """Public exchange-rates endpoint returns a non-empty payload (no key needed)."""
        result = cli_runner("wapupay", "rates")
        assert isinstance(result, dict) and result


class TestSmokeWapupayQuote:
    def test_quote(self, cli_runner):
        """A quote returns the USDT cost / rate for an ARS amount (no order created)."""
        result = cli_runner("wapupay", "quote", "--amount-ars", WAPUPAY_TEST_AMOUNT_ARS)
        assert any(k in result for k in ("usdt_amount", "total_amount", "exchange_rate"))

    def test_quote_with_alias(self, cli_runner):
        """Passing an alias surfaces `valid_cbu_alias` for pre-order validation."""
        if not WAPUPAY_TEST_ALIAS:
            pytest.skip("WAPUPAY_TEST_ALIAS not set")
        result = cli_runner(
            "wapupay", "quote",
            "--amount-ars", WAPUPAY_TEST_AMOUNT_ARS,
            "--alias", WAPUPAY_TEST_ALIAS,
        )
        assert "valid_cbu_alias" in result


class TestSmokeWapupaySpendingLimit:
    def test_spending_limit(self, cli_runner):
        """Spending limit returns the account's monthly USDT limit / tier."""
        result = cli_runner("wapupay", "spending-limit")
        assert isinstance(result, dict) and result


class TestSmokeWapupayTransactions:
    def test_transactions(self, cli_runner):
        """Transactions list returns a list (possibly empty on a fresh account)."""
        result = cli_runner("wapupay", "transactions")
        assert "transactions" in result
        assert isinstance(result["transactions"], list)


class TestSmokeWapupayOrders:
    def test_orders(self, cli_runner):
        """Local order list returns a list."""
        result = cli_runner("wapupay", "orders")
        assert "orders" in result
        assert isinstance(result["orders"], list)


class TestSmokeWapupayCreateOrder:
    """Create a REAL staging order but never fund it (no USDT deposit)."""

    _tentative_id = None

    def test_create_order(self, cli_runner):
        if not _is_staging():
            pytest.skip(
                f"WAPUPAY_BASE_URL is not staging ({aqua.wapupay.WAPUPAY_BASE_URL}) — "
                "refusing to create an order outside staging"
            )
        if not WAPUPAY_TEST_ALIAS:
            pytest.skip("WAPUPAY_TEST_ALIAS not set")

        args = [
            "wapupay", "create-order",
            "--amount-ars", WAPUPAY_TEST_AMOUNT_ARS,
            "--alias", WAPUPAY_TEST_ALIAS,
            "--yes",  # skip the interactive quote confirmation
        ]
        if WAPUPAY_TEST_RECEIVER_NAME:
            args += ["--receiver-name", WAPUPAY_TEST_RECEIVER_NAME]

        result = cli_runner(*args)
        assert "tentative_id" in result
        # Funding instructions return a Liquid USDT address — we get it but
        # NEVER pay it (no `aqua liquid send-asset`), so no deposit is sent.
        if result.get("funded"):
            assert result["address_destination"].startswith(("lq1", "ex1", "VJL"))
        TestSmokeWapupayCreateOrder._tentative_id = result["tentative_id"]

    def test_order_status(self, cli_runner):
        tentative_id = TestSmokeWapupayCreateOrder._tentative_id
        if tentative_id is None:
            pytest.skip("no tentative_id from create-order")
        result = cli_runner("wapupay", "order-status", "--tentative-id", tentative_id)
        assert "status" in result
