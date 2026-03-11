"""Tests for Boltz Exchange integration module (Layers 1, 2, 3)."""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from aqua_mcp.boltz import BoltzClient, SwapInfo, decode_bolt11_amount_sats, generate_keypair, verify_preimage
import io
import urllib.error

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

MOCK_SUBMARINE_PAIRS = {
    "L-BTC/BTC": {
        "rate": 1.0,
        "fees": {"percentage": 0.1, "minerFees": 19},
        "limits": {"maximal": 25000000, "minimal": 1000, "maximalZeroConf": 500000},
    }
}

MOCK_SWAP_RESPONSE = {
    "id": "test_swap_123",
    "address": "lq1qqexampleaddress",
    "expectedAmount": 50069,
    "claimPublicKey": "03" + "ab" * 32,
    "swapTree": {
        "claimLeaf": {"version": 192, "output": "a914..."},
        "refundLeaf": {"version": 192, "output": "b914..."},
    },
    "timeoutBlockHeight": 2500000,
}

MOCK_CLAIM_DETAILS = {
    "preimage": "aa" * 32,
    "transactionHash": "bb" * 32,
    "pubNonce": "cc" * 33,
}


def _mock_response(data, status=200):
    """Create a mock urllib response (context manager)."""
    resp = MagicMock()
    if isinstance(data, dict):
        resp.read.return_value = json.dumps(data).encode()
    else:
        resp.read.return_value = data
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ===========================================================================
# Layer 1: Cryptographic utilities
# ===========================================================================


class TestGenerateKeypair:
    """Tests for generate_keypair() - uses coincurve real."""

    def test_generate_keypair_returns_hex_strings(self):
        """Returns (privkey_hex, pubkey_hex) with correct sizes."""
        privkey, pubkey = generate_keypair()

        # privkey: 32 bytes = 64 hex chars
        assert len(privkey) == 64
        bytes.fromhex(privkey)  # valid hex

        # pubkey: 33 bytes compressed = 66 hex chars, starts with 02 or 03
        assert len(pubkey) == 66
        bytes.fromhex(pubkey)  # valid hex
        assert pubkey[:2] in ("02", "03")

    def test_generate_keypair_unique_per_call(self):
        """Two calls produce different keypairs."""
        priv1, pub1 = generate_keypair()
        priv2, pub2 = generate_keypair()
        assert priv1 != priv2
        assert pub1 != pub2


class TestVerifyPreimage:
    """Tests for verify_preimage() - uses hashlib real."""

    def test_verify_preimage_valid(self):
        """Returns True when SHA256(preimage) == expected_hash."""
        preimage = "aa" * 32
        expected = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        assert verify_preimage(preimage, expected) is True

    def test_verify_preimage_invalid(self):
        """Returns False when hash doesn't match."""
        preimage = "aa" * 32
        wrong_hash = "bb" * 32
        assert verify_preimage(preimage, wrong_hash) is False

    def test_verify_preimage_invalid_hex_raises(self):
        """Raises ValueError for invalid hex input."""
        with pytest.raises(ValueError):
            verify_preimage("xyz_not_hex", "aa" * 32)


# ===========================================================================
# Layer 2: BoltzClient HTTP
# ===========================================================================


