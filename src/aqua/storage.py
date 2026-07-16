"""Storage layer for wallet persistence."""

import base64
import json
import logging
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

SALT_LENGTH = 16


def restrict_permissions(path: Path | str, mode: int) -> None:
    """Apply Unix permission bits. No-op on Windows where chmod strips inherited ACLs."""
    if sys.platform == "win32":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


DEFAULT_DIR = Path.home() / ".aqua"
SWAP_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

# Hardcoded default password used to encrypt mnemonics at rest when the user
# does not supply one. This is *obfuscation, not security* — it defeats
# plaintext-seed scanners.
# (the `default:N:` version prefix enables this without format ambiguity).
_DEFAULT_MNEMONIC_PASSWORD = "Delphinus entropiam novit sed semen suum numquam revelat"

# Version embedded in the `default:N:` prefix. Bump this if the
# _DEFAULT_MNEMONIC_PASSWORD ever rotates. Both encode and decode reference this
# constant so they cannot desync.
_DEFAULT_PWD_VERSION = 1


@dataclass
class WalletData:
    """Wallet data structure."""

    name: str
    network: str  # "mainnet" or "testnet"
    descriptor: str  # CT descriptor (Liquid)
    btc_descriptor: Optional[str] = None  # BIP84 external descriptor (Bitcoin)
    btc_change_descriptor: Optional[str] = None  # BIP84 change descriptor (Bitcoin)
    encrypted_mnemonic: Optional[str] = None  # Encrypted, if full wallet
    watch_only: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Monotonically-increasing counter of handed-out receive addresses. Advances
    # ahead of lwk's "next-unused" tip for off-chain handouts (LN-address pool,
    # Boltz claims) so two flows never share an index.
    next_address_index: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WalletData":
        # Backward compatibility: old wallet files may not have btc_* fields
        data = {**data}
        data.setdefault("btc_descriptor", None)
        data.setdefault("btc_change_descriptor", None)
        # Migrate the legacy ``ln_addr_next_index`` key so wallets keep their history.
        legacy = data.pop("ln_addr_next_index", None)
        if "next_address_index" not in data and legacy is not None:
            data["next_address_index"] = legacy
        data.setdefault("next_address_index", 0)
        # Forward compatibility: drop unknown keys written by future branches.
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        return cls(**data)


@dataclass
class Config:
    """Global configuration."""

    network: str = "mainnet"
    default_wallet: str = "default"
    electrum_url: Optional[str] = None
    auto_sync: bool = True
    enabled_tools: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        data = {**data}
        raw = data.get("enabled_tools", {}) or {}
        if not isinstance(raw, dict):
            logger.warning(
                "enabled_tools must be an object mapping tool name -> bool; "
                "got %s. Treating as empty.",
                type(raw).__name__,
            )
            raw = {}
        coerced: dict[str, bool] = {}
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, bool):
                coerced[k] = v
            else:
                logger.warning(
                    "Dropping invalid enabled_tools entry %r=%r "
                    "(expected str -> bool).",
                    k,
                    v,
                )
        data["enabled_tools"] = coerced
        # Drop unknown top-level keys instead of crashing (mirrors WalletData.from_dict);
        # `aqua doctor --fix` removes them from disk.
        for key in data:
            if key not in KNOWN_CONFIG_KEYS:
                logger.warning(
                    "Unknown config key %r (ignored). "
                    "Run `aqua doctor --fix` to clean it up.",
                    key,
                )
        data = {k: v for k, v in data.items() if k in KNOWN_CONFIG_KEYS}
        return cls(**data)


# Single source of truth for the Config schema's top-level keys
# (shared with `aqua.doctor`, which reads the file as raw JSON).
KNOWN_CONFIG_KEYS: frozenset[str] = frozenset(f.name for f in fields(Config))


