"""Tests for the Click CLI interface."""

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from aqua.bitcoin import BitcoinWalletManager
from aqua.cli import password as password_mod
from aqua.cli.main import cli
from aqua.storage import Storage
from aqua.wallet import WalletManager

from tests.conftest import TEST_MNEMONIC


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
    import aqua.tools as tools_module

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
    runner.invoke(
        cli, ["wallet", "import-mnemonic", "--mnemonic-stdin"], input=TEST_MNEMONIC + "\n"
    )


def _cli_env(**overrides):
    """Build a Click env overlay that can also unset loaded .env secrets.

    CliRunner's env argument is an overlay, not a full replacement. Because the
    CLI loads repo .env at import time, omitting AQUA_* keys from a copied env
    does not remove them from os.environ during invoke().
    """
    env = {"AQUA_MNEMONIC": None, "AQUA_PASSWORD": None}
    env.update(overrides)
    return env


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

    def test_serve_runs_mcp_server(self, runner):
        """Serve command delegates to the MCP stdio server entrypoint."""
        with patch("aqua.server.run_server", new_callable=AsyncMock) as mock_run_server:
            result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 0
        mock_run_server.assert_awaited_once()

    def test_serve_help_has_no_transport_option(self, runner):
        """Serve help should not advertise a dead transport selector."""
        result = runner.invoke(cli, ["serve", "--help"])

        assert result.exit_code == 0
        assert "--transport" not in result.output
        assert "Start the MCP server over stdio (`aqua serve` or `aqua-mcp`)." in result.output


# QR commands


