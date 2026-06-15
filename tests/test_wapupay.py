"""Tests for the WapuPay direct-fiat integration.

Two seams, per tests/AGENTS.md:
- HTTP-client tests patch ``urllib.request.urlopen`` in ``aqua.wapupay`` to drive
  the single ``_api_request`` method (X-API-Key header, error mapping).
- Manager tests inject a ``FakeClient`` (``manager._client = fake``) so the
  orchestration logic (API-key gating, persist-before-fund, rail pinning) is
  exercised without any network.

Money/auth invariants checked: no fake-success fallbacks, secrets never logged,
funding amounts are integer sats, rail pinned to Liquid USDT, business calls send
X-API-Key (never a Bearer token), and exchange_rates is public.
"""

from __future__ import annotations

import io
import json
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import aqua.tools as tools
from aqua.storage import Storage
from aqua.wapupay import (
    AUTH_BASE_URL,
    FUNDING_METHOD_USDT,
    FUNDING_NETWORK_LIQUID,
    WAPUPAY_BASE_URL,
    WapuPayClient,
    WapuPayManager,
    WapuPayOrder,
    WapuPaySession,
    _ars_for_wire,
    _extract_error_message,
    _normalize_ars_amount,
    _redact,
    order_is_failed,
    order_is_final,
    order_is_success,
    usdt_to_sats,
)

# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Storage(Path(tmpdir))


@pytest.fixture(autouse=True)
def _wapupay_api_key(monkeypatch):
    """Business calls read WAPUPAY_API_KEY lazily; set a dummy for every test.

    Tests that exercise the missing-key path delenv it explicitly. Harmless for
    HTTP-client tests (only the manager reads the env var).
    """
    monkeypatch.setenv("WAPUPAY_API_KEY", "test-api-key")


class DummyWallet:
    """Stand-in wallet manager — WapuPay flows don't touch it (no auto-pay)."""

    def get_address(self, wallet_name="default"):  # pragma: no cover - unused
        raise AssertionError("WapuPay must not touch the wallet (no auto-pay)")


