"""Tests for Ankara Lightning receive integration (Layers 1-7)."""

import io
import json
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aqua.ankara import (
    ANKARA_API_URL,
    AUTH_BASE_URL,
    WAPUPAY_ACCOUNT_PATH,
    AnkaraClient,
    AnkaraSwapInfo,
    JAN3AccountManager,
    JAN3AuthClient,
    JAN3Session,
    SessionExpiredError,
    _access_token_expired,
    _AuthExpired,
    _jwt_exp,
)
from aqua.storage import Storage
from aqua.wallet import WalletManager
from tests.conftest import TEST_MNEMONIC

VALID_INVOICE = "lnbc500u1ptest_valid_bolt11_invoice_data"

MOCK_ANKARA_CREATE_RESPONSE = {
    "swap_id": "ankara_test_123",
    "boltz_swap_id": "boltz_abc_456",
    "invoice": VALID_INVOICE,
}

MOCK_ANKARA_VERIFY_RESPONSE_PENDING = {
    "settled": False,
}

MOCK_ANKARA_VERIFY_RESPONSE_SETTLED = {
    "settled": True,
    "preimage": "aa" * 32,
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


@pytest.fixture(autouse=True)
def isolated_manager():
    """Replace the global manager with one using a temp directory."""
    import aqua.tools as tools_module

    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Storage(Path(tmpdir))
        manager = WalletManager(storage=storage)
        tools_module._manager = manager
        tools_module._btc_manager = None
        yield manager
        tools_module._manager = None
        tools_module._btc_manager = None


@pytest.fixture
def test_wallet(isolated_manager):
    """Create a test wallet with balance."""
    isolated_manager.import_mnemonic(TEST_MNEMONIC, "default", "testnet")
    return isolated_manager.load_wallet("default")


class TestAnkaraSwapInfo:
    """Tests for AnkaraSwapInfo dataclass."""

    def test_to_dict(self):
        """Convert AnkaraSwapInfo to dict."""
        swap = AnkaraSwapInfo(
            swap_id="test_swap",
            boltz_swap_id="boltz_123",
            invoice="lnbc...",
            address="lq1test",
            amount=100000,
            wallet_name="default",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
        )
        result = swap.to_dict()
        assert result["swap_id"] == "test_swap"
        assert result["status"] == "pending"

    def test_from_dict(self):
        """Create AnkaraSwapInfo from dict."""
        data = {
            "swap_id": "test_swap",
            "boltz_swap_id": "boltz_123",
            "invoice": "lnbc...",
            "address": "lq1test",
            "amount": 100000,
            "wallet_name": "default",
            "status": "pending",
            "created_at": "2026-03-01T00:00:00+00:00",
            "preimage": None,
        }
        swap = AnkaraSwapInfo.from_dict(data)
        assert swap.swap_id == "test_swap"
        assert swap.wallet_name == "default"

    def test_with_preimage(self):
        """AnkaraSwapInfo with preimage."""
        swap = AnkaraSwapInfo(
            swap_id="test",
            boltz_swap_id="bolt",
            invoice="lnbc",
            address="lq1",
            amount=1000,
            wallet_name="w",
            status="settled",
            created_at="2026-01-01T00:00:00+00:00",
            preimage="aa" * 32,
        )
        assert swap.preimage == "aa" * 32
        assert swap.to_dict()["preimage"] == "aa" * 32


class TestAnkaraClientHTTP:
    """Tests for AnkaraClient HTTP communication."""

    def test_create_swap_success(self):
        """POST /api/v1/lightning/swaps/create/ succeeds."""
        client = AnkaraClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(MOCK_ANKARA_CREATE_RESPONSE)
            result = client.create_swap(100000, "lq1test")
            assert result["swap_id"] == "ankara_test_123"
            assert result["invoice"] == VALID_INVOICE

    def test_create_swap_http_error(self):
        """POST /api/v1/lightning/swaps/create/ handles HTTP error."""
        client = AnkaraClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_error = urllib.error.HTTPError(
                "url", 400, "Bad Request", {}, MagicMock()
            )
            mock_error.read = MagicMock(
                return_value=json.dumps({"error": "Invalid amount"}).encode()
            )
            mock_urlopen.side_effect = mock_error
            with pytest.raises(RuntimeError) as exc:
                client.create_swap(0, "lq1test")
            assert "Invalid amount" in str(exc.value)

    def test_claim_swap_success(self):
        """POST /api/v1/lightning/swaps/{swap_id}/claim/ succeeds."""
        client = AnkaraClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response({"status": "claimed"})
            result = client.claim_swap("test_swap_123")
            assert result["status"] == "claimed"

    def test_verify_swap_success(self):
        """GET /api/v1/lightning/lnurlp/verify/{swap_id} succeeds."""
        client = AnkaraClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(
                MOCK_ANKARA_VERIFY_RESPONSE_SETTLED
            )
            result = client.verify_swap("test_swap_123")
            assert result["settled"] is True
            assert result["preimage"] == "aa" * 32

    def test_api_request_url_error(self):
        """_api_request handles URLError."""
        client = AnkaraClient()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
            with pytest.raises(RuntimeError) as exc:
                client.create_swap(100000, "lq1test")
            assert "unreachable" in str(exc.value)


class TestAnkaraStoragePersistence:
    """Tests for Ankara swap storage operations."""

    @pytest.fixture
    def storage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Storage(Path(tmpdir))

    def test_save_ankara_swap(self, storage):
        """save_ankara_swap writes to disk."""
        swap = AnkaraSwapInfo(
            swap_id="test_123",
            boltz_swap_id="boltz_456",
            invoice="lnbc...",
            address="lq1test",
            amount=100000,
            wallet_name="default",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
        )
        storage.save_ankara_swap(swap)
        assert (storage.ankara_swaps_dir / "test_123.json").exists()

    def test_load_ankara_swap(self, storage):
        """load_ankara_swap reads from disk."""
        swap = AnkaraSwapInfo(
            swap_id="test_123",
            boltz_swap_id="boltz_456",
            invoice="lnbc...",
            address="lq1test",
            amount=100000,
            wallet_name="default",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
        )
        storage.save_ankara_swap(swap)
        loaded = storage.load_ankara_swap("test_123")
        assert loaded is not None
        assert loaded.swap_id == "test_123"
        assert loaded.wallet_name == "default"

    def test_load_ankara_swap_not_found(self, storage):
        """load_ankara_swap returns None for missing swap."""
        result = storage.load_ankara_swap("nonexistent")
        assert result is None

    def test_list_ankara_swaps(self, storage):
        """list_ankara_swaps returns all swap IDs."""
        swap1 = AnkaraSwapInfo(
            swap_id="swap_1",
            boltz_swap_id="bolt_1",
            invoice="lnbc1",
            address="lq1a",
            amount=1000,
            wallet_name="w1",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
        )
        swap2 = AnkaraSwapInfo(
            swap_id="swap_2",
            boltz_swap_id="bolt_2",
            invoice="lnbc2",
            address="lq1b",
            amount=2000,
            wallet_name="w2",
            status="settled",
            created_at="2026-03-02T00:00:00+00:00",
        )
        storage.save_ankara_swap(swap1)
        storage.save_ankara_swap(swap2)
        swaps = storage.list_ankara_swaps()
        assert len(swaps) == 2
        assert "swap_1" in swaps
        assert "swap_2" in swaps

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX file permissions not enforced on Windows",
    )
    def test_swap_file_permissions(self, storage):
        """Ankara swap files have restricted permissions (0o600)."""
        swap = AnkaraSwapInfo(
            swap_id="test_123",
            boltz_swap_id="boltz_456",
            invoice="lnbc...",
            address="lq1test",
            amount=100000,
            wallet_name="default",
            status="pending",
            created_at="2026-03-01T00:00:00+00:00",
        )
        storage.save_ankara_swap(swap)
        path = storage.ankara_swaps_dir / "test_123.json"
        assert path.exists()
        import os
        stat_info = os.stat(path)
        mode = stat_info.st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# JAN3 / AQUA account auth — fixtures + fakes
