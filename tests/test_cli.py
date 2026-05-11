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

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon about"
)


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
        assert manager.storage.is_mnemonic_encrypted(stored.encrypted_mnemonic)

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
        assert manager.storage.is_mnemonic_encrypted(stored.encrypted_mnemonic)

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
        assert manager.storage.is_mnemonic_encrypted(stored.encrypted_mnemonic)

    def test_no_password_stores_plaintext(self, runner, isolated_manager):
        """No stdin flag, no env var, --password-stdin omitted → wallet unencrypted."""
        manager, _ = isolated_manager
        result = runner.invoke(
            cli,
            ["--format", "json", "wallet", "import-mnemonic", "--wallet-name", "plain_one"],
            env=_cli_env(AQUA_MNEMONIC=TEST_MNEMONIC),
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
                     password=None, send_bitcoins=True, **_):
        self.calls.append(
            ("execute_swap", {
                "asset_id": asset_id,
                "send_amount": send_amount,
                "wallet_name": wallet_name,
                "password": password,
                "send_bitcoins": send_bitcoins,
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


class TestSideSwapPegIn:
    def test_peg_in_returns_deposit_address(self, runner, sideswap_managers):
        result = runner.invoke(
            cli, ["--format", "json", "sideswap", "peg-in"],
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
