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
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import aqua.tools as tools
from aqua.ankara import (
    JAN3AccountManager,
    JAN3Session,
    _extract_error_message,
    _mask,
    _redact,
)
from aqua.storage import Storage
from aqua.wapupay import (
    FUNDING_METHOD_USDT,
    FUNDING_NETWORK_LIQUID,
    WAPUPAY_BASE_URL,
    WapuPayApiKey,
    WapuPayClient,
    WapuPayManager,
    WapuPayOrder,
    _ars_for_wire,
    _normalize_ars_amount,
    order_is_failed,
    order_is_final,
    order_is_success,
    usdt_to_base_units,
    validate_liquid_refund_address,
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

    def provision_wapupay_account(self, access_token):
        return self._yield("provision_wapupay_account", access_token)

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
    # One scriptable fake stands in for both client seams: the JAN3 auth client
    # (login / verify / provision) and the WapuPay client. The JAN3
    # account manager is injected so provision_account() resolves the session +
    # backend call through it, exactly as get_wapupay_manager() wires production.
    jan3 = JAN3AccountManager(storage=storage)
    jan3._client = fake
    m = WapuPayManager(
        storage=storage, wallet_manager=DummyWallet(), jan3_manager=jan3
    )
    m._client = fake
    return m


def logged_in(storage, email="user@example.com", access="acc.tok", refresh="ref.tok"):
    storage.save_jan3_session(
        JAN3Session(email=email, access=access, refresh=refresh, created_at="t0")
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
    "funding_expires_at": "2026-05-24T14:35:00Z",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_usdt_to_base_units_precision():  # Sig:4
    assert usdt_to_base_units("6.99") == 699000000
    assert usdt_to_base_units("0.00000001") == 1
    assert usdt_to_base_units(1) == 100000000


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


def test_order_apply_tentative_derives_total_base_units_and_does_not_wipe():  # Sig:4
    order = WapuPayOrder(
        tentative_id=TENTATIVE_ID, status="CREATED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="t0",
    )
    # The send amount is derived from the TOTAL (funding + fee), not funding.
    order.apply_tentative({"total_amount_usdt": 7.13, "address_destination": "lq1x"})
    assert order.total_funding_amount_base_units == usdt_to_base_units("7.13")
    # A USDT/Liquid order never carries WapuPay's real BTC/Lightning sat field.
    assert order.funding_amount_sat is None
    # A later status poll that omits funding fields must not erase the address.
    order.apply_tentative({"status": "EXECUTED"})
    assert order.address_destination == "lq1x"
    assert order.status == "EXECUTED"
    # A re-quote that changes the total must re-derive (no stale base units).
    order.apply_tentative({"total_amount_usdt": 10.0})
    assert order.total_funding_amount_base_units == usdt_to_base_units("10.0")


def test_apply_tentative_passes_through_real_btc_sats_as_int():  # Sig:5
    """funding_amount_sat is WapuPay's REAL value for BTC/Lightning rails —
    passed through (never derived from USDT) and coerced to int if WapuPay
    sends it as a float, so lw_send_asset never gets a float."""
    order = WapuPayOrder(
        tentative_id=TENTATIVE_ID, status="FUNDING_ISSUED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="t0",
    )
    order.apply_tentative({"funding_network": "LIGHTNING", "funding_amount_sat": 699000000.0})
    assert order.funding_amount_sat == 699000000
    assert isinstance(order.funding_amount_sat, int)


def test_apply_tentative_rejects_non_dict():  # Sig:4
    order = WapuPayOrder(
        tentative_id=TENTATIVE_ID, status="CREATED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="t0",
    )
    with pytest.raises(ValueError):
        order.apply_tentative([1, 2])  # type: ignore[arg-type]


def test_money_fields_are_decimal_and_serialize_as_str():  # Sig:5
    """USDT amounts / rate are Decimal in memory (never float — CLAUDE.md
    invariant #1) and serialize as strings (JSON has no Decimal)."""
    order = WapuPayOrder(
        tentative_id=TENTATIVE_ID, status="CREATED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="t0",
    )
    order.apply_tentative({
        "total_amount_usdt": 7.13, "fee_amount_usdt": 0.14,
        "funding_amount_usdt": 6.99, "exchange_rate": 1432.5,
    })
    for fld in ("total_amount_usdt", "fee_amount_usdt", "funding_amount_usdt", "exchange_rate"):
        assert isinstance(getattr(order, fld), Decimal), fld
    data = order.to_dict()
    assert data["total_amount_usdt"] == "7.13"  # string, no float drift
    assert data["fee_amount_usdt"] == "0.14"
    # Round-trips back to Decimal (and the integer send amount is unaffected).
    reloaded = WapuPayOrder.from_dict(data)
    assert reloaded.total_amount_usdt == Decimal("7.13")
    assert isinstance(reloaded.total_amount_usdt, Decimal)
    assert reloaded.total_funding_amount_base_units == usdt_to_base_units("7.13")


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
# Manager: business calls + order orchestration
# (AQUA-account auth lives in tests/test_ankara.py)
# ---------------------------------------------------------------------------


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
    # Funded result surfaces the Liquid address + the integer TOTAL to send
    # (derived from total_amount_usdt=7.13, i.e. funding 6.99 + fee 0.14).
    assert out["funded"] is True
    assert out["address_destination"] == "lq1qqfunding0address"
    assert out["total_funding_amount_base_units"] == usdt_to_base_units("7.13")
    assert out["funding_amount_sat"] is None  # USDT/Liquid rail: no real BTC sats
    assert out["asset_id"] == FUNDING_RESP["asset_id"]
    # pay_instructions must quote the TOTAL (7.13 / 713000000), include the fee,
    # and never fabricate a "None" amount.
    assert "7.13 USDT" in out["pay_instructions"]
    assert "713000000 base units" in out["pay_instructions"]
    assert "0.14 USDT fee" in out["pay_instructions"]
    assert "None" not in out["pay_instructions"]
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


def test_create_order_rejects_non_uuid_tentative_id(storage):  # Sig:5
    """A non-UUID tentative_id from WapuPay is rejected with the SAME rule
    fund_order / order_status use, so we never persist an un-pollable order or
    attempt funding for it (storage's looser SWAP_ID_PATTERN would accept it)."""
    fake = FakeClient(
        {"create_tentative": {"tentative_id": "not-a-uuid", "status": "CREATED"}}
    )
    m = make_manager(storage, fake)
    with pytest.raises(ValueError, match="UUID"):
        m.create_order(amount_ars="10000", alias="al.cbu", transfer_type="fiat_transfer")
    # create_tentative ran, but funding was never attempted and nothing persisted.
    assert [c[0] for c in fake.calls] == ["create_tentative"]
    assert storage.list_wapupay_orders() == []


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


# A real Liquid mainnet (confidential lq1…) and testnet (tlq1…) address, used to
# prove the refund-address validator rejects garbage and wrong-network input.
_MAINNET_REFUND = (
    "lq1qqvxk052kf3qtkxmrakx50a9gc3smqad2ync54hzntjt980kfej9kkfe0247rp5h"
    "4yzmdftsahhw64uy8pzfe7cpg4fgykm7cv"
)
_TESTNET_REFUND = (
    "tlq1qq2xvpcvfup5j8zscjq05u2wxxjcyewk7979f3mmz5l7uw5pqmx6xf5xy50hsn6"
    "vhkm5euwt72x878eq6zxx2z58hd7zrsg9qn"
)
# A legacy base58 confidential mainnet address (VJL…) — still valid + deliverable.
_LEGACY_REFUND = (
    "VJL8PjuoM1VQZ3xhxq9sLChhXrG2NizaHTvM6oM9pUBF16gRoTq81qADeeLCdkdRhjCSnuA3YBN679gp"
)


def test_validate_liquid_refund_address_helper():  # Sig:4
    # Valid mainnet address parses and is returned stripped.
    assert validate_liquid_refund_address(f"  {_MAINNET_REFUND}  ") == _MAINNET_REFUND
    # Legacy base58 confidential mainnet addresses (VJL…) must be accepted too —
    # validation parses + checks network, never gates on the bech32 prefix.
    assert validate_liquid_refund_address(_LEGACY_REFUND) == _LEGACY_REFUND
    # Malformed address (the user's literal example) is rejected on format.
    with pytest.raises(ValueError, match="not a valid Liquid address"):
        validate_liquid_refund_address("lq12341234")
    # A well-formed but wrong-network (testnet) address is rejected on network.
    with pytest.raises(ValueError, match="mainnet"):
        validate_liquid_refund_address(_TESTNET_REFUND)


def test_create_order_rejects_invalid_refund_address(storage):  # Sig:5
    """A bad refund address fails before any network call or persistence."""
    fake = FakeClient({"create_tentative": dict(CREATE_RESP)})
    m = make_manager(storage, fake)
    with pytest.raises(ValueError, match="not a valid Liquid address"):
        m.create_order(
            amount_ars="10000", alias="al.cbu", transfer_type="fiat_transfer",
            refund_address="lq12341234",
        )
    assert fake.calls == []  # never reached the network
    assert storage.list_wapupay_orders() == []  # nothing persisted


def test_create_order_rejects_non_mainnet_refund_address(storage):  # Sig:5
    fake = FakeClient({"create_tentative": dict(CREATE_RESP)})
    m = make_manager(storage, fake)
    with pytest.raises(ValueError, match="mainnet"):
        m.create_order(
            amount_ars="10000", alias="al.cbu", transfer_type="fiat_transfer",
            refund_address=_TESTNET_REFUND,
        )
    assert fake.calls == []
    assert storage.list_wapupay_orders() == []


def test_create_order_carries_valid_refund_address(storage):  # Sig:5
    """A valid mainnet refund address is sent on the create body and persisted."""
    fake = FakeClient({
        "create_tentative": dict(CREATE_RESP),
        "issue_funding": dict(FUNDING_RESP),
    })
    m = make_manager(storage, fake)
    out = m.create_order(
        amount_ars="10000", alias="al.cbu", transfer_type="fiat_transfer",
        refund_address=f"  {_MAINNET_REFUND}  ",
    )
    body, _ = next(c for c in fake.calls if c[0] == "create_tentative")[1]
    assert body["refund_address"] == _MAINNET_REFUND  # normalized (stripped)
    assert out["refund_address"] == _MAINNET_REFUND
    saved = storage.load_wapupay_order(TENTATIVE_ID)
    assert saved.refund_address == _MAINNET_REFUND


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


def test_fund_order_thin_record_no_total_does_not_fabricate_amount(storage):  # Sig:5
    """Cross-device fund_order: no local order and the funding response carries
    no total_amount_usdt, so the exact total isn't known. pay_instructions must
    NOT print "Send exactly None USDT" — it points at order-status instead."""
    # FUNDING_RESP has no total_amount_usdt; no pre-existing local order.
    fake = FakeClient({"issue_funding": dict(FUNDING_RESP)})
    m = make_manager(storage, fake)
    out = m.fund_order(TENTATIVE_ID)
    assert out["funded"] is True
    assert out["address_destination"] == "lq1qqfunding0address"
    assert out["total_funding_amount_base_units"] is None
    assert "None" not in out["pay_instructions"]
    assert "wapupay_order_status" in out["pay_instructions"]


def test_fund_order_thin_record_with_total_but_no_fee_uses_placeholder(storage):  # Sig:5
    """A thin / cross-device funding response can carry the total but no fee.
    pay_instructions must show a placeholder, never a literal "None USDT" fee."""
    funding = {
        "tentative_id": TENTATIVE_ID, "status": "FUNDING_ISSUED",
        "address_destination": "lq1qqfunding0address",
        "asset_id": "ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2",
        "total_amount_usdt": 5.0,  # total known, but fee_amount_usdt absent
    }
    fake = FakeClient({"issue_funding": funding})
    m = make_manager(storage, fake)
    out = m.fund_order(TENTATIVE_ID)
    assert out["funded"] is True
    assert out["total_funding_amount_base_units"] == usdt_to_base_units("5.0")
    assert "None" not in out["pay_instructions"]
    assert "0 USDT fee" in out["pay_instructions"]


def test_from_dict_migration_drops_stale_sat_on_none_network(storage):  # Sig:5
    """A legacy thin record (funding_network missing) with a stale USDT-derived
    funding_amount_sat must NOT load as a real BTC sat. It's dropped, and the
    base-units send amount re-derives from the persisted total_amount_usdt."""
    legacy = {
        "tentative_id": TENTATIVE_ID, "status": "FUNDING_ISSUED",
        "type": "fiat_transfer", "amount_ars": "10000", "alias": "al.cbu",
        "created_at": "t0", "total_amount_usdt": 3.53,
        "funding_amount_sat": 339000000,  # old USDT-derived value, NOT real sats
        # no funding_network (old thin record)
    }
    o = WapuPayOrder.from_dict(legacy)
    assert o.funding_amount_sat is None  # stale value dropped
    assert o.total_funding_amount_base_units == usdt_to_base_units("3.53")


def test_from_dict_migration_keeps_real_sat_on_btc_network():  # Sig:5
    """On a genuine BTC/Lightning rail, WapuPay's real funding_amount_sat is a
    passthrough and must survive a load round-trip (not dropped)."""
    btc = {
        "tentative_id": TENTATIVE_ID, "status": "FUNDING_ISSUED",
        "type": "fiat_transfer", "amount_ars": "10000", "alias": "al.cbu",
        "created_at": "t0", "funding_network": "LIGHTNING",
        "funding_amount_sat": 699000000,  # real BTC sats from the wire
    }
    o = WapuPayOrder.from_dict(btc)
    assert o.funding_amount_sat == 699000000  # preserved


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


def test_tool_orders_wraps_local_records(monkeypatch, storage):  # Sig:3
    """wapupay_orders returns orders with txids for tracking."""
    storage.save_wapupay_order(WapuPayOrder(
        tentative_id="a" * 8, status="CREATED", type="fiat_transfer",
        amount_ars="10000", alias="al.cbu", created_at="2026-01-01",
        funding_transaction_id="fund_tx", executed_transaction_id="exec_tx",
    ))
    storage.save_wapupay_order(WapuPayOrder(
        tentative_id="b" * 8, status="EXECUTED", type="fiat_transfer",
        amount_ars="20000", alias="al.cbu", created_at="2026-02-01",
    ))
    mgr = make_manager(storage, FakeClient())
    monkeypatch.setattr(tools, "_wapupay_manager", mgr)
    out = tools.wapupay_orders()
    assert list(out.keys()) == ["orders"]
    assert [o["tentative_id"] for o in out["orders"]] == ["b" * 8, "a" * 8]
    assert out["orders"][1]["funding_transaction_id"] == "fund_tx"
    assert out["orders"][1]["executed_transaction_id"] == "exec_tx"


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


# ---------------------------------------------------------------------------
# WapuPayManager.provision_account — orchestration only (the AQUA-backend call
# is delegated to JAN3AccountManager; the JAN3AuthClient HTTP tests for
# provisioning live in tests/test_ankara.py)
# ---------------------------------------------------------------------------


def test_provision_manager_success_stores_and_masks(monkeypatch, storage):  # Sig:5
    # No key configured (env unset, none stored) -> the no-op is skipped and the
    # backend is called.
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    logged_in(storage, access="aqua.jwt.access")
    fake = FakeClient({"provision_wapupay_account": {"token": "WapuKey_2UJgLDyuY8"}})
    m = make_manager(storage, fake)
    out = m.provision_account()

    assert out["provisioned"] is True
    assert "rotated" not in out  # dropped: it lied (false on a backend-side rotation)
    # Backend always rotates -> an honest note is present whenever it was called.
    assert "warning" in out and "invalidates" in out["warning"].lower()
    # Raw key never returned; only a masked preview (first 4 + … + last 4).
    assert out["key_preview"] == _mask("WapuKey_2UJgLDyuY8") == "Wapu…yuY8"
    assert "WapuKey_2UJgLDyuY8" not in json.dumps(out)
    # The provisioning call carried the stored AQUA access token.
    assert fake.calls == [("provision_wapupay_account", ("aqua.jwt.access",))]
    # Persisted at the api-key path and now resolvable by _require_api_key.
    stored = storage.load_wapupay_api_key()
    assert stored is not None and stored.token == "WapuKey_2UJgLDyuY8"
    assert m._require_api_key() == "WapuKey_2UJgLDyuY8"


def test_provision_first_time_carries_backend_rotation_note(monkeypatch, storage):
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    logged_in(storage)
    fake = FakeClient({"provision_wapupay_account": {"token": "fresh-first-key"}})
    m = make_manager(storage, fake)
    out = m.provision_account()  # no key yet -> hits backend
    assert out["provisioned"] is True
    assert "rotated" not in out
    assert "warning" in out and "invalidates" in out["warning"].lower()
    assert fake.calls == [("provision_wapupay_account", ("acc.tok",))]


def test_provision_manager_requires_login(monkeypatch, storage):  # Sig:5
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    m = make_manager(storage, FakeClient())
    with pytest.raises(ValueError) as ei:
        m.provision_account()
    assert "aqua_login" in str(ei.value).lower() or "logged in" in str(ei.value).lower()
    assert m._client.calls == []  # never reached the backend


def test_provision_manager_missing_token_raises(monkeypatch, storage):  # Sig:5
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    logged_in(storage)
    fake = FakeClient({"provision_wapupay_account": {"not_token": "x"}})
    m = make_manager(storage, fake)
    with pytest.raises(ValueError) as ei:
        m.provision_account()
    assert "token" in str(ei.value).lower()
    assert storage.load_wapupay_api_key() is None  # nothing persisted on failure


def test_provision_noop_when_env_key_set(monkeypatch, storage):  # Sig:5
    # env key present -> no-op, NO backend call (env key never invalidated).
    monkeypatch.setenv("WAPUPAY_API_KEY", "env-set-key")
    logged_in(storage)
    fake = FakeClient({"provision_wapupay_account": {"token": "should-not-be-used"}})
    m = make_manager(storage, fake)
    out = m.provision_account()
    assert out["already_configured"] is True and out["source"] == "env"
    assert fake.calls == []
    assert storage.load_wapupay_api_key() is None  # didn't overwrite/persist
    assert "env-set-key" not in json.dumps(out)  # masked


def test_provision_noop_when_stored_key_exists(monkeypatch, storage):  # Sig:5
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    storage.save_wapupay_api_key(WapuPayApiKey(token="already-stored", created_at="t0"))
    logged_in(storage)
    fake = FakeClient({"provision_wapupay_account": {"token": "new-key"}})
    m = make_manager(storage, fake)
    out = m.provision_account()
    assert out["already_configured"] is True and out["source"] == "stored"
    assert fake.calls == []
    # Existing key untouched.
    assert storage.load_wapupay_api_key().token == "already-stored"


def test_require_api_key_env_wins_over_stored(monkeypatch, storage):  # Sig:5
    monkeypatch.setenv("WAPUPAY_API_KEY", "env-key")
    storage.save_wapupay_api_key(WapuPayApiKey(token="stored-key", created_at="t0"))
    m = make_manager(storage, FakeClient())
    assert m._require_api_key() == "env-key"


def test_require_api_key_falls_back_to_stored(monkeypatch, storage):  # Sig:5
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    storage.save_wapupay_api_key(WapuPayApiKey(token="stored-key", created_at="t0"))
    m = make_manager(storage, FakeClient())
    assert m._require_api_key() == "stored-key"


def test_require_api_key_missing_mentions_provision(monkeypatch, storage):  # Sig:4
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    m = make_manager(storage, FakeClient())
    with pytest.raises(ValueError) as ei:
        m._require_api_key()
    assert "wapupay_provision_account" in str(ei.value)


def test_token_field_is_redacted():  # Sig:5
    out = _redact({"token": "supersecret", "email": "a@b.com"})
    assert out["token"] == "***"
    assert out["email"] == "a@b.com"


def test_mask_short_and_long():  # Sig:3
    assert _mask("") == ""
    assert _mask("short8ch") == "***"  # <= 8 chars
    assert _mask("abcdEFGHijkl") == "abcd…ijkl"


def test_logout_keeps_api_key(monkeypatch, storage):  # Sig:5
    # The API key is decoupled from the AQUA login session: logout must not delete it.
    monkeypatch.delenv("WAPUPAY_API_KEY", raising=False)
    logged_in(storage)
    storage.save_wapupay_api_key(WapuPayApiKey(token="keep-me", created_at="t0"))
    m = make_manager(storage, FakeClient())
    m.jan3.logout()
    assert storage.load_jan3_session() is None
    assert storage.load_wapupay_api_key().token == "keep-me"
