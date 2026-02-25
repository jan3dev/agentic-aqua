"""Tests for storage module."""

import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from liquid_wallet.storage import Storage, WalletData, Config, _validate_wallet_name


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

    def test_mnemonic_encryption(self, temp_storage):
        """Test mnemonic encryption/decryption."""
        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        passphrase = "test123"
        
        encrypted = temp_storage.encrypt_mnemonic(mnemonic, passphrase)
        assert encrypted != mnemonic
        
        decrypted = temp_storage.decrypt_mnemonic(encrypted, passphrase)
        assert decrypted == mnemonic

    def test_mnemonic_wrong_passphrase(self, temp_storage):
        """Test that wrong passphrase fails."""
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