#
# These exercise the AQUA-account surface that lives in ankara.py
# (JAN3AuthClient / JAN3AccountManager). The HTTP seam is
# ``aqua.ankara.urllib.request.urlopen``; manager tests inject a FakeAuthClient
# (``m._client = fake``) so orchestration runs with no network. WapuPay
# + provision_account orchestration are tested in tests/test_wapupay.py.
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


@pytest.fixture
def jan3_storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Storage(Path(tmpdir))


class FakeAuthClient:
    """Scriptable JAN3AuthClient stand-in (no network).

    ``responses`` maps a method name to either a value (returned) or an Exception
    (raised); calls are recorded for assertions.
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def _yield(self, name, *args):
        self.calls.append((name, args))
        val = self.responses.get(name)
        # A list scripts a response sequence, e.g. a 401 then a success after refresh.
        if isinstance(val, list):
            val = val.pop(0)
        if isinstance(val, Exception):
            raise val
        return val if val is not None else {}

    def login(self, email, language="en"):
        return self._yield("login", email, language)

    def verify(self, email, otp_code):
        return self._yield("verify", email, otp_code)

    def refresh_token(self, refresh):
        return self._yield("refresh_token", refresh)

    def provision_wapupay_account(self, access_token):
        return self._yield("provision_wapupay_account", access_token)

    def refresh_access(self, refresh_token):
        return self._yield("refresh_access", refresh_token)

    def get_user(self, access_token):
        return self._yield("get_user", access_token)

    def ln_address_toggle(self, access_token, enabled):
        return self._yield("ln_address_toggle", access_token, enabled)

    def ln_username_available(self, username, access_token=None):
        return self._yield("ln_username_available", username, access_token)

    def register_addresses(self, access_token, fingerprint, addresses, override_fingerprint=False):
        return self._yield(
            "register_addresses", access_token, fingerprint, list(addresses), override_fingerprint
        )


def make_jan3(storage, fake):
    m = JAN3AccountManager(storage=storage)
    m._client = fake
    return m


def logged_in(storage, email="user@example.com", access="acc.tok", refresh="ref.tok"):
    storage.save_jan3_session(
        JAN3Session(email=email, access=access, refresh=refresh, created_at="t0")
    )


def _jwt_with_exp(exp):
    """Build an unsigned JWT carrying the given ``exp`` claim (test fixture)."""
    import base64

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg({'exp': exp})}.sig"


# ---------------------------------------------------------------------------
# JAN3AuthClient: HTTP targets + auth headers
# ---------------------------------------------------------------------------


def test_client_login_verify_targets():
    # login/verify are AQUA-account auth — POSTed to Ankara's /auth/ by JAN3AuthClient.
    client = JAN3AuthClient()
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen.setdefault("urls", []).append(req.full_url)
        seen["last_body"] = json.loads(req.data)
        return _mock_resp({"access": "a", "refresh": "r", "message": "ok"})

    with patch("aqua.ankara.urllib.request.urlopen", side_effect=fake_urlopen):
        client.login("u@e.com", language="es")
        assert seen["last_body"] == {"email": "u@e.com", "language": "es"}
        client.verify("u@e.com", "123456")
        assert seen["last_body"] == {"email": "u@e.com", "otp_code": "123456"}
    assert seen["urls"][0] == f"{AUTH_BASE_URL}/login/"
    assert seen["urls"][1] == f"{AUTH_BASE_URL}/verify/"


# ---------------------------------------------------------------------------
# JAN3AccountManager: auth lifecycle
# ---------------------------------------------------------------------------


def test_login_returns_message_and_passes_args(jan3_storage):
    fake = FakeAuthClient({"login": {"message": "sent"}})
    m = make_jan3(jan3_storage, fake)
    out = m.login("u@e.com", language="es")
    assert out["email"] == "u@e.com"
    assert out["message"] == "sent"
    assert "next_step" in out
    assert fake.calls[0] == ("login", ("u@e.com", "es"))


def test_login_rejects_bad_email(jan3_storage):
    m = make_jan3(jan3_storage, FakeAuthClient())
    with pytest.raises(ValueError):
        m.login("not-an-email")


def test_verify_persists_session(jan3_storage):
    fake = FakeAuthClient({"verify": {"access": "ACC", "refresh": "REF"}})
    m = make_jan3(jan3_storage, fake)
    out = m.verify("u@e.com", "123456")
    assert out["logged_in"] is True
    sess = jan3_storage.load_jan3_session()
    assert sess is not None and sess.access == "ACC" and sess.refresh == "REF"
    assert sess.email == "u@e.com"


def test_verify_without_tokens_raises_and_saves_nothing(jan3_storage):
    fake = FakeAuthClient({"verify": {"message": "wrong"}})
    m = make_jan3(jan3_storage, fake)
    with pytest.raises(ValueError):
        m.verify("u@e.com", "000000")
    assert jan3_storage.load_jan3_session() is None


def test_logout_and_session_status(jan3_storage):
    # session_status is a live check: it refreshes to confirm the session works.
    fake = FakeAuthClient({"refresh_token": {"access": "fresh.acc"}})
    m = make_jan3(jan3_storage, fake)
    assert m.session_status() == {"logged_in": False}
    logged_in(jan3_storage, email="x@y.com")
    st = m.session_status()
    assert st["logged_in"] is True and st["email"] == "x@y.com"
    assert st["valid"] is True
    assert "access" not in st and "refresh" not in st  # no secrets leaked
    assert m.logout()["logged_out"] is True
    assert jan3_storage.load_jan3_session() is None


def test_session_status_live_valid_persists_rotated_tokens(jan3_storage):
    # A successful live check persists the rotated access/refresh.
    fake = FakeAuthClient(
        {"refresh_token": {"access": "rot.acc", "refresh": "rot.ref"}}
    )
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage, access="old.acc", refresh="old.ref")
    st = m.session_status()
    assert st["valid"] is True
    sess = jan3_storage.load_jan3_session()
    assert sess.access == "rot.acc" and sess.refresh == "rot.ref"
    assert fake.calls == [("refresh_token", ("old.ref",))]


def test_session_status_expired_when_refresh_rejected(jan3_storage):
    fake = FakeAuthClient({"refresh_token": SessionExpiredError("401")})
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    st = m.session_status()
    assert st["logged_in"] is True and st["valid"] is False
    assert "aqua_login" in st["message"]


def test_session_status_unknown_on_network_error(jan3_storage):
    fake = FakeAuthClient({"refresh_token": ValueError("AQUA backend unreachable")})
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage, access=_jwt_with_exp(1_000_000_000))  # long expired
    st = m.session_status()
    assert st["logged_in"] is True and st["valid"] is None
    assert "Could not verify" in st["message"]


def test_session_status_valid_access_skips_refresh(jan3_storage):
    # A still-valid access token reports valid WITHOUT any network call/rotation.
    fake = FakeAuthClient({"refresh_token": {"access": "should.not.be.used"}})
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage, access=_jwt_with_exp(9_999_999_999))  # far future
    st = m.session_status()
    assert st["valid"] is True
    assert fake.calls == []  # exp checked locally, refresh never called


# ---------------------------------------------------------------------------
# JWT expiry helpers
# ---------------------------------------------------------------------------


def test_jwt_exp_reads_claim_and_handles_garbage():
    assert _jwt_exp(_jwt_with_exp(1782677992)) == 1782677992
    assert _jwt_exp("not-a-jwt") is None
    assert _jwt_exp("only.two") is None  # missing payload segment is unreadable
    assert _jwt_exp("") is None


def test_access_token_expired_uses_skew():
    now = 1_000_000.0
    # Past exp -> expired; comfortably future -> not; within skew window -> expired.
    assert _access_token_expired(_jwt_with_exp(999_000), now=now) is True
    assert _access_token_expired(_jwt_with_exp(2_000_000), now=now) is False
    assert _access_token_expired(_jwt_with_exp(int(now) + 10), now=now) is True
    # Unreadable token is treated as expired (fall back to live refresh).
    assert _access_token_expired("garbage", now=now) is True


# ---------------------------------------------------------------------------
# JAN3AuthClient: WapuPay-key provisioning (AQUA backend, Bearer JWT)
# ---------------------------------------------------------------------------


def test_provision_client_sends_bearer_not_api_key():
    # The provisioning call hits the AQUA backend with the AQUA JWT, so it sends
    # Authorization: Bearer and NEVER an X-API-Key (that's WapuPay-only).
    client = JAN3AuthClient(aqua_backend_url="http://localhost:8000")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _mock_resp({"token": "provisioned-key"})

    with patch("aqua.ankara.urllib.request.urlopen", side_effect=fake_urlopen):
        out = client.provision_wapupay_account("jwt.access.token")
    assert out == {"token": "provisioned-key"}
    req = captured["req"]
    # Trailing slash preserved (DRF APPEND_SLASH would 301 a slashless POST).
    assert req.full_url == "http://localhost:8000/api/v1/wapupay/account/"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer jwt.access.token"
    assert req.get_header("X-api-key") is None
    assert req.data is None  # bodyless POST


def test_provision_client_default_backend_is_ankara():
    # Provisioning hits the same AQUA/Ankara backend, so it uses ANKARA_API_URL.
    client = JAN3AuthClient()
    assert client.aqua_backend_url == ANKARA_API_URL.rstrip("/")
    assert WAPUPAY_ACCOUNT_PATH == "/api/v1/wapupay/account/"


def test_provision_client_401_points_at_relogin():
    client = JAN3AuthClient(aqua_backend_url="http://h")
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=_http_error(401, '{"detail": "Authentication credentials were not provided."}'),
    ):
        with pytest.raises(ValueError) as ei:
            client.provision_wapupay_account("expired")
    msg = str(ei.value)
    assert "aqua_login" in msg
    # DRF noise is replaced, not appended.
    assert "credentials were not provided" not in msg


def test_provision_client_403_reports_not_enabled():
    client = JAN3AuthClient(aqua_backend_url="http://h")
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=_http_error(403, '{"detail": "forbidden"}'),
    ):
        with pytest.raises(ValueError) as ei:
            client.provision_wapupay_account("jwt")
    assert "WapuPay is not enabled for your AQUA account." in str(ei.value)


def test_provision_client_502_points_at_upstream():
    client = JAN3AuthClient(aqua_backend_url="http://h")
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=_http_error(502, "<html>bad gateway</html>"),
    ):
        with pytest.raises(ValueError) as ei:
            client.provision_wapupay_account("jwt")
    assert "upstream" in str(ei.value).lower()


def test_provision_client_other_error_includes_detail():
    client = JAN3AuthClient(aqua_backend_url="http://h")
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=_http_error(400, '{"error": "bad request shape"}'),
    ):
        with pytest.raises(ValueError) as ei:
            client.provision_wapupay_account("jwt")
    msg = str(ei.value)
    assert "AQUA backend" in msg and "bad request shape" in msg


def test_provision_client_unreachable():
    client = JAN3AuthClient(aqua_backend_url="http://h")
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=urllib.error.URLError("conn refused"),
    ):
        with pytest.raises(ValueError) as ei:
            client.provision_wapupay_account("jwt")
    assert "unreachable" in str(ei.value).lower()


def test_provision_client_401_is_session_expired():
    # 401 → SessionExpiredError (recoverable, ValueError subclass) so the manager can retry.
    client = JAN3AuthClient(aqua_backend_url="http://h")
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=_http_error(401, '{"detail": "expired"}'),
    ):
        with pytest.raises(SessionExpiredError):
            client.provision_wapupay_account("expired")


# ---------------------------------------------------------------------------
# JAN3AuthClient: account/profile HTTP targets
# ---------------------------------------------------------------------------


def test_client_get_user_targets_auth_user():
    client = JAN3AuthClient()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _mock_resp({"email": "u@e.com", "ln_username": "alice"})

    with patch("aqua.ankara.urllib.request.urlopen", side_effect=fake_urlopen):
        out = client.get_user("jwt.access")
    assert out["ln_username"] == "alice"
    assert captured["req"].full_url == f"{AUTH_BASE_URL}/user/"
    assert captured["req"].get_method() == "GET"
    assert captured["req"].get_header("Authorization") == "Bearer jwt.access"


def test_client_ln_address_toggle_posts_enabled_flag():
    client = JAN3AuthClient()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _mock_resp({"enabled": False})

    with patch("aqua.ankara.urllib.request.urlopen", side_effect=fake_urlopen):
        client.ln_address_toggle("jwt", enabled=False)
    assert captured["url"] == f"{AUTH_BASE_URL}/user/ln-address-toggle/"
    assert captured["body"] == {"enabled": False}


def test_client_ln_username_available_is_anonymous_and_url_encodes():
    client = JAN3AuthClient()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _mock_resp({"is_available": True, "reason": None})

    with patch("aqua.ankara.urllib.request.urlopen", side_effect=fake_urlopen):
        client.ln_username_available("alice.smith")
    assert captured["req"].full_url == f"{AUTH_BASE_URL}/user/ln-username/alice.smith/is-available"
    # Anonymous endpoint: no Authorization header.
    assert captured["req"].get_header("Authorization") is None


def test_client_register_addresses_query_string_for_override():
    client = JAN3AuthClient()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _mock_resp({"addresses": ["lq1qfoo"]})

    with patch("aqua.ankara.urllib.request.urlopen", side_effect=fake_urlopen):
        client.register_addresses(
            "jwt", "deadbeef", ["lq1qfoo", "lq1qbar"], override_fingerprint=True
        )
    assert captured["url"] == f"{AUTH_BASE_URL}/user/addresses/?override_fingerprint=true"
    assert captured["body"] == {"fingerprint": "deadbeef", "addresses": ["lq1qfoo", "lq1qbar"]}


def test_client_refresh_access_returns_new_access():
    client = JAN3AuthClient()
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=lambda req, timeout=None: _mock_resp({"access": "ACC2"}),
    ):
        assert client.refresh_access("REF") == "ACC2"


def test_client_refresh_access_raises_when_response_missing_access():
    client = JAN3AuthClient()
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=lambda req, timeout=None: _mock_resp({}),
    ):
        with pytest.raises(ValueError):
            client.refresh_access("REF")


def test_client_401_with_token_raises_auth_expired():
    client = JAN3AuthClient()
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=_http_error(401, '{"detail": "expired"}'),
    ):
        with pytest.raises(_AuthExpired):
            client.get_user("expired.jwt")


def test_client_401_without_token_falls_through_to_value_error():
    # Anonymous endpoints don't trigger auto-refresh: a 401 there is a real error.
    client = JAN3AuthClient()
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=_http_error(401, '{"detail": "denied"}'),
    ):
        with pytest.raises(ValueError):
            client.ln_username_available("alice")


# ---------------------------------------------------------------------------
# JAN3AccountManager: profile + LN-address surface (with auto-refresh)
# ---------------------------------------------------------------------------


def test_manager_get_user_returns_profile(jan3_storage):
    profile = {"email": "u@e.com", "ln_username": "alice", "new_addresses_needed": 0}
    fake = FakeAuthClient({"get_user": profile})
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    assert m.get_user() == profile
    # Called with the persisted access token.
    assert fake.calls[0] == ("get_user", ("acc.tok",))


def test_manager_get_user_refreshes_on_401_and_retries(jan3_storage):
    from aqua.ankara import _AuthExpired

    profile = {"ln_username": "alice"}
    # First get_user call: 401. Refresh returns "ACC2". Retry succeeds.
    responses = {
        "get_user": [_AuthExpired(), profile],
        "refresh_access": "ACC2",
    }
    fake = FakeAuthClient()

    def get_user(access):
        fake.calls.append(("get_user", (access,)))
        val = responses["get_user"].pop(0)
        if isinstance(val, Exception):
            raise val
        return val

    def refresh_access(refresh):
        fake.calls.append(("refresh_access", (refresh,)))
        return responses["refresh_access"]

    fake.get_user = get_user
    fake.refresh_access = refresh_access

    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage, access="OLD", refresh="REF")
    out = m.get_user()
    assert out == profile
    # Sequence: 401 → refresh → retry with new token.
    assert [c[0] for c in fake.calls] == ["get_user", "refresh_access", "get_user"]
    assert fake.calls[0][1] == ("OLD",)
    assert fake.calls[2][1] == ("ACC2",)
    # Persisted session was updated.
    assert jan3_storage.load_jan3_session().access == "ACC2"


def test_manager_get_user_refresh_failure_points_to_relogin(jan3_storage):
    from aqua.ankara import _AuthExpired

    fake = FakeAuthClient({
        "get_user": _AuthExpired(),
        "refresh_access": ValueError("refresh dead"),
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    with pytest.raises(ValueError) as ei:
        m.get_user()
    assert "aqua_login" in str(ei.value)


def test_manager_ln_username_available_anonymous_when_no_session(jan3_storage):
    fake = FakeAuthClient({"ln_username_available": {"is_available": True, "reason": None}})
    m = make_jan3(jan3_storage, fake)
    out = m.ln_username_available("  alice  ")
    assert out["is_available"] is True
    # No session → no access token forwarded.
    assert fake.calls == [("ln_username_available", ("alice", None))]


def test_manager_ln_username_available_uses_token_when_session_exists(jan3_storage):
    fake = FakeAuthClient({"ln_username_available": {"is_available": False, "reason": "taken"}})
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage, access="ACC1")
    m.ln_username_available("alice")
    # Session present → token forwarded so the live (auth-gated) deployment accepts the call.
    assert fake.calls == [("ln_username_available", ("alice", "ACC1"))]


def test_manager_ln_username_available_rejects_blank(jan3_storage):
    m = make_jan3(jan3_storage, FakeAuthClient())
    with pytest.raises(ValueError):
        m.ln_username_available("   ")


def test_manager_ln_address_toggle_uses_session_token(jan3_storage):
    fake = FakeAuthClient({"ln_address_toggle": {"enabled": True}})
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage, access="ACC1")
    m.ln_address_toggle(True)
    assert fake.calls == [("ln_address_toggle", ("ACC1", True))]


# ---------------------------------------------------------------------------
# JAN3AccountManager.register_ln_addresses — orchestration
# ---------------------------------------------------------------------------


def _wm_stub(fingerprint="deadbeef", addrs=None):
    """Wallet manager stub for register_ln_addresses tests.

    Counter persistence now lives inside ``WalletManager.reserve_addresses``
    (covered by tests/test_tools.py); from the orchestration's point of view,
    ``reserve_addresses(name, count)`` just returns N fresh ``Address``-like
    objects in one call.
    """
    wm = MagicMock()
    wm.fingerprint.return_value = fingerprint
    addrs = addrs or [f"lq1q{i:04d}" for i in range(20)]
    items = [MagicMock(address=a, index=i) for i, a in enumerate(addrs)]
    wm.reserve_addresses.side_effect = lambda _name, count: items[:count]
    return wm


def test_register_ln_addresses_happy_path_uses_server_count(jan3_storage):
    user = {
        "ln_username": "alice",
        "ln_address_toggled": True,
        "fingerprint": "deadbeef",
        "new_addresses_needed": 3,
    }
    fake = FakeAuthClient({
        "get_user": user,
        "register_addresses": {"addresses": ["lq1q0000", "lq1q0001", "lq1q0002"]},
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    wm = _wm_stub()
    out = m.register_ln_addresses(wallet_manager=wm, wallet_name="default")
    assert out["requested_count"] == 3
    assert out["pool_size"] == 3
    assert out["fingerprint"] == "deadbeef"
    assert out["addresses"] == ["lq1q0000", "lq1q0001", "lq1q0002"]
    # Three no-arg mints — no tip probe, no manual indexing.
    wm.reserve_addresses.assert_called_once_with("default", 3)
    # POST shape matched.
    register_call = next(c for c in fake.calls if c[0] == "register_addresses")
    _, (access, fp, addrs, override) = register_call
    assert access == "acc.tok"
    assert fp == "deadbeef"
    assert len(addrs) == 3
    assert override is False


def test_register_ln_addresses_defaults_to_five_when_server_says_zero(jan3_storage):
    user = {
        "ln_username": "alice",
        "ln_address_toggled": True,
        "fingerprint": "deadbeef",
        "new_addresses_needed": 0,
    }
    fake = FakeAuthClient({
        "get_user": user,
        "register_addresses": {"addresses": []},
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    wm = _wm_stub()
    m.register_ln_addresses(wallet_manager=wm, wallet_name="default")
    wm.reserve_addresses.assert_called_once_with("default", 5)


def test_register_ln_addresses_respects_explicit_count(jan3_storage):
    user = {
        "ln_username": "alice",
        "ln_address_toggled": True,
        "fingerprint": "deadbeef",
        "new_addresses_needed": 0,
    }
    fake = FakeAuthClient({
        "get_user": user,
        "register_addresses": {"addresses": []},
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    wm = _wm_stub()
    m.register_ln_addresses(wallet_manager=wm, wallet_name="default", count=2)
    wm.reserve_addresses.assert_called_once_with("default", 2)


def test_register_ln_addresses_caps_at_max(jan3_storage):
    from aqua.ankara import MAX_ADDRESSES_PER_REGISTRATION

    fake = FakeAuthClient({
        "get_user": {"ln_username": "a", "ln_address_toggled": True, "fingerprint": "dead", "new_addresses_needed": 0},
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    with pytest.raises(ValueError) as ei:
        m.register_ln_addresses(
            wallet_manager=_wm_stub("dead"),
            wallet_name="default",
            count=MAX_ADDRESSES_PER_REGISTRATION + 1,
        )
    assert str(MAX_ADDRESSES_PER_REGISTRATION) in str(ei.value)


def test_register_ln_addresses_refuses_without_username(jan3_storage):
    fake = FakeAuthClient({"get_user": {"ln_username": None, "ln_address_toggled": True}})
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    with pytest.raises(ValueError) as ei:
        m.register_ln_addresses(wallet_manager=_wm_stub(), wallet_name="default")
    assert "purchase_ln_username" in str(ei.value)


def test_register_ln_addresses_refuses_when_toggled_off(jan3_storage):
    fake = FakeAuthClient({
        "get_user": {"ln_username": "alice", "ln_address_toggled": False, "fingerprint": "dead"},
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    with pytest.raises(ValueError) as ei:
        m.register_ln_addresses(wallet_manager=_wm_stub("dead"), wallet_name="default")
    assert "ln_address_toggle" in str(ei.value)


def test_register_ln_addresses_refuses_on_fingerprint_mismatch(jan3_storage):
    fake = FakeAuthClient({
        "get_user": {
            "ln_username": "alice",
            "ln_address_toggled": True,
            "fingerprint": "11111111",
            "new_addresses_needed": 5,
        },
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    with pytest.raises(ValueError) as ei:
        m.register_ln_addresses(wallet_manager=_wm_stub("22222222"), wallet_name="default")
    msg = str(ei.value)
    assert "fingerprint mismatch" in msg.lower()
    assert "11111111" in msg and "22222222" in msg
    # No POST attempted.
    assert not any(c[0] == "register_addresses" for c in fake.calls)


def test_register_ln_addresses_override_skips_fingerprint_and_toggle_checks(jan3_storage):
    fake = FakeAuthClient({
        "get_user": {
            "ln_username": "alice",
            "ln_address_toggled": False,  # off — would normally refuse
            "fingerprint": "11111111",     # mismatched — would normally refuse
            "new_addresses_needed": 0,
        },
        "register_addresses": {"addresses": []},
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    m.register_ln_addresses(
        wallet_manager=_wm_stub("22222222"),
        wallet_name="default",
        override_fingerprint=True,
    )
    call = next(c for c in fake.calls if c[0] == "register_addresses")
    _, (_, fp, _, override) = call
    assert fp == "22222222"
    assert override is True


# ---------------------------------------------------------------------------
# JAN3AccountManager.ensure_ln_pool — idempotent top-up
# ---------------------------------------------------------------------------


def test_ensure_ln_pool_no_username_skips(jan3_storage):
    fake = FakeAuthClient({"get_user": {"ln_username": None, "ln_address_toggled": True}})
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    out = m.ensure_ln_pool(wallet_manager=_wm_stub(), wallet_name="default")
    assert out == {"refilled": False, "reason": "no_ln_username", "fingerprint": None}
    assert not any(c[0] == "register_addresses" for c in fake.calls)


def test_ensure_ln_pool_disabled_skips(jan3_storage):
    fake = FakeAuthClient({
        "get_user": {"ln_username": "alice", "ln_address_toggled": False, "fingerprint": "dead"},
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    out = m.ensure_ln_pool(wallet_manager=_wm_stub("dead"), wallet_name="default")
    assert out["refilled"] is False and out["reason"] == "ln_address_disabled"


def test_ensure_ln_pool_full_skips(jan3_storage):
    fake = FakeAuthClient({
        "get_user": {
            "ln_username": "alice",
            "ln_address_toggled": True,
            "fingerprint": "dead",
            "new_addresses_needed": 0,
        },
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    out = m.ensure_ln_pool(wallet_manager=_wm_stub("dead"), wallet_name="default")
    assert out == {
        "refilled": False,
        "reason": "pool_full",
        "fingerprint": "dead",
        "new_addresses_needed": 0,
    }


def test_ensure_ln_pool_fingerprint_mismatch_skips(jan3_storage):
    fake = FakeAuthClient({
        "get_user": {
            "ln_username": "alice",
            "ln_address_toggled": True,
            "fingerprint": "11111111",
            "new_addresses_needed": 3,
        },
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    out = m.ensure_ln_pool(wallet_manager=_wm_stub("22222222"), wallet_name="default")
    assert out == {
        "refilled": False,
        "reason": "fingerprint_mismatch",
        "server_fingerprint": "11111111",
        "local_fingerprint": "22222222",
    }
    assert not any(c[0] == "register_addresses" for c in fake.calls)


def test_ensure_ln_pool_happy_path_refills(jan3_storage):
    fake = FakeAuthClient({
        "get_user": {
            "ln_username": "alice",
            "ln_address_toggled": True,
            "fingerprint": "deadbeef",
            "new_addresses_needed": 4,
        },
        "register_addresses": {"addresses": ["lq1qa", "lq1qb", "lq1qc", "lq1qd"]},
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    out = m.ensure_ln_pool(wallet_manager=_wm_stub(), wallet_name="default")
    assert out["refilled"] is True
    assert out["requested_count"] == 4
    assert out["pool_size"] == 4
    assert out["fingerprint"] == "deadbeef"


def test_ensure_ln_pool_caps_at_max(jan3_storage):
    from aqua.ankara import MAX_ADDRESSES_PER_REGISTRATION

    fake = FakeAuthClient({
        "get_user": {
            "ln_username": "alice",
            "ln_address_toggled": True,
            "fingerprint": "deadbeef",
            "new_addresses_needed": 50,
        },
        "register_addresses": {"addresses": []},
    })
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    wm = _wm_stub()
    m.ensure_ln_pool(wallet_manager=wm, wallet_name="default")
    # Capped — never sends more than the server allows per call.
    wm.reserve_addresses.assert_called_once_with("default", MAX_ADDRESSES_PER_REGISTRATION)


# ---------------------------------------------------------------------------
# _authed_call — opportunistic LN-pool top-up after refresh
# ---------------------------------------------------------------------------


def test_authed_call_tops_up_after_refresh(jan3_storage):
    """Successful 401 → refresh → retry triggers an opportunistic top-up."""
    profile = {
        "ln_username": "alice",
        "ln_address_toggled": True,
        "fingerprint": "deadbeef",
        "new_addresses_needed": 2,
    }
    # First get_user (the caller's): 401. Retry: profile. Top-up's single
    # fetch with the new token: profile. ensure_ln_pool + register_ln_addresses
    # reuse that profile, so no more get_user calls.
    get_user_responses = [_AuthExpired(), profile, profile]
    fake = FakeAuthClient()

    def get_user(access):
        fake.calls.append(("get_user", (access,)))
        val = get_user_responses.pop(0)
        if isinstance(val, Exception):
            raise val
        return val

    def refresh_access(refresh):
        fake.calls.append(("refresh_access", (refresh,)))
        return "ACC2"

    def register_addresses(access, fp, addrs, override_fingerprint=False):
        fake.calls.append(("register_addresses", (access, fp, list(addrs), override_fingerprint)))
        return {"addresses": ["lq1qa", "lq1qb"]}

    fake.get_user = get_user
    fake.refresh_access = refresh_access
    fake.register_addresses = register_addresses

    wm = _wm_stub("deadbeef")
    m = JAN3AccountManager(storage=jan3_storage, wallet_manager_factory=lambda: wm)
    m._client = fake
    logged_in(jan3_storage, access="OLD", refresh="REF")

    # First call is what the user invoked; refresh + retry succeeds; THEN the
    # top-up fires before returning to the caller.
    out = m.get_user()
    assert out == profile

    # Expected call sequence: caller's get_user (401), refresh, retry get_user,
    # top-up's single get_user, then the POST. The profile is threaded through
    # ensure_ln_pool → register_ln_addresses so the top-up only costs one
    # extra round trip total.
    methods = [c[0] for c in fake.calls]
    assert methods == [
        "get_user",
        "refresh_access",
        "get_user",
        "get_user",          # _opportunistic_top_up's single fetch
        "register_addresses",
    ]
    # Top-up uses the NEW access token, not the stale one.
    assert fake.calls[-1][1][0] == "ACC2"


def test_authed_call_swallows_top_up_errors(jan3_storage):
    """A failing top-up never breaks the caller's actual operation."""
    profile = {
        "ln_username": "alice",
        "ln_address_toggled": True,
        "fingerprint": "deadbeef",
        "new_addresses_needed": 2,
    }
    get_user_responses = [_AuthExpired(), profile, ValueError("top-up boom")]
    fake = FakeAuthClient()

    def get_user(access):
        fake.calls.append(("get_user", (access,)))
        val = get_user_responses.pop(0)
        if isinstance(val, Exception):
            raise val
        return val

    fake.get_user = get_user
    fake.refresh_access = lambda r: "ACC2"

    wm = _wm_stub("deadbeef")
    m = JAN3AccountManager(storage=jan3_storage, wallet_manager_factory=lambda: wm)
    m._client = fake
    logged_in(jan3_storage, access="OLD", refresh="REF")

    # The original call still succeeds.
    assert m.get_user() == profile