def _validate_wallet_name(name: str) -> str:
    """Validate wallet name to prevent path traversal."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
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
        self.swaps_dir = self.base_dir / "swaps"
        self.ankara_swaps_dir = self.base_dir / "ankara_swaps"
        self.lightning_swaps_dir = self.base_dir / "lightning_swaps"
        self.changelly_swaps_dir = self.base_dir / "changelly_swaps"
        self.sideshift_shifts_dir = self.base_dir / "sideshift_shifts"
        self.sideswap_pegs_dir = self.base_dir / "sideswap_pegs"
        self.sideswap_swaps_dir = self.base_dir / "sideswap_swaps"
        self.wapupay_dir = self.base_dir / "wapupay"
        self.wapupay_orders_dir = self.wapupay_dir / "orders"
        self.wapupay_api_key_path = self.wapupay_dir / "api_key.json"
        self.jan3_dir = self.base_dir / "jan3"
        self.qr_dir = self.base_dir / "qr"
        self.config_path = self.base_dir / "config.json"
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create necessary directories with restricted permissions."""
        self.base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        restrict_permissions(self.base_dir, 0o700)
        self.wallets_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.wallets_dir, 0o700)
        self.cache_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.cache_dir, 0o700)
        self.swaps_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.swaps_dir, 0o700)
        self.ankara_swaps_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.ankara_swaps_dir, 0o700)
        self.lightning_swaps_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.lightning_swaps_dir, 0o700)
        self.changelly_swaps_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.changelly_swaps_dir, 0o700)
        self.sideshift_shifts_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.sideshift_shifts_dir, 0o700)
        self.sideswap_pegs_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.sideswap_pegs_dir, 0o700)
        self.sideswap_swaps_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.sideswap_swaps_dir, 0o700)
        self.wapupay_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.wapupay_dir, 0o700)
        self.wapupay_orders_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.wapupay_orders_dir, 0o700)
        self.jan3_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.jan3_dir, 0o700)
        self.qr_dir.mkdir(exist_ok=True, mode=0o700)
        restrict_permissions(self.qr_dir, 0o700)

    def _derive_key(self, password: str, salt: bytes) -> bytes:
        """Derive encryption key from password."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,
        )
        return base64.urlsafe_b64encode(kdf.derive(password.encode()))

    def encrypt_mnemonic(self, mnemonic: str, password: str) -> str:
        """Encrypt mnemonic with password (used only for at-rest encryption)."""
        salt = os.urandom(SALT_LENGTH)
        key = self._derive_key(password, salt)
        f = Fernet(key)
        encrypted = f.encrypt(mnemonic.encode())
        # Store salt + encrypted data
        return base64.b64encode(salt + encrypted).decode()

    def decrypt_mnemonic(self, encrypted: str, password: str) -> str:
        """Decrypt mnemonic with password."""
        data = base64.b64decode(encrypted)
        salt = data[:SALT_LENGTH]
        encrypted_data = data[SALT_LENGTH:]
        key = self._derive_key(password, salt)
        f = Fernet(key)
        return f.decrypt(encrypted_data).decode()

    def store_mnemonic(self, mnemonic: str, password: Optional[str] = None) -> str:
        """Store mnemonic, encrypting with user password or default password.

        NOTE: ``password`` is used exclusively to encrypt the mnemonic on disk.
        It is NOT used as a BIP39 passphrase — the derived seed/keys depend
        only on the mnemonic itself, so descriptors stay portable across
        wallets that accept the same mnemonic (AQUA, Blockstream Green, etc.).

        When no password is supplied, the mnemonic is still encrypted using a
        hardcoded default password (``default:N:`` prefix).
        """
        if password:
            return "user:" + self.encrypt_mnemonic(mnemonic, password)
        return (
            f"default:{_DEFAULT_PWD_VERSION}:"
            + self.encrypt_mnemonic(mnemonic, _DEFAULT_MNEMONIC_PASSWORD)
        )

    def retrieve_mnemonic(self, stored: str, password: Optional[str] = None) -> str:
        """Retrieve mnemonic stored by store_mnemonic.

        Recognizes four prefix formats:
          * ``default:N:<blob>`` — encrypted with the hardcoded default
            password at version ``N``. ``password`` is ignored.
          * ``user:<blob>`` — encrypted with a user-supplied password.
          * ``plain:<blob>`` — legacy base64-only blob (no password).
          * untagged — legacy raw encrypted blob from a user password.
        """
        if stored.startswith("default:"):
            # Parse "default:<version>:<blob>"
            rest = stored[len("default:") :]
            try:
                version_str, blob = rest.split(":", 1)
                version = int(version_str)
            except ValueError as e:
                raise ValueError(
                    f"Malformed default-encrypted mnemonic prefix: {stored[:32]!r}"
                ) from e
            if version != _DEFAULT_PWD_VERSION:
                raise ValueError(
                    f"Unsupported default encryption version: {version}"
                )
            return self.decrypt_mnemonic(blob, _DEFAULT_MNEMONIC_PASSWORD)
        if stored.startswith("user:"):
            if not password:
                raise ValueError("Password required to decrypt mnemonic")
            return self.decrypt_mnemonic(stored[len("user:") :], password)
        if stored.startswith("plain:"):
            return base64.b64decode(stored[len("plain:") :]).decode()
        # Untagged legacy blob: assume user-password-encrypted.
        if not password:
            raise ValueError("Password required to decrypt mnemonic")
        return self.decrypt_mnemonic(stored, password)

    def requires_user_password(self, stored: str) -> bool:
        """Return True if decrypting ``stored`` needs a user-supplied password.

        Returns True only for ``user:`` and untagged legacy blobs.
        Returns False for ``default:`` (any version) and ``plain:`` prefixes.
        """
        if stored.startswith("default:") or stored.startswith("plain:"):
            return False
        # "user:" prefix and untagged legacy blobs require a password.
        return True

    def read_and_migrate_mnemonic(
        self, wallet: WalletData, password: Optional[str] = None
    ) -> str:
        """Decrypt the wallet's mnemonic and lazily migrate ``plain:`` blobs.

        Accepts an already-loaded ``WalletData`` (no redundant disk read).
        If the stored blob uses the legacy ``plain:`` prefix, re-encrypt it
        with the default password and atomically write it back. Write-back
        failures are logged but never raised — the returned plaintext is
        always correct so signing continues to work even on a read-only
        filesystem.
        """
        if not wallet.encrypted_mnemonic:
            raise ValueError(f"Wallet '{wallet.name}' has no stored mnemonic")
        plaintext = self.retrieve_mnemonic(wallet.encrypted_mnemonic, password)
        if wallet.encrypted_mnemonic.startswith("plain:"):
            new_blob = self.store_mnemonic(plaintext)
            try:
                # Persist first via a temporary copy. Only mutate the caller's
                # WalletData on success so the in-memory object never diverges
                # from disk on a write failure.
                self.save_wallet(replace(wallet, encrypted_mnemonic=new_blob))
                wallet.encrypted_mnemonic = new_blob
                logger.info(
                    "Migrated wallet '%s' from plain to default encryption",
                    wallet.name,
                )
            except OSError as e:
                # Housekeeping fault tolerance: the read succeeded; only the
                # optional re-write failed. Signing must not break on a
                # read-only filesystem.
                logger.warning(
                    "Migration write-back failed for wallet '%s': %s",
                    wallet.name,
                    e,
                )
        return plaintext

    # Config operations

    def load_config(self) -> Config:
        """Load global configuration."""
        if self.config_path.exists():
            with open(self.config_path) as f:
                return Config.from_dict(json.load(f))
        return Config()

    def _atomic_write_json(self, path: Path, data: dict) -> None:
        """Atomically write JSON data to a file with restricted permissions."""
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            restrict_permissions(tmp_path, 0o600)
            os.replace(tmp_path, path)
            restrict_permissions(path, 0o600)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def save_config(self, config: Config):
        self._atomic_write_json(self.config_path, config.to_dict())

    def save_raw_config(self, data: dict) -> None:
        """Persist an already-shaped raw config dict atomically (0o600).

        Skips the `Config` round-trip so `doctor` can rewrite a file `Config.from_dict`
        would reject, preserving entries verbatim instead of coercing them.
        """
        self._atomic_write_json(self.config_path, data)

    # Wallet operations

    def _wallet_path(self, name: str) -> Path:
        """Get path to wallet file."""
        _validate_wallet_name(name)
        return self.wallets_dir / f"{name}.json"

    @contextmanager
    def wallet_lock(self, name: str, timeout_seconds: float = 30.0):
        """Cross-process exclusive lock for read-modify-write of a wallet record.

        ``save_wallet`` is a last-writer-wins overwrite, so counter-advancing
        writers must load-and-save while holding this lock to avoid two
        processes handing out the same index. Raises ``TimeoutError`` on timeout.
        """
        _validate_wallet_name(name)
        lock_path = self.wallets_dir / f"{name}.lock"
        # "a" creates the file if missing without ever truncating it.
        fh = open(lock_path, "a")
        try:
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    if sys.platform == "win32":
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Could not lock wallet {name!r} within "
                            f"{timeout_seconds:.0f}s — another aqua process "
                            "is holding it."
                        ) from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def wallet_exists(self, name: str) -> bool:
        """Check if wallet exists."""
        return self._wallet_path(name).exists()

    def list_wallets(self) -> list[str]:
        """List all wallet names."""
        return [
            p.stem
            for p in self.wallets_dir.glob("*.json")
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", p.stem)
        ]

    def load_wallet(self, name: str) -> Optional[WalletData]:
        """Load wallet data."""
        path = self._wallet_path(name)
        if not path.exists():
            return None
        with open(path) as f:
            return WalletData.from_dict(json.load(f))

    def save_wallet(self, wallet: WalletData):
        path = self._wallet_path(wallet.name)
        self._atomic_write_json(path, wallet.to_dict())

    def delete_wallet(self, name: str) -> bool:
        """Delete wallet and its cache directory."""
        path = self._wallet_path(name)
        if not path.exists():
            return False
        path.unlink()
        cache_path = self.cache_dir / name
        if cache_path.is_dir():
            shutil.rmtree(cache_path)
        return True

    # Swap operations

    def _swap_path(self, swap_id: str) -> Path:
        """Get path to swap file, validating the ID to prevent path traversal."""
        if not SWAP_ID_PATTERN.fullmatch(swap_id):
            raise ValueError(
                f"Invalid swap ID '{swap_id}'. "
                "Use only letters, numbers, hyphens and underscores (max 128 chars)."
            )
        return self.swaps_dir / f"{swap_id}.json"

    def save_swap(self, swap) -> None:
        """Save swap data for recovery."""
        path = self._swap_path(swap.swap_id)
        self._atomic_write_json(path, swap.to_dict())

    def load_swap(self, swap_id: str):
        """Load swap data. Returns SwapInfo or None."""
        from .boltz import SwapInfo

        path = self._swap_path(swap_id)
        if not path.exists():
            return None
        with open(path) as f:
            return SwapInfo(**json.load(f))

    def list_swaps(self) -> list[str]:
        """List all swap IDs."""
        return [p.stem for p in self.swaps_dir.glob("*.json") if SWAP_ID_PATTERN.fullmatch(p.stem)]

    # Ankara swap operations

    def _ankara_swap_path(self, swap_id: str) -> Path:
        """Get path to Ankara swap file, validating the ID to prevent path traversal."""
        if not SWAP_ID_PATTERN.fullmatch(swap_id):
            raise ValueError(
                f"Invalid swap ID '{swap_id}'. "
                "Use only letters, numbers, hyphens and underscores (max 128 chars)."
            )
        return self.ankara_swaps_dir / f"{swap_id}.json"

    def save_ankara_swap(self, swap) -> None:
        """Save Ankara swap data for recovery."""
        path = self._ankara_swap_path(swap.swap_id)
        self._atomic_write_json(path, swap.to_dict())

    def load_ankara_swap(self, swap_id: str):
        """Load Ankara swap data. Returns AnkaraSwapInfo or None."""
        from .ankara import AnkaraSwapInfo

        path = self._ankara_swap_path(swap_id)
        if not path.exists():
            return None
        with open(path) as f:
            return AnkaraSwapInfo(**json.load(f))

    def list_ankara_swaps(self) -> list[str]:
        """List all Ankara swap IDs."""
        return [
            p.stem
            for p in self.ankara_swaps_dir.glob("*.json")
            if SWAP_ID_PATTERN.fullmatch(p.stem)
        ]

    # Lightning swap operations

    def _lightning_swap_path(self, swap_id: str) -> Path:
        """Get path to Lightning swap file, validating the ID to prevent path traversal."""
        if not SWAP_ID_PATTERN.fullmatch(swap_id):
            raise ValueError(
                f"Invalid swap ID '{swap_id}'. "
                "Use only letters, numbers, hyphens and underscores (max 128 chars)."
            )
        return self.lightning_swaps_dir / f"{swap_id}.json"

    def save_lightning_swap(self, swap) -> None:
        """Save Lightning swap data for recovery."""
        path = self._lightning_swap_path(swap.swap_id)
        self._atomic_write_json(path, swap.to_dict())

    def load_lightning_swap(self, swap_id: str):
        """Load Lightning swap data. Returns LightningSwap or None."""
        from .lightning import LightningSwap

        path = self._lightning_swap_path(swap_id)
        if not path.exists():
            return None
        with open(path) as f:
            return LightningSwap.from_dict(json.load(f))

    def list_lightning_swaps(self) -> list[str]:
        """List all Lightning swap IDs."""
        return [
            p.stem
            for p in self.lightning_swaps_dir.glob("*.json")
            if SWAP_ID_PATTERN.fullmatch(p.stem)
        ]

    # Changelly swap operations

    def _changelly_swap_path(self, order_id: str) -> Path:
        """Get path to Changelly swap file, validating the ID to prevent path traversal."""
        if not SWAP_ID_PATTERN.fullmatch(order_id):
            raise ValueError(
                f"Invalid Changelly order ID '{order_id}'. "
                "Use only letters, numbers, hyphens and underscores (max 128 chars)."
            )
        return self.changelly_swaps_dir / f"{order_id}.json"

    def save_changelly_swap(self, swap) -> None:
        """Save Changelly swap data for recovery."""
        path = self._changelly_swap_path(swap.order_id)
        self._atomic_write_json(path, swap.to_dict())

    def load_changelly_swap(self, order_id: str):
        """Load Changelly swap data. Returns ChangellySwap or None."""
        from .changelly import ChangellySwap

        path = self._changelly_swap_path(order_id)
        if not path.exists():
            return None
        with open(path) as f:
            return ChangellySwap.from_dict(json.load(f))

    def list_changelly_swaps(self) -> list[str]:
        """List all Changelly swap order IDs."""
        return [
            p.stem
            for p in self.changelly_swaps_dir.glob("*.json")
            if SWAP_ID_PATTERN.fullmatch(p.stem)
        ]

    # SideShift shift operations

    def _sideshift_shift_path(self, shift_id: str) -> Path:
        """Get path to SideShift shift file, validating the ID to prevent path traversal."""
        if not SWAP_ID_PATTERN.fullmatch(shift_id):
            raise ValueError(
                f"Invalid SideShift shift ID '{shift_id}'. "
                "Use only letters, numbers, hyphens and underscores (max 128 chars)."
            )
        return self.sideshift_shifts_dir / f"{shift_id}.json"

    def save_sideshift_shift(self, shift) -> None:
        """Save SideShift shift data for recovery."""
        path = self._sideshift_shift_path(shift.shift_id)
        self._atomic_write_json(path, shift.to_dict())

    def load_sideshift_shift(self, shift_id: str):
        """Load SideShift shift data. Returns SideShiftShift or None."""
        from .sideshift import SideShiftShift

        path = self._sideshift_shift_path(shift_id)
        if not path.exists():
            return None
        with open(path) as f:
            return SideShiftShift.from_dict(json.load(f))

    def list_sideshift_shifts(self) -> list[str]:
        """List all SideShift shift IDs."""
        return [
            p.stem
            for p in self.sideshift_shifts_dir.glob("*.json")
            if SWAP_ID_PATTERN.fullmatch(p.stem)
        ]

    # SideSwap peg operations

    def _sideswap_peg_path(self, order_id: str) -> Path:
        """Get path to SideSwap peg file, validating the ID to prevent path traversal."""
        if not SWAP_ID_PATTERN.fullmatch(order_id):
            raise ValueError(
                f"Invalid SideSwap order ID '{order_id}'. "
                "Use only letters, numbers, hyphens and underscores (max 128 chars)."
            )
        return self.sideswap_pegs_dir / f"{order_id}.json"

    def save_sideswap_peg(self, peg) -> None:
        """Save SideSwap peg data for recovery."""
        path = self._sideswap_peg_path(peg.order_id)
        self._atomic_write_json(path, peg.to_dict())

    def load_sideswap_peg(self, order_id: str):
        """Load SideSwap peg data. Returns SideSwapPeg or None."""
        from .sideswap import SideSwapPeg

        path = self._sideswap_peg_path(order_id)
        if not path.exists():
            return None
        with open(path) as f:
            return SideSwapPeg.from_dict(json.load(f))

    def list_sideswap_pegs(self) -> list[str]:
        """List all SideSwap peg order IDs."""
        return [
            p.stem
            for p in self.sideswap_pegs_dir.glob("*.json")
            if SWAP_ID_PATTERN.fullmatch(p.stem)
        ]

    # SideSwap asset-swap operations

    def _sideswap_swap_path(self, order_id: str) -> Path:
        """Get path to SideSwap swap file, validating the ID to prevent path traversal."""
        if not SWAP_ID_PATTERN.fullmatch(order_id):
            raise ValueError(
                f"Invalid SideSwap order ID '{order_id}'. "
                "Use only letters, numbers, hyphens and underscores (max 128 chars)."
            )
        return self.sideswap_swaps_dir / f"{order_id}.json"

    def save_sideswap_swap(self, swap) -> None:
        """Save SideSwap asset swap data for recovery."""
        path = self._sideswap_swap_path(swap.order_id)
        self._atomic_write_json(path, swap.to_dict())

    def load_sideswap_swap(self, order_id: str):
        """Load SideSwap swap data. Returns SideSwapSwap or None."""
        from .sideswap import SideSwapSwap

        path = self._sideswap_swap_path(order_id)
        if not path.exists():
            return None
        with open(path) as f:
            return SideSwapSwap.from_dict(json.load(f))

    def list_sideswap_swaps(self) -> list[str]:
        """List all SideSwap swap order IDs."""
        return [
            p.stem
            for p in self.sideswap_swaps_dir.glob("*.json")
            if SWAP_ID_PATTERN.fullmatch(p.stem)
        ]

    def delete_sideswap_pegs_for_wallet(self, wallet_name: str) -> int:
        """Delete SideSwap peg records whose `wallet_name` matches.

        Idempotent — returns 0 silently if the directory or matching files
        don't exist. Returns the number of files removed.
        """
        if not self.sideswap_pegs_dir.exists():
            return 0
        removed = 0
        for path in self.sideswap_pegs_dir.glob("*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("wallet_name") == wallet_name:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    # JAN3 / AQUA account sessions are managed (multi-account, per-email) by
    # Jan3AccountsManager in jan3_accounts.py, which reads/writes files directly
    # under jan3_dir. Storage only exposes the directory.

    # WapuPay operations

    def save_wapupay_api_key(self, api_key) -> None:
        """Persist the provisioned WapuPay API key."""
        self._atomic_write_json(self.wapupay_api_key_path, api_key.to_dict())

    def load_wapupay_api_key(self):
        """Load WapuPay API key; return None and log warning if unreadable or invalid."""

        from .wapupay import WapuPayApiKey

        if not self.wapupay_api_key_path.exists():
            return None
        try:
            with open(self.wapupay_api_key_path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("api_key.json is not a JSON object")
            return WapuPayApiKey.from_dict(data)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.warning(
                "Ignoring unreadable WapuPay API key file: %s",
                self.wapupay_api_key_path,
            )
            return None

    def delete_wapupay_api_key(self) -> None:
        """Remove the persisted WapuPay API key (idempotent)."""
        try:
            self.wapupay_api_key_path.unlink()
        except FileNotFoundError:
            pass

    def _wapupay_order_path(self, tentative_id: str) -> Path:
        """Get path to a WapuPay order file, validating the ID against traversal."""
        if not SWAP_ID_PATTERN.fullmatch(tentative_id):
            raise ValueError(
                f"Invalid WapuPay tentative ID '{tentative_id}'. "
                "Use only letters, numbers, hyphens and underscores (max 128 chars)."
            )
        return self.wapupay_orders_dir / f"{tentative_id}.json"

    def save_wapupay_order(self, order) -> None:
        """Save a WapuPay order record for recovery."""
        path = self._wapupay_order_path(order.tentative_id)
        self._atomic_write_json(path, order.to_dict())

    def load_wapupay_order(self, tentative_id: str):
        """Load a WapuPay order record. Returns WapuPayOrder or None."""
        from .wapupay import WapuPayOrder

        path = self._wapupay_order_path(tentative_id)
        if not path.exists():
            return None
        with open(path) as f:
            return WapuPayOrder.from_dict(json.load(f))

    def list_wapupay_orders(self) -> list[str]:
        """List all WapuPay order (tentative) IDs."""
        return [
            p.stem
            for p in self.wapupay_orders_dir.glob("*.json")
            if SWAP_ID_PATTERN.fullmatch(p.stem)
        ]

    # Cache operations

    def get_cache_path(self, wallet_name: str) -> Path:
        """Get cache directory for wallet."""
        _validate_wallet_name(wallet_name)
        cache_path = self.cache_dir / wallet_name
        cache_path.mkdir(exist_ok=True, mode=0o700)
        return cache_path
