"""Tests for storage module."""

import base64
import logging
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from aqua.storage import (
    Storage,
    WalletData,
    Config,
    _DEFAULT_MNEMONIC_PASSWORD,
    _DEFAULT_PWD_VERSION,
    _validate_wallet_name,
)
from tests.conftest import TEST_MNEMONIC


@pytest.fixture
def temp_storage():
    """Create a temporary storage instance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Storage(Path(tmpdir))


class TestStorage:
    """Tests for Storage class."""

    def test_init_creates_directories(self, temp_storage):
        """Test that initialization creates required directories."""
        assert temp_storage.base_dir.exists()
        assert temp_storage.wallets_dir.exists()
        assert temp_storage.cache_dir.exists()

    def test_config_save_load(self, temp_storage):
        """Test saving and loading config."""
        config = Config(network="testnet", default_wallet="test")
        temp_storage.save_config(config)
        
        loaded = temp_storage.load_config()
        assert loaded.network == "testnet"
        assert loaded.default_wallet == "test"

    def test_wallet_save_load(self, temp_storage):
        """Test saving and loading wallet."""
        wallet = WalletData(
            name="test",
            network="mainnet",
            descriptor="ct(...)",
        )
        temp_storage.save_wallet(wallet)
        
        assert temp_storage.wallet_exists("test")
        
        loaded = temp_storage.load_wallet("test")
        assert loaded.name == "test"
        assert loaded.network == "mainnet"
        assert loaded.descriptor == "ct(...)"

    def test_list_wallets(self, temp_storage):
        """Test listing wallets."""
        assert temp_storage.list_wallets() == []
        
        wallet1 = WalletData(name="w1", network="mainnet", descriptor="ct1")
        wallet2 = WalletData(name="w2", network="testnet", descriptor="ct2")
        temp_storage.save_wallet(wallet1)
        temp_storage.save_wallet(wallet2)
        
        wallets = temp_storage.list_wallets()
        assert set(wallets) == {"w1", "w2"}

    def test_delete_wallet(self, temp_storage):
        """Test deleting wallet."""
        wallet = WalletData(name="todelete", network="mainnet", descriptor="ct")
        temp_storage.save_wallet(wallet)

        assert temp_storage.wallet_exists("todelete")
        assert temp_storage.delete_wallet("todelete")
        assert not temp_storage.wallet_exists("todelete")

    def test_delete_wallet_removes_cache(self, temp_storage):
        """Deleting a wallet also removes its cache directory."""
        wallet = WalletData(name="withcache", network="mainnet", descriptor="ct")
        temp_storage.save_wallet(wallet)
        cache_path = temp_storage.get_cache_path("withcache")
        # Create a dummy file inside the cache to verify rmtree
        (cache_path / "dummy.db").touch()
        assert cache_path.exists()

        temp_storage.delete_wallet("withcache")
        assert not temp_storage.wallet_exists("withcache")
        assert not cache_path.exists()

    def test_mnemonic_encryption(self, temp_storage):
        """Test mnemonic encryption/decryption."""
        mnemonic = TEST_MNEMONIC
        password = "test123"

        encrypted = temp_storage.encrypt_mnemonic(mnemonic, password)
        assert encrypted != mnemonic

        decrypted = temp_storage.decrypt_mnemonic(encrypted, password)
        assert decrypted == mnemonic

    def test_mnemonic_wrong_password(self, temp_storage):
        """Test that the wrong password fails to decrypt."""
        mnemonic = "test mnemonic"
        encrypted = temp_storage.encrypt_mnemonic(mnemonic, "correct")

        with pytest.raises(Exception):
            temp_storage.decrypt_mnemonic(encrypted, "wrong")


class TestWalletNameValidation:
    """Tests for wallet name validation (path traversal prevention)."""

    @pytest.mark.parametrize("name", ["default", "my-wallet", "wallet_1", "A", "a" * 64])
    def test_valid_names(self, name):
        """Valid wallet names should pass validation."""
        assert _validate_wallet_name(name) == name

    @pytest.mark.parametrize("name", [
        "../../etc/passwd",
        "../evil",
        "wallet/name",
        "wallet.json",
        "",
        "a" * 65,
        "hello world",
        "/absolute",
        "wallet\x00evil",
    ])
    def test_invalid_names(self, name):
        """Invalid wallet names should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid wallet name"):
            _validate_wallet_name(name)

    def test_path_traversal_blocked_on_save(self, temp_storage):
        """Path traversal in wallet name should be blocked during save."""
        wallet = WalletData(
            name="../../etc/evil",
            network="mainnet",
            descriptor="ct(...)",
        )
        with pytest.raises(ValueError, match="Invalid wallet name"):
            temp_storage.save_wallet(wallet)

    def test_path_traversal_blocked_on_load(self, temp_storage):
        """Path traversal in wallet name should be blocked during load."""
        with pytest.raises(ValueError, match="Invalid wallet name"):
            temp_storage.load_wallet("../evil")

    def test_path_traversal_blocked_on_cache(self, temp_storage):
        """Path traversal in wallet name should be blocked on cache path."""
        with pytest.raises(ValueError, match="Invalid wallet name"):
            temp_storage.get_cache_path("../../tmp/evil")


