#!/usr/bin/env python3
"""Real immutable book blobs: bounded proof warming and terminal refusals."""
from base64 import b64encode
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.web_vitrina_history_live_adapter import (
    LiveNativeAdapter, LiveSourceUnavailable, capture_initial_proofs,
)

DAY = '2026-10-28'


class BookCapture(LiveNativeAdapter):
    """Isolate the real book loader; simulate preceding completed source proofs."""
    def __init__(self, root, *, cap=5000, prior_bytes=0, prior_progress=False):
        super().__init__(db_path=root / 'unused.sqlite3', runtime_dir=root / 'runtime',
            cache_dir=root / 'proofs', now=datetime(2026, 10, 29, tzinfo=timezone.utc),
            date_from=DAY, date_to=DAY, formula_epoch='book-fixture', max_read_bytes=cap)
        self.cache = {'book': {}}
        self.book_path = root / 'book.sqlite3'
        self.prior_bytes, self.prior_progress = prior_bytes, prior_progress
        self.source_cached = False
        self.attempt_bytes = []
        self.payload_reads = []

    def capture(self):
        self.capture_calls += 1
        self.deadline = time.monotonic() + self.max_capture_seconds
        first = not self.source_cached
        self.stats = {'bytes': self.prior_bytes if first else 0, 'queries': 0,
                      'plans_loaded': int(first and self.prior_progress), 'book_blobs_loaded': 0}
        self.source_cached = self.source_cached or self.prior_progress
        self.used_book = set()
        with closing(sqlite3.connect(self.book_path.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            conn.set_trace_callback(lambda sql: self.payload_reads.append(sql)
                if sql.startswith('SELECT payload FROM accounting_blobs') else None)
            context = SimpleNamespace(borrow_book=lambda _: conn)
            bindings = [SimpleNamespace(date=DAY, source_key=('bundle', DAY))]
            nodes = {('bundle', DAY): {'book': {DAY: {'book_version': 'revision'}}}}
            result = {DAY: {}}
            try:
                self._book(context, nodes, bindings, self.cache, result, '2026-10-29')
                return result
            finally:
                self.attempt_bytes.append(self.stats['bytes'])
                # Match persistence across independent initial captures. No
                # source bodies or completed publication are stored here.
                self.cache = json.loads(json.dumps(self.cache))


class BookBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def adapter(self, payloads, **kwargs):
        adapter = BookCapture(self.root, **kwargs)
        names = ('shared_days', 'wb_days', 'retained_days', 'presentations')
        self.assertLessEqual(len(payloads), len(names))
        refs = [fingerprint(value) for value in payloads]
        with closing(sqlite3.connect(adapter.book_path)) as conn, conn:
            conn.executescript('''CREATE TABLE accounting_current(singleton,version);
                CREATE TABLE accounting_revisions(version,payload);
                CREATE TABLE accounting_blobs(digest PRIMARY KEY,payload);''')
            conn.execute('INSERT INTO accounting_current VALUES(1,?)', ('revision',))
            conn.execute('INSERT INTO accounting_revisions VALUES(?,?)', ('revision', json.dumps(
                {name: {DAY: ref} for name, ref in zip(names, refs)})))
            for ref, value in zip(refs, payloads):
                conn.execute('INSERT INTO accounting_blobs VALUES(?,?)',
                    (ref, zlib.compress(json.dumps(value).encode())))
        return adapter, refs

    def noise(self, size=2000, seed=1):
        return {'payload': b64encode(random.Random(seed).randbytes(size)).decode()}

    def test_native_capture_persists_book_warming_and_reuses_exact_vector(self):
        from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
        from apps.ready_publication_smoke import make_book
        from packages.application import fbs_accounting_runtime as accounting
        from packages.application.web_vitrina_history_store import _read, _atomic
        now = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
        server = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=1, now=now)
        with server:
            runtime = server.entrypoint.runtime
            book, expected = make_book(runtime.runtime_dir, opening=True, now=now)
            accounting.save(runtime.runtime_dir, book, expected=expected, operation_id='native-book')
            options = dict(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir, now=now,
                date_from='2026-04-20', date_to='2026-04-20', formula_epoch='native-book-fixture')
            adapter = LiveNativeAdapter(cache_dir=self.root / 'native-cache', **options)
            adapter.capture()
            cache_path = adapter.cache_dir / 'source-proofs.json'
            cache = _read(cache_path)
            for record in cache['book'].values():
                record['verified'] = []
            _atomic(cache_path, cache)
            # A real changed Ready revision must contribute a completed proof.
            with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
                row = conn.execute('SELECT bundle_version,as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots LIMIT 1').fetchone()
                plan = json.loads(row[2])
                plan.setdefault('metadata', {})['book_budget_fixture'] = 'changed'
                conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=?',
                    (json.dumps(plan), row[0], row[1]))
            reference = LiveNativeAdapter(cache_dir=self.root / 'native-reference', **options).capture()
            original = adapter._book
            calls = []
            def source_bytes_then_book(context, *args):
                calls.append(adapter.stats['plans_loaded'])
                if len(calls) == 1:
                    self.assertGreater(adapter.stats['plans_loaded'], 0)
                    # Consume actual query bytes in the same pinned capture,
                    # representing the source proofs preceding the book reader.
                    size = adapter.max_read_bytes - adapter.stats['bytes'] - 100
                    adapter._rows(context.borrow(runtime.db_path), 'SELECT ?', ('x' * size,))
                return original(context, *args)
            with patch.object(adapter, '_book', side_effect=source_bytes_then_book):
                vector, reads = capture_initial_proofs(adapter, time.monotonic() + 30)
            self.assertEqual(vector, reference)
            self.assertEqual(reads['captures'], 2)
            self.assertGreater(reads['book_blobs_loaded'], 0)
            self.assertEqual(calls, [1, 0])
            self.assertTrue(all(record['verified'] for record in _read(cache_path)['book'].values()))
            self.assertEqual(adapter.capture(), reference)
            self.assertEqual(adapter.stats['book_blobs_loaded'], 0)

    def test_cumulative_book_proofs_warm_and_equal_unbounded_vector(self):
        adapter, refs = self.adapter([self.noise(seed=n) for n in range(4)])
        vector, reads = capture_initial_proofs(adapter, time.monotonic() + 20)
        self.assertGreater(reads['captures'], 1)
        self.assertEqual(reads['book_blobs_loaded'], 4)
        self.assertEqual(set(adapter.cache['book']['revision']['verified']), set(refs))
        self.assertTrue(all(size <= adapter.max_read_bytes for size in adapter.attempt_bytes))
        self.assertEqual(len(adapter.payload_reads), 4)  # No repeated body reads.
        self.assertEqual(adapter.capture(), vector)
        self.assertEqual(adapter.stats['book_blobs_loaded'], 0)
        adapter.cache = {'book': {}}
        adapter.max_read_bytes = 100000
        self.assertEqual(adapter.capture(), vector)

    def test_prior_source_progress_allows_small_book_after_shared_budget(self):
        adapter, refs = self.adapter([self.noise()], prior_bytes=3400, prior_progress=True)
        vector, reads = capture_initial_proofs(adapter, time.monotonic() + 20)
        self.assertEqual(reads['captures'], 2)
        self.assertEqual(reads['book_blobs_loaded'], 1)
        self.assertEqual(len(adapter.payload_reads), 1)
        self.assertEqual(vector[DAY]['book'], ['revision', refs])

    def test_direct_fresh_capture_never_retries(self):
        adapter, _ = self.adapter([self.noise()], prior_bytes=3400, prior_progress=True)
        with self.assertRaisesRegex(LiveSourceUnavailable, '^live_book_bootstrap_pending$'):
            adapter.capture()
        self.assertEqual(adapter.capture_calls, 1)
        self.assertEqual(adapter.payload_reads, [])

    def test_no_progress_or_no_deadline_cannot_resume(self):
        adapter, _ = self.adapter([self.noise()], prior_bytes=3400)
        with self.assertRaisesRegex(LiveSourceUnavailable, '^book_source_resource_limit$'):
            capture_initial_proofs(adapter, time.monotonic() + 20)
        self.assertEqual(adapter.capture_calls, 1)
        adapter.prior_progress = True
        with self.assertRaisesRegex(LiveSourceUnavailable, '^live_book_bootstrap_pending$'):
            capture_initial_proofs(adapter)
        self.assertEqual(adapter.capture_calls, 2)
        self.assertEqual(adapter.payload_reads, [])

    def test_single_compressed_oversize_never_reads_or_retries_despite_progress(self):
        adapter, _ = self.adapter([self.noise(size=10000)], prior_progress=True)
        with self.assertRaisesRegex(LiveSourceUnavailable, '^book_source_resource_limit$'):
            capture_initial_proofs(adapter, time.monotonic() + 20)
        self.assertEqual(adapter.capture_calls, 1)
        self.assertEqual(adapter.payload_reads, [])
        self.assertEqual(adapter.cache['book']['revision']['verified'], [])

    def test_decompression_bomb_and_bad_fingerprint_never_become_progress(self):
        adapter, refs = self.adapter([{'payload': 'x' * 10000}], prior_progress=True)
        with self.assertRaisesRegex(LiveSourceUnavailable, '^book_blob_content_unknown$'):
            capture_initial_proofs(adapter, time.monotonic() + 20)
        self.assertEqual(adapter.capture_calls, 1)
        self.assertEqual(adapter.stats['book_blobs_loaded'], 0)
        with closing(sqlite3.connect(adapter.book_path)) as conn, conn:
            conn.execute('UPDATE accounting_blobs SET payload=?', (zlib.compress(b'{"other":true}'),))
        with self.assertRaisesRegex(LiveSourceUnavailable, '^book_blob_content_unknown$'):
            capture_initial_proofs(adapter, time.monotonic() + 20)
        self.assertEqual(adapter.capture_calls, 2)
        self.assertEqual(adapter.cache['book']['revision']['verified'], [])

    def test_capture_expiry_is_terminal_without_caching_verified_blob(self):
        adapter, _ = self.adapter([self.noise()], prior_progress=True)
        adapter.max_capture_seconds = 0
        with self.assertRaises(LiveSourceUnavailable) as error:
            capture_initial_proofs(adapter, time.monotonic() + 20)
        self.assertIn(str(error.exception), {'book_source_resource_limit', 'live_source_resource_limit'})
        self.assertEqual(adapter.capture_calls, 1)
        self.assertEqual(adapter.payload_reads, [])

    def test_post_fetch_length_mismatch_is_terminal_without_progress(self):
        adapter, _ = self.adapter([self.noise()])
        with closing(sqlite3.connect(adapter.book_path)) as conn:
            class ChangedLength:
                def execute(self, sql, args=()):
                    if sql.startswith('SELECT length(payload)'):
                        return SimpleNamespace(fetchone=lambda: (1,))
                    return conn.execute(sql, args)
            adapter.stats = {'bytes': 0, 'queries': 0, 'plans_loaded': 1}
            adapter.used_book = set()
            adapter.deadline = time.monotonic() + 20
            with self.assertRaisesRegex(LiveSourceUnavailable, '^book_source_resource_limit$'):
                adapter._book(SimpleNamespace(borrow_book=lambda _: ChangedLength()),
                    {('bundle', DAY): {'book': {DAY: {'book_version': 'revision'}}}},
                    [SimpleNamespace(date=DAY, source_key=('bundle', DAY))],
                    adapter.cache, {DAY: {}}, '2026-10-29')
        self.assertEqual(adapter.cache['book']['revision']['verified'], [])
        self.assertEqual(adapter.stats.get('book_blobs_loaded', 0), 0)

    def test_safe_diagnostics_for_standalone_and_owned_worker(self):
        from apps import web_vitrina_history_candidate_build as command
        from packages.application.owned_history_worker_capability import history_child_failure
        from apps.web_vitrina_finished_snapshot_build import bounded_worker
        error = LiveSourceUnavailable('live_book_bootstrap_pending:private-source-data')
        standalone = command.source_failure_result(error,
            {'bytes': 42, 'book_blobs_loaded': 3, 'private': 'source data'})
        self.assertEqual(standalone['reason_code'], 'live_book_bootstrap_pending')
        self.assertEqual(standalone['source_reads'], {'bytes': 42, 'book_blobs_loaded': 3})
        owned = history_child_failure(error, mode='capture', source_error=True)
        self.assertEqual(owned['diagnostic']['reason_code'], 'live_book_bootstrap_pending')
        self.assertNotIn('private', json.dumps([owned, standalone]))
        script = 'import sys; print(' + repr(json.dumps(standalone)) + '); sys.exit(3)'
        received = bounded_worker([sys.executable, '-c', script], 2,
            allowed_failure_reasons=command.SAFE_SOURCE_FAILURE_REASONS)
        self.assertEqual(received['reason_code'], 'live_book_bootstrap_pending')
        self.assertEqual(received['source_reads']['book_blobs_loaded'], 3)

    def test_deadline_exhaustion_stops_between_warming_passes(self):
        adapter, _ = self.adapter([self.noise()], prior_bytes=3400, prior_progress=True)
        real_capture = adapter.capture
        def expired_after_progress():
            try:
                return real_capture()
            finally:
                time.sleep(0.02)
        with patch.object(adapter, 'capture', side_effect=expired_after_progress):
            with self.assertRaisesRegex(LiveSourceUnavailable, '^live_bootstrap_deadline$'):
                capture_initial_proofs(adapter, time.monotonic() + 0.01)
        self.assertEqual(adapter.capture_calls, 1)


if __name__ == '__main__':
    unittest.main()
