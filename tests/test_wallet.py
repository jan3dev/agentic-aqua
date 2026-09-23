"""Tests for WalletManager's persistent address counter + fingerprint.

These back the LN-address feature: ``reserve_addresses`` mints unused Liquid
receive addresses for the JAN3 LN-address pool, and ``fingerprint`` binds the
account to a wallet. The no-arg ``get_address`` was changed to advance a
persisted counter so off-chain handouts never reuse an index.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import lwk
import pytest

from aqua.storage import Storage
from aqua.wallet import (
    LIQUID_BACKEND_URLS,
    OWN_ESPLORA_CONCURRENCY,
    PUBLIC_ESPLORA_CONCURRENCY,
    WalletManager,
    _esplora_concurrency,
)
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

    def test_counter_promoted_when_lwk_tip_is_higher(self, wallet_manager):
        """The handout frontier is max(lwk tip, counter): a handed-out index is
        never below the persisted counter, and the counter advances past it."""
        record = wallet_manager.storage.load_wallet("default")
        record.next_address_index = 100
        wallet_manager.storage.save_wallet(record)
        out = wallet_manager.get_address("default")
        assert out.index >= 100
        after = wallet_manager.storage.load_wallet("default").next_address_index
        assert after == out.index + 1

    def test_reserve_waits_for_wallet_lock(self, wallet_manager):
        # reserve_addresses must hold the cross-process wallet lock for its
        # read-modify-write: while another holder has it, the reserve blocks.
        import threading
        import time

        got: list[int] = []

        def reserve():
            got.append(wallet_manager.get_address("default").index)

        with wallet_manager.storage.wallet_lock("default"):
            t = threading.Thread(target=reserve)
            t.start()
            time.sleep(0.3)
            assert not got  # blocked while the lock is held elsewhere
        t.join(timeout=5)
        assert got  # proceeded once the lock was released

    def test_ensure_counter_covers_repairs_reset_counter(self, wallet_manager):
        # Simulate a seed reimport: server pool holds indices 5..7 but the
        # local counter is 0; reconciliation must bump it past the highest.
        pool = [
            wallet_manager.peek_address("default", index=i).address
            for i in (5, 6, 7)
        ]
        assert wallet_manager.storage.load_wallet("default").next_address_index == 0
        new_counter = wallet_manager.ensure_counter_covers("default", pool)
        assert new_counter == 8
        rec = wallet_manager.storage.load_wallet("default")
        assert rec.next_address_index == 8
        # The next handout must sit above the recovered pool.
        assert wallet_manager.get_address("default").index >= 8

    def test_ensure_counter_covers_is_idempotent_and_never_lowers(
        self, wallet_manager
    ):
        wallet_manager.reserve_addresses("default", 10)
        pool = [wallet_manager.peek_address("default", index=2).address]
        counter = wallet_manager.ensure_counter_covers("default", pool)
        assert counter == 10  # already covered — unchanged

    def test_ensure_counter_covers_ignores_foreign_addresses(self, wallet_manager):
        counter = wallet_manager.ensure_counter_covers(
            "default", ["lq1qqnotfromthiswallet"]
        )
        assert counter == 0

    def test_concurrent_reserves_never_share_an_index(self, wallet_manager):
        # Two racing reservers must produce disjoint index ranges and a
        # counter equal to the total handed out.
        import threading

        results: list[list[int]] = [[], []]

        def reserve(slot):
            results[slot] = [
                a.index for a in wallet_manager.reserve_addresses("default", 5)
            ]

        threads = [
            threading.Thread(target=reserve, args=(i,)) for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        all_indices = results[0] + results[1]
        assert len(set(all_indices)) == 10  # no shared index
        rec = wallet_manager.storage.load_wallet("default")
        assert rec.next_address_index == max(all_indices) + 1


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
        monkeypatch.setattr(wallet_manager, "_get_client", lambda network, url: fake)
        wallet_manager.sync_wallet("default")

        fake.full_scan_to_index.assert_called_once()
        scanned_to = fake.full_scan_to_index.call_args.args[1]
        assert scanned_to > peek.index


class TestBackendFallback:
    """_with_client_fallback walks LIQUID_BACKEND_URLS, building each client
    lazily and only stepping to the next backend on transient network errors.
    Mirrors tests/test_bitcoin.py::TestEsploraFallback.
    """

    @staticmethod
    def _stub_clients(wallet_manager, monkeypatch, clients):
        """Map the network's backend URLs onto ``clients``, in order.

        Patching _get_client keeps every test free of network I/O; a client
        given as an Exception instance simulates a failing constructor.
        """
        urls = wallet_manager._backend_urls("testnet")
        assert len(clients) <= len(urls), "more stubs than configured backends"
        by_url = dict(zip(urls, clients))

        def fake_get_client(network, url):
            client = by_url[url]
            if isinstance(client, Exception):
                raise client
            return client

        monkeypatch.setattr(wallet_manager, "_get_client", fake_get_client)
        return urls

    def test_fallback_uses_second_when_first_raises_transient(
        self, wallet_manager, monkeypatch
    ):
        c1 = MagicMock()
        c1.full_scan.side_effect = Exception("connection reset by peer")
        c2 = MagicMock()
        c2.full_scan.return_value = "ok"
        self._stub_clients(wallet_manager, monkeypatch, [c1, c2])

        result = wallet_manager._with_client_fallback(
            "testnet", lambda c: c.full_scan("wollet")
        )

        assert result == "ok"
        c1.full_scan.assert_called_once()
        c2.full_scan.assert_called_once()

    def test_non_transient_error_is_reraised_without_fallback(
        self, wallet_manager, monkeypatch
    ):
        """A rejected request would fail identically on every backend, so it
        surfaces as-is (CLAUDE.md "No silent fallbacks")."""
        c1 = MagicMock()
        c1.broadcast.side_effect = ValueError("bad-txns-inputs-missingorspent")
        c2 = MagicMock()
        self._stub_clients(wallet_manager, monkeypatch, [c1, c2])

        with pytest.raises(ValueError, match="bad-txns-inputs-missingorspent"):
            wallet_manager._with_client_fallback("testnet", lambda c: c.broadcast("tx"))

        c1.broadcast.assert_called_once()
        c2.broadcast.assert_not_called()

    def test_all_backends_transient_raises_last_error(
        self, wallet_manager, monkeypatch
    ):
        clients = []
        for i in range(len(wallet_manager._backend_urls("testnet"))):
            c = MagicMock()
            c.full_scan.side_effect = Exception(f"timed out on backend {i}")
            clients.append(c)
        self._stub_clients(wallet_manager, monkeypatch, clients)

        with pytest.raises(Exception, match=f"backend {len(clients) - 1}"):
            wallet_manager._with_client_fallback("testnet", lambda c: c.full_scan("w"))

        for c in clients:
            c.full_scan.assert_called_once()

    def test_json_parse_error_counts_as_transient(self, wallet_manager, monkeypatch):
        """A backend answering 200 with an HTML error page makes lwk fail inside
        serde_json; that is the backend being down, not a bad request."""
        c1 = MagicMock()
        c1.full_scan.side_effect = lwk.LwkError.Generic(
            'JsonFrom(Error("expected value", line: 1, column: 1))'
        )
        c2 = MagicMock()
        c2.full_scan.return_value = "ok"
        self._stub_clients(wallet_manager, monkeypatch, [c1, c2])

        assert (
            wallet_manager._with_client_fallback("testnet", lambda c: c.full_scan("w"))
            == "ok"
        )
        c2.full_scan.assert_called_once()

    def test_unexpected_value_is_not_transient(self, wallet_manager, monkeypatch):
        """Guards the 'expected value' marker: lwk's own "returned an unexpected
        value for call" must not be mistaken for a serde_json parse failure."""
        c1 = MagicMock()
        c1.full_scan.side_effect = lwk.LwkError.Generic(
            "Elements RPC returned an unexpected value for call getblock"
        )
        c2 = MagicMock()
        self._stub_clients(wallet_manager, monkeypatch, [c1, c2])

        with pytest.raises(Exception, match="unexpected value"):
            wallet_manager._with_client_fallback("testnet", lambda c: c.full_scan("w"))
        c2.full_scan.assert_not_called()

    def test_http_5xx_counts_as_transient(self, wallet_manager, monkeypatch):
        """lwk reports HTTP status structurally on EsploraHttpError, not in text."""
        c1 = MagicMock()
        c1.full_scan.side_effect = lwk.LwkError.EsploraHttpError(
            "https://example.invalid/blocks/tip/hash", 503, "<h1>503</h1>"
        )
        c2 = MagicMock()
        c2.full_scan.return_value = "ok"
        self._stub_clients(wallet_manager, monkeypatch, [c1, c2])

        assert (
            wallet_manager._with_client_fallback("testnet", lambda c: c.full_scan("w"))
            == "ok"
        )
        c2.full_scan.assert_called_once()

    def test_electrum_dns_failure_counts_as_transient(
        self, wallet_manager, monkeypatch
    ):
        c1 = MagicMock()
        c1.full_scan.side_effect = lwk.LwkError.Generic(
            'ClientError(IOError(Custom { kind: Uncategorized, error: "failed to '
            'lookup address information: nodename nor servname provided, or not '
            'known" }))'
        )
        c2 = MagicMock()
        c2.full_scan.return_value = "ok"
        self._stub_clients(wallet_manager, monkeypatch, [c1, c2])

        assert (
            wallet_manager._with_client_fallback("testnet", lambda c: c.full_scan("w"))
            == "ok"
        )

    def test_failing_client_constructor_does_not_stop_next_backend(
        self, wallet_manager, monkeypatch
    ):
        """lwk.ElectrumClient connects in its constructor, so a dead entry blows
        up at build time. That must not mask the remaining backends."""
        c2 = MagicMock()
        c2.full_scan.return_value = "ok"
        self._stub_clients(
            wallet_manager, monkeypatch, [RuntimeError("cannot connect"), c2]
        )

        assert (
            wallet_manager._with_client_fallback("testnet", lambda c: c.full_scan("w"))
            == "ok"
        )
        c2.full_scan.assert_called_once()

    def test_backend_urls_rejects_unknown_network(self, wallet_manager):
        with pytest.raises(ValueError, match="Unknown network"):
            wallet_manager._backend_urls("regtest")

    def test_backend_urls_returns_a_copy(self, wallet_manager):
        urls = wallet_manager._backend_urls("mainnet")
        urls.append("https://attacker.invalid/api")
        assert "https://attacker.invalid/api" not in LIQUID_BACKEND_URLS["mainnet"]

    def test_backend_url_override_disables_fallback(self, wallet_manager):
        config = wallet_manager.storage.load_config()
        config.electrum_url = "ssl://electrum.example:50002"
        wallet_manager.storage.save_config(config)
        assert wallet_manager._backend_urls("mainnet") == [
            "ssl://electrum.example:50002"
        ]

    def test_own_backend_keeps_high_concurrency_public_does_not(self):
        assert (
            _esplora_concurrency("https://airavata.aquabtc.com/liquid/api")
            == OWN_ESPLORA_CONCURRENCY
        )
        for url in ("https://blockstream.info/liquid/api", "https://liquid.network/api"):
            assert _esplora_concurrency(url) == PUBLIC_ESPLORA_CONCURRENCY


