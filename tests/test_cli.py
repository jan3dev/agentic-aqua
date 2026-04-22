"""Tests for the Click CLI interface."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from aqua_mcp.bitcoin import BitcoinWalletManager
from aqua_mcp.cli.main import cli
from aqua_mcp.storage import Storage
from aqua_mcp.wallet import WalletManager

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


@pytest.fixture(autouse=True)
def isolated_manager():
    """Replace global managers with ones using a temp directory for every test.

    Patches sync_wallet on both LWK and BDK managers to avoid network calls.
    """
    import aqua_mcp.tools as tools_module

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        storage = Storage(Path(tmpdir))
        manager = WalletManager(storage=storage)
        btc_manager = BitcoinWalletManager(storage=storage)
        tools_module._manager = manager
        tools_module._btc_manager = btc_manager
        tools_module._lightning_manager = None
        with (
            patch.object(manager, "sync_wallet"),
            patch.object(btc_manager, "sync_wallet"),
        ):
            yield manager, btc_manager
        tools_module._manager = None
        tools_module._btc_manager = None
        tools_module._lightning_manager = None


@pytest.fixture
def runner():
    return CliRunner()


def _import_wallet(runner):
    """Helper: import the test wallet via CLI."""
    runner.invoke(cli, ["wallet", "import-mnemonic", "--mnemonic", TEST_MNEMONIC])



# Root CLI


class TestRootCli:
    def test_balance_json(self, runner):
        """Top-level balance command returns JSON with unified payload."""
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "balance"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "wallet_name" in data
        assert "liquid" in data

    def test_balance_pretty(self, runner):
        """Pretty format should not be valid JSON."""
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "pretty", "balance"])
        assert result.exit_code == 0
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.output)


# Wallet commands


class TestWalletCommands:
    def test_generate_mnemonic(self, runner):
        result = runner.invoke(cli, ["--format", "json", "wallet", "generate-mnemonic"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "mnemonic" in data
        assert data["words"] == 12

    def test_import_mnemonic(self, runner):
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "import-mnemonic", "--mnemonic", TEST_MNEMONIC],
        )
        assert result.exit_code == 0
        assert "shell history" in (result.stderr or "")
        data = json.loads(result.stdout)
        assert data["wallet_name"] == "default"
        assert data["watch_only"] is False

    def test_import_mnemonic_custom_name(self, runner):
        result = runner.invoke(
            cli,
            [
                "--format", "json",
                "wallet", "import-mnemonic",
                "--mnemonic", TEST_MNEMONIC,
                "--wallet-name", "test_wallet",
            ],
        )
        assert result.exit_code == 0
        assert "shell history" in (result.stderr or "")
        data = json.loads(result.stdout)
        assert data["wallet_name"] == "test_wallet"

    def test_import_mnemonic_from_env(self, runner):
        env = {k: v for k, v in os.environ.items() if k != "AQUA_MNEMONIC"}
        env["AQUA_MNEMONIC"] = TEST_MNEMONIC
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "import-mnemonic"],
            env=env,
        )
        assert result.exit_code == 0
        assert "shell history" not in (result.stderr or "")
        data = json.loads(result.stdout)
        assert data["wallet_name"] == "default"
        assert data["watch_only"] is False

    @patch("aqua_mcp.cli.wallet.click.prompt", return_value=TEST_MNEMONIC)
    def test_import_mnemonic_from_prompt(self, mock_prompt, runner):
        env = {k: v for k, v in os.environ.items() if k != "AQUA_MNEMONIC"}
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "import-mnemonic"],
            env=env,
        )
        assert result.exit_code == 0
        assert "shell history" not in (result.stderr or "")
        mock_prompt.assert_called_once_with("Mnemonic", hide_input=True)
        data = json.loads(result.stdout)
        assert data["wallet_name"] == "default"

    def test_list_wallets_empty(self, runner):
        result = runner.invoke(cli, ["--format", "json", "wallet", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 0

    def test_list_wallets_after_import(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "wallet", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] >= 1

    def test_export_descriptor(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "wallet", "export-descriptor"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "descriptor" in data

    def test_import_descriptor(self, runner):
        _import_wallet(runner)
        exp = runner.invoke(cli, ["--format", "json", "wallet", "export-descriptor"])
        descriptor = json.loads(exp.output)["descriptor"]
        result = runner.invoke(
            cli,
            [
                "--format", "json",
                "wallet", "import-descriptor",
                "--descriptor", descriptor,
                "--wallet-name", "watch_only",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["watch_only"] is True

    def test_delete_wallet_with_yes(self, runner):
        _import_wallet(runner)
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "delete", "--wallet-name", "default", "--yes"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["deleted"] is True

    def test_delete_wallet_cancelled(self, runner):
        _import_wallet(runner)
        result = runner.invoke(
            cli,
            ["wallet", "delete", "--wallet-name", "default"],
            input="wrong_name\n",
        )
        assert result.exit_code == 1

    def test_delete_nonexistent_wallet(self, runner):
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "delete", "--wallet-name", "nope", "--yes"],
        )
        assert result.exit_code == 1
        # Error goes to output (stderr captured in output by CliRunner)
        assert "error" in result.output.lower() or "not found" in result.output.lower()


# Liquid commands

class TestLiquidCommands:
    def test_balance(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "liquid", "balance"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "balances" in data

    def test_address(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "liquid", "address"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "address" in data

    def test_address_rejects_negative_index(self, runner):
        _import_wallet(runner)
        result = runner.invoke(
            cli,
            ["--format", "json", "liquid", "address", "--index", "-1"],
        )
        assert result.exit_code == 2
        assert "index must be non-negative" in result.output.lower()

    def test_transactions(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "liquid", "transactions"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "transactions" in data

    def test_send_missing_wallet(self, runner):
        """Send without a wallet should error."""
        result = runner.invoke(
            cli,
            ["--format", "json", "liquid", "send", "--wallet-name", "nope", "--address", "lq1x", "--amount", "1000"],
        )
        assert result.exit_code == 1

    def test_send_lbtc_amount_must_be_positive(self, runner):
        """liquid send rejects non-positive --amount before invoking the tool."""
        _import_wallet(runner)
        result = runner.invoke(
            cli,
            [
                "--format", "json",
                "liquid", "send",
                "--wallet-name", "default",
                "--address", "lq1x",
                "--amount", "0",
            ],
        )
        assert result.exit_code == 2
        assert "amount" in result.output.lower()
        assert "1" in result.output or "range" in result.output.lower()

    def test_assets_lists_known_assets(self, runner):
        """liquid assets returns the mainnet registry with id/ticker/name/precision."""
        result = runner.invoke(cli, ["--format", "json", "liquid", "assets"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["network"] == "mainnet"
        assert data["count"] >= 1
        tickers = {a["ticker"] for a in data["assets"]}
        assert "L-BTC" in tickers
        assert "USDt" in tickers
        # Every entry exposes the fields an agent needs to send
        for entry in data["assets"]:
            assert set(entry.keys()) == {"asset_id", "ticker", "name", "precision"}
            assert len(entry["asset_id"]) == 64


    def test_send_asset_requires_exactly_one_of_id_or_ticker(self, runner):
        """send-asset must receive exactly one of --asset-id or --asset-ticker."""
        _import_wallet(runner)
        neither = runner.invoke(
            cli,
            ["liquid", "send-asset", "--wallet-name", "default",
             "--address", "lq1x", "--amount", "100"],
        )
        assert neither.exit_code != 0
        assert "exactly one" in neither.output.lower()

        both = runner.invoke(
            cli,
            ["liquid", "send-asset", "--wallet-name", "default",
             "--address", "lq1x", "--amount", "100",
             "--asset-id", "abc", "--asset-ticker", "USDt"],
        )
        assert both.exit_code != 0
        assert "exactly one" in both.output.lower()

    def test_send_asset_unknown_ticker(self, runner):
        """Unknown ticker produces a helpful usage error, never reaches the tool."""
        _import_wallet(runner)
        result = runner.invoke(
            cli,
            ["liquid", "send-asset", "--wallet-name", "default",
             "--address", "lq1x", "--amount", "100",
             "--asset-ticker", "NOTAREAL"],
        )
        assert result.exit_code != 0
        assert "unknown ticker" in result.output.lower()

    def test_send_asset_amount_must_be_positive(self, runner):
        """Non-positive --amount is rejected before ticker resolution."""
        _import_wallet(runner)
        result = runner.invoke(
            cli,
            [
                "liquid", "send-asset", "--wallet-name", "default",
                "--address", "lq1x", "--amount", "0",
                "--asset-ticker", "USDt",
            ],
        )
        assert result.exit_code != 0
        assert "positive" in result.output.lower()


# BTC commands

class TestBtcCommands:
    def test_balance(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "btc", "balance"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "balance_sats" in data

    def test_address(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "btc", "address"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "address" in data

    def test_transactions(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "btc", "transactions"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "transactions" in data


# Lightning commands

class TestLightningCommands:
    def test_status_missing_swap(self, runner):
        """Status for nonexistent swap should error."""
        result = runner.invoke(
            cli,
            ["--format", "json", "lightning", "status", "--swap-id", "nonexistent"],
        )
        assert result.exit_code == 1

# Error handling

class TestErrorHandling:
    def test_json_error_shape(self, runner):
        """Errors in JSON mode match the MCP error shape."""
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "delete", "--wallet-name", "nonexistent", "--yes"],
        )
        assert result.exit_code == 1
        # In Click 8.2+, stderr output is captured in result.output
        error_output = result.output
        data = json.loads(error_output)
        assert "error" in data
        assert "code" in data["error"]
        assert "message" in data["error"]

    def test_pretty_error(self, runner):
        """Errors in pretty mode show human-readable message."""
        result = runner.invoke(
            cli,
            ["--format", "pretty", "wallet", "delete", "--wallet-name", "nonexistent", "--yes"],
        )
        assert result.exit_code == 1
        assert "Error" in result.output