class TestBoltzClient:
    """Tests for BoltzClient with mocked HTTP."""

    @patch("aqua_mcp.boltz.urllib.request.urlopen")
    def test_get_submarine_pairs_returns_lbtc_btc(self, mock_urlopen):
        """GET /v2/swap/submarine returns L-BTC/BTC pair info."""
        mock_urlopen.return_value = _mock_response(MOCK_SUBMARINE_PAIRS)
        client = BoltzClient(network="mainnet")
        result = client.get_submarine_pairs()

        assert "L-BTC/BTC" in result
        pair = result["L-BTC/BTC"]
        assert "fees" in pair
        assert "limits" in pair
        assert pair["fees"]["percentage"] == 0.1
        assert pair["limits"]["minimal"] == 1000

    @patch("aqua_mcp.boltz.urllib.request.urlopen")
    def test_create_submarine_swap_sends_correct_body(self, mock_urlopen):
        """POST body contains invoice, from, to, refundPublicKey."""
        mock_urlopen.return_value = _mock_response(MOCK_SWAP_RESPONSE)
        client = BoltzClient(network="mainnet")

        invoice = "lnbc500u1ptest..."
        refund_pubkey = "03" + "ff" * 32
        client.create_submarine_swap(invoice, refund_pubkey)

        # Capture the Request object passed to urlopen
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0] if call_args[0] else call_args[1].get("url")
        body = json.loads(request_obj.data.decode())

        assert body["invoice"] == invoice
        assert body["from"] == "L-BTC"
        assert body["to"] == "BTC"
        assert body["refundPublicKey"] == refund_pubkey

    @patch("aqua_mcp.boltz.urllib.request.urlopen")
    def test_create_submarine_swap_returns_swap_data(self, mock_urlopen):
        """Response is parsed into dict with expected fields."""
        mock_urlopen.return_value = _mock_response(MOCK_SWAP_RESPONSE)
        client = BoltzClient(network="mainnet")

        result = client.create_submarine_swap("lnbc500u1ptest...", "03" + "ff" * 32)

        assert result["id"] == "test_swap_123"
        assert result["address"] == "lq1qqexampleaddress"
        assert result["expectedAmount"] == 50069
        assert "claimPublicKey" in result
        assert "swapTree" in result
        assert "timeoutBlockHeight" in result

    @patch("aqua_mcp.boltz.urllib.request.urlopen")
    def test_get_swap_status_returns_status(self, mock_urlopen):
        """get_swap_status returns current swap status."""
        mock_urlopen.return_value = _mock_response({"status": "transaction.mempool"})
        client = BoltzClient(network="mainnet")

        result = client.get_swap_status("test_swap_123")
        assert result["status"] == "transaction.mempool"

    @patch("aqua_mcp.boltz.urllib.request.urlopen")
    def test_get_claim_details_returns_preimage(self, mock_urlopen):
        """get_claim_details returns preimage and transactionHash."""
        mock_urlopen.return_value = _mock_response(MOCK_CLAIM_DETAILS)
        client = BoltzClient(network="mainnet")

        result = client.get_claim_details("test_swap_123")
        assert result["preimage"] == "aa" * 32
        assert result["transactionHash"] == "bb" * 32

    @patch("aqua_mcp.boltz.urllib.request.urlopen")
    def test_api_request_http_error_includes_boltz_message(self, mock_urlopen):
        """HTTP errors include Boltz error detail in RuntimeError."""

        err = urllib.error.HTTPError(
            url="https://api.boltz.exchange/v2/swap/submarine",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"error": "invoice already used"}).encode()),
        )
        mock_urlopen.side_effect = err
        client = BoltzClient(network="mainnet")

        with pytest.raises(RuntimeError, match="invoice already used"):
            client.get_submarine_pairs()

    @patch("aqua_mcp.boltz.urllib.request.urlopen")
    def test_api_request_http_error_without_body(self, mock_urlopen):
        """HTTP errors without parseable body still produce a useful message."""
        err = urllib.error.HTTPError(
            url="https://api.boltz.exchange/v2/swap/submarine",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b"not json"),
        )
        mock_urlopen.side_effect = err
        client = BoltzClient(network="mainnet")

        with pytest.raises(RuntimeError, match="Boltz API error.*500"):
            client.get_submarine_pairs()

    @patch("aqua_mcp.boltz.urllib.request.urlopen")
    def test_api_request_timeout_raises(self, mock_urlopen):
        """Network timeout raises RuntimeError with context."""

        mock_urlopen.side_effect = urllib.error.URLError("timeout")
        client = BoltzClient(network="mainnet")

        with pytest.raises(RuntimeError, match="Boltz API unreachable"):
            client.get_submarine_pairs()

    @patch("aqua_mcp.boltz.urllib.request.urlopen")
    def test_api_request_invalid_json_raises(self, mock_urlopen):
        """Non-JSON response raises exception."""
        mock_urlopen.return_value = _mock_response(b"not json at all")
        client = BoltzClient(network="mainnet")

        with pytest.raises(Exception):
            client.get_submarine_pairs()


# ===========================================================================
# Layer 3: SwapInfo dataclass
# ===========================================================================


class TestSwapInfo:
    """Tests for SwapInfo dataclass."""

    def _make_swap(self, **overrides) -> SwapInfo:
        """Create a SwapInfo with default values."""
        defaults = {
            "swap_id": "test_swap_123",
            "address": "lq1qqexampleaddress",
            "expected_amount": 50069,
            "claim_public_key": "03" + "ab" * 32,
            "swap_tree": {"claimLeaf": {}, "refundLeaf": {}},
            "timeout_block_height": 2500000,
            "refund_private_key": "aa" * 32,
            "refund_public_key": "03" + "cc" * 32,
            "invoice": "lnbc500u1ptest...",
            "status": "swap.created",
            "network": "mainnet",
            "created_at": "2026-03-05T12:00:00",
        }
        defaults.update(overrides)
        return SwapInfo(**defaults)

    def test_swap_info_to_dict_roundtrip(self):
        """SwapInfo serializes to dict and reconstructs without data loss."""
        original = self._make_swap(
            lockup_txid="dd" * 32,
            preimage="ee" * 32,
            claim_txid="ff" * 32,
        )
        data = original.to_dict()
        reconstructed = SwapInfo(**data)

        assert reconstructed == original

    def test_swap_info_optional_fields_default_none(self):
        """Optional fields default to None."""
        swap = self._make_swap()
        assert swap.lockup_txid is None
        assert swap.preimage is None
        assert swap.claim_txid is None


# ===========================================================================
# BOLT11 invoice amount decoder
# ===========================================================================


class TestDecodeBolt11AmountSats:
    """Tests for decode_bolt11_amount_sats."""

    @pytest.mark.parametrize("invoice, expected_sats", [
        ("lnbc500u1ptest0000", 50_000),           # 500 micro-BTC
        ("lnbc1m1ptest0000", 100_000),             # 1 milli-BTC
        ("lnbc10m1ptest0000", 1_000_000),          # 10 milli-BTC
        ("lnbc2500u1ptest0000", 250_000),          # 2500 micro-BTC
        ("lnbc1500n1ptest0000", 150),              # 1500 nano-BTC
        ("lnbc21ptest0000", 200_000_000),          # 2 BTC (no multiplier)
        ("lntb500u1ptest0000", 50_000),            # testnet
    ])
    def test_known_amounts(self, invoice, expected_sats):
        assert decode_bolt11_amount_sats(invoice) == expected_sats

    def test_zero_amount_invoice_returns_none(self):
        assert decode_bolt11_amount_sats("lnbc1ptest0000") is None

    def test_invalid_prefix_returns_none(self):
        assert decode_bolt11_amount_sats("xxinvalid") is None

    def test_empty_string_returns_none(self):
        assert decode_bolt11_amount_sats("") is None

    def test_case_insensitive(self):
        assert decode_bolt11_amount_sats("LNBC500u1ptest") == 50_000