class TestBroadcastIdempotency:
    """Broadcast is retried across backends, so a node that already has the tx
    must read as success — the tx is live. Every other rejection still raises.
    """

    @staticmethod
    def _fake_tx(txid="ab" * 32):
        tx = MagicMock()
        tx.txid.return_value = txid
        return tx

    def test_already_in_block_chain_on_fallback_returns_txid(
        self, wallet_manager, monkeypatch
    ):
        tx = self._fake_tx()
        c1 = MagicMock()
        c1.broadcast.side_effect = Exception("error sending request")
        c2 = MagicMock()
        # Real wording seen from Esplora POST /tx against a Liquid node.
        c2.broadcast.side_effect = lwk.LwkError.EsploraHttpError(
            "https://example.invalid/tx",
            400,
            "sendrawtransaction RPC error -27: Transaction already in block chain",
        )
        TestBackendFallback._stub_clients(wallet_manager, monkeypatch, [c1, c2])

        assert wallet_manager._broadcast("testnet", tx) == "ab" * 32

    def test_already_known_on_first_backend_returns_txid(
        self, wallet_manager, monkeypatch
    ):
        tx = self._fake_tx("cd" * 32)
        c1 = MagicMock()
        c1.broadcast.side_effect = Exception(
            "sendrawtransaction RPC error -26: txn-already-in-mempool"
        )
        c2 = MagicMock()
        TestBackendFallback._stub_clients(wallet_manager, monkeypatch, [c1, c2])

        assert wallet_manager._broadcast("testnet", tx) == "cd" * 32
        c2.broadcast.assert_not_called()

    def test_other_rejection_still_raises(self, wallet_manager, monkeypatch):
        tx = self._fake_tx()
        c1 = MagicMock()
        c1.broadcast.side_effect = lwk.LwkError.EsploraHttpError(
            "https://example.invalid/tx",
            400,
            "sendrawtransaction RPC error -26: bad-txns-in-belowout",
        )
        c2 = MagicMock()
        TestBackendFallback._stub_clients(wallet_manager, monkeypatch, [c1, c2])

        with pytest.raises(Exception, match="bad-txns-in-belowout"):
            wallet_manager._broadcast("testnet", tx)
        c2.broadcast.assert_not_called()

    def test_successful_broadcast_returns_backend_txid(
        self, wallet_manager, monkeypatch
    ):
        tx = self._fake_tx()
        c1 = MagicMock()
        c1.broadcast.return_value = "ef" * 32
        TestBackendFallback._stub_clients(wallet_manager, monkeypatch, [c1])

        assert wallet_manager._broadcast("testnet", tx) == "ef" * 32
        tx.txid.assert_not_called()
