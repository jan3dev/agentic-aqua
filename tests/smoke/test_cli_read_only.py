"""CLI smoke tests: read-only operations against MAINNET.

These tests hit MAINNET via real Electrum/Esplora servers.
By default they use a throwaway BIP39 mnemonic with no on-chain history;
override with SIGNER_MNEMONIC to run against a wallet with real history.

Usage:
    uv run python -m pytest tests/smoke/ -v
"""

import json
import os
import time

import pytest
from click.testing import CliRunner

from aqua_mcp.cli.main import cli

DEFAULT_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
SIGNER_MNEMONIC = os.getenv("SIGNER_MNEMONIC", DEFAULT_MNEMONIC)


@pytest.fixture(scope="module")
def wallet_name():
    """Unique wallet name for this smoke run."""
    return f"smoke_cli_{int(time.time())}"


# Esplora/Electrum: connection resets, timeouts, Windows WSAETIMEDOUT (10060), minreq
TRANSIENT_NETWORK_MARKERS = (
    "minreq",
    "connection reset",
    "timed out",
    "timeout",
    "10060",
    "connection attempt failed",
    "failed to respond",
    "established connection failed",
)


_SMOKE_CLI_ATTEMPTS = 6
_SMOKE_CLI_RETRY_DELAY_S = 3


@pytest.fixture(scope="module")
def cli_runner():
    """Return a function that invokes aqua-cli and returns parsed JSON.

    Retries on transient Esplora/Electrum network errors so smoke tests are
    not brittle against occasional upstream connection resets.
    If the upstream stays unreachable, skips the test instead of hard-failing CI.
    """
    runner = CliRunner()

    def run(*args):
        last_output = ""
        result = None
        for attempt in range(_SMOKE_CLI_ATTEMPTS):
            result = runner.invoke(cli, ["--format", "json", *args])
            if result.exit_code == 0:
                return json.loads(result.stdout)
            last_output = f"{result.stdout!r} {result.stderr!r}"
            if not any(m in last_output.lower() for m in TRANSIENT_NETWORK_MARKERS):
                break
            if attempt < _SMOKE_CLI_ATTEMPTS - 1:
                time.sleep(_SMOKE_CLI_RETRY_DELAY_S)

        if result is not None and result.exit_code != 0 and any(
            m in last_output.lower() for m in TRANSIENT_NETWORK_MARKERS
        ):
            pytest.skip(
                "Network error after retries (mainnet API unreachable). "
                f"Last output: {last_output[:400]}"
            )
        raise AssertionError(f"CLI failed: {last_output}")

    return run


class TestSmokeImportWallet:
	def test_import_wallet(self, wallet_name):
		"""Import the test wallet from SIGNER_MNEMONIC."""
		runner = CliRunner()
		result = runner.invoke(
			cli,
			["--format", "json", "wallet", "import-mnemonic",
			 "--mnemonic-stdin", "--wallet-name", wallet_name],
			input=SIGNER_MNEMONIC + "\n",
		)
		assert result.exit_code == 0
		data = json.loads(result.stdout)
		assert data["wallet_name"] == wallet_name
		assert data["watch_only"] is False


class TestSmokeAddresses:
    def test_liquid_address(self, cli_runner, wallet_name):
        """Liquid address starts with lq1 (mainnet)."""
        result = cli_runner("liquid", "address", "--wallet-name", wallet_name)
        assert result["address"].startswith("lq1") or result["address"].startswith("ex1")

    def test_btc_address(self, cli_runner, wallet_name):
        """Bitcoin address starts with bc1 (mainnet)."""
        result = cli_runner("btc", "address", "--wallet-name", wallet_name)
        assert result["address"].startswith("bc1")


class TestSmokeBalance:
    def test_unified_balance(self, cli_runner, wallet_name):
        """Unified balance returns both networks."""
        result = cli_runner("balance", "--wallet-name", wallet_name)
        assert "liquid" in result
        assert "bitcoin" in result or "bitcoin_error" in result


class TestSmokeBtcTransactions:
    def test_btc_transactions(self, cli_runner, wallet_name):
        """BTC transactions returns a list."""
        result = cli_runner("btc", "transactions", "--wallet-name", wallet_name)
        assert "transactions" in result
        assert isinstance(result["transactions"], list)


