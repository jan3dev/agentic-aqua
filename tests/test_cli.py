"""Tests for the Click CLI interface."""

import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from aqua_mcp.bitcoin import BitcoinWalletManager
from aqua_mcp.cli import password as password_mod
from aqua_mcp.cli.main import cli
from aqua_mcp.storage import Storage
from aqua_mcp.wallet import WalletManager

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


class StringIOWithIsatty(io.StringIO):
    """StringIO that also implements isatty(), used to simulate piped vs TTY stdin."""

    def __init__(self, initial_value: str = "", *, isatty: bool = False):
        super().__init__(initial_value)
        self._isatty = isatty

    def isatty(self) -> bool:  # noqa: D401
        return self._isatty


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
    runner.invoke(cli, ["wallet", "import-mnemonic", "--mnemonic-stdin"],
                  input=TEST_MNEMONIC + "\n")



# Secret resolution (unit tests for cli.password.resolve_secret)


class TestResolveSecret:
    """Direct unit tests of the resolve_secret helper.

    Three sources × required/optional. Precedence: --*-stdin flag > env var > prompt/None.
    """

    def test_resolve_from_stdin_flag_piped(self, monkeypatch):
        """use_stdin=True + non-TTY stdin → reads one line from stdin."""
        monkeypatch.setattr(password_mod.sys, "stdin", StringIOWithIsatty("abc\n", isatty=False))
        assert (
            password_mod.resolve_secret("Password", use_stdin=True, env_var="AQUA_PASSWORD")
            == "abc"
        )

    def test_resolve_from_tty_prompt_when_stdin_flag(self, monkeypatch):
        """use_stdin=True + TTY → delegates to click.prompt (hidden)."""
        monkeypatch.setattr(password_mod.sys, "stdin", StringIOWithIsatty("", isatty=True))
        with patch.object(password_mod.click, "prompt", return_value="tty_val") as mock:
            assert password_mod.resolve_secret("Password", use_stdin=True) == "tty_val"
        mock.assert_called_once_with("Password", hide_input=True)

    def test_resolve_from_env_var(self, monkeypatch):
        """No flag, env var set → returns env var (whitespace trimmed)."""
        monkeypatch.setenv("AQUA_PASSWORD", "  s3cret  ")
        assert (
            password_mod.resolve_secret(
                "Password", use_stdin=False, env_var="AQUA_PASSWORD", required=False
            )
            == "s3cret"
        )

    def test_resolve_prompt_when_required(self, monkeypatch):
        """No flag, no env, required=True → prompts interactively."""
        monkeypatch.delenv("AQUA_PASSWORD", raising=False)
        with patch.object(password_mod.click, "prompt", return_value="prompted") as mock:
            assert (
                password_mod.resolve_secret(
                    "Password", use_stdin=False, env_var="AQUA_PASSWORD", required=True
                )
                == "prompted"
            )
        mock.assert_called_once_with("Password", hide_input=True)

    def test_resolve_none_when_not_required(self, monkeypatch):
        """No flag, no env, required=False → returns None without prompting."""
        monkeypatch.delenv("AQUA_PASSWORD", raising=False)
        with patch.object(password_mod.click, "prompt") as mock:
            assert (
                password_mod.resolve_secret(
                    "Password", use_stdin=False, env_var="AQUA_PASSWORD", required=False
                )
                is None
            )
        mock.assert_not_called()

    def test_resolve_flag_wins_over_env(self, monkeypatch):
        """use_stdin=True takes precedence over env var."""
        monkeypatch.setenv("AQUA_PASSWORD", "env_val")
        monkeypatch.setattr(
            password_mod.sys, "stdin", StringIOWithIsatty("stdin_val\n", isatty=False)
        )
        assert (
            password_mod.resolve_secret("Password", use_stdin=True, env_var="AQUA_PASSWORD")
            == "stdin_val"
        )

    def test_resolve_empty_env_var_is_treated_as_unset(self, monkeypatch):
        """Whitespace-only env var is ignored; falls through to required/None."""
        monkeypatch.setenv("AQUA_PASSWORD", "   ")
        assert (
            password_mod.resolve_secret(
                "Password", use_stdin=False, env_var="AQUA_PASSWORD", required=False
            )
            is None
        )


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
            ["--format", "json", "wallet", "import-mnemonic", "--mnemonic-stdin"],
            input=TEST_MNEMONIC + "\n",
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["wallet_name"] == "default"
        assert data["watch_only"] is False

    def test_import_mnemonic_custom_name(self, runner):
        result = runner.invoke(
            cli,
            [
                "--format", "json",
                "wallet", "import-mnemonic",
                "--mnemonic-stdin",
                "--wallet-name", "test_wallet",
            ],
            input=TEST_MNEMONIC + "\n",
        )
        assert result.exit_code == 0
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
        mock_prompt.assert_called_once_with("Mnemonic", hide_input=True)
        data = json.loads(result.stdout)
        assert data["wallet_name"] == "default"

    # ------------------------------------------------------------------
    # Password resolution on import-mnemonic: stdin, env var, TTY prompt, none.
    # (Mnemonic resolution for the same three sources is covered above by
    # test_import_mnemonic, test_import_mnemonic_from_env, and
    # test_import_mnemonic_from_prompt.)
    # ------------------------------------------------------------------

    def test_password_via_stdin_encrypts_wallet(self, runner, isolated_manager):
        """--password-stdin (piped) produces an encrypted wallet."""
        manager, _ = isolated_manager
        result = runner.invoke(
            cli,
            [
                "--format", "json",
                "wallet", "import-mnemonic",
                "--mnemonic-stdin", "--password-stdin",
                "--wallet-name", "enc_stdin",
            ],
            input=TEST_MNEMONIC + "\ns3cret\n",
        )
        assert result.exit_code == 0, result.output
        stored = manager.storage.load_wallet("enc_stdin")
        assert manager.storage.is_mnemonic_encrypted(stored.encrypted_mnemonic)

    def test_password_via_env_var_encrypts_wallet(self, runner, isolated_manager):
        """AQUA_PASSWORD env var produces an encrypted wallet (regression test)."""
        manager, _ = isolated_manager
        env = {k: v for k, v in os.environ.items() if k not in ("AQUA_MNEMONIC", "AQUA_PASSWORD")}
        env["AQUA_MNEMONIC"] = TEST_MNEMONIC
        env["AQUA_PASSWORD"] = "s3cret"
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "import-mnemonic", "--wallet-name", "enc_env"],
            env=env,
        )
        assert result.exit_code == 0, result.output
        stored = manager.storage.load_wallet("enc_env")
        assert manager.storage.is_mnemonic_encrypted(stored.encrypted_mnemonic)

    def test_password_via_prompt_encrypts_wallet(self, runner, isolated_manager):
        """--password-stdin on a TTY: read_secret prompts and encrypts the wallet.

        CliRunner replaces sys.stdin with a non-TTY stream, so we can't exercise
        the TTY branch inside read_secret through CliRunner alone. We patch
        read_secret to simulate the prompted value; the unit tests in
        TestResolveSecret separately verify read_secret's TTY branch.
        """
        manager, _ = isolated_manager
        env = {k: v for k, v in os.environ.items() if k not in ("AQUA_MNEMONIC", "AQUA_PASSWORD")}
        env["AQUA_MNEMONIC"] = TEST_MNEMONIC
        with patch(
            "aqua_mcp.cli.password.read_secret", return_value="s3cret"
        ) as mock_read:
            result = runner.invoke(
                cli,
                [
                    "--format", "json",
                    "wallet", "import-mnemonic",
                    "--password-stdin",
                    "--wallet-name", "enc_prompt",
                ],
                env=env,
            )
        assert result.exit_code == 0, result.output
        mock_read.assert_called_once_with("Password")
        stored = manager.storage.load_wallet("enc_prompt")
        assert manager.storage.is_mnemonic_encrypted(stored.encrypted_mnemonic)

    def test_no_password_stores_plaintext(self, runner, isolated_manager):
        """No stdin flag, no env var, --password-stdin omitted → wallet unencrypted."""
        manager, _ = isolated_manager
        env = {k: v for k, v in os.environ.items() if k not in ("AQUA_MNEMONIC", "AQUA_PASSWORD")}
        env["AQUA_MNEMONIC"] = TEST_MNEMONIC
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "import-mnemonic", "--wallet-name", "plain_one"],
            env=env,
        )
        assert result.exit_code == 0, result.output
        stored = manager.storage.load_wallet("plain_one")
        assert not manager.storage.is_mnemonic_encrypted(stored.encrypted_mnemonic)
        assert stored.encrypted_mnemonic.startswith("plain:")

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

    def test_send_reads_password_from_env_var(self, runner):
        """btc send honors AQUA_PASSWORD when --password-stdin is not passed.

        Proves the resolve_secret wiring reaches non-wallet commands too. The
        unit tests in TestResolveSecret already cover the helper itself; here
        we only verify that this command passes the env var value to btc_send.
        """
        _import_wallet(runner)
        env = {k: v for k, v in os.environ.items() if k != "AQUA_PASSWORD"}
        env["AQUA_PASSWORD"] = "s3cret"
        with patch("aqua_mcp.cli.btc.btc_send", return_value={"txid": "fake"}) as mock_send:
            result = runner.invoke(
                cli,
                [
                    "--format", "json",
                    "btc", "send",
                    "--wallet-name", "default",
                    "--address", "bc1qxy",
                    "--amount", "1000",
                ],
                env=env,
            )
        assert result.exit_code == 0, result.output
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["password"] == "s3cret"


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
