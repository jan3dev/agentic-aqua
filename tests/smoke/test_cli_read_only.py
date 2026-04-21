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


@pytest.fixture(scope="module")
def cli_runner():
    """Return a function that invokes aqua-cli and returns parsed JSON."""
    runner = CliRunner()

    def run(*args):
        result = runner.invoke(cli, ["--format", "json", *args])
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        return json.loads(result.output)

    return run


class TestSmokeImportWallet:
    def test_import_wallet(self, cli_runner, wallet_name):
        """Import the test wallet from SIGNER_MNEMONIC."""
        result = cli_runner(
            "wallet", "import-mnemonic",
            "--mnemonic", SIGNER_MNEMONIC,
            "--wallet-name", wallet_name,
        )
        assert result["wallet_name"] == wallet_name
        assert result["watch_only"] is False


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
    def test_delete_wallet(self, cli_runner):
        """Import a throwaway wallet then delete it; verify it's gone."""
        name = f"delete_test_{int(time.time())}"
        cli_runner(
            "wallet", "import-mnemonic",
            "--mnemonic", DEFAULT_MNEMONIC,
            "--wallet-name", name,
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "delete", "--wallet-name", name, "--yes"],
        )
        assert result.exit_code == 0

        wallets = cli_runner("wallet", "list")
        wallet_names = [w["name"] if isinstance(w, dict) else w for w in wallets["wallets"]]
        assert name not in wallet_names


class TestSmokeSeedRestore:
    def test_seed_backup_and_restore(self, cli_runner):
        """Re-importing the same mnemonic + password yields the same BTC address."""
        name = f"test_seed_restore_{int(time.time())}"
        cli_runner(
            "wallet", "import-mnemonic",
            "--mnemonic", DEFAULT_MNEMONIC,
            "--wallet-name", name,
            "--password", "test",
        )
        first_address = cli_runner("btc", "address", "--wallet-name", name)["address"]

        runner = CliRunner()
        runner.invoke(cli, ["--format", "json", "wallet", "delete", "--wallet-name", name, "--yes"])

        cli_runner(
            "wallet", "import-mnemonic",
            "--mnemonic", DEFAULT_MNEMONIC,
            "--wallet-name", name,
            "--password", "test",
        )
        restored_address = cli_runner("btc", "address", "--wallet-name", name)["address"]
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
