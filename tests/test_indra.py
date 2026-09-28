"""Tests for the Indra swap provider and the provider registry."""

import importlib
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from aqua import indra
from aqua.boltz import BoltzClient
from aqua.lightning_providers import (
    DEFAULT_PROVIDER,
    ENV_VAR,
    PROVIDERS,
    get_provider,
    resolve_send_provider,
)
from aqua.storage import Config


class TestIndraClient:
    """Tests for indra.IndraClient construction."""

    def test_mainnet_base_url(self):
        client = indra.IndraClient(network="mainnet")
        assert client.base_url == "https://indra.aquabtc.com"
        assert client.provider_label == "Indra"

    def test_default_network_is_mainnet(self):
        assert indra.IndraClient().base_url == indra.INDRA_API["mainnet"]

    def test_testnet_raises_value_error(self):
        """Indra has no testnet host — raise instead of falling back silently."""
        with pytest.raises(ValueError, match="no testnet endpoint"):
            indra.IndraClient(network="testnet")

    def test_testnet_error_points_at_boltz(self):
        with pytest.raises(ValueError, match="boltz"):
            indra.IndraClient(network="testnet")

    def test_env_var_overrides_base_url(self, monkeypatch):
        """INDRA_API_URL is read at import time."""
        monkeypatch.setenv("INDRA_API_URL", "https://staging.example.com")
        reloaded = importlib.reload(indra)
        try:
            assert reloaded.IndraClient().base_url == "https://staging.example.com"
        finally:
            monkeypatch.delenv("INDRA_API_URL", raising=False)
            importlib.reload(indra)

    def test_non_https_base_url_rejected(self, monkeypatch):
        monkeypatch.setenv("INDRA_API_URL", "http://insecure.example.com")
        reloaded = importlib.reload(indra)
        try:
            with pytest.raises(ValueError, match="must be https"):
                reloaded.IndraClient()
        finally:
            monkeypatch.delenv("INDRA_API_URL", raising=False)
            importlib.reload(indra)

    def test_inherits_boltz_protocol(self):
        assert isinstance(indra.IndraClient(), BoltzClient)

    def test_limits_match_live_pair(self):
        assert indra.MIN_SWAP_AMOUNT_SATS == 1_000
        assert indra.MAX_SWAP_AMOUNT_SATS == 100_000


class TestIndraErrorMessages:
    """HTTP failures must name Indra, not Boltz."""

    @patch("aqua.boltz.urllib.request.urlopen")
    def test_http_error_names_indra(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://indra.aquabtc.com/v2/swap/submarine",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b"not json"),
        )
        with pytest.raises(RuntimeError, match="Indra API error.*500"):
            indra.IndraClient().get_submarine_pairs()

    @patch("aqua.boltz.urllib.request.urlopen")
    def test_unreachable_names_indra(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("timeout")
        with pytest.raises(RuntimeError, match="Indra API unreachable"):
            indra.IndraClient().get_submarine_pairs()

    @patch("aqua.boltz.urllib.request.urlopen")
    def test_duplicate_invoice_error_names_indra(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://indra.aquabtc.com/v2/swap/submarine",
            code=409,
            msg="Conflict",
            hdrs=None,
            fp=io.BytesIO(
                json.dumps({"error": "a swap with this invoice exists already"}).encode()
            ),
        )
        with pytest.raises(Exception, match="Indra"):
            indra.IndraClient().create_submarine_swap("lnbc500u1ptest", "03" + "ff" * 32)


class TestProviderRegistry:
    """Tests for lightning_providers."""

    def test_default_provider_is_indra(self):
        assert DEFAULT_PROVIDER == "indra"
        assert resolve_send_provider(Config()).name == "indra"

    def test_registry_holds_both_providers(self):
        assert set(PROVIDERS) == {"indra", "boltz"}

    def test_config_selects_boltz(self):
        provider = resolve_send_provider(Config(lightning_provider="boltz"))
        assert provider.name == "boltz"
        assert provider.min_sats == 100
        assert provider.max_sats == 25_000_000

    def test_env_var_wins_over_config(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "boltz")
        assert resolve_send_provider(Config(lightning_provider="indra")).name == "boltz"

    def test_env_var_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "  BOLTZ ")
        assert resolve_send_provider(Config()).name == "boltz"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown Lightning swap provider"):
            resolve_send_provider(Config(lightning_provider="nostr"))

    def test_non_string_provider_raises_value_error(self):
        """A malformed config value fails with a message, not an AttributeError."""
        with pytest.raises(ValueError, match="Unknown Lightning swap provider"):
            resolve_send_provider(Config(lightning_provider=7))

    def test_unknown_provider_lists_valid_values(self):
        with pytest.raises(ValueError, match="boltz, indra"):
            get_provider("nope")

    def test_client_factory_builds_the_right_client(self):
        assert isinstance(PROVIDERS["indra"].client_factory(network="mainnet"), indra.IndraClient)
        boltz_client = PROVIDERS["boltz"].client_factory(network="testnet")
        assert boltz_client.base_url == "https://api.testnet.boltz.exchange"

    def test_indra_provider_has_no_testnet(self):
        with pytest.raises(ValueError, match="no testnet endpoint"):
            PROVIDERS["indra"].client_factory(network="testnet")
