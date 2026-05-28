"""Tests for the LNURL-pay (LUD-16) Lightning Address resolver."""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from aqua.lnurl import (
    LNURL_HTTP_TIMEOUT,
    _http_get_json,
    is_lightning_address,
    resolve_lightning_address,
)


# 50000 sats invoice (500u). Used for the canonical happy-path fixtures below.
INVOICE_50000_SATS = "lnbc500u1ptest_valid_invoice"
# 50 sats invoice (500n). Used for amount-mismatch tests.
INVOICE_50_SATS = "lnbc500n1ptest_smaller_invoice"
# Zero-amount invoice (no digit prefix). decode_bolt11_amount_sats returns None.
INVOICE_ZERO_AMOUNT = "lnbc1ptest_zero_amount"


def _mock_response(data, *, final_url: str | None = None):
    """Mock a urllib response (context manager). Mirrors tests/test_boltz.py:44."""
    resp = MagicMock()
    if isinstance(data, dict):
        resp.read.return_value = json.dumps(data).encode()
    else:
        resp.read.return_value = data
    resp.geturl.return_value = final_url or "https://example.com/x"
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _payreq(
    *,
    callback: str = "https://example.com/lnurlp/callback",
    min_msat: int = 1_000,
    max_msat: int = 100_000_000_000,
    metadata: str = '[["text/plain","pay alice"]]',
    tag: str = "payRequest",
) -> dict:
    return {
        "tag": tag,
        "callback": callback,
        "minSendable": min_msat,
        "maxSendable": max_msat,
        "metadata": metadata,
    }


# ===========================================================================
# is_lightning_address
# ===========================================================================


class TestIsLightningAddress:
    @pytest.mark.parametrize(
        "addr",
        [
            "alice@getalby.com",
            "BOB@example.io",
            "user.name@sub.domain.com",
            "user+tag@example.com",
            "user_with_underscore@example.com",
            "user-dash@example.io",
            "u@x.io",
        ],
    )
    def test_accepts_valid(self, addr):
        assert is_lightning_address(addr) is True

    @pytest.mark.parametrize(
        "addr",
        [
            "lnbc500u1ptest_valid_invoice",  # BOLT11
            "lntb1ptest_valid",  # BOLT11 testnet
            "no_at_sign.example.com",
            "missing@tld",
            "@nouser.com",
            "user@",
            "",
            "user@@double.com",
            "user@.com",
        ],
    )
    def test_rejects_invalid(self, addr):
        assert is_lightning_address(addr) is False


# ===========================================================================
# _http_get_json
# ===========================================================================


