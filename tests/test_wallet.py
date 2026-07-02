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


class TestEncryptedWalletNoPassword:
    """An at-rest-encrypted wallet still yields a fingerprint and fresh receive
    addresses WITHOUT the password — deriving addresses needs only the
    descriptor (xpub), not the mnemonic. This is the real behavior behind the
    LN-address pool self-heal: jan3_user_info can top up the pool of a
    password-encrypted wallet without ever decrypting the seed.
    """

    def test_fingerprint_and_reserve_without_password(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir))
            # Import with an at-rest password, then drop the cached signer by
            # using a FRESH manager over the same storage (as a new process
            # would) so no mnemonic is available in-memory.
            WalletManager(storage=storage).import_mnemonic(
                TEST_MNEMONIC, "enc", "testnet", password="pw-123-strong"
            )
            fresh = WalletManager(storage=storage)

            # No password supplied: fingerprint falls back to the descriptor's
            # [fp/derivation] block and reserve_addresses derives from the
            # descriptor — neither needs the decrypted mnemonic.
            fp = fresh.fingerprint("enc")
            assert len(fp) == 8
            int(fp, 16)
            addrs = fresh.reserve_addresses("enc", 2)
            assert len({a.address for a in addrs}) == 2

    def test_descriptor_fingerprint_matches_signer(self):
        # The watch-only descriptor parse must yield the SAME fingerprint the
        # loaded signer reports, so the account↔wallet binding is consistent
        # whether or not the wallet is unlocked.
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir))
            hot = WalletManager(storage=storage)
            hot.import_mnemonic(
                TEST_MNEMONIC, "enc", "testnet", password="pw-123-strong"
            )
            signer_fp = hot.fingerprint("enc")  # from the cached signer

            descriptor_fp = WalletManager(storage=storage).fingerprint("enc")
            assert descriptor_fp == signer_fp


class TestPeekAddress:
    """peek_address is the idempotent DISPLAY path (backs lw_address): it must
    never advance the counter, never write to disk, and never surface an index
    already committed to a swap/pool reserve.
    """

    def test_peek_is_idempotent(self, wallet_manager):
        a = wallet_manager.peek_address("default")
        b = wallet_manager.peek_address("default")
        assert a.address == b.address
        assert a.index == b.index

    def test_peek_does_not_advance_counter(self, wallet_manager):
        before = wallet_manager.storage.load_wallet("default").next_address_index
        wallet_manager.peek_address("default")
        wallet_manager.peek_address("default")
        after = wallet_manager.storage.load_wallet("default").next_address_index
        assert after == before

    def test_peek_frontier_is_never_below_committed(self, wallet_manager):
        # Reserve (commit) indices 0..4, advancing the counter to 5.
        reserved = wallet_manager.reserve_addresses("default", 5)
        committed = {a.index for a in reserved}
        counter = wallet_manager.storage.load_wallet("default").next_address_index
        peek = wallet_manager.peek_address("default")
        # Display must land ON the frontier, never on an already-committed index.
        assert peek.index >= counter
        assert peek.index not in committed

    def test_peek_explicit_index_does_not_advance(self, wallet_manager):
        before = wallet_manager.storage.load_wallet("default").next_address_index
        got = wallet_manager.peek_address("default", index=3)
        after = wallet_manager.storage.load_wallet("default").next_address_index
        assert got.index == 3
        assert after == before

    def test_peek_moves_forward_after_reserve(self, wallet_manager):
        # A pure display peek is idempotent, but once an address is COMMITTED via
        # reserve the frontier advances, so the next peek shows a new address —
        # display never re-shows an index handed to an external party.
        first = wallet_manager.peek_address("default")
        wallet_manager.reserve_addresses("default", 1)
        second = wallet_manager.peek_address("default")
        assert second.index > first.index

    def test_sync_scans_strictly_past_peek_frontier(self, wallet_manager, monkeypatch):
        # Fund-safety invariant: peek can display an address AT next_address_index
        # (the frontier). sync_wallet must scan STRICTLY past it, so a payment to
        # a freshly-displayed high-index address is discovered regardless of
        # whether lwk's full_scan_to_index bound is inclusive or exclusive.
        from unittest.mock import MagicMock

        wallet_manager.reserve_addresses("default", 5)  # counter -> 5
        counter = wallet_manager.storage.load_wallet("default").next_address_index
        peek = wallet_manager.peek_address("default")
        assert peek.index == counter  # frontier sits exactly at the scan boundary

        fake = MagicMock()
        fake.full_scan_to_index.return_value = None
        monkeypatch.setattr(wallet_manager, "_get_client", lambda network: fake)
        wallet_manager.sync_wallet("default")

        fake.full_scan_to_index.assert_called_once()
        scanned_to = fake.full_scan_to_index.call_args.args[1]
        assert scanned_to > peek.index
