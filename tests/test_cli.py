"""Tests for the Click CLI interface."""

import inspect
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    with tempfile.TemporaryDirectory() as tmpdir:
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


# ---------------------------------------------------------------------------
# Root CLI
# ---------------------------------------------------------------------------


class TestRootCli:
    def test_help(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "AQUA wallet CLI" in result.output

    def test_version(self, runner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.2.1" in result.output

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


# ---------------------------------------------------------------------------
# Wallet commands
# ---------------------------------------------------------------------------


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
        data = json.loads(result.output)
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
        data = json.loads(result.output)
        assert data["wallet_name"] == "test_wallet"

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


# ---------------------------------------------------------------------------
# Liquid commands
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# BTC commands
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Lightning commands
# ---------------------------------------------------------------------------


class TestLightningCommands:
    def test_status_missing_swap(self, runner):
        """Status for nonexistent swap should error."""
        result = runner.invoke(
            cli,
            ["--format", "json", "lightning", "status", "--swap-id", "nonexistent"],
        )
        assert result.exit_code == 1

    def test_receive_help(self, runner):
        result = runner.invoke(cli, ["lightning", "receive", "--help"])
        assert result.exit_code == 0
        assert "--amount" in result.output

    def test_send_help(self, runner):
        result = runner.invoke(cli, ["lightning", "send", "--help"])
        assert result.exit_code == 0
        assert "--invoice" in result.output


# ---------------------------------------------------------------------------
# Serve command
# ---------------------------------------------------------------------------


class TestServeCommand:
    def test_serve_help(self, runner):
        result = runner.invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--transport" in result.output


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Parameter sync test
# ---------------------------------------------------------------------------


class TestParameterSync:
    """Ensure Click commands stay in sync with tool function signatures."""

    def _get_cli_commands(self):
        """Walk the CLI tree and return {tool_name: click_command} mapping."""
        from aqua_mcp.cli.wallet import wallet
        from aqua_mcp.cli.liquid import liquid
        from aqua_mcp.cli.btc import btc
        from aqua_mcp.cli.lightning import lightning

        mapping = {
            ("wallet", "generate-mnemonic"): "lw_generate_mnemonic",
            ("wallet", "import-mnemonic"): "lw_import_mnemonic",
            ("wallet", "import-descriptor"): "lw_import_descriptor",
            ("wallet", "export-descriptor"): "lw_export_descriptor",
            ("wallet", "list"): "lw_list_wallets",
            ("wallet", "delete"): "delete_wallet",
            ("liquid", "balance"): "lw_balance",
            ("liquid", "address"): "lw_address",
            ("liquid", "transactions"): "lw_transactions",
            ("liquid", "send"): "lw_send",
            ("liquid", "send-asset"): "lw_send_asset",
            ("liquid", "tx-status"): "lw_tx_status",
            ("btc", "balance"): "btc_balance",
            ("btc", "address"): "btc_address",
            ("btc", "transactions"): "btc_transactions",
            ("btc", "send"): "btc_send",
            ("balance",): "unified_balance",
            ("lightning", "receive"): "lightning_receive",
            ("lightning", "send"): "lightning_send",
            ("lightning", "status"): "lightning_transaction_status",
        }

        groups = {
            "wallet": wallet,
            "liquid": liquid,
            "btc": btc,
            "lightning": lightning,
        }

        result = {}
        for cli_path, tool_name in mapping.items():
            if len(cli_path) == 1:
                from aqua_mcp.cli.commands import balance as balance_cmd
                result[tool_name] = balance_cmd
            else:
                group_name, cmd_name = cli_path
                group = groups[group_name]
                cmd = group.commands.get(cmd_name)
                if cmd is not None:
                    result[tool_name] = cmd

        return result

    def test_all_tools_have_cli_commands(self):
        """Every tool in TOOLS registry has a corresponding CLI command."""
        from aqua_mcp.tools import TOOLS

        cli_commands = self._get_cli_commands()
        for tool_name in TOOLS:
            assert tool_name in cli_commands, f"Tool '{tool_name}' has no CLI command"

    def test_cli_params_cover_tool_params(self):
        """Click commands have options for all non-trivial tool function parameters."""
        from aqua_mcp.tools import TOOLS

        cli_commands = self._get_cli_commands()
        cli_only_params = {"yes"}

        for tool_name, cmd in cli_commands.items():
            tool_fn = TOOLS.get(tool_name)
            if tool_fn is None:
                continue

            sig = inspect.signature(tool_fn)
            tool_params = set(sig.parameters.keys())

            cli_param_names = set()
            for param in cmd.params:
                cli_param_names.add(param.name)

            cli_param_names -= cli_only_params

            for tp in tool_params:
                assert tp in cli_param_names, (
                    f"Tool '{tool_name}' param '{tp}' is missing from CLI command"
                )