class TestHttpGetJson:
    def test_rejects_non_https_url_before_request(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(RuntimeError, match="LNURL-pay URL must use HTTPS"):
                _http_get_json("http://example.com/.well-known/lnurlp/u")

            mock_urlopen.assert_not_called()


# ===========================================================================
# resolve_lightning_address
# ===========================================================================


class TestResolveLightningAddress:
    def test_fetches_well_known_url(self):
        """Step 1 hits https://{domain}/.well-known/lnurlp/{user}."""
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen, patch(
            "aqua.lnurl.decode_bolt11_amount_sats", return_value=50_000
        ):
            mock_urlopen.side_effect = [
                _mock_response(
                    _payreq(),
                    final_url="https://getalby.com/.well-known/lnurlp/alice",
                ),
                _mock_response(
                    {"pr": INVOICE_50000_SATS},
                    final_url="https://example.com/lnurlp/callback?amount=50000000",
                ),
            ]
            resolve_lightning_address("alice@getalby.com", 50_000)

            first_call = mock_urlopen.call_args_list[0]
            req = first_call.args[0]
            assert req.full_url == "https://getalby.com/.well-known/lnurlp/alice"
            assert first_call.kwargs.get("timeout") == LNURL_HTTP_TIMEOUT

    def test_callback_appends_amount_msat(self):
        """Step 3 issues a GET to {callback}?amount={amount_sats * 1000}."""
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen, patch(
            "aqua.lnurl.decode_bolt11_amount_sats", return_value=50
        ):
            mock_urlopen.side_effect = [
                _mock_response(_payreq(), final_url="https://x.io/.well-known/lnurlp/u"),
                _mock_response(
                    {"pr": INVOICE_50_SATS},
                    final_url="https://example.com/lnurlp/callback?amount=50000",
                ),
            ]
            resolve_lightning_address("u@x.io", 50)

            second_req = mock_urlopen.call_args_list[1].args[0]
            assert "amount=50000" in second_req.full_url

    def test_callback_url_with_existing_query_uses_ampersand(self):
        """When `callback` already has `?`, append with `&` instead of `?`."""
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen, patch(
            "aqua.lnurl.decode_bolt11_amount_sats", return_value=50_000
        ):
            mock_urlopen.side_effect = [
                _mock_response(
                    _payreq(callback="https://example.com/cb?prev=1"),
                    final_url="https://x.io/.well-known/lnurlp/u",
                ),
                _mock_response(
                    {"pr": INVOICE_50000_SATS},
                    final_url="https://example.com/cb?prev=1&amount=50000000",
                ),
            ]
            resolve_lightning_address("u@x.io", 50_000)
            url = mock_urlopen.call_args_list[1].args[0].full_url
            assert "prev=1&amount=50000000" in url

    def test_returns_invoice_on_happy_path(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen, patch(
            "aqua.lnurl.decode_bolt11_amount_sats", return_value=50_000
        ):
            mock_urlopen.side_effect = [
                _mock_response(_payreq(), final_url="https://x.io/.well-known/lnurlp/u"),
                _mock_response({"pr": INVOICE_50000_SATS}),
            ]
            assert resolve_lightning_address("u@x.io", 50_000) == INVOICE_50000_SATS

    def test_rejects_withdraw_request_tag(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(_payreq(tag="withdrawRequest"))
            with pytest.raises(ValueError, match="withdrawRequest"):
                resolve_lightning_address("u@x.io", 50)

    @pytest.mark.parametrize("tag", ["channelRequest", "login", "garbage"])
    def test_rejects_other_unsupported_tags(self, tag):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(_payreq(tag=tag))
            with pytest.raises(ValueError, match=f"Unsupported LNURL tag: {tag}"):
                resolve_lightning_address("u@x.io", 50)

    def test_rejects_missing_tag(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(
                {
                    "callback": "https://x.io/cb",
                    "minSendable": 1000,
                    "maxSendable": 100000,
                    "metadata": "[]",
                }
            )
            with pytest.raises(ValueError, match="missing 'tag'"):
                resolve_lightning_address("u@x.io", 50)

    def test_rejects_amount_below_min(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            # min 1000 sats, requesting 50
            mock_urlopen.return_value = _mock_response(
                _payreq(min_msat=1_000_000, max_msat=10_000_000)
            )
            with pytest.raises(ValueError, match=r"outside Lightning Address bounds \(1000-10000 sats\)"):
                resolve_lightning_address("u@x.io", 50)

    def test_rejects_amount_above_max(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(
                _payreq(min_msat=1_000, max_msat=10_000)
            )
            with pytest.raises(ValueError, match=r"outside Lightning Address bounds \(1-10 sats\)"):
                resolve_lightning_address("u@x.io", 100)

    def test_rejects_non_https_callback(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(
                _payreq(callback="http://insecure.example.com/cb")
            )
            with pytest.raises(ValueError, match="must be an https URL"):
                resolve_lightning_address("u@x.io", 50)

    def test_rejects_missing_pr_in_callback_response(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [
                _mock_response(_payreq()),
                _mock_response({"routes": []}),  # no 'pr' field
            ]
            with pytest.raises(ValueError, match="missing valid 'pr' field"):
                resolve_lightning_address("u@x.io", 50)

    def test_rejects_amount_mismatch(self):
        """BOLT11 amount differs from requested amount."""
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen, patch(
            "aqua.lnurl.decode_bolt11_amount_sats", return_value=12_345
        ):
            mock_urlopen.side_effect = [
                _mock_response(_payreq()),
                _mock_response({"pr": INVOICE_50000_SATS}),
            ]
            with pytest.raises(ValueError, match=r"\(12345 sats\) does not match"):
                resolve_lightning_address("u@x.io", 50_000)

    def test_rejects_zero_amount_invoice(self):
        """decode_bolt11_amount_sats returning None → zero-amount rejection."""
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen, patch(
            "aqua.lnurl.decode_bolt11_amount_sats", return_value=None
        ):
            mock_urlopen.side_effect = [
                _mock_response(_payreq()),
                _mock_response({"pr": INVOICE_ZERO_AMOUNT}),
            ]
            with pytest.raises(ValueError, match="zero-amount invoice"):
                resolve_lightning_address("u@x.io", 50)

    def test_lnurlp_http_error_raises_runtime_error(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://x.io/.well-known/lnurlp/u", 404, "Not Found", {}, None
            )
            with pytest.raises(RuntimeError, match="LNURL-pay endpoint error \\(404"):
                resolve_lightning_address("u@x.io", 50)

    def test_lnurlp_unreachable_raises_runtime_error(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("DNS failure")
            with pytest.raises(RuntimeError, match="unreachable"):
                resolve_lightning_address("u@x.io", 50)

    def test_lnurlp_malformed_json_raises_runtime_error(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            resp = MagicMock()
            resp.read.return_value = b"not json"
            resp.geturl.return_value = "https://x.io/.well-known/lnurlp/u"
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = resp
            with pytest.raises(RuntimeError, match="malformed JSON"):
                resolve_lightning_address("u@x.io", 50)

    def test_lnurlp_status_error_raises_value_error(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(
                {"status": "ERROR", "reason": "user not found"}
            )
            with pytest.raises(ValueError, match="LNURL-pay error: user not found"):
                resolve_lightning_address("u@x.io", 50)

    def test_callback_status_error_raises_value_error(self):
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [
                _mock_response(_payreq()),
                _mock_response({"status": "ERROR", "reason": "below dust limit"}),
            ]
            with pytest.raises(ValueError, match="callback error: below dust limit"):
                resolve_lightning_address("u@x.io", 50)

    def test_rejects_onion_address(self):
        with pytest.raises(ValueError, match=r"Tor \(\.onion\) addresses not supported"):
            resolve_lightning_address("u@hiddenservice.onion", 50)

    def test_rejects_invalid_address_format(self):
        with pytest.raises(ValueError, match="Invalid Lightning Address"):
            resolve_lightning_address("not-an-address", 50)

    @pytest.mark.parametrize(
        "callback",
        [
            "https://192.168.1.1/pay",
            "https://10.0.0.1/pay",
            "https://172.16.0.1/pay",
            "https://127.0.0.1/pay",
            "https://169.254.169.254/pay",  # AWS metadata
            "https://[::1]/pay",            # IPv6 loopback
        ],
    )
    def test_rejects_private_ip_callback(self, callback):
        """Callback URL with private/loopback IP is rejected before fetch (SSRF guard)."""
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(_payreq(callback=callback))
            with pytest.raises(ValueError, match="non-public address"):
                resolve_lightning_address("u@x.io", 50)
            assert mock_urlopen.call_count == 1

    @pytest.mark.parametrize("hostname", ["localhost", "localhost.localdomain", "ip6-localhost"])
    def test_rejects_localhost_callback(self, hostname):
        """Callback URL with localhost hostname is rejected before fetch."""
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(
                _payreq(callback=f"https://{hostname}/pay")
            )
            with pytest.raises(ValueError, match="hostname not allowed"):
                resolve_lightning_address("u@x.io", 50)

    def test_accepts_float_sendable_values(self):
        """minSendable/maxSendable as JSON floats are accepted (real-world servers)."""
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen, patch(
            "aqua.lnurl.decode_bolt11_amount_sats", return_value=50_000
        ):
            payreq_data = _payreq()
            payreq_data["minSendable"] = 1_000.0
            payreq_data["maxSendable"] = 100_000_000_000.0
            mock_urlopen.side_effect = [
                _mock_response(payreq_data, final_url="https://x.io/.well-known/lnurlp/u"),
                _mock_response({"pr": INVOICE_50000_SATS}),
            ]
            assert resolve_lightning_address("u@x.io", 50_000) == INVOICE_50000_SATS

    def test_preserves_user_case_in_lookup_url(self):
        """User part of LN address is NOT lowercased — domain part is."""
        with patch("aqua.lnurl.urllib.request.urlopen") as mock_urlopen, patch(
            "aqua.lnurl.decode_bolt11_amount_sats", return_value=50_000
        ):
            mock_urlopen.side_effect = [
                _mock_response(_payreq(), final_url="https://getalby.com/.well-known/lnurlp/Alice"),
                _mock_response({"pr": INVOICE_50000_SATS}),
            ]
            resolve_lightning_address("Alice@GETALBY.COM", 50_000)
            req = mock_urlopen.call_args_list[0].args[0]
            assert req.full_url == "https://getalby.com/.well-known/lnurlp/Alice"