class TestSmokeLiquidTransactions:
    def test_liquid_transactions(self, cli_runner, wallet_name):
        """Liquid transactions returns a list."""
        result = cli_runner("liquid", "transactions", "--wallet-name", wallet_name)
        assert "transactions" in result
        assert isinstance(result["transactions"], list)


class TestSmokeListWallets:
    def test_list_wallets(self, cli_runner, wallet_name):
        """Wallet list includes the smoke wallet."""
        result = cli_runner("wallet", "list")
        assert result["count"] >= 1
        wallet_names = [w["name"] if isinstance(w, dict) else w for w in result["wallets"]]
        assert wallet_name in wallet_names


class TestSmokeExportDescriptor:
    def test_export_descriptor(self, cli_runner, wallet_name):
        """Export descriptor returns a CT descriptor string."""
        result = cli_runner("wallet", "export-descriptor", "--wallet-name", wallet_name)
        assert "descriptor" in result
        assert result["descriptor"].startswith("ct(")


class TestSmokeLightningReceive:
    def test_lightning_receive(self, cli_runner, wallet_name):
        """Generate a Lightning invoice for 500 sats."""
        result = cli_runner(
            "lightning", "receive",
            "--amount", "500",
            "--wallet-name", wallet_name,
        )
        assert "swap_id" in result
        assert "invoice" in result
        assert result["amount"] == 500
        TestSmokeLightningReceive._swap_id = result["swap_id"]


class TestSmokeLightningStatus:
    def test_lightning_status(self, cli_runner):
        """Check status of the receive swap created above."""
        swap_id = getattr(TestSmokeLightningReceive, "_swap_id", None)
        if swap_id is None:
            pytest.skip("No swap_id from lightning receive test")
        result = cli_runner("lightning", "status", "--swap-id", swap_id)
        assert "swap_id" in result
        assert "status" in result


class TestSmokeDeleteWallet:
	def test_delete_wallet(self):
		"""Import a throwaway wallet then delete it; verify it's gone."""
		name = f"delete_test_{int(time.time())}"
		runner = CliRunner()
		result = runner.invoke(
			cli,
			["--format", "json", "wallet", "import-mnemonic",
			 "--mnemonic-stdin", "--wallet-name", name],
			input=DEFAULT_MNEMONIC + "\n",
		)
		assert result.exit_code == 0

		result = runner.invoke(
			cli,
			["--format", "json", "wallet", "delete", "--wallet-name", name, "--yes"],
		)
		assert result.exit_code == 0

		result = runner.invoke(
			cli,
			["--format", "json", "wallet", "list"],
		)
		assert result.exit_code == 0
		wallets = json.loads(result.stdout)
		wallet_names = [w["name"] if isinstance(w, dict) else w for w in wallets["wallets"]]
		assert name not in wallet_names


class TestSmokeSeedRestore:
	def test_seed_backup_and_restore(self):
		"""Re-importing the same mnemonic + password yields the same BTC address."""
		name = f"test_seed_restore_{int(time.time())}"
		runner = CliRunner()
		result = runner.invoke(
			cli,
			["--format", "json", "wallet", "import-mnemonic",
			 "--mnemonic-stdin", "--password-stdin", "--wallet-name", name],
			input=DEFAULT_MNEMONIC + "\ntest\n",
		)
		assert result.exit_code == 0

		result = runner.invoke(
			cli,
			["--format", "json", "btc", "address", "--wallet-name", name],
		)
		assert result.exit_code == 0
		first_address = json.loads(result.stdout)["address"]

		runner.invoke(cli, ["--format", "json", "wallet", "delete", "--wallet-name", name, "--yes"])

		result = runner.invoke(
			cli,
			["--format", "json", "wallet", "import-mnemonic",
			 "--mnemonic-stdin", "--password-stdin", "--wallet-name", name],
			input=DEFAULT_MNEMONIC + "\ntest\n",
		)
		assert result.exit_code == 0

		result = runner.invoke(
			cli,
			["--format", "json", "btc", "address", "--wallet-name", name],
		)
		assert result.exit_code == 0
		restored_address = json.loads(result.stdout)["address"]
		assert restored_address == first_address

		runner.invoke(cli, ["--format", "json", "wallet", "delete", "--wallet-name", name, "--yes"])


class TestSmokeCleanup:
    def test_delete_smoke_wallet(self, wallet_name):
        """Delete the smoke wallet after all tests."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "delete", "--wallet-name", wallet_name, "--yes"],
        )
        assert result.exit_code == 0