def test_authed_call_no_factory_no_top_up(jan3_storage):
    """Managers constructed without a wallet_manager_factory skip the top-up."""
    profile = {"ln_username": "alice", "ln_address_toggled": True, "new_addresses_needed": 2}
    get_user_responses = [_AuthExpired(), profile]
    fake = FakeAuthClient()

    def get_user(access):
        fake.calls.append(("get_user", (access,)))
        val = get_user_responses.pop(0)
        if isinstance(val, Exception):
            raise val
        return val

    fake.get_user = get_user
    fake.refresh_access = lambda r: "ACC2"

    m = JAN3AccountManager(storage=jan3_storage)  # no factory
    m._client = fake
    logged_in(jan3_storage, access="OLD", refresh="REF")
    m.get_user()
    # Only the caller's two get_user calls + the refresh — no top-up get_user.
    assert [c[0] for c in fake.calls] == ["get_user", "get_user"]


# ---------------------------------------------------------------------------
# JAN3AuthClient: refresh token
# ---------------------------------------------------------------------------


def test_client_refresh_token_target():
    # refresh hits the custom /api/v1/auth/refresh/ with the refresh token body.
    client = JAN3AuthClient()
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        return _mock_resp({"access": "new.acc"})

    with patch("aqua.ankara.urllib.request.urlopen", side_effect=fake_urlopen):
        out = client.refresh_token("the.refresh.tok")
    assert out == {"access": "new.acc"}
    assert seen["url"] == f"{AUTH_BASE_URL}/refresh/"
    assert seen["body"] == {"refresh": "the.refresh.tok"}


