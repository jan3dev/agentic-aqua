"""LNURL-pay (LUD-16) Lightning Address resolver.

Resolves user@domain Lightning Addresses to BOLT11 invoices via the LNURL-pay
flow. Mirrors the behaviour of the AQUA Flutter wallet
(https://github.com/AquaWallet/aqua-wallet) for cross-client consistency.

LUD-06 description-hash verification is intentionally not enforced, matching
AQUA Flutter's pragmatic compatibility behaviour. Strict-mode verification is
a possible future hardening.
"""

import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .boltz import decode_bolt11_amount_sats

# Email-format regex (matches AQUA Flutter lnurl_provider.dart:14-17).
_LN_ADDRESS_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

LNURL_HTTP_TIMEOUT = 30

_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "broadcasthost"})


def is_lightning_address(s: str) -> bool:
    """True if `s` matches the LN-address email-like format. Pure syntactic check."""
    return bool(s and _LN_ADDRESS_RE.match(s))


def _reject_if_private_ip(host: str, label: str) -> None:
    """Raise ValueError if `host` is a literal private/reserved/loopback/link-local IP."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return  # Domain name, not a literal IP — allow
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
        raise ValueError(f"LNURL-pay {label} points to a non-public address: {host!r}")


def _validate_safe_https_url(url: str, label: str) -> None:
    """Raise ValueError if `url` is not a safe public HTTPS URL.

    Guards against SSRF by rejecting non-HTTPS schemes, localhost aliases, and
    literal private/loopback/link-local/reserved IP addresses in the hostname.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(f"LNURL-pay {label} must be an https URL")
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        raise ValueError(f"LNURL-pay {label} is not a valid URL")
    if not host or host.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError(f"LNURL-pay {label} hostname not allowed: {host!r}")
    _reject_if_private_ip(host, label)


def _http_get_json(url: str) -> dict:
    """GET `url` and return parsed JSON.

    Raises:
        RuntimeError on non-HTTPS URL, HTTP / network failure, redirect to non-https,
        or malformed JSON.
    """
    if not url.startswith("https://"):
        raise RuntimeError(f"LNURL-pay URL must use HTTPS: {url}")

    req = urllib.request.Request(url, headers={"User-Agent": "agentic-aqua"})
    try:
        with urllib.request.urlopen(req, timeout=LNURL_HTTP_TIMEOUT) as resp:
            final_url = resp.geturl()
            if not final_url.startswith("https://"):
                raise RuntimeError(
                    f"LNURL-pay redirected to non-https URL: {final_url}"
                )
            body = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LNURL-pay endpoint error ({e.code} GET {url})") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"LNURL-pay endpoint unreachable (GET {url}): {e.reason}"
        ) from e

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"LNURL-pay endpoint returned malformed JSON (GET {url}): {e}"
        ) from e


def resolve_lightning_address(address: str, amount_sats: int) -> str:
    """Resolve a Lightning Address (`user@domain`) to a BOLT11 invoice.

    LUD-16 + LUD-06 flow:
      1. GET https://{domain}/.well-known/lnurlp/{user}
      2. Validate payRequest params (tag, callback, min/maxSendable, metadata)
      3. GET {callback}?amount={amount_msat}
      4. Cross-check returned BOLT11 amount equals requested amount

    Description-hash verification is intentionally NOT performed (matches the
    AQUA Flutter wallet at lnurl_provider.dart:54-68).

    Args:
        address: Lightning Address in user@domain form.
        amount_sats: Amount to pay, in satoshis.

    Returns:
        A BOLT11 invoice string ready to pay.

    Raises:
        ValueError: protocol or validation failures.
        RuntimeError: HTTP / network failures, malformed JSON.
    """
    if "@" in address:
        domain_check = address.rsplit("@", 1)[1].lower().rstrip(".")
        if domain_check.endswith(".onion"):
            raise ValueError("Tor (.onion) addresses not supported")
    if not is_lightning_address(address):
        raise ValueError(f"Invalid Lightning Address: {address!r}")

    user, domain = address.split("@", 1)
    domain = domain.lower()

    lnurlp_url = f"https://{domain}/.well-known/lnurlp/{user}"
    payreq = _http_get_json(lnurlp_url)

    if payreq.get("status") == "ERROR":
        raise ValueError(f"LNURL-pay error: {payreq.get('reason', 'unknown')}")

    tag = payreq.get("tag")
    if tag is None:
        raise ValueError("LNURL-pay response missing 'tag' field")
    if tag == "withdrawRequest":
        raise ValueError(
            "Lightning Address returned a withdrawRequest, not a payRequest"
        )
    if tag != "payRequest":
        raise ValueError(f"Unsupported LNURL tag: {tag}")

    callback = payreq.get("callback")
    min_sendable = payreq.get("minSendable")
    max_sendable = payreq.get("maxSendable")

    _validate_safe_https_url(callback, "'callback'")
    if not isinstance(min_sendable, (int, float)) or not isinstance(max_sendable, (int, float)):
        raise ValueError(
            "LNURL-pay 'minSendable' and 'maxSendable' must be numbers (msat)"
        )
    min_sendable = int(min_sendable)
    max_sendable = int(max_sendable)

    amount_msat = amount_sats * 1000
    if amount_msat < min_sendable or amount_msat > max_sendable:
        min_sats = min_sendable // 1000
        max_sats = max_sendable // 1000
        raise ValueError(
            f"amount_sats {amount_sats} outside Lightning Address bounds "
            f"({min_sats}-{max_sats} sats)"
        )

    sep = "&" if "?" in callback else "?"
    callback_url = f"{callback}{sep}amount={amount_msat}"
    callback_resp = _http_get_json(callback_url)

    if callback_resp.get("status") == "ERROR":
        raise ValueError(
            f"LNURL-pay callback error: {callback_resp.get('reason', 'unknown')}"
        )

    pr = callback_resp.get("pr")
    if not isinstance(pr, str) or not pr.lower().startswith(("lnbc", "lntb")):
        raise ValueError("LNURL-pay callback response missing valid 'pr' field")

    decoded_amount = decode_bolt11_amount_sats(pr)
    if decoded_amount is None:
        raise ValueError("LNURL-pay returned a zero-amount invoice")
    if decoded_amount != amount_sats:
        raise ValueError(
            f"LNURL-pay invoice amount ({decoded_amount} sats) does not match "
            f"requested amount ({amount_sats} sats)"
        )

    # Future hardening: verify LUD-06 description hash
    # (sha256(metadata) == BOLT11 'h' field). AQUA Flutter does not enforce this.
    return pr