class TestSwapStorage:
    """Tests for swap persistence (Layer 4)."""

    def _make_swap(self, **overrides):
        from aqua.boltz import SwapInfo

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

    def test_swaps_dir_created_on_init(self, temp_storage):
        """Storage init creates swaps/ directory."""
        assert temp_storage.swaps_dir.exists()

    def test_save_and_load_swap(self, temp_storage):
        """SwapInfo saved can be loaded back correctly."""
        swap = self._make_swap()
        temp_storage.save_swap(swap)

        loaded = temp_storage.load_swap("test_swap_123")
        assert loaded is not None
        assert loaded.swap_id == swap.swap_id
        assert loaded.address == swap.address
        assert loaded.expected_amount == swap.expected_amount
        assert loaded.status == swap.status
        assert loaded.refund_private_key == swap.refund_private_key

    def test_load_swap_not_found_returns_none(self, temp_storage):
        """load_swap with nonexistent ID returns None."""
        result = temp_storage.load_swap("nonexistent")
        assert result is None

    def test_list_swaps_empty(self, temp_storage):
        """list_swaps returns empty list when no swaps."""
        assert temp_storage.list_swaps() == []

    def test_list_swaps_returns_ids(self, temp_storage):
        """list_swaps returns all saved swap IDs."""
        swap1 = self._make_swap(swap_id="swap_aaa")
        swap2 = self._make_swap(swap_id="swap_bbb")
        temp_storage.save_swap(swap1)
        temp_storage.save_swap(swap2)

        ids = temp_storage.list_swaps()
        assert set(ids) == {"swap_aaa", "swap_bbb"}

    def test_save_swap_updates_existing(self, temp_storage):
        """Saving swap with same ID overwrites previous data."""
        swap = self._make_swap(status="swap.created")
        temp_storage.save_swap(swap)

        swap.status = "transaction.mempool"
        swap.lockup_txid = "dd" * 32
        temp_storage.save_swap(swap)

        loaded = temp_storage.load_swap("test_swap_123")
        assert loaded.status == "transaction.mempool"
        assert loaded.lockup_txid == "dd" * 32

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix mode bits")
    def test_swap_file_permissions(self, temp_storage):
        """Swap files are created with 0o600 permissions."""
        swap = self._make_swap()
        temp_storage.save_swap(swap)

        swap_path = temp_storage.swaps_dir / "test_swap_123.json"
        mode = stat.S_IMODE(os.stat(swap_path).st_mode)
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"


