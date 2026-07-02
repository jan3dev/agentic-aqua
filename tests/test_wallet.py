"""Tests for WalletManager's persistent address counter + fingerprint.

These back the LN-address feature: ``reserve_addresses`` mints unused Liquid
receive addresses for the JAN3 LN-address pool, and ``fingerprint`` binds the
account to a wallet. The no-arg ``get_address`` was changed to advance a
persisted counter so off-chain handouts never reuse an index.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from aqua.storage import Storage
from aqua.wallet import WalletManager
from tests.conftest import TEST_MNEMONIC


@pytest.fixture
def wallet_manager():
    with tempfile.TemporaryDirectory() as tmpdir:
        wm = WalletManager(storage=Storage(Path(tmpdir)))
        wm.import_mnemonic(TEST_MNEMONIC, "default", "testnet")
        yield wm


class TestAddressCounter:
    def test_get_address_no_arg_advances_and_is_unique(self, wallet_manager):
        a = wallet_manager.get_address("default")
        b = wallet_manager.get_address("default")
        assert a.address != b.address
        assert b.index > a.index

    def test_get_address_persists_counter(self, wallet_manager):
        first = wallet_manager.get_address("default")
        rec = wallet_manager.storage.load_wallet("default")
        assert rec.next_address_index > first.index

    def test_explicit_index_does_not_advance_counter(self, wallet_manager):
        before = wallet_manager.storage.load_wallet("default").next_address_index
        got = wallet_manager.get_address("default", index=0)
        after = wallet_manager.storage.load_wallet("default").next_address_index
        assert got.index == 0
        assert after == before

    def test_reserve_addresses_batches_distinct(self, wallet_manager):
        addrs = wallet_manager.reserve_addresses("default", 5)
        assert len(addrs) == 5
        assert len({a.address for a in addrs}) == 5
        rec = wallet_manager.storage.load_wallet("default")
        assert rec.next_address_index >= addrs[-1].index + 1

    def test_reserve_addresses_rejects_nonpositive(self, wallet_manager):
        with pytest.raises(ValueError, match="positive"):
            wallet_manager.reserve_addresses("default", 0)

    def test_get_address_and_reserve_never_collide(self, wallet_manager):
        one = wallet_manager.get_address("default")
        batch = wallet_manager.reserve_addresses("default", 3)
        indices = {one.index} | {a.index for a in batch}
        assert len(indices) == 4  # all distinct — no reuse across the two paths


class TestFingerprint:
    def test_hot_wallet_fingerprint_is_8_hex(self, wallet_manager):
        fp = wallet_manager.fingerprint("default")
        assert len(fp) == 8
        int(fp, 16)  # must parse as hex

    def test_fingerprint_is_stable(self, wallet_manager):
        assert wallet_manager.fingerprint("default") == wallet_manager.fingerprint(
            "default"
        )
