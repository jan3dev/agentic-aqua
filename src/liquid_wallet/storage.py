"""Storage layer for wallet persistence."""

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64


DEFAULT_DIR = Path.home() / ".liquid-wallet"


@dataclass
class WalletData:
    """Wallet data structure."""
    name: str
    network: str  # "mainnet" or "testnet"
    descriptor: str  # CT descriptor
    encrypted_mnemonic: Optional[str] = None  # Encrypted, if full wallet
    watch_only: bool = False
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WalletData":
        return cls(**data)


@dataclass
class Config:
    """Global configuration."""
    network: str = "mainnet"
    default_wallet: str = "default"
    electrum_url: Optional[str] = None
    auto_sync: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        return cls(**data)


def _validate_wallet_name(name: str) -> str:
    """Validate wallet name to prevent path traversal."""
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name):
        raise ValueError(
            f"Invalid wallet name '{name}'. "
            "Use only letters, numbers, hyphens and underscores (max 64 chars)."
        )
    return name


class Storage:
    """Handles wallet and config persistence."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or DEFAULT_DIR
        self.wallets_dir = self.base_dir / "wallets"
        self.cache_dir = self.base_dir / "cache"
        self.config_path = self.base_dir / "config.json"
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create necessary directories with restricted permissions."""
        self.base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.base_dir, 0o700)
        self.wallets_dir.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.wallets_dir, 0o700)
        self.cache_dir.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.cache_dir, 0o700)

    def _derive_key(self, passphrase: str, salt: bytes) -> bytes:
        """Derive encryption key from passphrase."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,
        )
        return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))

    def encrypt_mnemonic(self, mnemonic: str, passphrase: str) -> str:
        """Encrypt mnemonic with passphrase."""
        salt = os.urandom(16)
        key = self._derive_key(passphrase, salt)
        f = Fernet(key)
        encrypted = f.encrypt(mnemonic.encode())
        # Store salt + encrypted data
        return base64.b64encode(salt + encrypted).decode()

    def decrypt_mnemonic(self, encrypted: str, passphrase: str) -> str:
        """Decrypt mnemonic with passphrase."""
        data = base64.b64decode(encrypted)
        salt = data[:16]
        encrypted_data = data[16:]
        key = self._derive_key(passphrase, salt)
        f = Fernet(key)
        return f.decrypt(encrypted_data).decode()

    # Config operations

    def load_config(self) -> Config:
        """Load global configuration."""
        if self.config_path.exists():
            with open(self.config_path) as f:
                return Config.from_dict(json.load(f))
        return Config()

    def save_config(self, config: Config):
        """Save global configuration."""
        fd = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(config.to_dict(), f, indent=2)

    # Wallet operations

    def _wallet_path(self, name: str) -> Path:
        """Get path to wallet file."""
        _validate_wallet_name(name)
        return self.wallets_dir / f"{name}.json"

    def wallet_exists(self, name: str) -> bool:
        """Check if wallet exists."""
        return self._wallet_path(name).exists()

    def list_wallets(self) -> list[str]:
        """List all wallet names."""
        return [
            p.stem for p in self.wallets_dir.glob("*.json")
            if re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', p.stem)
        ]

    def load_wallet(self, name: str) -> Optional[WalletData]:
        """Load wallet data."""
        path = self._wallet_path(name)
        if not path.exists():
            return None
        with open(path) as f:
            return WalletData.from_dict(json.load(f))

    def save_wallet(self, wallet: WalletData):
        """Save wallet data."""
        path = self._wallet_path(wallet.name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(wallet.to_dict(), f, indent=2)

    def delete_wallet(self, name: str) -> bool:
        """Delete wallet."""
        path = self._wallet_path(name)
        if path.exists():
            path.unlink()
            return True
        return False

    # Cache operations

    def get_cache_path(self, wallet_name: str) -> Path:
        """Get cache directory for wallet."""
        _validate_wallet_name(wallet_name)
        cache_path = self.cache_dir / wallet_name
        cache_path.mkdir(exist_ok=True, mode=0o700)
        return cache_path