def test_client_refresh_token_401_is_session_expired():
    client = JAN3AuthClient()
    with patch(
        "aqua.ankara.urllib.request.urlopen",
        side_effect=_http_error(401, '{"detail": "token not valid"}'),
    ):
        with pytest.raises(SessionExpiredError):
            client.refresh_token("dead.refresh")


# ---------------------------------------------------------------------------
# JAN3AccountManager: refresh + auth-retry fallback
# ---------------------------------------------------------------------------


def test_refresh_session_keeps_old_refresh_when_not_rotated(jan3_storage):
    # No rotated refresh in the response -> the stored one is preserved.
    fake = FakeAuthClient({"refresh_token": {"access": "new.acc"}})
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage, access="old.acc", refresh="keep.ref")
    updated = m._refresh_session(jan3_storage.load_jan3_session())
    assert updated.access == "new.acc" and updated.refresh == "keep.ref"
    assert jan3_storage.load_jan3_session().access == "new.acc"


def test_refresh_session_no_refresh_token_raises(jan3_storage):
    m = make_jan3(jan3_storage, FakeAuthClient())
    sess = JAN3Session(email="x@y.com", access="a", refresh="", created_at="t0")
    with pytest.raises(SessionExpiredError):
        m._refresh_session(sess)


def test_provision_token_refreshes_and_retries_on_401(jan3_storage):
    # First provision 401s; manager refreshes and retries with the fresh token.
    fake = FakeAuthClient(
        {
            "provision_wapupay_account": [
                SessionExpiredError("401"),
                {"token": "WapuKey_after_refresh"},
            ],
            "refresh_token": {"access": "fresh.acc", "refresh": "fresh.ref"},
        }
    )
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage, access="stale.acc", refresh="good.ref")
    token = m.provision_wapupay_token()
    assert token == "WapuKey_after_refresh"
    # Order: stale access -> refresh -> retry with the fresh access.
    assert fake.calls == [
        ("provision_wapupay_account", ("stale.acc",)),
        ("refresh_token", ("good.ref",)),
        ("provision_wapupay_account", ("fresh.acc",)),
    ]
    # Rotated tokens persisted.
    assert jan3_storage.load_jan3_session().access == "fresh.acc"


def test_provision_token_relogin_when_refresh_fails(jan3_storage):
    # Access rejected AND refresh rejected -> a clear "log in again" ValueError.
    fake = FakeAuthClient(
        {
            "provision_wapupay_account": SessionExpiredError("401"),
            "refresh_token": SessionExpiredError("401"),
        }
    )
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage)
    with pytest.raises(ValueError) as ei:
        m.provision_wapupay_token()
    assert "aqua_login" in str(ei.value)


def test_provision_token_no_retry_on_success(jan3_storage):
    # Happy path never touches refresh.
    fake = FakeAuthClient({"provision_wapupay_account": {"token": "k"}})
    m = make_jan3(jan3_storage, fake)
    logged_in(jan3_storage, access="good.acc")
    assert m.provision_wapupay_token() == "k"
    assert fake.calls == [("provision_wapupay_account", ("good.acc",))]


