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
        if isinstance(val, Exception):
            raise val
        return val if val is not None else {}

    def login(self, email, language="en"):
        return self._yield("login", email, language)

    def verify(self, email, otp_code):
        return self._yield("verify", email, otp_code)

    def provision_wapupay_account(self, access_token):
        return self._yield("provision_wapupay_account", access_token)


def make_jan3(storage, fake):
    m = JAN3AccountManager(storage=storage)
    m._client = fake
    return m


def logged_in(storage, email="user@example.com", access="acc.tok", refresh="ref.tok"):
    storage.save_jan3_session(
        JAN3Session(email=email, access=access, refresh=refresh, created_at="t0")
    )


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
    m = make_jan3(jan3_storage, FakeAuthClient())
    assert m.session_status() == {"logged_in": False}
    logged_in(jan3_storage, email="x@y.com")
    st = m.session_status()
    assert st["logged_in"] is True and st["email"] == "x@y.com"
    assert "access" not in st and "refresh" not in st  # no secrets leaked
    assert m.logout()["logged_out"] is True
    assert jan3_storage.load_jan3_session() is None


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


