"""Real DELETE-journal readers versus fulfillment receipt/status COMMIT.

The holder is a separate read-only connection. This reproduces the lock class,
not the identity of a reader in a historical production incident.
"""
from contextlib import closing, contextmanager
from pathlib import Path
import gc
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.fulfillment_recalc_intents_smoke import fixture
from apps.operator_fulfillment_services_native_smoke import (
    seed, publish_functional, publish_accounting, MOMENT,
)
from apps.sheet_vitrina_v1_fulfillment_services_smoke import _build_workbook, _valid_row
from packages.application import operator_fulfillment_services as receipt
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.sqlite_contention import connect_sqlite, SQLiteContentionExhausted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock


@contextmanager
def blocked_commit(runtime, *, release_after=None, timeout_ms=None, module=receipt):
    """Release a pinned reader only after the writer actually enters COMMIT."""
    ready = threading.Event()
    committing = threading.Event()
    stop = threading.Event()
    failures = []
    statements = []
    observers = []
    original_readonly = module.readonly

    def reader():
        try:
            with closing(original_readonly(runtime.db_path)) as conn:
                conn.execute('SELECT count(*) FROM ' + module.TABLE).fetchone()
                ready.set()
                if release_after is not None:
                    assert committing.wait(10), 'writer never entered COMMIT'
                    stop.wait(release_after)
                else:
                    stop.wait(10)
        except BaseException as exc:
            failures.append(exc)
            ready.set()

    def track_readonly(path):
        conn = original_readonly(path)
        observers.append(conn)
        return conn

    def writer(path):
        # The exception path must close all proof/list observers before writing.
        # Successful proof handoff can retain connections, but no read transaction.
        for observer in observers:
            try:
                assert not observer.in_transaction, 'own proof reader still pinned'
            except sqlite3.ProgrammingError:
                pass  # Closed by ExitStack/closing, as required.
        conn = connect_sqlite(path, timeout_ms=timeout_ms)
        def trace(sql):
            statements.append(sql)
            if sql == 'COMMIT':
                committing.set()
        conn.set_trace_callback(trace)
        return conn

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert ready.wait(5) and not failures, failures
        with patch.object(module, 'readonly', track_readonly), patch.object(module, 'connect_sqlite', writer):
            yield statements
    finally:
        stop.set()
        thread.join(12)
        assert not thread.is_alive() and not failures, failures


def reconcile(runtime):
    with heavy_admitted(runtime.runtime_dir, operation='fulfillment-contention-fixture'), warehouse_functional_job_lock(runtime.runtime_dir):
        return receipt.reconcile(runtime, now=MOMENT)


def values(runtime, table):
    with closing(receipt.readonly(runtime.db_path)) as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM ' + table)]


def assert_single_write(statements, table):
    assert len([sql for sql in statements if sql.startswith('INSERT') and table in sql]) == 1, statements
    assert statements.count('COMMIT') > 1, 'fixture did not exercise COMMIT retry'


def test_pending_reader_release():
    with TemporaryDirectory() as raw:
        runtime, block = fixture(raw)
        operation = block.upload_xlsx(_build_workbook([_valid_row('1001')]))['acceptance']['operation_id']
        with closing(sqlite3.connect(runtime.db_path)) as conn:
            assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        with blocked_commit(runtime, release_after=2.3) as statements:
            result = reconcile(runtime)
        assert result == {'status': 'pending', 'operations': [{
            'operation_id': operation, 'state': 'pending',
            'reason_code': 'fulfillment_functional_publication_pending'}]}, result
        assert_single_write(statements, receipt.ATTEMPTS)
        assert values(runtime, receipt.COMPLETIONS) == []
        attempts = values(runtime, receipt.ATTEMPTS)
        assert len(attempts) == 1 and attempts[0]['state'] == 'processing'
        assert attempts[0]['reason_code'] == 'fulfillment_functional_publication_pending'


def test_pending_exhaustion_rolls_back():
    with TemporaryDirectory() as raw:
        runtime, block = fixture(raw)
        block.upload_xlsx(_build_workbook([_valid_row('1001')]))
        with blocked_commit(runtime, timeout_ms=350):
            try:
                reconcile(runtime)
            except SQLiteContentionExhausted as exc:
                assert exc.phase == 'commit' and exc.retries > 0
            else:
                raise AssertionError('persistent reader must not become success')
        assert values(runtime, receipt.ATTEMPTS) == []
        assert values(runtime, receipt.COMPLETIONS) == []
        assert reconcile(runtime)['status'] == 'pending'
        assert len(values(runtime, receipt.ATTEMPTS)) == 1


def test_completed_reader_release():
    with TemporaryDirectory() as raw:
        runtime, block = seed(raw)
        # Native fixture uses WAL for unrelated observer race tests. This test
        # deliberately restores production's DELETE journal before publication.
        gc.collect()  # Release connection cycles left by the shared WAL fixture.
        with closing(sqlite3.connect(runtime.db_path)) as conn:
            assert conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
        operation = block.upload_xlsx(_build_workbook([_valid_row('1001')]))['acceptance']['operation_id']
        publish_functional(runtime)
        publish_accounting(runtime, opening=True)
        WbFinanceWeeklyBlock(runtime.runtime_dir, seller_id='canonical', now_factory=lambda: MOMENT).ensure_schema()
        with blocked_commit(runtime, release_after=2.3) as statements:
            result = reconcile(runtime)
        assert result['status'] == 'ok', result
        assert_single_write(statements, receipt.COMPLETIONS)
        completed = values(runtime, receipt.COMPLETIONS)
        assert len(completed) == 1 and completed[0]['operation_id'] == operation
        assert values(runtime, receipt.ATTEMPTS) == []
        assert receipt.read_acceptance(runtime.db_path, operation)['state'] == 'completed'
        assert reconcile(runtime) == {'status': 'ok', 'operations': []}
        assert values(runtime, receipt.COMPLETIONS) == completed


if __name__ == '__main__':
    test_pending_reader_release()
    test_pending_exhaustion_rolls_back()
    test_completed_reader_release()
    print('operator_fulfillment_services_contention_smoke: OK; DELETE reader release, bounded failure, atomic pending/completion, no replay')