class TestFilePermissions:
    """Tests for restrictive file permissions."""

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix mode bits")
    def test_wallet_file_permissions(self, temp_storage):
        """Wallet files should be created with 0600 permissions."""
        wallet = WalletData(name="secure", network="mainnet", descriptor="ct(...)")
        temp_storage.save_wallet(wallet)

        path = temp_storage._wallet_path("secure")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix mode bits")
    def test_config_file_permissions(self, temp_storage):
        """Config file should be created with 0600 permissions."""
        config = Config(network="testnet")
        temp_storage.save_config(config)

        mode = stat.S_IMODE(os.stat(temp_storage.config_path).st_mode)
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix mode bits")
    def test_directory_permissions(self):
        """Directories should be created with 0700 permissions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "new_wallet_dir"
            storage = Storage(base)

            for d in [storage.base_dir, storage.wallets_dir, storage.cache_dir]:
                mode = stat.S_IMODE(os.stat(d).st_mode)
                assert mode == 0o700, f"Expected 0700 for {d}, got {oct(mode)}"


class TestLightningSwapStorage:
    """Tests for Lightning swap storage."""

    def test_lightning_swaps_dir_created(self, temp_storage):
        """Lightning swaps directory is created on init."""
        assert temp_storage.lightning_swaps_dir.exists()
        assert temp_storage.lightning_swaps_dir.is_dir()

    def test_save_load_lightning_swap_receive(self, temp_storage):
        """Test saving and loading a receive Lightning swap."""
        from aqua.lightning import LightningSwap
        from datetime import datetime, UTC

        swap = LightningSwap(
            swap_id="ankara_uuid_123",
            swap_type="receive",
            provider="ankara",
            invoice="lnbc...",
            amount=100000,
            wallet_name="default",
            status="pending",
            network="mainnet",
            created_at=datetime.now(UTC).isoformat(),
            receive_address="lq1...",
        )
        temp_storage.save_lightning_swap(swap)

        loaded = temp_storage.load_lightning_swap("ankara_uuid_123")
        assert loaded is not None
        assert loaded.swap_id == "ankara_uuid_123"
        assert loaded.swap_type == "receive"
        assert loaded.provider == "ankara"
        assert loaded.receive_address == "lq1..."

    def test_save_load_lightning_swap_send(self, temp_storage):
        """Test saving and loading a send Lightning swap."""
        from aqua.lightning import LightningSwap
        from datetime import datetime, UTC

        swap = LightningSwap(
            swap_id="boltz_swap_456",
            swap_type="send",
            provider="boltz",
            invoice="lnbc...",
            amount=50069,
            wallet_name="default",
            status="processing",
            network="mainnet",
            created_at=datetime.now(UTC).isoformat(),
            lockup_txid="abc123",
            timeout_block_height=2500000,
            refund_private_key="secret_key",
        )
        temp_storage.save_lightning_swap(swap)

        loaded = temp_storage.load_lightning_swap("boltz_swap_456")
        assert loaded is not None
        assert loaded.swap_type == "send"
        assert loaded.lockup_txid == "abc123"
        assert loaded.refund_private_key == "secret_key"

    def test_load_lightning_swap_not_found(self, temp_storage):
        """Loading non-existent swap returns None."""
        loaded = temp_storage.load_lightning_swap("nonexistent")
        assert loaded is None

    def test_list_lightning_swaps(self, temp_storage):
        """Test listing all Lightning swap IDs."""
        from aqua.lightning import LightningSwap
        from datetime import datetime, UTC

        assert temp_storage.list_lightning_swaps() == []

        swap1 = LightningSwap(
            swap_id="swap_1",
            swap_type="receive",
            provider="ankara",
            invoice="lnbc...",
            amount=100000,
            wallet_name="default",
            status="pending",
            network="mainnet",
            created_at=datetime.now(UTC).isoformat(),
        )
        swap2 = LightningSwap(
            swap_id="swap_2",
            swap_type="send",
            provider="boltz",
            invoice="lnbc...",
            amount=100000,
            wallet_name="default",
            status="processing",
            network="mainnet",
            created_at=datetime.now(UTC).isoformat(),
        )
        temp_storage.save_lightning_swap(swap1)
        temp_storage.save_lightning_swap(swap2)

        swaps = temp_storage.list_lightning_swaps()
        assert set(swaps) == {"swap_1", "swap_2"}

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix mode bits")
    def test_lightning_swap_file_permissions(self, temp_storage):
        """Lightning swap files should be created with 0600 permissions."""
        from aqua.lightning import LightningSwap
        from datetime import datetime, UTC

        swap = LightningSwap(
            swap_id="secure_swap",
            swap_type="send",
            provider="boltz",
            invoice="lnbc...",
            amount=100000,
            wallet_name="default",
            status="pending",
            network="mainnet",
            created_at=datetime.now(UTC).isoformat(),
            refund_private_key="secret",
        )
        temp_storage.save_lightning_swap(swap)

        path = temp_storage._lightning_swap_path("secure_swap")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Default at-rest encryption (issue #84)
# ---------------------------------------------------------------------------


def _make_plain_blob(mnemonic: str) -> str:
    """Build a legacy ``plain:`` mnemonic blob (no encryption)."""
    return "plain:" + base64.b64encode(mnemonic.encode()).decode()


def _make_untagged_legacy_blob(storage: Storage, mnemonic: str, password: str) -> str:
    """Build a legacy untagged blob (raw user-password-encrypted, no prefix)."""
    return storage.encrypt_mnemonic(mnemonic, password)


class TestDefaultPasswordEncryption:
    """Tests for the default-password at-rest encryption (#84)."""

    # --- store_mnemonic / retrieve_mnemonic ------------------------------

    def test_store_mnemonic_no_password_produces_default_prefix(self, temp_storage):
        stored = temp_storage.store_mnemonic(TEST_MNEMONIC, None)
        assert stored.startswith(f"default:{_DEFAULT_PWD_VERSION}:")

    def test_store_mnemonic_with_password_produces_user_prefix(self, temp_storage):
        stored = temp_storage.store_mnemonic(TEST_MNEMONIC, "s3cret")
        assert stored.startswith("user:")
        # Sanity: payload is not the literal mnemonic.
        assert TEST_MNEMONIC not in stored

    def test_retrieve_default_prefix_no_password_needed(self, temp_storage):
        stored = temp_storage.store_mnemonic(TEST_MNEMONIC, None)
        assert temp_storage.retrieve_mnemonic(stored, None) == TEST_MNEMONIC
        # Even with a (wrong/extra) password argument, default decrypt ignores it.
        assert temp_storage.retrieve_mnemonic(stored, "ignored") == TEST_MNEMONIC

    def test_retrieve_user_prefix_needs_password(self, temp_storage):
        stored = temp_storage.store_mnemonic(TEST_MNEMONIC, "s3cret")
        with pytest.raises(ValueError, match="Password required"):
            temp_storage.retrieve_mnemonic(stored, None)

    def test_retrieve_user_prefix_with_password(self, temp_storage):
        stored = temp_storage.store_mnemonic(TEST_MNEMONIC, "s3cret")
        assert temp_storage.retrieve_mnemonic(stored, "s3cret") == TEST_MNEMONIC

    def test_retrieve_plain_prefix_legacy(self, temp_storage):
        legacy = _make_plain_blob(TEST_MNEMONIC)
        # No migration happens inside retrieve_mnemonic (pure read).
        assert temp_storage.retrieve_mnemonic(legacy, None) == TEST_MNEMONIC

    def test_retrieve_untagged_legacy_needs_password(self, temp_storage):
        legacy = _make_untagged_legacy_blob(temp_storage, TEST_MNEMONIC, "s3cret")
        with pytest.raises(ValueError, match="Password required"):
            temp_storage.retrieve_mnemonic(legacy, None)

    def test_retrieve_untagged_legacy_with_password(self, temp_storage):
        legacy = _make_untagged_legacy_blob(temp_storage, TEST_MNEMONIC, "s3cret")
        assert temp_storage.retrieve_mnemonic(legacy, "s3cret") == TEST_MNEMONIC

    def test_retrieve_unsupported_default_version_raises(self, temp_storage):
        # Encrypted with the real default password but tagged as v99.
        raw = temp_storage.encrypt_mnemonic(TEST_MNEMONIC, _DEFAULT_MNEMONIC_PASSWORD)
        bogus = f"default:99:{raw}"
        with pytest.raises(ValueError, match="Unsupported default encryption version"):
            temp_storage.retrieve_mnemonic(bogus, None)

    # --- malformed default: prefix parsing ------------------------------

    @pytest.mark.parametrize(
        "bad_blob",
        [
            "default:",  # nothing after the prefix
            "default:novalidversion",  # single segment, no second colon
            "default::blob",  # empty version string
            "default:abc:blob",  # non-integer version
        ],
    )
    def test_retrieve_malformed_default_prefix_raises(self, temp_storage, bad_blob):
        with pytest.raises(ValueError, match="Malformed default-encrypted mnemonic prefix"):
            temp_storage.retrieve_mnemonic(bad_blob, None)

    def test_malformed_prefix_error_truncates_long_input(self, temp_storage):
        # Error message must not echo arbitrarily long ciphertext.
        long_garbage = "default:abc:" + ("X" * 5000)
        with pytest.raises(ValueError) as excinfo:
            temp_storage.retrieve_mnemonic(long_garbage, None)
        assert "X" * 100 not in str(excinfo.value)

    # --- requires_user_password truth table ------------------------------

    @pytest.mark.parametrize(
        "make_blob, expected",
        [
            (lambda s: s.store_mnemonic(TEST_MNEMONIC, None), False),  # default:1:
            (
                lambda s: "default:99:"
                + s.encrypt_mnemonic(TEST_MNEMONIC, _DEFAULT_MNEMONIC_PASSWORD),
                False,
            ),  # default:99: (unsupported version still doesn't require user pw)
            (lambda s: s.store_mnemonic(TEST_MNEMONIC, "pw"), True),  # user:
            (lambda s: _make_plain_blob(TEST_MNEMONIC), False),  # plain:
            (
                lambda s: _make_untagged_legacy_blob(s, TEST_MNEMONIC, "pw"),
                True,
            ),  # untagged legacy
        ],
        ids=["default-v1", "default-v99", "user", "plain", "untagged-legacy"],
    )
    def test_requires_user_password_truth_table(self, temp_storage, make_blob, expected):
        blob = make_blob(temp_storage)
        assert temp_storage.requires_user_password(blob) is expected

    # --- read_and_migrate_mnemonic ---------------------------------------

    def test_lazy_migration_plain_to_default(self, temp_storage):
        """A ``plain:`` wallet read via read_and_migrate_mnemonic is rewritten to ``default:1:``."""
        wallet = WalletData(
            name="legacy_plain",
            network="mainnet",
            descriptor="ct(...)",
            encrypted_mnemonic=_make_plain_blob(TEST_MNEMONIC),
        )
        temp_storage.save_wallet(wallet)

        returned = temp_storage.read_and_migrate_mnemonic(wallet, None)
        assert returned == TEST_MNEMONIC

        # Reload from disk and verify it was rewritten.
        reloaded = temp_storage.load_wallet("legacy_plain")
        assert reloaded.encrypted_mnemonic.startswith(f"default:{_DEFAULT_PWD_VERSION}:")
        assert not temp_storage.requires_user_password(reloaded.encrypted_mnemonic)
        # And the round-trip still gives the original mnemonic.
        assert temp_storage.retrieve_mnemonic(reloaded.encrypted_mnemonic, None) == TEST_MNEMONIC

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix mode bits")
    def test_lazy_migration_atomicity(self, temp_storage):
        """After migration the wallet file remains 0o600 and is valid JSON."""
        import json

        wallet = WalletData(
            name="atomic_plain",
            network="mainnet",
            descriptor="ct(...)",
            encrypted_mnemonic=_make_plain_blob(TEST_MNEMONIC),
        )
        temp_storage.save_wallet(wallet)

        temp_storage.read_and_migrate_mnemonic(wallet, None)

        path = temp_storage._wallet_path("atomic_plain")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"Expected 0600 after migration, got {oct(mode)}"
        with open(path) as f:
            data = json.load(f)
        assert data["encrypted_mnemonic"].startswith(f"default:{_DEFAULT_PWD_VERSION}:")

    def test_lazy_migration_continues_on_write_failure(self, temp_storage, caplog):
        """If save_wallet raises OSError during migration, plaintext is still returned."""
        wallet = WalletData(
            name="ro_plain",
            network="mainnet",
            descriptor="ct(...)",
            encrypted_mnemonic=_make_plain_blob(TEST_MNEMONIC),
        )
        temp_storage.save_wallet(wallet)

        with patch.object(
            temp_storage, "save_wallet", side_effect=OSError("read-only filesystem")
        ):
            with caplog.at_level(logging.WARNING, logger="aqua.storage"):
                returned = temp_storage.read_and_migrate_mnemonic(wallet, None)

        assert returned == TEST_MNEMONIC
        # logger.warning must mention the wallet name.
        assert any("ro_plain" in rec.message for rec in caplog.records)
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_read_and_migrate_does_not_touch_default_user_or_untagged(self, temp_storage):
        """Non-plain blobs are NOT rewritten by read_and_migrate_mnemonic."""
        # default:1: blob — should pass through untouched.
        default_blob = temp_storage.store_mnemonic(TEST_MNEMONIC, None)
        w1 = WalletData(
            name="def_w", network="mainnet", descriptor="ct(...)",
            encrypted_mnemonic=default_blob,
        )
        temp_storage.save_wallet(w1)
        assert temp_storage.read_and_migrate_mnemonic(w1, None) == TEST_MNEMONIC
        assert temp_storage.load_wallet("def_w").encrypted_mnemonic == default_blob

        # user: blob.
        user_blob = temp_storage.store_mnemonic(TEST_MNEMONIC, "pw")
        w2 = WalletData(
            name="user_w", network="mainnet", descriptor="ct(...)",
            encrypted_mnemonic=user_blob,
        )
        temp_storage.save_wallet(w2)
        assert temp_storage.read_and_migrate_mnemonic(w2, "pw") == TEST_MNEMONIC
        assert temp_storage.load_wallet("user_w").encrypted_mnemonic == user_blob

        # untagged legacy.
        untagged = _make_untagged_legacy_blob(temp_storage, TEST_MNEMONIC, "pw")
        w3 = WalletData(
            name="untagged_w", network="mainnet", descriptor="ct(...)",
            encrypted_mnemonic=untagged,
        )
        temp_storage.save_wallet(w3)
        assert temp_storage.read_and_migrate_mnemonic(w3, "pw") == TEST_MNEMONIC
        assert temp_storage.load_wallet("untagged_w").encrypted_mnemonic == untagged

    # --- End-to-end via WalletManager / BitcoinWalletManager -------------

    def test_default_encrypted_wallet_signs_without_password(self, temp_storage):
        """No-password import yields a wallet that loads signer with no password."""
        from aqua.wallet import WalletManager

        wm = WalletManager(storage=temp_storage)
        wm.import_mnemonic(TEST_MNEMONIC, "default_signer", "mainnet")

        # Drop cached signer to force the disk read path.
        wm._signers.pop("default_signer", None)
        loaded = wm.load_wallet("default_signer")
        assert loaded.encrypted_mnemonic.startswith(f"default:{_DEFAULT_PWD_VERSION}:")
        assert "default_signer" in wm._signers

    def test_user_encrypted_wallet_still_requires_password(self, temp_storage):
        """User-password wallets continue to need the password.

        Contract: load_wallet without password silently skips signer caching
        (legacy behavior), but any signing operation (``send``) must raise
        ``ValueError("Password required ...")``. With the correct password,
        both load and signing setup succeed.
        """
        from aqua.wallet import WalletManager

        wm = WalletManager(storage=temp_storage)
        wm.import_mnemonic(TEST_MNEMONIC, "user_signer", "mainnet", password="s3cret")
        wm._signers.pop("user_signer", None)

        # Without a password: signer is NOT loaded (legacy behavior preserved;
        # load_wallet does not raise here, but the signer simply is not cached).
        wm.load_wallet("user_signer")
        assert "user_signer" not in wm._signers

        # A signing operation without the password must raise.
        with pytest.raises(ValueError, match="Password required"):
            wm.send("user_signer", "lq1qbogus", 1000)

        # With the correct password the signer loads.
        wm.load_wallet("user_signer", password="s3cret")
        assert "user_signer" in wm._signers

    def test_bitcoin_send_migrates_plain_wallet(self, temp_storage):
        """BitcoinWalletManager.send on a legacy plain: wallet migrates it on disk.

        We intercept ``_get_wallet_with_signer`` to short-circuit the BDK
        broadcast path; by that point ``read_and_migrate_mnemonic`` must have
        already rewritten the on-disk blob to ``default:1:``.
        """
        from aqua.bitcoin import BitcoinWalletManager

        # Build a plain: BTC-capable wallet directly via the storage layer.
        # We do not call WalletManager.import_mnemonic (that path now produces
        # default:1:), so we hand-roll the legacy state for this test.
        legacy_blob = _make_plain_blob(TEST_MNEMONIC)
        wallet = WalletData(
            name="btc_plain",
            network="mainnet",
            descriptor="ct(...)",  # not exercised; bitcoin.py only uses btc_* fields
            btc_descriptor="wpkh([deadbeef/84'/0'/0']xpub.../0/*)",
            btc_change_descriptor="wpkh([deadbeef/84'/0'/0']xpub.../1/*)",
            encrypted_mnemonic=legacy_blob,
            watch_only=False,
        )
        temp_storage.save_wallet(wallet)

        btc_manager = BitcoinWalletManager(storage=temp_storage)

        # Short-circuit BDK after migration has already happened.
        sentinel = RuntimeError("short-circuit after migration")
        with patch.object(
            btc_manager, "_get_wallet_with_signer", side_effect=sentinel
        ):
            with pytest.raises(RuntimeError, match="short-circuit after migration"):
                btc_manager.send(
                    wallet_name="btc_plain",
                    address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
                    amount=1000,
                )

        # The on-disk blob must now be default:1: regardless of the short-circuit.
        reloaded = temp_storage.load_wallet("btc_plain")
        assert reloaded.encrypted_mnemonic.startswith(f"default:{_DEFAULT_PWD_VERSION}:")
        assert temp_storage.retrieve_mnemonic(reloaded.encrypted_mnemonic, None) == TEST_MNEMONIC


class TestWapuPayApiKeyStorage:
    """Persistence of the WapuPay API key provisioned via the AQUA backend."""

    def _make_key(self):
        from aqua.wapupay import WapuPayApiKey

        return WapuPayApiKey(token="WapuKey_secret_123", created_at="2026-06-15T00:00:00+00:00")

    def test_save_load_roundtrip(self, temp_storage):
        temp_storage.save_wapupay_api_key(self._make_key())
        loaded = temp_storage.load_wapupay_api_key()
        assert loaded is not None
        assert loaded.token == "WapuKey_secret_123"
        assert loaded.created_at == "2026-06-15T00:00:00+00:00"

    def test_load_missing_returns_none(self, temp_storage):
        assert temp_storage.load_wapupay_api_key() is None

    def test_delete_is_idempotent(self, temp_storage):
        temp_storage.save_wapupay_api_key(self._make_key())
        temp_storage.delete_wapupay_api_key()
        assert temp_storage.load_wapupay_api_key() is None
        temp_storage.delete_wapupay_api_key()  # no error second time

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix mode bits")
    def test_api_key_file_permissions(self, temp_storage):
        temp_storage.save_wapupay_api_key(self._make_key())
        mode = stat.S_IMODE(os.stat(temp_storage.wapupay_api_key_path).st_mode)
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"

    def test_delete_session_keeps_api_key(self, temp_storage):
        """The API key is decoupled from the AQUA session — logout must not drop it."""
        from aqua.ankara import JAN3Session

        temp_storage.save_jan3_session(
            JAN3Session(email="a@b.com", access="acc", refresh="ref", created_at="t0")
        )
        temp_storage.save_wapupay_api_key(self._make_key())
        temp_storage.delete_jan3_session()
        assert temp_storage.load_jan3_session() is None
        assert temp_storage.load_wapupay_api_key().token == "WapuKey_secret_123"