class FakeClient:
    """Scriptable WapuPayClient stand-in.

    ``responses`` maps a method name to either a dict (returned) or an Exception
    (raised). For 401-retry tests, map to a list consumed left-to-right.
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def _yield(self, name, *args):
        self.calls.append((name, args))
        val = self.responses.get(name)
        if isinstance(val, list):
            val = val.pop(0)
        if isinstance(val, Exception):
            raise val
        if callable(val):
            return val(*args)
        return val if val is not None else {}

    # auth (AQUA account login against Ankara — unchanged)
    def login(self, email, language="en"):
        return self._yield("login", email, language)

    def verify(self, email, otp_code):
        return self._yield("verify", email, otp_code)

    # WapuPay direct API. exchange_rates is public (no key); the rest carry the
    # X-API-Key — recorded as the last call arg so tests can assert it.
    def exchange_rates(self):
        return self._yield("exchange_rates")

    def tentative_amount(self, body, *, api_key=None):
        return self._yield("tentative_amount", body, api_key)

    def create_tentative(self, body, *, api_key=None):
        return self._yield("create_tentative", body, api_key)

    def issue_funding(self, tentative_id, *, api_key=None):
        return self._yield("issue_funding", tentative_id, api_key)

    def get_tentative(self, tentative_id, *, api_key=None):
        return self._yield("get_tentative", tentative_id, api_key)

    def my_transactions(self, *, api_key=None):
        return self._yield("my_transactions", api_key)

    def get_transaction(self, tx_id, *, api_key=None):
        return self._yield("get_transaction", tx_id, api_key)

    def spending_limit(self, *, api_key=None):
        return self._yield("spending_limit", api_key)


def make_manager(storage, fake):
    m = WapuPayManager(storage=storage, wallet_manager=DummyWallet())
    m._client = fake
    return m


def logged_in(storage, email="user@example.com", access="acc.tok", refresh="ref.tok"):
    storage.save_wapupay_session(
        WapuPaySession(email=email, access=access, refresh=refresh, created_at="t0")
    )


TENTATIVE_ID = "7f4b8b8d-39a4-4f80-8e89-44d1f8dff111"

CREATE_RESP = {
    "tentative_id": TENTATIVE_ID,
    "status": "CREATED",
    "funding_currency": "USDT",
    "funding_network": "LIQUID",
    "exchange_rate": 1432.5,
    "fee_amount_usdt": 0.14,
    "funding_amount_usdt": 6.99,
    "total_amount_usdt": 7.13,
    "expires_at": "2026-05-24 14:35:00",
}

FUNDING_RESP = {
    "tentative_id": TENTATIVE_ID,
    "status": "FUNDING_ISSUED",
    "address_destination": "lq1qqfunding0address",
    "asset_id": "ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2",
    "funding_amount_usdt": 6.99,
    "funding_amount_sat": 699000000,
    "funding_expires_at": "2026-05-24T14:35:00Z",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_usdt_to_sats_precision():  # Sig:4
    assert usdt_to_sats("6.99") == 699000000
    assert usdt_to_sats("0.00000001") == 1
    assert usdt_to_sats(1) == 100000000


def test_normalize_ars_amount_rejects_nonpositive_and_garbage():  # Sig:4
    assert _normalize_ars_amount("10000") == _normalize_ars_amount(10000)
    for bad in ("0", "-5", "abc", ""):
        with pytest.raises(ValueError):
            _normalize_ars_amount(bad)


def test_ars_must_be_whole_pesos():  # Sig:4
    assert _ars_for_wire(_normalize_ars_amount("10000")) == 10000
    assert isinstance(_ars_for_wire(_normalize_ars_amount("10000")), int)
    # Non-integral ARS is rejected (no float on the third-party wire).
    with pytest.raises(ValueError):
        _normalize_ars_amount("100.50")


def test_normalize_ars_rejects_non_finite():  # Sig:5
    # A money validator must never accept NaN / Infinity, and must surface a
    # clean ValueError (not decimal.InvalidOperation / OverflowError).
    for bad in ("Infinity", "-Infinity", "NaN", "sNaN"):
        with pytest.raises(ValueError):
            _normalize_ars_amount(bad)


def test_redact_hides_secrets_and_bank_pii_recursively():  # Sig:5
    payload = {
        "access": "supersecret",
        "refresh": "anothersecret",
        "Authorization": "Bearer x",
        "alias": "victima.cbu",
        "receiver_name": "Jane Doe",
        "amount_ars": 10000,
        "nested": {"refund_address": "lq1secret", "ok": "visible"},
        "list": [{"alias": "deep.cbu"}],
    }
    red = _redact(payload)
    assert red["access"] == "***"
    assert red["refresh"] == "***"
    assert red["Authorization"] == "***"
    assert red["alias"] == "***"
    assert red["receiver_name"] == "***"
    assert red["amount_ars"] == 10000
    assert red["nested"]["refund_address"] == "***"
    assert red["nested"]["ok"] == "visible"
    assert red["list"][0]["alias"] == "***"


def test_extract_error_message_variants():  # Sig:3
    assert _extract_error_message('{"error": "boom"}') == "boom"
    assert _extract_error_message('{"detail": "nope"}') == "nope"
    assert _extract_error_message('{"message": "msg"}') == "msg"
    assert "boom" in _extract_error_message("plain boom text")


def test_extract_error_message_scrubs_pii():  # Sig:5
    # Long digit runs (CBU/CVU/account) and emails are masked in free text.
    assert "<redacted>" in _extract_error_message('{"error": "bad CBU 0001234567890123456789"}')
    assert "0001234567890123456789" not in _extract_error_message(
        '{"error": "bad CBU 0001234567890123456789"}'
    )
    assert "<redacted-email>" in _extract_error_message('{"detail": "no user a@b.com here"}')
    # A short legitimate amount is NOT masked.
    assert "10000" in _extract_error_message('{"error": "amount 10000 below minimum"}')
    # Dict-dump fallback redacts PII that sits under a sensitive KEY.
    msg = _extract_error_message('{"details": {"alias": "victima.cbu"}}')
    assert "victima.cbu" not in msg


def test_status_helpers():  # Sig:3
    assert order_is_final("EXECUTED") and order_is_success("EXECUTED")
    assert order_is_final("FAILED") and order_is_failed("FAILED")
    assert order_is_final("EXPIRED") and order_is_failed("EXPIRED")
    assert not order_is_final("CREATED")
    assert not order_is_final("FUNDING_ISSUED")
    # SETTLED_TO_BALANCE: final but neither success nor failed (funded, payout
    # not made) — surfaced as needs-attention, never silently "ok".
    assert order_is_final("SETTLED_TO_BALANCE")
    assert not order_is_success("SETTLED_TO_BALANCE")
    assert not order_is_failed("SETTLED_TO_BALANCE")


def test_order_apply_tentative_derives_sats_and_does_not_wipe():  # Sig:4
    order = WapuPayOrder(
        tentative_id=TENTATIVE_ID, status="CREATED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="t0",
    )
    order.apply_tentative({"funding_amount_usdt": 6.99, "address_destination": "lq1x"})
    assert order.funding_amount_sat == 699000000  # derived when omitted
    # A later status poll that omits funding fields must not erase the address.
    order.apply_tentative({"status": "EXECUTED"})
    assert order.address_destination == "lq1x"
    assert order.status == "EXECUTED"
    # A re-funding that changes USDT without a sat must re-derive sat (no stale).
    order.apply_tentative({"funding_amount_usdt": 10.0})
    assert order.funding_amount_sat == usdt_to_sats("10.0")


def test_apply_tentative_coerces_float_sat_to_int():  # Sig:5
    """If WapuPay sends funding_amount_sat as a float, it's coerced to int
    (integer-satoshis invariant) so lw_send_asset never gets a float."""
    order = WapuPayOrder(
        tentative_id=TENTATIVE_ID, status="FUNDING_ISSUED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="t0",
    )
    order.apply_tentative({"funding_amount_usdt": 6.99, "funding_amount_sat": 699000000.0})
    assert order.funding_amount_sat == 699000000
    assert isinstance(order.funding_amount_sat, int)


def test_apply_tentative_rejects_non_dict():  # Sig:4
    order = WapuPayOrder(
        tentative_id=TENTATIVE_ID, status="CREATED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="t0",
    )
    with pytest.raises(ValueError):
        order.apply_tentative([1, 2])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# HTTP client seam (patch urlopen)
# ---------------------------------------------------------------------------


def _mock_resp(body):
    resp = MagicMock()
    resp.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code, body):
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="err", hdrs=None,
        fp=io.BytesIO(body.encode() if isinstance(body, str) else body),
    )


def test_api_request_sets_api_key_and_content_type():  # Sig:5
    # urllib.request.Request capitalizes header names, so "X-API-Key" is stored
    # (and fetched) as "X-api-key".
    client = WapuPayClient()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _mock_resp({"ok": True})

    with patch("aqua.wapupay.urllib.request.urlopen", side_effect=fake_urlopen):
        out = client._api_request(
            "POST", "http://h/x", json_body={"a": 1}, api_key="key123"
        )
    assert out == {"ok": True}
    req = captured["req"]
    assert req.get_header("X-api-key") == "key123"
    assert req.get_header("Authorization") is None  # never a Bearer token
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {"a": 1}


def test_api_request_no_auth_header_without_api_key():  # Sig:4
    client = WapuPayClient()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _mock_resp({})

    with patch("aqua.wapupay.urllib.request.urlopen", side_effect=fake_urlopen):
        client._api_request("GET", "http://h/x")
    assert captured["req"].get_header("X-api-key") is None
    assert captured["req"].get_header("Authorization") is None


def test_api_request_401_raises_valueerror_about_config():  # Sig:5
    # A 401 now means the API key is missing/invalid (config error), not an
    # expired session — it surfaces as a plain ValueError, no special type.
    client = WapuPayClient()
    with patch(
        "aqua.wapupay.urllib.request.urlopen",
        side_effect=_http_error(401, '{"detail": "invalid api key"}'),
    ):
        with pytest.raises(ValueError) as ei:
            client._api_request("GET", "http://h/x", api_key="bad")
    assert "401" in str(ei.value)


def test_api_request_other_http_error_raises_valueerror_with_message():  # Sig:5
    client = WapuPayClient()
    with patch(
        "aqua.wapupay.urllib.request.urlopen",
        side_effect=_http_error(400, '{"error": "Invalid payment amount"}'),
    ):
        with pytest.raises(ValueError) as ei:
            client._api_request("POST", "http://h/x", json_body={}, api_key="t")
    assert "Invalid payment amount" in str(ei.value)


def test_api_request_non_json_2xx_raises_valueerror():  # Sig:4
    """A 2xx with a non-JSON body (e.g. an HTML page from a proxy/LB) surfaces a
    clean ValueError, not a leaked json.JSONDecodeError."""
    client = WapuPayClient()
    with patch(
        "aqua.wapupay.urllib.request.urlopen",
        side_effect=lambda req, timeout=None: _mock_resp(b"<html>oops</html>"),
    ):
        with pytest.raises(ValueError) as ei:
            client._api_request("GET", "http://h/x")
    assert "non-json" in str(ei.value).lower()


def test_api_request_unreachable_raises_valueerror():  # Sig:4
    client = WapuPayClient()
    with patch(
        "aqua.wapupay.urllib.request.urlopen",
        side_effect=urllib.error.URLError("conn refused"),
    ):
        with pytest.raises(ValueError) as ei:
            client._api_request("GET", "http://h/x")
    assert "unreachable" in str(ei.value).lower()


def test_client_login_verify_targets():  # Sig:4
    # login/verify are AQUA-account auth — still POSTed to Ankara's /auth/.
    client = WapuPayClient()
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen.setdefault("urls", []).append(req.full_url)
        seen["last_body"] = json.loads(req.data)
        return _mock_resp({"access": "a", "refresh": "r", "message": "ok"})

    with patch("aqua.wapupay.urllib.request.urlopen", side_effect=fake_urlopen):
        client.login("u@e.com", language="es")
        assert seen["last_body"] == {"email": "u@e.com", "language": "es"}
        client.verify("u@e.com", "123456")
        assert seen["last_body"] == {"email": "u@e.com", "otp_code": "123456"}
    assert seen["urls"][0] == f"{AUTH_BASE_URL}/login/"
    assert seen["urls"][1] == f"{AUTH_BASE_URL}/verify/"


def test_client_exchange_rates_is_public():  # Sig:4
    # exchange_rates hits WapuPay directly with NO auth header (public endpoint).
    client = WapuPayClient()
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["api_key"] = req.get_header("X-api-key")
        seen["auth"] = req.get_header("Authorization")
        return _mock_resp({"rates": []})

    with patch("aqua.wapupay.urllib.request.urlopen", side_effect=fake_urlopen):
        client.exchange_rates()
    assert seen["url"] == f"{WAPUPAY_BASE_URL}/exchange_rates"
    assert seen["api_key"] is None  # public — no key sent
    assert seen["auth"] is None  # and never a Bearer token


def test_client_business_call_sends_api_key_not_bearer():  # Sig:5
    # A keyed business call carries X-API-Key and NEVER an Authorization header
    # (WapuPay treats the two as mutually exclusive → 400 if both).
    client = WapuPayClient()
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["api_key"] = req.get_header("X-api-key")
        seen["auth"] = req.get_header("Authorization")
        return _mock_resp({"available": 1})

    with patch("aqua.wapupay.urllib.request.urlopen", side_effect=fake_urlopen):
        client.spending_limit(api_key="secret-key")
    assert seen["url"] == f"{WAPUPAY_BASE_URL}/users/spending_limit"
    assert seen["api_key"] == "secret-key"
    assert seen["auth"] is None


def test_client_uses_configured_base_url_for_native_paths():  # Sig:4
    # WAPUPAY_BASE_URL is read at import, so override per-client (e.g. be-stage)
    # to confirm the native WapuPay subpaths build under it (no Ankara proxy).
    client = WapuPayClient(base_url="https://be-stage.wapu.app")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _mock_resp({})

    with patch("aqua.wapupay.urllib.request.urlopen", side_effect=fake_urlopen):
        client.create_tentative({"amount_ars": 10000}, api_key="k")
    assert seen["url"] == "https://be-stage.wapu.app/transactions/direct-fiat/tentatives"


# ---------------------------------------------------------------------------
# Manager: auth lifecycle
# ---------------------------------------------------------------------------


def test_login_returns_message_and_passes_args(storage):  # Sig:4
    fake = FakeClient({"login": {"message": "sent"}})
    m = make_manager(storage, fake)
    out = m.login("u@e.com", language="es")
    assert out["email"] == "u@e.com"
    assert out["message"] == "sent"
    assert "next_step" in out
    assert fake.calls[0] == ("login", ("u@e.com", "es"))


def test_login_rejects_bad_email(storage):  # Sig:3
    m = make_manager(storage, FakeClient())
    with pytest.raises(ValueError):
        m.login("not-an-email")


def test_verify_persists_session(storage):  # Sig:5
    fake = FakeClient({"verify": {"access": "ACC", "refresh": "REF"}})
    m = make_manager(storage, fake)
    out = m.verify("u@e.com", "123456")
    assert out["logged_in"] is True
    sess = storage.load_wapupay_session()
    assert sess is not None and sess.access == "ACC" and sess.refresh == "REF"
    assert sess.email == "u@e.com"


def test_verify_without_tokens_raises_and_saves_nothing(storage):  # Sig:5
    fake = FakeClient({"verify": {"message": "wrong"}})
    m = make_manager(storage, fake)
    with pytest.raises(ValueError):
        m.verify("u@e.com", "000000")
    assert storage.load_wapupay_session() is None


def test_logout_and_session_status(storage):  # Sig:3
    m = make_manager(storage, FakeClient())
    assert m.session_status() == {"logged_in": False}
    logged_in(storage, email="x@y.com")
    st = m.session_status()
    assert st["logged_in"] is True and st["email"] == "x@y.com"
    assert "access" not in st and "refresh" not in st  # no secrets leaked
    assert m.logout()["logged_out"] is True
    assert storage.load_wapupay_session() is None


def test_business_call_without_api_key_raises(monkeypatch, storage):  # Sig:5
    # A keyed business call with no WAPUPAY_API_KEY fails fast with a clear,
    # config-pointing ValueError (not a "not logged in" / session error).
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    m = make_manager(storage, FakeClient())
    with pytest.raises(ValueError) as ei:
        m.spending_limit()
    assert "WAPUPAY_API_KEY" in str(ei.value)


def test_exchange_rates_works_without_api_key(monkeypatch, storage):  # Sig:4
    # exchange_rates is public + decoupled from any login: no key, no session.
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    fake = FakeClient({"exchange_rates": {"rates": ["ok"]}})
    m = make_manager(storage, fake)
    assert m.exchange_rates() == {"rates": ["ok"]}
    assert fake.calls == [("exchange_rates", ())]  # called with no auth args


# ---------------------------------------------------------------------------
# Manager: quote + order lifecycle
# ---------------------------------------------------------------------------


def test_quote_forces_currencies_and_validates_type(storage):  # Sig:5
    fake = FakeClient({"tentative_amount": {"usdt_amount": 6.99, "valid_cbu_alias": True}})
    m = make_manager(storage, fake)
    out = m.quote("10000", "fiat_transfer", alias="al.cbu")
    assert out["valid_cbu_alias"] is True
    name, (body, api_key) = fake.calls[0]
    assert name == "tentative_amount"
    assert api_key == "test-api-key"  # X-API-Key carried, not a session token
    assert body["currency_payment"] == "ARS"
    assert body["currency_taken"] == "USDT"
    assert body["amount"] == 10000
    assert body["type"] == "fiat_transfer"
    assert body["alias"] == "al.cbu"
    with pytest.raises(ValueError):
        m.quote("10000", "bogus_type")


def test_create_order_pins_rail_and_persists_then_funds(storage):  # Sig:5
    fake = FakeClient({
        "create_tentative": dict(CREATE_RESP),
        "issue_funding": dict(FUNDING_RESP),
    })
    m = make_manager(storage, fake)
    out = m.create_order(
        amount_ars="10000", alias="al.cbu", transfer_type="fiat_transfer",
        receiver_name="Jane Doe", wallet_name="default",
    )
    # Rail pinned + X-API-Key carried on the create body.
    create_call = next(c for c in fake.calls if c[0] == "create_tentative")
    body, create_key = create_call[1]
    assert create_key == "test-api-key"
    assert body["funding_method"] == FUNDING_METHOD_USDT
    assert body["network"] == FUNDING_NETWORK_LIQUID
    assert body["amount_ars"] == 10000
    assert body["alias"] == "al.cbu"
    assert body["receiver_name"] == "Jane Doe"
    # Funding call carries the key too (tentative_id, api_key).
    fund_call = next(c for c in fake.calls if c[0] == "issue_funding")
    assert fund_call[1] == (TENTATIVE_ID, "test-api-key")
    # Funded result surfaces the Liquid address + integer sats.
    assert out["funded"] is True
    assert out["address_destination"] == "lq1qqfunding0address"
    assert out["funding_amount_sat"] == 699000000
    assert out["asset_id"] == FUNDING_RESP["asset_id"]
    assert "pay_instructions" in out
    # Persisted with funding applied.
    saved = storage.load_wapupay_order(TENTATIVE_ID)
    assert saved.status == "FUNDING_ISSUED"
    assert saved.address_destination == "lq1qqfunding0address"


def test_create_order_persists_before_funding_failure(storage):  # Sig:5
    """Funding failure leaves a recoverable CREATED order — no orphan, no fake success."""
    fake = FakeClient({
        "create_tentative": dict(CREATE_RESP),
        "issue_funding": ValueError("funding upstream 400"),
    })
    m = make_manager(storage, fake)
    out = m.create_order(amount_ars="10000", alias="al.cbu", transfer_type="fiat_transfer")
    assert out["funded"] is False
    assert out["tentative_id"] == TENTATIVE_ID
    assert "funding upstream 400" in out["last_error"]
    assert "wapupay_fund_order" in out["next_step"]
    # The order was persisted BEFORE funding was attempted.
    saved = storage.load_wapupay_order(TENTATIVE_ID)
    assert saved is not None
    assert saved.status == "CREATED"
    assert saved.address_destination is None


def test_create_order_without_api_key_raises_before_persist(monkeypatch, storage):  # Sig:5
    """No WAPUPAY_API_KEY → ValueError up front: no network call, no order persisted."""
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    fake = FakeClient({"create_tentative": dict(CREATE_RESP)})
    m = make_manager(storage, fake)
    with pytest.raises(ValueError) as ei:
        m.create_order(amount_ars="10000", alias="al.cbu", transfer_type="fiat_transfer")
    assert "WAPUPAY_API_KEY" in str(ei.value)
    assert fake.calls == []  # never reached the network
    assert storage.list_wapupay_orders() == []  # nothing persisted


def test_create_order_missing_tentative_id_raises(storage):  # Sig:5
    fake = FakeClient({"create_tentative": {"status": "CREATED"}})  # no tentative_id
    m = make_manager(storage, fake)
    with pytest.raises(ValueError):
        m.create_order(amount_ars="10000", alias="al.cbu", transfer_type="fiat_transfer")


def test_create_order_non_dict_response_raises_valueerror(storage):  # Sig:4
    """A non-dict create response yields a clean ValueError, not AttributeError."""
    # callable form returns the list verbatim (a bare list value is a response queue)
    fake = FakeClient({"create_tentative": lambda *a: [1, 2]})
    m = make_manager(storage, fake)
    with pytest.raises(ValueError):
        m.create_order(amount_ars="10000", alias="al.cbu", transfer_type="fiat_transfer")


def test_create_order_validates_inputs(storage):  # Sig:4
    m = make_manager(storage, FakeClient())
    with pytest.raises(ValueError):
        m.create_order(amount_ars="10000", alias="", transfer_type="fiat_transfer")
    with pytest.raises(ValueError):
        m.create_order(amount_ars="0", alias="al.cbu", transfer_type="fiat_transfer")
    with pytest.raises(ValueError):
        m.create_order(amount_ars="10000", alias="al.cbu", transfer_type="bad")


def test_fund_order_recovers_created_order(storage):  # Sig:5
    # Pre-existing CREATED order (e.g. from a prior funding failure).
    storage.save_wapupay_order(WapuPayOrder(
        tentative_id=TENTATIVE_ID, status="CREATED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="t0",
    ))
    fake = FakeClient({"issue_funding": dict(FUNDING_RESP)})
    m = make_manager(storage, fake)
    out = m.fund_order(TENTATIVE_ID)
    assert out["funded"] is True
    assert out["address_destination"] == "lq1qqfunding0address"
    saved = storage.load_wapupay_order(TENTATIVE_ID)
    assert saved.status == "FUNDING_ISSUED"
    assert saved.last_error is None


def test_order_status_refreshes_and_flags(storage):  # Sig:5
    storage.save_wapupay_order(WapuPayOrder(
        tentative_id=TENTATIVE_ID, status="FUNDING_ISSUED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="t0",
    ))
    fake = FakeClient({"get_tentative": {"status": "EXECUTED", "executed_transaction_id": "abc"}})
    m = make_manager(storage, fake)
    out = m.order_status(TENTATIVE_ID)
    assert out["status"] == "EXECUTED"
    assert out["is_final"] and out["is_success"] and not out["is_failed"]
    assert out["executed_transaction_id"] == "abc"
    assert storage.load_wapupay_order(TENTATIVE_ID).status == "EXECUTED"


def test_order_status_warns_when_remote_fails_but_local_exists(storage):  # Sig:4
    storage.save_wapupay_order(WapuPayOrder(
        tentative_id=TENTATIVE_ID, status="FUNDING_ISSUED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="t0",
    ))
    fake = FakeClient({"get_tentative": ValueError("upstream 500")})
    m = make_manager(storage, fake)
    out = m.order_status(TENTATIVE_ID)
    assert out["status"] == "FUNDING_ISSUED"  # falls back to last-known local
    assert "warning" in out


def test_order_status_unknown_order_raises_when_remote_fails(storage):  # Sig:3
    fake = FakeClient({"get_tentative": ValueError("nope")})
    m = make_manager(storage, fake)
    unknown = "00000000-0000-0000-0000-000000000000"  # valid UUID, not stored
    with pytest.raises(ValueError):
        m.order_status(unknown)


def test_fund_order_rejects_malformed_id_without_network(storage):  # Sig:5
    fake = FakeClient({"issue_funding": ValueError("must not be called")})
    m = make_manager(storage, fake)
    with pytest.raises(ValueError) as ei:
        m.fund_order("not-a-uuid")
    assert "tentative_id" in str(ei.value).lower()
    assert fake.calls == []  # rejected before any network call


def test_read_only_delegations(storage):  # Sig:3
    fake = FakeClient({
        "my_transactions": {"transactions": [1, 2]},
        "get_transaction": {"transaction_id": "tx1"},
        "spending_limit": {"available": 376.55},
    })
    m = make_manager(storage, fake)
    assert m.transactions() == {"transactions": [1, 2]}
    assert m.transaction("tx1")["transaction_id"] == "tx1"
    assert m.spending_limit()["available"] == 376.55
    # Every business read carries the X-API-Key (recorded as the last call arg).
    assert ("my_transactions", ("test-api-key",)) in fake.calls
    assert ("get_transaction", ("tx1", "test-api-key")) in fake.calls
    assert ("spending_limit", ("test-api-key",)) in fake.calls


def test_list_orders_sorted(storage):  # Sig:2
    for tid, created in [("a" * 8, "2026-01-01"), ("b" * 8, "2026-02-01")]:
        storage.save_wapupay_order(WapuPayOrder(
            tentative_id=tid, status="CREATED", type="fiat_transfer",
            amount_ars="10000", alias="al.cbu", created_at=created,
        ))
    m = make_manager(storage, FakeClient())
    orders = m.list_orders()
    assert [o["created_at"] for o in orders] == ["2026-02-01", "2026-01-01"]


def test_list_orders_skips_corrupt_file(storage):  # Sig:4
    storage.save_wapupay_order(WapuPayOrder(
        tentative_id="a" * 8, status="CREATED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="2026-01-01",
    ))
    # A corrupt order file must not abort listing the good ones.
    (storage.wapupay_orders_dir / "bbbbbbbb.json").write_text("{ not json")
    m = make_manager(storage, FakeClient())
    orders = m.list_orders()
    assert [o["tentative_id"] for o in orders] == ["a" * 8]


# ---------------------------------------------------------------------------
# Tool layer: error envelope path uses the global manager
# ---------------------------------------------------------------------------


def test_tool_create_order_without_api_key_raises(monkeypatch, storage):  # Sig:5
    """tools.wapupay_create_order surfaces a ValueError (server wraps it into the
    error envelope) when WAPUPAY_API_KEY is unset. Routed through a temp-storage
    manager so no real ~/.aqua."""
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    mgr = make_manager(storage, FakeClient())
    monkeypatch.setattr(tools, "_wapupay_manager", mgr)
    with pytest.raises(ValueError):
        tools.wapupay_create_order(amount_ars="10000", alias="al.cbu")


def test_tool_create_order_attaches_qr(monkeypatch, storage):  # Sig:4
    fake = FakeClient({
        "create_tentative": dict(CREATE_RESP),
        "issue_funding": dict(FUNDING_RESP),
    })
    mgr = make_manager(storage, fake)
    monkeypatch.setattr(tools, "_wapupay_manager", mgr)
    # tools._attach_deposit_qr uses get_manager().storage.qr_dir; point it at temp.
    monkeypatch.setattr(tools, "get_manager", lambda: mgr)
    out = tools.wapupay_create_order(amount_ars="10000", alias="al.cbu")
    assert out["address_destination"] == "lq1qqfunding0address"
    assert "qr_code_path" in out or "qr_error" in out