class TestQrCommands:
    def test_qr_generate_json_creates_png_that_decodes(self, runner):
        from aqua.qr import decode_qr

        payload = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"

        result = runner.invoke(cli, ["--format", "json", "qr", "generate", payload])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["content"] == payload
        path = Path(data["qr_code_path"])
        assert path.is_file()
        assert decode_qr(str(path)) == payload

    def test_qr_generate_can_include_terminal_qr(self, runner):
        payload = "liquid-address-example"

        result = runner.invoke(
            cli, ["--format", "json", "qr", "generate", "--terminal", payload]
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["content"] == payload
        assert "terminal_qr" in data
        assert "██" in data["terminal_qr"]
        assert len(data["terminal_qr"].splitlines()) > 5


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
                "--format",
                "json",
                "wallet",
                "import-mnemonic",
                "--mnemonic-stdin",
                "--wallet-name",
                "test_wallet",
            ],
            input=TEST_MNEMONIC + "\n",
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["wallet_name"] == "test_wallet"

    def test_import_mnemonic_from_env(self, runner):
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "import-mnemonic"],
            env=_cli_env(AQUA_MNEMONIC=TEST_MNEMONIC),
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["wallet_name"] == "default"
        assert data["watch_only"] is False

    @patch("aqua.cli.wallet.click.prompt", return_value=TEST_MNEMONIC)
    def test_import_mnemonic_from_prompt(self, mock_prompt, runner):
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "import-mnemonic"],
            env=_cli_env(),
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
                "--format",
                "json",
                "wallet",
                "import-mnemonic",
                "--mnemonic-stdin",
                "--password-stdin",
                "--wallet-name",
                "enc_stdin",
            ],
            input=TEST_MNEMONIC + "\ns3cret\n",
        )
        assert result.exit_code == 0, result.output
        stored = manager.storage.load_wallet("enc_stdin")
        assert manager.storage.requires_user_password(stored.encrypted_mnemonic)

    def test_password_via_env_var_encrypts_wallet(self, runner, isolated_manager):
        """AQUA_PASSWORD env var produces an encrypted wallet (regression test)."""
        manager, _ = isolated_manager
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "import-mnemonic", "--wallet-name", "enc_env"],
            env=_cli_env(AQUA_MNEMONIC=TEST_MNEMONIC, AQUA_PASSWORD="s3cret"),
        )
        assert result.exit_code == 0, result.output
        stored = manager.storage.load_wallet("enc_env")
        assert manager.storage.requires_user_password(stored.encrypted_mnemonic)

    def test_password_via_prompt_encrypts_wallet(self, runner, isolated_manager):
        """--password-stdin on a TTY: read_secret prompts and encrypts the wallet.

        CliRunner replaces sys.stdin with a non-TTY stream, so we can't exercise
        the TTY branch inside read_secret through CliRunner alone. We patch
        read_secret to simulate the prompted value; the unit tests in
        TestResolveSecret separately verify read_secret's TTY branch.
        """
        manager, _ = isolated_manager
        with patch("aqua.cli.password.read_secret", return_value="s3cret") as mock_read:
            result = runner.invoke(
                cli,
                [
                    "--format",
                    "json",
                    "wallet",
                    "import-mnemonic",
                    "--password-stdin",
                    "--wallet-name",
                    "enc_prompt",
                ],
                env=_cli_env(AQUA_MNEMONIC=TEST_MNEMONIC),
            )
        assert result.exit_code == 0, result.output
        mock_read.assert_called_once_with("Password")
        stored = manager.storage.load_wallet("enc_prompt")
        assert manager.storage.requires_user_password(stored.encrypted_mnemonic)

    def test_no_password_stores_default_encrypted(self, runner, isolated_manager):
        """No stdin flag, no env var, --password-stdin omitted → wallet uses default encryption."""
        manager, _ = isolated_manager
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "import-mnemonic", "--wallet-name", "plain_one"],
            env=_cli_env(AQUA_MNEMONIC=TEST_MNEMONIC),
        )
        assert result.exit_code == 0, result.output
        stored = manager.storage.load_wallet("plain_one")
        assert not manager.storage.requires_user_password(stored.encrypted_mnemonic)
        assert stored.encrypted_mnemonic.startswith("default:1:")

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
            [
                "--format",
                "json",
                "liquid",
                "send",
                "--wallet-name",
                "nope",
                "--address",
                "lq1x",
                "--amount",
                "1000",
            ],
        )
        assert result.exit_code == 1

    def test_send_lbtc_amount_must_be_positive(self, runner):
        """liquid send rejects non-positive --amount before invoking the tool."""
        _import_wallet(runner)
        result = runner.invoke(
            cli,
            [
                "--format",
                "json",
                "liquid",
                "send",
                "--wallet-name",
                "default",
                "--address",
                "lq1x",
                "--amount",
                "0",
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
            [
                "liquid",
                "send-asset",
                "--wallet-name",
                "default",
                "--address",
                "lq1x",
                "--amount",
                "100",
            ],
        )
        assert neither.exit_code != 0
        assert "exactly one" in neither.output.lower()

        both = runner.invoke(
            cli,
            [
                "liquid",
                "send-asset",
                "--wallet-name",
                "default",
                "--address",
                "lq1x",
                "--amount",
                "100",
                "--asset-id",
                "abc",
                "--asset-ticker",
                "USDt",
            ],
        )
        assert both.exit_code != 0
        assert "exactly one" in both.output.lower()

    def test_send_asset_unknown_ticker(self, runner):
        """Unknown ticker produces a helpful usage error, never reaches the tool."""
        _import_wallet(runner)
        result = runner.invoke(
            cli,
            [
                "liquid",
                "send-asset",
                "--wallet-name",
                "default",
                "--address",
                "lq1x",
                "--amount",
                "100",
                "--asset-ticker",
                "NOTAREAL",
            ],
        )
        assert result.exit_code != 0
        assert "unknown ticker" in result.output.lower()

    def test_send_asset_amount_must_be_positive(self, runner):
        """Non-positive --amount is rejected before ticker resolution."""
        _import_wallet(runner)
        result = runner.invoke(
            cli,
            [
                "liquid",
                "send-asset",
                "--wallet-name",
                "default",
                "--address",
                "lq1x",
                "--amount",
                "0",
                "--asset-ticker",
                "USDt",
            ],
        )
        assert result.exit_code != 0
        assert "positive" in result.output.lower()


# Liquid descriptor commands


class TestLiquidDescriptorCli:
    def test_export_descriptor(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "liquid", "export-descriptor"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "descriptor" in data

    def test_import_descriptor(self, runner):
        _import_wallet(runner)
        exp = runner.invoke(cli, ["--format", "json", "liquid", "export-descriptor"])
        descriptor = json.loads(exp.output)["descriptor"]
        result = runner.invoke(
            cli,
            [
                "--format",
                "json",
                "liquid",
                "import-descriptor",
                "--descriptor",
                descriptor,
                "--wallet-name",
                "watch_only",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["watch_only"] is True

    def test_wallet_import_descriptor_removed(self, runner):
        result = runner.invoke(cli, ["wallet", "import-descriptor", "--help"])
        assert result.exit_code != 0
        assert "no such command" in result.output.lower()

    def test_wallet_export_descriptor_removed(self, runner):
        result = runner.invoke(cli, ["wallet", "export-descriptor", "--help"])
        assert result.exit_code != 0
        assert "no such command" in result.output.lower()


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
        with patch("aqua.cli.btc.btc_send", return_value={"txid": "fake"}) as mock_send:
            result = runner.invoke(
                cli,
                [
                    "--format",
                    "json",
                    "btc",
                    "send",
                    "--wallet-name",
                    "default",
                    "--address",
                    "bc1qxy",
                    "--amount",
                    "1000",
                ],
                env=_cli_env(AQUA_PASSWORD="s3cret"),
            )
        assert result.exit_code == 0, result.output
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["password"] == "s3cret"


# BTC descriptor commands


class TestBtcDescriptorCli:
    def test_btc_export_descriptor(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "btc", "export-descriptor"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        for key in (
            "external_descriptor", "change_descriptor", "xpub",
            "fingerprint", "derivation_path", "note",
        ):
            assert key in data

    def test_btc_export_descriptor_includes_note(self, runner):
        _import_wallet(runner)
        result = runner.invoke(cli, ["--format", "json", "btc", "export-descriptor"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "lw_export_descriptor" in data["note"]

    def test_btc_import_descriptor_minimal(self, runner):
        _import_wallet(runner)
        exp = runner.invoke(cli, ["--format", "json", "btc", "export-descriptor"])
        ext_descriptor = json.loads(exp.output)["external_descriptor"]
        result = runner.invoke(
            cli,
            [
                "--format",
                "json",
                "btc",
                "import-descriptor",
                "--descriptor",
                ext_descriptor,
                "--wallet-name",
                "watch_btc",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["watch_only"] is True

    def test_btc_import_descriptor_explicit_change(self, runner):
        _import_wallet(runner)
        exp = runner.invoke(cli, ["--format", "json", "btc", "export-descriptor"])
        exp_data = json.loads(exp.output)
        ext_descriptor = exp_data["external_descriptor"]
        change_descriptor = exp_data["change_descriptor"]
        result = runner.invoke(
            cli,
            [
                "--format",
                "json",
                "btc",
                "import-descriptor",
                "--descriptor",
                ext_descriptor,
                "--change-descriptor",
                change_descriptor,
                "--wallet-name",
                "watch_btc2",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["watch_only"] is True

    def test_btc_import_descriptor_missing_descriptor_fails(self, runner):
        result = runner.invoke(
            cli,
            ["btc", "import-descriptor", "--wallet-name", "watch_btc"],
        )
        assert result.exit_code == 2

    def test_btc_import_descriptor_help_mentions_liquid(self, runner):
        result = runner.invoke(cli, ["btc", "import-descriptor", "--help"])
        assert result.exit_code == 0
        # Click may wrap long lines; normalize whitespace before checking.
        normalized = " ".join(result.output.split())
        assert "aqua liquid import-descriptor" in normalized

    def test_btc_export_descriptor_help_mentions_liquid(self, runner):
        result = runner.invoke(cli, ["btc", "export-descriptor", "--help"])
        assert result.exit_code == 0
        # Click may wrap long lines; normalize whitespace before checking.
        normalized = " ".join(result.output.split())
        assert "aqua liquid export-descriptor" in normalized


# Lightning commands


class TestLightningCommands:
    def test_status_missing_swap(self, runner):
        """Status for nonexistent swap should error."""
        result = runner.invoke(
            cli,
            ["--format", "json", "lightning", "status", "--swap-id", "nonexistent"],
        )
        assert result.exit_code == 1

    def test_send_help_mentions_ln_address_and_amount_sats(self, runner):
        result = runner.invoke(cli, ["lightning", "send", "--help"])
        assert result.exit_code == 0
        normalized = " ".join(result.output.split())
        assert "--ln-address" in result.output
        assert "--amount-sats" in result.output
        assert "Mutually exclusive with --invoice" in normalized

    def test_send_requires_exactly_one_of_invoice_or_ln_address(self, runner):
        result = runner.invoke(cli, ["lightning", "send"])
        assert result.exit_code != 0
        assert "Provide exactly one of --invoice or --ln-address" in result.output

    def test_send_rejects_both_invoice_and_ln_address(self, runner):
        result = runner.invoke(
            cli,
            [
                "lightning",
                "send",
                "--invoice",
                "lnbc500u1ptest_valid_invoice",
                "--ln-address",
                "alice@getalby.com",
            ],
        )
        assert result.exit_code != 0
        assert "Provide exactly one of --invoice or --ln-address" in result.output

    def test_send_requires_amount_sats_with_ln_address(self, runner):
        result = runner.invoke(
            cli,
            ["lightning", "send", "--ln-address", "alice@getalby.com"],
        )
        assert result.exit_code != 0
        assert "--amount-sats is required when using --ln-address" in result.output

    def test_send_ln_address_passes_amount_sats_to_tool(self, runner):
        with patch("aqua.cli.lightning.lightning_send") as mock_send:
            mock_send.return_value = {
                "swap_id": "boltz_123",
                "lockup_txid": "abc123",
                "status": "processing",
                "amount": 1020,
            }
            result = runner.invoke(
                cli,
                [
                    "--format",
                    "json",
                    "lightning",
                    "send",
                    "--ln-address",
                    "alice@getalby.com",
                    "--amount-sats",
                    "1000",
                    "--wallet-name",
                    "tuna",
                ],
                env=_cli_env(),
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["swap_id"] == "boltz_123"
        mock_send.assert_called_once_with(
            invoice="alice@getalby.com",
            wallet_name="tuna",
            password=None,
            amount_sats=1000,
        )

    def test_send_uppercase_invoice_is_normalized_by_tool_layer(self, runner):
        uppercase_invoice = (
            "LNBC4U1P4PPSS2DQVWPE82ETZVYCSNP4QGT72S92AK77WSSZT7DQS8SHKJY0RE5R8FS8TNSAY4ZG7GPJEKRR7"
            "PP5ZZZWZYNN2TDA7ZCKPLFZZWYPYJRJYJDYRU6PQPV8LJKGVX4Z2NGSSP58M0Y52ZKMU54RT7R8AJPLFZYYS"
            "JLV7NFXMNTJ3EA4T9W7UW7PE7S9QYYSGQCQZP2XQYZ5VQ4LZJZEMW8MNUFCAHKTC608TDVML9PVUVW7JXEYSL"
            "TAFET3FG3HKZ658N93F6YMH2ZS9GXVLUVYEAH4K8VQLMAH6K7SHL7C0TCCW59USPDRH0FJ"
        )
        with patch("aqua.cli.lightning.handle_password_retry") as mock_retry:
            mock_retry.return_value = {
                "swap_id": "boltz_456",
                "lockup_txid": "def456",
                "status": "processing",
                "amount": 420,
            }
            result = runner.invoke(
                cli,
                [
                    "--format",
                    "json",
                    "lightning",
                    "send",
                    "--invoice",
                    uppercase_invoice,
                    "--wallet-name",
                    "tuna",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["swap_id"] == "boltz_456"
        called_tool = mock_retry.call_args.args[0]
        called_payload = mock_retry.call_args.args[1]
        assert called_tool.__name__ == "lightning_send"
        assert called_payload["invoice"] == uppercase_invoice


# ---------------------------------------------------------------------------
# Changelly CLI
# ---------------------------------------------------------------------------


class _FakeChangellyManager:
    """Stand-in for ChangellyManager — records calls, returns canned responses."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.currencies_response = ["btc", "lusdt", "usdt20", "usdtrx"]
        self.quote_response = {
            "id": "rate1",
            "result": "0.99",
            "amountFrom": "100",
            "amountTo": "99",
            "networkFee": "1",
            "expiredAt": 1_900_000_000,
        }
        self.send_response = None
        self.receive_response = None
        self.status_response = None

    def list_currencies(self):
        self.calls.append(("list_currencies", {}))
        return self.currencies_response

    def fixed_quote(self, from_asset, to_asset, amount_from=None, amount_to=None):
        self.calls.append(("fixed_quote", {
            "from_asset": from_asset, "to_asset": to_asset,
            "amount_from": amount_from, "amount_to": amount_to,
        }))
        return self.quote_response

    def send_swap(self, **kwargs):
        from aqua.changelly import ChangellySwap

        self.calls.append(("send_swap", kwargs))
        if self.send_response is not None:
            return self.send_response
        return ChangellySwap(
            order_id="ord_send",
            swap_type="fixed",
            direction="send",
            from_asset="lusdt",
            to_asset=f"usdt-{kwargs['external_network']}",
            settle_address=kwargs["settle_address"],
            deposit_address="lq1qdeposit",
            refund_address="lq1qrefund",
            wallet_name=kwargs["wallet_name"],
            status="new",
            created_at="2026-05-08T12:00:00+00:00",
            amount_from=kwargs.get("amount_from"),
            amount_to=kwargs.get("amount_to", "99"),
            deposit_hash="lqtxid" + ("0" * 58),
            track_url="https://changelly.com/track/ord_send",
        )

    def receive_swap(self, **kwargs):
        from aqua.changelly import ChangellySwap

        self.calls.append(("receive_swap", kwargs))
        if self.receive_response is not None:
            return self.receive_response
        return ChangellySwap(
            order_id="ord_recv",
            swap_type="variable",
            direction="receive",
            from_asset=f"usdt-{kwargs['external_network']}",
            to_asset="lusdt",
            settle_address="lq1qreceive",
            deposit_address="TXdepositAddr",
            refund_address=kwargs.get("external_refund_address"),
            wallet_name=kwargs["wallet_name"],
            status="new",
            created_at="2026-05-08T12:00:00+00:00",
            track_url="https://changelly.com/track/ord_recv",
        )

    def status(self, order_id):
        self.calls.append(("status", {"order_id": order_id}))
        if self.status_response is not None:
            return {**self.status_response, "order_id": order_id}
        return {
            "order_id": order_id,
            "status": "finished",
            "is_final": True,
            "is_success": True,
            "is_failed": False,
        }


@pytest.fixture
def changelly_manager():
    """Inject a fake ChangellyManager into the global tool layer."""
    import aqua.tools as tools_module

    fake = _FakeChangellyManager()
    saved = tools_module._changelly_manager
    tools_module._changelly_manager = fake
    try:
        yield fake
    finally:
        tools_module._changelly_manager = saved


class TestChangellyCurrencies:
    def test_currencies_returns_list(self, runner, changelly_manager):
        result = runner.invoke(cli, ["--format", "json", "changelly", "currencies"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 4
        assert "lusdt" in data["currencies"]


class TestChangellyQuote:
    def test_quote_requires_exactly_one_amount(self, runner):
        result = runner.invoke(
            cli,
            ["changelly", "quote", "--external-network", "tron"],
        )
        assert result.exit_code == 2

    def test_quote_rejects_both_amounts(self, runner):
        result = runner.invoke(
            cli,
            ["changelly", "quote", "--external-network", "tron",
             "--amount-from", "100", "--amount-to", "99"],
        )
        assert result.exit_code == 2

    def test_quote_send_direction(self, runner, changelly_manager):
        result = runner.invoke(
            cli,
            ["--format", "json", "changelly", "quote",
             "--external-network", "tron", "--amount-from", "100"],
        )
        assert result.exit_code == 0
        last = changelly_manager.calls[-1]
        assert last[0] == "fixed_quote"
        assert last[1]["from_asset"] == "lusdt"
        assert last[1]["to_asset"] == "usdtrx"

    def test_quote_receive_direction(self, runner, changelly_manager):
        runner.invoke(
            cli,
            ["changelly", "quote", "--external-network", "ethereum",
             "--direction", "receive", "--amount-from", "100"],
        )
        last = changelly_manager.calls[-1]
        assert last[1]["from_asset"] == "usdt20"
        assert last[1]["to_asset"] == "lusdt"

    def test_quote_rejects_unsupported_network(self, runner):
        result = runner.invoke(
            cli,
            ["changelly", "quote", "--external-network", "avalanche",
             "--amount-from", "100"],
        )
        assert result.exit_code == 2  # Click choice validation


class TestChangellySend:
    def test_send_with_yes_skips_prompt(self, runner, changelly_manager):
        result = runner.invoke(
            cli,
            ["--format", "json", "changelly", "send",
             "--external-network", "tron",
             "--settle-address", "TXrecv",
             "--amount-from", "100",
             "--yes"],
            env=_cli_env(),
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["order_id"] == "ord_send"
        assert data["deposit_hash"].startswith("lqtxid")
        send_call = next(c for c in changelly_manager.calls if c[0] == "send_swap")
        assert send_call[1]["external_network"] == "tron"
        assert send_call[1]["amount_from"] == "100"
        assert send_call[1]["settle_address"] == "TXrecv"

    def test_send_rejects_unsupported_network(self, runner):
        result = runner.invoke(
            cli,
            ["changelly", "send",
             "--external-network", "avalanche",
             "--settle-address", "0xfoo",
             "--amount-from", "100",
             "--yes"],
        )
        assert result.exit_code == 2

    def test_send_with_amount_to_uses_recipient_amount(self, runner, changelly_manager):
        """--amount-to specifies what the recipient receives (fees from sender)."""
        result = runner.invoke(
            cli,
            ["--format", "json", "changelly", "send",
             "--external-network", "tron",
             "--settle-address", "TXrecipient",
             "--amount-to", "50",
             "--yes"],
            env=_cli_env(),
        )
        assert result.exit_code == 0, result.output
        send_call = next(c for c in changelly_manager.calls if c[0] == "send_swap")
        assert send_call[1].get("amount_to") == "50"
        assert send_call[1].get("amount_from") is None

    def test_send_requires_exactly_one_amount(self, runner):
        # Neither --amount-from nor --amount-to
        r1 = runner.invoke(
            cli,
            ["changelly", "send",
             "--external-network", "tron",
             "--settle-address", "TXabc",
             "--yes"],
        )
        assert r1.exit_code != 0
        # Both --amount-from and --amount-to
        r2 = runner.invoke(
            cli,
            ["changelly", "send",
             "--external-network", "tron",
             "--settle-address", "TXabc",
             "--amount-from", "100",
             "--amount-to", "99",
             "--yes"],
        )
        assert r2.exit_code != 0


class TestChangellyReceive:
    def test_receive_returns_deposit_address(self, runner, changelly_manager):
        result = runner.invoke(
            cli,
            ["--format", "json", "changelly", "receive",
             "--external-network", "tron",
             "--external-refund-address", "TXrefund",
             "--amount-from", "50"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["deposit_address"] == "TXdepositAddr"
        assert data["refund_address"] == "TXrefund"
        recv_call = next(c for c in changelly_manager.calls if c[0] == "receive_swap")
        assert recv_call[1]["external_network"] == "tron"

    def test_receive_with_amount_to(self, runner, changelly_manager):
        """--amount-to specifies what should arrive in the wallet."""
        result = runner.invoke(
            cli,
            ["--format", "json", "changelly", "receive",
             "--external-network", "tron",
             "--external-refund-address", "TXrefund",
             "--amount-to", "45"],
        )
        assert result.exit_code == 0, result.output
        recv_call = next(c for c in changelly_manager.calls if c[0] == "receive_swap")
        assert recv_call[1].get("amount_to") == "45"
        assert recv_call[1].get("amount_from") is None

    def test_receive_requires_exactly_one_amount(self, runner):
        # Neither
        r1 = runner.invoke(
            cli,
            ["changelly", "receive",
             "--external-network", "tron"],
        )
        assert r1.exit_code != 0
        # Both
        r2 = runner.invoke(
            cli,
            ["changelly", "receive",
             "--external-network", "tron",
             "--amount-from", "50",
             "--amount-to", "45"],
        )
        assert r2.exit_code != 0


class TestChangellyStatus:
    def test_status_passes_order_id(self, runner, changelly_manager):
        result = runner.invoke(
            cli,
            ["--format", "json", "changelly", "status", "--order-id", "ord_xyz"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["order_id"] == "ord_xyz"
        assert data["is_final"] is True


# ---------------------------------------------------------------------------
# SideSwap CLI
# ---------------------------------------------------------------------------


class _FakePegManager:
    """Stand-in for SideSwapPegManager — records calls, returns canned dicts."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.server_status_response: dict = {
            "min_peg_in_amount": 1286,
            "min_peg_out_amount": 100_000,
            "server_fee_percent_peg_in": 0.1,
            "server_fee_percent_peg_out": 0.1,
            "peg_in_wallet_balance": 50_000_000,
            "peg_out_wallet_balance": 200_000_000,
        }
        self.peg_quote_response: dict = {
            "send_amount": 100_000,
            "recv_amount": 99_900,
            "fee_amount": 100,
            "peg_in": True,
        }
        self.peg_in_response = None  # set per-test
        self.peg_out_response = None
        self.status_response: dict = {
            "order_id": "ord_test",
            "peg_in": True,
            "status": "pending",
            "amount": None,
            "expected_recv": None,
            "wallet_name": "default",
            "network": "mainnet",
            "peg_addr": "bc1qdeposit",
            "recv_addr": "lq1qreceive",
            "created_at": "2026-05-08T12:00:00+00:00",
        }

    def get_server_status(self, network):
        self.calls.append(("get_server_status", {"network": network}))
        return self.server_status_response

    def quote_peg(self, amount, peg_in, network):
        self.calls.append(("quote_peg", {"amount": amount, "peg_in": peg_in, "network": network}))
        return self.peg_quote_response

    def peg_in(self, wallet_name="default", password=None):
        self.calls.append(("peg_in", {"wallet_name": wallet_name, "password": password}))
        if self.peg_in_response is None:
            from aqua.sideswap import SideSwapPeg

            return SideSwapPeg(
                order_id="ord_in",
                peg_in=True,
                peg_addr="bc1qdeposit",
                recv_addr="lq1qreceive",
                amount=None,
                expected_recv=None,
                wallet_name=wallet_name,
                network="mainnet",
                status="pending",
                created_at="2026-05-08T12:00:00+00:00",
            )
        return self.peg_in_response

    def peg_out(self, wallet_name, amount, btc_address, password=None):
        self.calls.append(
            ("peg_out", {
                "wallet_name": wallet_name,
                "amount": amount,
                "btc_address": btc_address,
                "password": password,
            })
        )
        if self.peg_out_response is None:
            from aqua.sideswap import SideSwapPeg

            return SideSwapPeg(
                order_id="ord_out",
                peg_in=False,
                peg_addr="VJLdeposit",
                recv_addr=btc_address,
                amount=amount,
                expected_recv=amount - 100,
                wallet_name=wallet_name,
                network="mainnet",
                status="processing",
                created_at="2026-05-08T12:00:00+00:00",
                lockup_txid="dead" * 16,
            )
        return self.peg_out_response

    def status(self, order_id):
        self.calls.append(("status", {"order_id": order_id}))
        return {**self.status_response, "order_id": order_id}


class _FakeSwapManager:
    """Stand-in for SideSwapSwapManager."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.execute_response = None
        self.status_response: dict = {
            "order_id": "mkt_42",
            "submit_id": "42",
            "send_asset": "lbtc",
            "send_amount": 100_000,
            "recv_asset": "usdt",
            "recv_amount": 9_500_000,
            "price": 95.0,
            "wallet_name": "default",
            "network": "mainnet",
            "status": "broadcast",
            "created_at": "2026-05-08T12:00:00+00:00",
            "txid": "ee" * 32,
        }

    def execute_swap(self, asset_id, send_amount, wallet_name="default",
                     password=None, send_bitcoins=True, flexible_small_amount=False, **_):
        self.calls.append(
            ("execute_swap", {
                "asset_id": asset_id,
                "send_amount": send_amount,
                "wallet_name": wallet_name,
                "password": password,
                "send_bitcoins": send_bitcoins,
                "flexible_small_amount": flexible_small_amount,
            })
        )
        if self.execute_response is None:
            from aqua.sideswap import SideSwapSwap

            return SideSwapSwap(
                order_id="mkt_42",
                submit_id="42",
                send_asset="lbtc" if send_bitcoins else asset_id,
                send_amount=send_amount,
                recv_asset=asset_id if send_bitcoins else "lbtc",
                recv_amount=9_500_000,
                price=95.0,
                wallet_name=wallet_name,
                network="mainnet",
                status="broadcast",
                created_at="2026-05-08T12:00:00+00:00",
                txid="ee" * 32,
            )
        return self.execute_response

    def status(self, order_id):
        self.calls.append(("status", {"order_id": order_id}))
        return {**self.status_response, "order_id": order_id}


@pytest.fixture
def sideswap_managers():
    """Inject fake SideSwap managers into the global tool layer."""
    import aqua.tools as tools_module

    peg = _FakePegManager()
    swap = _FakeSwapManager()
    saved_peg = tools_module._sideswap_peg_manager
    saved_swap = tools_module._sideswap_swap_manager
    tools_module._sideswap_peg_manager = peg
    tools_module._sideswap_swap_manager = swap
    try:
        yield peg, swap
    finally:
        tools_module._sideswap_peg_manager = saved_peg
        tools_module._sideswap_swap_manager = saved_swap


_SKIP_SIDESWAP_DISABLED = pytest.mark.skip(
    reason="SideSwap ships disabled-by-default; CLI commands hidden via feature flag."
)


@_SKIP_SIDESWAP_DISABLED
class TestSideSwapServerStatus:
    def test_status_uses_default_network(self, runner, sideswap_managers):
        peg, _ = sideswap_managers
        result = runner.invoke(cli, ["--format", "json", "sideswap", "status"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["min_peg_in_amount"] == 1286
        assert peg.calls[-1] == ("get_server_status", {"network": "mainnet"})

    def test_status_passes_testnet_flag(self, runner, sideswap_managers):
        peg, _ = sideswap_managers
        runner.invoke(cli, ["sideswap", "status", "--network", "testnet"])
        assert peg.calls[-1] == ("get_server_status", {"network": "testnet"})


@_SKIP_SIDESWAP_DISABLED
class TestSideSwapRecommend:
    def test_recommend_btc_to_lbtc(self, runner, sideswap_managers):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideswap", "recommend",
             "--amount", "10000000", "--direction", "btc_to_lbtc"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["recommendation"] in ("peg", "swap", "either")
        assert data["amount"] == 10_000_000

    def test_recommend_lbtc_to_btc_recommends_peg(self, runner, sideswap_managers):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideswap", "recommend",
             "--amount", "200000", "--direction", "lbtc_to_btc"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["recommendation"] == "peg"

    def test_recommend_rejects_bad_direction(self, runner):
        result = runner.invoke(
            cli, ["sideswap", "recommend", "--amount", "1000", "--direction", "sideways"],
        )
        assert result.exit_code != 0


@_SKIP_SIDESWAP_DISABLED
class TestSideSwapPegQuote:
    def test_peg_quote_default_is_peg_in(self, runner, sideswap_managers):
        peg, _ = sideswap_managers
        result = runner.invoke(
            cli, ["--format", "json", "sideswap", "peg-quote", "--amount", "100000"]
        )
        assert result.exit_code == 0
        last_call = peg.calls[-1]
        assert last_call[0] == "quote_peg"
        assert last_call[1]["peg_in"] is True

    def test_peg_quote_peg_out_flag(self, runner, sideswap_managers):
        peg, _ = sideswap_managers
        runner.invoke(
            cli, ["sideswap", "peg-quote", "--amount", "200000", "--peg-out"]
        )
        last_call = peg.calls[-1]
        assert last_call[0] == "quote_peg"
        assert last_call[1]["peg_in"] is False
        assert last_call[1]["amount"] == 200_000


@_SKIP_SIDESWAP_DISABLED
class TestSideSwapPegIn:
    def test_peg_in_returns_deposit_address(self, runner, sideswap_managers):
        result = runner.invoke(
            cli, ["--format", "json", "sideswap", "peg-in"],
            env=_cli_env(),
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["peg_addr"] == "bc1qdeposit"
        assert data["recv_addr"] == "lq1qreceive"
        assert "order_id" in data
        assert "message" in data

    def test_peg_in_passes_wallet_name(self, runner, sideswap_managers):
        peg, _ = sideswap_managers
        runner.invoke(
            cli, ["sideswap", "peg-in", "--wallet-name", "cold"],
            env=_cli_env(),
        )
        peg_in_call = next(c for c in peg.calls if c[0] == "peg_in")
        assert peg_in_call[1]["wallet_name"] == "cold"


@_SKIP_SIDESWAP_DISABLED
class TestSideSwapPegOut:
    def test_peg_out_returns_lockup_txid(self, runner, sideswap_managers):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideswap", "peg-out",
             "--amount", "200000", "--btc-address", "bc1qdest",
             "--wallet-name", "default"],
            env=_cli_env(),
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["lockup_txid"] == "dead" * 16
        assert data["recv_addr"] == "bc1qdest"
        assert data["amount"] == 200_000

    def test_peg_out_amount_must_be_positive(self, runner):
        result = runner.invoke(
            cli,
            ["sideswap", "peg-out", "--amount", "0",
             "--btc-address", "bc1q", "--wallet-name", "default"],
        )
        assert result.exit_code == 2

    def test_peg_out_requires_btc_address(self, runner):
        result = runner.invoke(
            cli,
            ["sideswap", "peg-out", "--amount", "200000", "--wallet-name", "default"],
        )
        assert result.exit_code == 2


@_SKIP_SIDESWAP_DISABLED
class TestSideSwapPegStatus:
    def test_peg_status_passes_order_id(self, runner, sideswap_managers):
        peg, _ = sideswap_managers
        result = runner.invoke(
            cli, ["--format", "json", "sideswap", "peg-status", "--order-id", "ord_xyz"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["order_id"] == "ord_xyz"
        assert peg.calls[-1] == ("status", {"order_id": "ord_xyz"})


@_SKIP_SIDESWAP_DISABLED
class TestSideSwapAssets:
    def test_assets_invokes_quote_subscription(self, runner, sideswap_managers):
        # The list-assets tool hits the live WS — patch fetch_assets directly
        with patch("aqua.sideswap.fetch_assets") as fetch:
            fetch.return_value = []
            result = runner.invoke(
                cli, ["--format", "json", "sideswap", "assets"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["network"] == "mainnet"
        assert data["count"] == 0


@_SKIP_SIDESWAP_DISABLED
class TestSideSwapQuote:
    def test_quote_requires_send_or_recv(self, runner):
        result = runner.invoke(
            cli,
            ["sideswap", "quote", "--asset-id", "a" * 64],
        )
        assert result.exit_code == 2

    def test_quote_rejects_both_send_and_recv(self, runner):
        result = runner.invoke(
            cli,
            ["sideswap", "quote", "--asset-id", "a" * 64,
             "--send-amount", "1000", "--recv-amount", "1000"],
        )
        assert result.exit_code == 2

    def test_quote_requires_exactly_one_of_id_or_ticker(self, runner):
        result = runner.invoke(
            cli,
            ["sideswap", "quote", "--send-amount", "1000"],
        )
        assert result.exit_code == 2

    def test_quote_resolves_ticker_to_asset_id(self, runner, sideswap_managers):
        with patch("aqua.sideswap.fetch_swap_quote") as fetch:
            from aqua.sideswap import SideSwapPriceQuote

            fetch.return_value = SideSwapPriceQuote(
                asset_id="ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2",
                send_bitcoins=True,
                send_amount=100_000,
                recv_amount=9_500_000,
                price=95.0,
                fixed_fee=100,
            )
            result = runner.invoke(
                cli,
                ["--format", "json", "sideswap", "quote",
                 "--asset-ticker", "USDt", "--send-amount", "100000"],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["price"] == 95.0
        # Ticker resolved to USDt's asset_id
        called_asset = fetch.call_args.kwargs["asset_id"]
        assert called_asset.startswith("ce091c99")

    def test_quote_unknown_ticker_errors(self, runner):
        result = runner.invoke(
            cli,
            ["sideswap", "quote", "--asset-ticker", "XYZNOTAREALTOKEN", "--send-amount", "1000"],
        )
        assert result.exit_code == 2


@_SKIP_SIDESWAP_DISABLED
class TestSideSwapSwap:
    def test_swap_with_yes_flag_no_prompt(self, runner, sideswap_managers):
        _import_wallet(runner)  # so the network resolver finds a wallet
        _, swap = sideswap_managers
        result = runner.invoke(
            cli,
            ["--format", "json", "sideswap", "swap",
             "--asset-ticker", "USDt", "--amount", "100000", "--yes"],
            env=_cli_env(),
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "broadcast"
        assert data["txid"] == "ee" * 32
        # Manager was called with send_bitcoins=True (default direction)
        execute_calls = [c for c in swap.calls if c[0] == "execute_swap"]
        assert len(execute_calls) == 1
        assert execute_calls[0][1]["send_bitcoins"] is True
        assert execute_calls[0][1]["send_amount"] == 100_000

    def test_swap_reverse_flag(self, runner, sideswap_managers):
        _import_wallet(runner)
        _, swap = sideswap_managers
        runner.invoke(
            cli,
            ["sideswap", "swap", "--asset-ticker", "USDt",
             "--amount", "9500000", "--reverse", "--yes"],
            env=_cli_env(),
        )
        execute_calls = [c for c in swap.calls if c[0] == "execute_swap"]
        assert execute_calls[-1][1]["send_bitcoins"] is False

    def test_swap_flexible_flag_passed_through(self, runner, sideswap_managers):
        _import_wallet(runner)
        _, swap = sideswap_managers
        result = runner.invoke(
            cli,
            ["--format", "json", "sideswap", "swap",
             "--asset-ticker", "USDt", "--amount", "5000", "--flexible", "--yes"],
            env=_cli_env(),
        )
        assert result.exit_code == 0, result.output
        execute_calls = [c for c in swap.calls if c[0] == "execute_swap"]
        assert len(execute_calls) == 1
        assert execute_calls[0][1]["flexible_small_amount"] is True

    def test_swap_default_strict_no_flexible(self, runner, sideswap_managers):
        _import_wallet(runner)
        _, swap = sideswap_managers
        runner.invoke(
            cli,
            ["sideswap", "swap", "--asset-ticker", "USDt", "--amount", "100000", "--yes"],
            env=_cli_env(),
        )
        execute_calls = [c for c in swap.calls if c[0] == "execute_swap"]
        # Default debe ser False (estricto)
        assert execute_calls[-1][1].get("flexible_small_amount", False) is False

    def test_swap_amount_must_be_positive(self, runner):
        result = runner.invoke(
            cli,
            ["sideswap", "swap", "--asset-ticker", "USDt", "--amount", "0", "--yes"],
        )
        assert result.exit_code == 2

    def test_swap_status_passes_order_id(self, runner, sideswap_managers):
        _, swap = sideswap_managers
        result = runner.invoke(
            cli,
            ["--format", "json", "sideswap", "swap-status", "--order-id", "mkt_77"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["order_id"] == "mkt_77"
        assert swap.calls[-1] == ("status", {"order_id": "mkt_77"})


# ---------------------------------------------------------------------------
# SideShift CLI
# ---------------------------------------------------------------------------


class _FakeSideShiftManager:
    """Stand-in for SideShiftManager — records calls, returns canned dicts."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.coins_response = [
            {"coin": "BTC", "name": "Bitcoin", "networks": ["bitcoin", "liquid"]}
        ]
        self.pair_response = {
            "rate": "20000",
            "min": "0.0001",
            "max": "1.0",
            "depositCoin": "USDT",
            "settleCoin": "BTC",
            "depositNetwork": "tron",
            "settleNetwork": "bitcoin",
        }
        self.quote_response = {
            "id": "q_test",
            "depositAmount": "100",
            "settleAmount": "0.005",
            "rate": "20000",
            "expiresAt": "2026-05-08T12:15:00Z",
        }
        self.send_response = None
        self.receive_response = None
        self.status_response = None

    def list_coins(self):
        self.calls.append(("list_coins", {}))
        return self.coins_response

    def pair_info(self, from_coin, from_network, to_coin, to_network, amount=None):
        self.calls.append(("pair_info", {
            "from_coin": from_coin, "from_network": from_network,
            "to_coin": to_coin, "to_network": to_network, "amount": amount,
        }))
        return self.pair_response

    def quote(self, **kwargs):
        self.calls.append(("quote", kwargs))
        return self.quote_response

    def send_shift(self, **kwargs):
        from aqua.sideshift import SideShiftShift

        self.calls.append(("send_shift", kwargs))
        if self.send_response is not None:
            return self.send_response
        return SideShiftShift(
            shift_id="shift_send",
            shift_type="fixed",
            direction="send",
            deposit_coin=kwargs["deposit_coin"].upper(),
            deposit_network=kwargs["deposit_network"].lower(),
            settle_coin=kwargs["settle_coin"].upper(),
            settle_network=kwargs["settle_network"].lower(),
            settle_address=kwargs["settle_address"],
            deposit_address="lq1qdeposit",
            refund_address="lq1qrefund",
            wallet_name=kwargs["wallet_name"],
            status="waiting",
            created_at="2026-05-08T12:00:00+00:00",
            deposit_amount=kwargs.get("deposit_amount"),
            settle_amount=kwargs.get("settle_amount"),
            deposit_hash="lqtxid" + ("0" * 58),
        )

    def receive_shift(self, **kwargs):
        from aqua.sideshift import SideShiftShift

        self.calls.append(("receive_shift", kwargs))
        if self.receive_response is not None:
            return self.receive_response
        return SideShiftShift(
            shift_id="shift_recv",
            shift_type="variable",
            direction="receive",
            deposit_coin=kwargs["deposit_coin"].upper(),
            deposit_network=kwargs["deposit_network"].lower(),
            settle_coin=kwargs["settle_coin"].upper(),
            settle_network=kwargs["settle_network"].lower(),
            settle_address="lq1qreceive",
            deposit_address="TXdepositAddr",
            refund_address=kwargs.get("external_refund_address"),
            wallet_name=kwargs["wallet_name"],
            status="waiting",
            created_at="2026-05-08T12:00:00+00:00",
            deposit_min="10",
            deposit_max="10000",
        )

    def status(self, shift_id):
        self.calls.append(("status", {"shift_id": shift_id}))
        if self.status_response is not None:
            return {**self.status_response, "shift_id": shift_id}
        return {
            "shift_id": shift_id,
            "status": "settled",
            "is_final": True,
            "is_success": True,
            "is_failed": False,
        }


@pytest.fixture
def sideshift_manager():
    """Inject a fake SideShiftManager into the global tool layer."""
    import aqua.tools as tools_module

    fake = _FakeSideShiftManager()
    saved = tools_module._sideshift_manager
    tools_module._sideshift_manager = fake
    try:
        yield fake
    finally:
        tools_module._sideshift_manager = saved


class TestSideShiftCoins:
    def test_coins_returns_list(self, runner, sideshift_manager):
        result = runner.invoke(cli, ["--format", "json", "sideshift", "coins"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1
        assert data["coins"][0]["coin"] == "BTC"
        assert sideshift_manager.calls[-1][0] == "list_coins"


class TestSideShiftPairInfo:
    def test_pair_info_passes_args(self, runner, sideshift_manager):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideshift", "pair-info",
             "--from-coin", "USDT", "--from-network", "tron",
             "--to-coin", "BTC", "--to-network", "bitcoin",
             "--amount", "100"],
        )
        assert result.exit_code == 0
        last = sideshift_manager.calls[-1]
        assert last[0] == "pair_info"
        assert last[1] == {
            "from_coin": "USDT", "from_network": "tron",
            "to_coin": "BTC", "to_network": "bitcoin", "amount": "100",
        }


class TestSideShiftQuote:
    def test_quote_requires_exactly_one_amount(self, runner):
        result = runner.invoke(
            cli,
            ["sideshift", "quote",
             "--deposit-coin", "USDT", "--deposit-network", "liquid",
             "--settle-coin", "BTC", "--settle-network", "bitcoin"],
        )
        assert result.exit_code == 2

    def test_quote_rejects_both_amounts(self, runner):
        result = runner.invoke(
            cli,
            ["sideshift", "quote",
             "--deposit-coin", "USDT", "--deposit-network", "liquid",
             "--settle-coin", "BTC", "--settle-network", "bitcoin",
             "--deposit-amount", "100", "--settle-amount", "0.001"],
        )
        assert result.exit_code == 2

    def test_quote_passes_deposit_amount(self, runner, sideshift_manager):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideshift", "quote",
             "--deposit-coin", "USDT", "--deposit-network", "liquid",
             "--settle-coin", "BTC", "--settle-network", "bitcoin",
             "--deposit-amount", "100"],
        )
        assert result.exit_code == 0
        last = sideshift_manager.calls[-1]
        assert last[0] == "quote"
        assert last[1]["deposit_amount"] == "100"
        assert last[1]["settle_amount"] is None


class TestSideShiftRecommend:
    def test_btc_to_lbtc_recommends_sideswap(self, runner):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideshift", "recommend",
             "--from-coin", "btc", "--from-network", "bitcoin",
             "--to-coin", "btc", "--to-network", "liquid"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["recommendation"] == "sideswap"

    def test_usdt_liquid_to_usdt_tron_recommends_sideshift(self, runner):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideshift", "recommend",
             "--from-coin", "usdt", "--from-network", "liquid",
             "--to-coin", "usdt", "--to-network", "tron"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["recommendation"] == "sideshift"


class TestSideShiftSend:
    def test_send_with_yes_flag_skips_quote_prompt(self, runner, sideshift_manager):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideshift", "send",
             "--deposit-coin", "btc", "--deposit-network", "liquid",
             "--settle-coin", "usdt", "--settle-network", "tron",
             "--settle-address", "TXYZ",
             "--deposit-amount", "0.0005",
             "--yes"],
            env=_cli_env(),
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["shift_id"] == "shift_send"
        # Manager was called with the right kwargs
        send_call = next(c for c in sideshift_manager.calls if c[0] == "send_shift")
        assert send_call[1]["deposit_amount"] == "0.0005"
        assert send_call[1]["settle_address"] == "TXYZ"

    def test_send_amount_validation(self, runner):
        # Neither amount → rejected
        result = runner.invoke(
            cli,
            ["sideshift", "send",
             "--deposit-coin", "btc", "--deposit-network", "liquid",
             "--settle-coin", "usdt", "--settle-network", "tron",
             "--settle-address", "TXYZ", "--yes"],
        )
        assert result.exit_code == 2

    def test_send_rejects_non_native_deposit_network(self, runner):
        # Click validates the choice before the manager is called
        result = runner.invoke(
            cli,
            ["sideshift", "send",
             "--deposit-coin", "usdt", "--deposit-network", "tron",
             "--settle-coin", "btc", "--settle-network", "liquid",
             "--settle-address", "lq1qfoo",
             "--deposit-amount", "100", "--yes"],
        )
        assert result.exit_code == 2

    def test_send_passes_liquid_asset_id(self, runner, sideshift_manager):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideshift", "send",
             "--deposit-coin", "usdt", "--deposit-network", "liquid",
             "--settle-coin", "usdt", "--settle-network", "tron",
             "--settle-address", "TXYZ",
             "--deposit-amount", "100",
             "--liquid-asset-id", "ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2",
             "--yes"],
            env=_cli_env(),
        )
        assert result.exit_code == 0
        send_call = next(c for c in sideshift_manager.calls if c[0] == "send_shift")
        assert send_call[1]["liquid_asset_id"].startswith("ce091c99")

    def test_send_auto_resolves_liquid_asset_id_for_usdt(self, runner, sideshift_manager):
        """When depositing USDt on Liquid, --liquid-asset-id is resolved from the ticker."""
        result = runner.invoke(
            cli,
            ["--format", "json", "sideshift", "send",
             "--deposit-coin", "USDt", "--deposit-network", "liquid",
             "--settle-coin", "usdt", "--settle-network", "tron",
             "--settle-address", "TXYZ",
             "--deposit-amount", "100",
             "--yes"],
            env=_cli_env(),
        )
        assert result.exit_code == 0, result.output
        send_call = next(c for c in sideshift_manager.calls if c[0] == "send_shift")
        assert send_call[1]["liquid_asset_id"].startswith("ce091c99")

    def test_send_unknown_liquid_ticker_errors(self, runner):
        result = runner.invoke(
            cli,
            ["sideshift", "send",
             "--deposit-coin", "xyznotreal", "--deposit-network", "liquid",
             "--settle-coin", "usdt", "--settle-network", "tron",
             "--settle-address", "TXYZ",
             "--deposit-amount", "100",
             "--yes"],
        )
        assert result.exit_code != 0
        assert "Unknown Liquid asset" in result.output or "xyznotreal" in result.output


class TestSideShiftReceive:
    def test_receive_into_liquid(self, runner, sideshift_manager):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideshift", "receive",
             "--deposit-coin", "usdt", "--deposit-network", "tron",
             "--settle-coin", "usdt", "--settle-network", "liquid",
             "--external-refund-address", "TXrefund"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["shift_id"] == "shift_recv"
        assert data["deposit_address"] == "TXdepositAddr"
        assert data["refund_address"] == "TXrefund"

    def test_receive_rejects_non_native_settle_network(self, runner):
        result = runner.invoke(
            cli,
            ["sideshift", "receive",
             "--deposit-coin", "usdt", "--deposit-network", "tron",
             "--settle-coin", "usdt", "--settle-network", "ethereum"],
        )
        assert result.exit_code == 2


class TestSideShiftStatus:
    def test_status_passes_shift_id(self, runner, sideshift_manager):
        result = runner.invoke(
            cli,
            ["--format", "json", "sideshift", "status", "--shift-id", "shift_xyz"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["shift_id"] == "shift_xyz"
        assert data["is_final"] is True
        assert sideshift_manager.calls[-1] == ("status", {"shift_id": "shift_xyz"})



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


# ---------------------------------------------------------------------------
# WapuPay CLI (dark-launched — re-register the CLI with the group enabled)
# ---------------------------------------------------------------------------


class _FakeJAN3Manager:
    """Stand-in for Jan3AccountsManager — records calls, returns canned responses.

    The `jan3_*` tools route through `tools._jan3_manager`, so the `jan3` CLI
    tests inject this (not the WapuPay manager)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        # When True, rebind_wallet reports an already-bound no-op (no confirm gate).
        self.rebind_already_bound = False

    def login(self, email, language="en"):
        self.calls.append(("login", {"email": email, "language": language}))
        return {"email": email, "message": "OTP sent", "next_step": "verify"}

    def rebind_wallet(self, email, wallet_name="default", confirm=False):
        self.calls.append((
            "rebind_wallet",
            {
                "email": email,
                "wallet_name": wallet_name,
                "confirm": confirm,
            },
        ))
        if self.rebind_already_bound:
            return {
                "email": email,
                "ln_username": "alice@aquabtc.com",
                "wallet_name": wallet_name,
                "fingerprint": "samefp",
                "already_bound": True,
                "requires_confirmation": False,
                "rebound": False,
                "ln_address_pool": {"reason": "pool_full"},
            }
        if not confirm:
            return {
                "email": email,
                "ln_username": "alice@aquabtc.com",
                "wallet_name": wallet_name,
                "state": "rebind",
                "current_fingerprint": "serverfp",
                "new_fingerprint": "localfp",
                "requires_confirmation": True,
                "rebound": False,
                "warning": (
                    "Re-binding your Lightning Address alice@aquabtc.com to wallet "
                    f"{wallet_name!r} [localfp] moves delivery away from [serverfp]."
                ),
            }
        return {
            "email": email,
            "ln_username": "alice@aquabtc.com",
            "wallet_name": wallet_name,
            "state": "rebind",
            "current_fingerprint": "serverfp",
            "new_fingerprint": "localfp",
            "rebound": True,
            "requires_confirmation": False,
            "pool_size": 5,
            "fingerprint": "localfp",
        }

    def get_user(self, email, wallet_name="default"):
        self.calls.append(("get_user", {"email": email, "wallet_name": wallet_name}))
        return {
            "email": email,
            "ln_username": "alice@aquabtc.com",
            "ln_address_toggled": True,
            "fingerprint": "samefp",
            "new_addresses_needed": 0,
            "ln_address_pool": {"refilled": False, "reason": "pool_full"},
        }

    def ln_address_toggle(self, email, enabled, wallet_name="default"):
        self.calls.append((
            "ln_address_toggle",
            {"email": email, "enabled": enabled, "wallet_name": wallet_name},
        ))
        out = {"email": email, "enabled": enabled, "result": {"ln_address_toggled": enabled}}
        if enabled:
            out["ln_address_pool"] = {"refilled": True, "pool_size": 5}
        return out

    def ln_username_available(self, email, username):
        self.calls.append((
            "ln_username_available", {"email": email, "username": username}
        ))
        return {"is_available": True, "ln_username": username}

    def purchase_ln_username(
        self,
        email,
        ln_username,
        wallet_name="default",
        password=None,
        asset="L-BTC",
        confirm=False,
        expected_amount_base_units=None,
    ):
        self.calls.append((
            "purchase_ln_username",
            {
                "email": email,
                "ln_username": ln_username,
                "wallet_name": wallet_name,
                "password": password,
                "asset": asset,
                "confirm": confirm,
                "expected_amount_base_units": expected_amount_base_units,
            },
        ))
        if not confirm:
            return {
                "requires_confirmation": True,
                "confirmed": False,
                "ln_username": ln_username,
                "asset_ticker": asset,
                "amount_base_units": 777,
                "display_amount": "777 Sats",
                "expires_at": "2026-07-03T18:13:55Z",
            }
        return {
            "confirmed": True,
            "payment_id": "pay1",
            "status": "PENDING",
            "txid": "txid1",
            "ln_username": ln_username,
            "amount_base_units": 777,
            "display_amount": "777 Sats",
        }

    def verify(self, email, otp_code, fingerprint=None, captcha_exempt=False):
        self.calls.append((
            "verify",
            {
                "email": email,
                "otp_code": otp_code,
                "fingerprint": fingerprint,
                "captcha_exempt": captcha_exempt,
            },
        ))
        return {"email": email, "logged_in": True, "message": "ok"}

    def logout(self, email):
        self.calls.append(("logout", {"email": email}))
        return {"email": email, "logged_out": True}

    def session_status(self, email):
        self.calls.append(("session_status", {"email": email}))
        return {"logged_in": True, "email": email, "created_at": "t0"}


class _FakeWapuPayManager:
    """Stand-in for WapuPayManager — records calls, returns canned responses."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def quote(self, amount_ars, transfer_type, alias=None):
        self.calls.append(("quote", {
            "amount_ars": amount_ars, "type": transfer_type, "alias": alias,
        }))
        return {"usdt_amount": 6.99, "fee": 0.14, "total_amount": 7.13,
                "exchange_rate": 1432.5, "valid_cbu_alias": True}

    def create_order(self, **kwargs):
        self.calls.append(("create_order", kwargs))
        return {
            "tentative_id": "7f4b8b8d-39a4-4f80-8e89-44d1f8dff111",
            "status": "FUNDING_ISSUED",
            "address_destination": "lq1qqfunding0address",
            "asset_id": "ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2",
            "funding_amount_usdt": 6.99,
            "total_amount_usdt": 7.13,
            "total_funding_amount_base_units": 713000000,
            "funded": True,
        }

    def provision_account(self, email):
        self.calls.append(("provision_account", {"email": email}))
        return {
            "provisioned": True,
            "key_preview": "Wapu…uY8e",
            "message": "WapuPay API key provisioned and stored locally.",
            "warning": "AQUA's backend rotates on every call, invalidating the previous key.",
        }


@pytest.fixture
def wapupay_cli():
    """Re-register the CLI with WapuPay enabled and inject a fake manager.

    WapuPay is enabled by default; we re-register here only to swap in a fake
    manager for the duration of the test, then restore the default registration.
    """
    import aqua.tools as tools_module
    from aqua.cli.commands import register_commands
    from aqua.features import SHIPPED_DEFAULTS_ENABLED_TOOLS
    from aqua.storage import Config

    enabled = dict(SHIPPED_DEFAULTS_ENABLED_TOOLS)
    for k in enabled:
        if k.startswith("wapupay_"):
            enabled[k] = True
    register_commands(cli, Config(enabled_tools=enabled))

    fake = _FakeWapuPayManager()
    saved = tools_module._wapupay_manager
    tools_module._wapupay_manager = fake
    try:
        yield fake
    finally:
        tools_module._wapupay_manager = saved
        # Restore the default (wapupay-hidden) registration deterministically,
        # without touching ~/.aqua via load_config_with_merge().
        register_commands(cli, Config(enabled_tools=dict(SHIPPED_DEFAULTS_ENABLED_TOOLS)))


@pytest.fixture
def auth_cli():
    """Re-register the CLI with the JAN3 account tools enabled + a fake manager.

    The `jan3_*` tools are enabled by default; we re-register here only to swap
    in a fake manager, then restore the default registration. The `jan3_login`/
    `jan3_verify` tools route through `tools._jan3_manager`, so the
    `_FakeJAN3Manager` stand-in records the calls.
    """
    import aqua.tools as tools_module
    from aqua.cli.commands import register_commands
    from aqua.features import SHIPPED_DEFAULTS_ENABLED_TOOLS
    from aqua.storage import Config

    enabled = dict(SHIPPED_DEFAULTS_ENABLED_TOOLS)
    for k in enabled:
        if k.startswith("jan3_"):
            enabled[k] = True
    register_commands(cli, Config(enabled_tools=enabled))

    fake = _FakeJAN3Manager()
    saved = tools_module._jan3_manager
    tools_module._jan3_manager = fake
    try:
        yield fake
    finally:
        tools_module._jan3_manager = saved
        register_commands(cli, Config(enabled_tools=dict(SHIPPED_DEFAULTS_ENABLED_TOOLS)))


class TestWapuPayCli:
    def test_groups_present_by_default(self):
        """The `wapupay` and `jan3` groups are enabled by default, so both are
        present in the command registry."""
        from aqua.cli.commands import register_commands
        from aqua.features import SHIPPED_DEFAULTS_ENABLED_TOOLS
        from aqua.storage import Config

        register_commands(cli, Config(enabled_tools=dict(SHIPPED_DEFAULTS_ENABLED_TOOLS)))
        assert "wapupay" in cli.commands
        assert "jan3" in cli.commands

    def test_login(self, runner, auth_cli):
        result = runner.invoke(
            cli, ["--format", "json", "jan3", "login", "--email", "u@e.com"]
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["message"] == "OTP sent"
        assert auth_cli.calls[-1] == ("login", {"email": "u@e.com", "language": "en"})

    def test_verify_reads_otp_arg(self, runner, auth_cli):
        result = runner.invoke(
            cli,
            ["--format", "json", "jan3", "verify", "--email", "u@e.com", "--otp", "123456"],
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["logged_in"] is True
        assert auth_cli.calls[-1][1]["otp_code"] == "123456"

    def test_rebind_wallet_yes_skips_prompt(self, runner, auth_cli):
        """--yes runs the two-step handshake without prompting: preview
        (confirm=False) then execute (confirm=True)."""
        result = runner.invoke(
            cli,
            ["--format", "json", "jan3", "rebind-wallet",
             "--email", "u@e.com", "--wallet-name", "new", "--yes"],
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["rebound"] is True
        rebinds = [c for c in auth_cli.calls if c[0] == "rebind_wallet"]
        assert rebinds[0][1]["confirm"] is False   # preview first
        assert rebinds[-1][1]["confirm"] is True    # then execute
        assert rebinds[-1][1]["wallet_name"] == "new"

    def test_rebind_wallet_decline_aborts_without_executing(self, runner, auth_cli):
        """Declining the prompt aborts: the destructive confirm=True call is
        never made."""
        result = runner.invoke(
            cli,
            ["jan3", "rebind-wallet", "--email", "u@e.com", "--wallet-name", "new"],
            input="n\n",
        )
        assert result.exit_code != 0  # click.Abort
        rebinds = [c for c in auth_cli.calls if c[0] == "rebind_wallet"]
        assert rebinds and all(c[1]["confirm"] is False for c in rebinds)

    def test_rebind_wallet_accept_executes(self, runner, auth_cli):
        """Accepting the prompt (y) runs the destructive execute call."""
        result = runner.invoke(
            cli,
            ["jan3", "rebind-wallet", "--email", "u@e.com", "--wallet-name", "new"],
            input="y\n",
        )
        assert result.exit_code == 0
        # These phrases only come from the pre-prompt warning, not the result JSON.
        assert "moves delivery away" in result.output
        assert "alice@aquabtc.com" in result.output
        assert any(
            c[0] == "rebind_wallet" and c[1]["confirm"] is True for c in auth_cli.calls
        )

    def test_rebind_wallet_already_bound_is_noop(self, runner, auth_cli):
        """When already bound to this wallet, no prompt and no destructive
        execute call."""
        auth_cli.rebind_already_bound = True
        result = runner.invoke(
            cli, ["--format", "json", "jan3", "rebind-wallet", "--email", "u@e.com"]
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["already_bound"] is True
        assert not any(
            c[0] == "rebind_wallet" and c[1]["confirm"] is True for c in auth_cli.calls
        )

    def test_user_info(self, runner, auth_cli):
        result = runner.invoke(
            cli,
            ["--format", "json", "jan3", "user-info",
             "--email", "u@e.com", "--wallet-name", "w2"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ln_username"] == "alice@aquabtc.com"
        assert data["ln_address_pool"]["reason"] == "pool_full"
        assert auth_cli.calls[-1] == (
            "get_user", {"email": "u@e.com", "wallet_name": "w2"}
        )

    def test_enable_lightning_address(self, runner, auth_cli):
        result = runner.invoke(
            cli,
            ["--format", "json", "jan3", "enable-lightning-address",
             "--email", "u@e.com", "--enable"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["enabled"] is True
        assert data["ln_address_pool"]["refilled"] is True
        assert auth_cli.calls[-1] == (
            "ln_address_toggle",
            {"email": "u@e.com", "enabled": True, "wallet_name": "default"},
        )

    def test_disable_lightning_address(self, runner, auth_cli):
        result = runner.invoke(
            cli,
            ["--format", "json", "jan3", "enable-lightning-address",
             "--email", "u@e.com", "--disable"],
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["enabled"] is False
        assert auth_cli.calls[-1][1]["enabled"] is False

    def test_ln_check_username(self, runner, auth_cli):
        result = runner.invoke(
            cli,
            ["--format", "json", "jan3", "ln-check-username",
             "--email", "u@e.com", "--ln-username", "alice"],
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["is_available"] is True
        assert auth_cli.calls[-1] == (
            "ln_username_available", {"email": "u@e.com", "username": "alice"}
        )

    def test_purchase_ln_username_accept_binds_quote(self, runner, auth_cli):
        """Accepting the prompt executes confirm=True bound to the quoted
        amount (expected_amount_base_units)."""
        result = runner.invoke(
            cli,
            ["jan3", "purchase-ln-username",
             "--email", "u@e.com", "--ln-username", "alice"],
            input="y\n",
        )
        assert result.exit_code == 0
        assert "777 Sats" in result.output  # quote echoed before the prompt
        purchases = [c for c in auth_cli.calls if c[0] == "purchase_ln_username"]
        assert purchases[0][1]["confirm"] is False   # quote first
        assert purchases[-1][1]["confirm"] is True   # then execute
        assert purchases[-1][1]["expected_amount_base_units"] == 777

    def test_purchase_ln_username_decline_aborts_without_spending(
        self, runner, auth_cli
    ):
        """Declining the price prompt aborts: the spending confirm=True call
        is never made."""
        result = runner.invoke(
            cli,
            ["jan3", "purchase-ln-username",
             "--email", "u@e.com", "--ln-username", "alice"],
            input="n\n",
        )
        assert result.exit_code != 0  # click.Abort
        purchases = [c for c in auth_cli.calls if c[0] == "purchase_ln_username"]
        assert purchases and all(c[1]["confirm"] is False for c in purchases)

    def test_purchase_ln_username_yes_skips_quote(self, runner, auth_cli):
        """--yes goes straight to the confirmed purchase: no quote call (which
        would create an orphaned server-side order) and no amount bound."""
        result = runner.invoke(
            cli,
            ["--format", "json", "jan3", "purchase-ln-username",
             "--email", "u@e.com", "--ln-username", "alice", "--yes"],
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["confirmed"] is True
        purchases = [c for c in auth_cli.calls if c[0] == "purchase_ln_username"]
        assert len(purchases) == 1
        assert purchases[0][1]["confirm"] is True
        assert purchases[0][1]["expected_amount_base_units"] is None

    def test_create_order_with_yes_skips_confirm(self, runner, wapupay_cli):
        result = runner.invoke(
            cli,
            ["--format", "json", "wapupay", "create-order",
             "--amount-ars", "10000", "--alias", "al.cbu", "--yes"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["address_destination"] == "lq1qqfunding0address"
        assert data["total_funding_amount_base_units"] == 713000000
        call = next(c for c in wapupay_cli.calls if c[0] == "create_order")
        assert call[1]["amount_ars"] == "10000"
        assert call[1]["alias"] == "al.cbu"

    def test_create_order_rejects_invalid_refund_address(self, runner, wapupay_cli):
        """A bad --refund-address fails fast: before the quote fetch and before
        any manager call (no order is created)."""
        result = runner.invoke(
            cli,
            ["wapupay", "create-order", "--amount-ars", "10000", "--alias", "al.cbu",
             "--refund-address", "lq12341234", "--yes"],
        )
        assert result.exit_code != 0
        assert "not a valid Liquid address" in result.output
        assert not any(c[0] == "create_order" for c in wapupay_cli.calls)

    def test_create_order_confirm_flow_quotes_first(self, runner, wapupay_cli):
        """Without --yes, a quote is fetched and the user confirms."""
        result = runner.invoke(
            cli,
            ["wapupay", "create-order", "--amount-ars", "10000", "--alias", "al.cbu"],
            input="y\n",
        )
        assert result.exit_code == 0
        assert any(c[0] == "quote" for c in wapupay_cli.calls)
        assert any(c[0] == "create_order" for c in wapupay_cli.calls)

    def test_quote(self, runner, wapupay_cli):
        result = runner.invoke(
            cli,
            ["--format", "json", "wapupay", "quote", "--amount-ars", "10000",
             "--alias", "al.cbu"],
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["valid_cbu_alias"] is True

    def test_provision_account(self, runner, wapupay_cli):
        result = runner.invoke(
            cli,
            ["--format", "json", "wapupay", "provision-account", "--email", "u@e.com"],
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["provisioned"] is True
        assert wapupay_cli.calls[-1] == ("provision_account", {"email": "u@e.com"})

    def test_provision_account_present_by_default(self):
        """WapuPay is enabled by default, so `provision-account` is registered."""
        from aqua.cli.commands import register_commands
        from aqua.features import SHIPPED_DEFAULTS_ENABLED_TOOLS
        from aqua.storage import Config

        register_commands(cli, Config(enabled_tools=dict(SHIPPED_DEFAULTS_ENABLED_TOOLS)))
        assert "wapupay" in cli.commands
        assert "provision-account" in cli.commands["wapupay"].commands
