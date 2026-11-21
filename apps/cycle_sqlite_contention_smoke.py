"""Real DELETE locks at cycle read, supplier BEGIN and receipt COMMIT boundaries."""
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import gc
import sqlite3
import sys
import threading
from unittest.mock import patch
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application import ff_stock_ledger as ledger
from packages.application import operator_warehouse_documents as warehouse
from packages.application import operator_supplier_processing as processing
from packages.application import operator_supplier_history as history
from packages.application import operator_supplier_shipments as source
from packages.application.sqlite_contention import connect_sqlite, SQLiteContentionExhausted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from apps.operator_supplier_history_smoke import PendingTransactionTests, MOMENT
from apps.operator_supplier_processing_cohort_smoke import finance_fixture, _reconcile


@contextmanager
def contention(runtime, module, *, lock, release_after=None, observers=(), on_wait=None):
    """A different physical connection holds the lock until the target phase."""
    ready, attempting, stop = threading.Event(), threading.Event(), threading.Event()
    errors, statements = [], []

    def holder():
        try:
            with closing(sqlite3.connect(runtime.db_path)) as conn:
                assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
                if lock == 'reader':
                    conn.execute('PRAGMA query_only=ON')
                    conn.execute('BEGIN')
                    conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
                else:
                    conn.execute('BEGIN ' + ('EXCLUSIVE' if lock == 'exclusive' else 'IMMEDIATE'))
                ready.set()
                if release_after is not None:
                    assert attempting.wait(10), 'target boundary not reached'
                    stop.wait(release_after)
                    if on_wait is not None:
                        on_wait()
                        stop.wait(.35)
                else:
                    stop.wait(10)
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    def connected(*args, **kwargs):
        for observer in observers:
            try:
                assert not observer.in_transaction, 'writer retains own pinned proof reader'
            except sqlite3.ProgrammingError:
                pass  # Closed by its preparation/ExitStack.
        conn = connect_sqlite(*args, **kwargs, timeout_ms=4000 if on_wait is not None else (2000 if release_after is not None else 350))
        def trace(sql):
            statements.append(sql.strip())
            if sql == ('COMMIT' if lock == 'reader' else 'BEGIN IMMEDIATE'):
                attempting.set()
        conn.set_trace_callback(trace)
        if lock == 'exclusive':
            attempting.set()  # SQLite may block while preparing the first SELECT.
        return conn

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert ready.wait(5) and not errors, errors
        with patch.object(module, 'connect_sqlite', connected):
            yield statements
    finally:
        stop.set()
        thread.join(12)
        assert not thread.is_alive() and not errors, errors


def read_cases():
    with TemporaryDirectory(prefix='cycle-read-lock-') as raw:
        runtime = SimpleNamespace(db_path=Path(raw)/'op.sqlite3', runtime_dir=Path(raw))
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute('CREATE TABLE '+warehouse.TABLE+'(request_id TEXT,state TEXT)')
            conn.execute('CREATE TABLE sheet_vitrina_v1_ff_stock_operations(operation_id TEXT,source_key TEXT,source_object_id TEXT,created_at TEXT)')
        block = object.__new__(ledger.FfStockLedgerBlock)
        block.runtime = runtime
        def warehouse_read():
            from packages.application import fbs_accounting_runtime as accounting
            with patch.object(accounting, 'load', return_value=(None, '')), patch.object(accounting, 'current_publication_receipt', return_value=None), warehouse_functional_job_lock(runtime.runtime_dir):
                result = warehouse.reconcile(runtime, request_ids=[], finance_receipt={})
                assert result['processed_count'] == 0, result
        for module, read in ((warehouse, warehouse_read), (ledger, lambda: block._legacy_orphan_supply_records(excluded_supply_ids=set()))):
            with contention(runtime, module, lock='exclusive', release_after=.65) as statements:
                read()
            assert any(sql.startswith('SELECT') for sql in statements), statements
            with contention(runtime, module, lock='exclusive'):
                try:
                    read()
                except SQLiteContentionExhausted as exc:
                    assert exc.phase == 'read_statement', exc.phase
                else:
                    raise AssertionError('permanent exclusive lock became success')
        with closing(warehouse.readonly(runtime.db_path)) as conn:
            assert conn.in_transaction and conn.execute('PRAGMA query_only').fetchone()[0] == 1
            try:
                conn.execute('DELETE FROM '+warehouse.TABLE)
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError('warehouse reader accepted a write')
    print('cycle reads: real exclusive lock recovery/exhaustion, query-only snapshot preserved: OK')


def history_cases():
    case = PendingTransactionTests()
    try:
        for lock in ('writer', 'reader'):
            runtime, identity, _ = case.fixture()
            original = case.image(runtime)
            with contention(runtime, history, lock=lock, release_after=.65) as statements:
                assert history.pending(runtime, now=MOMENT) is None
            assert case.attempts(runtime) == [(identity, 'processing', 'supplier_history_native_source_pending', MOMENT.isoformat())]
            assert case.image(runtime) == original
            if lock == 'reader':
                assert statements.count('COMMIT') > 1, statements
                assert not any(sql.startswith('INSERT') for sql in statements[statements.index('COMMIT')+1:]), statements
            with contention(runtime, history, lock=lock):
                try:
                    history.pending(runtime, now=MOMENT)
                except SQLiteContentionExhausted as exc:
                    assert exc.phase == ('begin' if lock == 'writer' else 'commit')
                else:
                    raise AssertionError('permanent history lock became success')
            assert case.image(runtime) == original
            assert case.attempts(runtime) == [(identity, 'processing', 'supplier_history_native_source_pending', MOMENT.isoformat())]
    finally:
        case.doCleanups()
    print('supplier pending: BEGIN/COMMIT recovery, no DML replay, permanent contention rolls back: OK')


def processing_cases():
    from packages.application.operator_supplier_cost_proof import FinanceProofCohort
    for lock, permanent in (('reader', False), ('reader', True), ('writer', False), ('writer', True)):
        with TemporaryDirectory(prefix='supplier-completion-lock-') as raw:
            runtime, identities, _, _ = finance_fixture(raw)
            gc.collect()
            with closing(sqlite3.connect(runtime.db_path)) as conn:
                assert conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
            observers = []
            native_readonly, finance_open = source.readonly, FinanceProofCohort._open
            def readonly(path):
                conn = native_readonly(path)
                observers.append(conn)
                return conn
            def opened(cohort):
                result = finance_open(cohort)
                observers.append(cohort.conn)
                return result
            with patch.object(source, 'readonly', readonly), patch.object(FinanceProofCohort, '_open', opened):
                with contention(runtime, processing, lock=lock, release_after=None if permanent else .65, observers=observers) as statements:
                    if permanent:
                        try:
                            _reconcile(runtime)
                        except SQLiteContentionExhausted as exc:
                            assert exc.phase == ('commit' if lock == 'reader' else 'begin')
                        else:
                            raise AssertionError('permanent supplier commit became success')
                    else:
                        result = _reconcile(runtime)
                        assert all(value['complete'] for value in result['operations']), result
                        assert statements.count('COMMIT' if lock == 'reader' else 'BEGIN IMMEDIATE') > 1
                        assert not any(sql.startswith('INSERT') for sql in statements[statements.index('COMMIT')+1:]), statements
            with closing(source.readonly(runtime.db_path)) as conn:
                assert conn.execute('SELECT count(*) FROM '+processing.COMPLETIONS).fetchone()[0] == (0 if permanent else len(identities))
                assert conn.execute('SELECT count(*) FROM '+processing.ATTEMPTS).fetchone()[0] == 0
            if permanent:
                assert all(value['complete'] for value in _reconcile(runtime)['operations'])
    # A diagnostic fallback must close every observer before its own COMMIT.
    with TemporaryDirectory(prefix='supplier-fallback-reader-') as raw:
        runtime, identities, _, _ = finance_fixture(raw)
        gc.collect()
        with closing(sqlite3.connect(runtime.db_path)) as conn:
            assert conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
        original = processing._check_independent_handoff
        retained = []
        def fail(runtime, handoff):
            if not retained:
                observer = handoff[0][0]
                observer.execute('BEGIN')
                observer.execute('SELECT count(*) FROM sqlite_master').fetchone()
                retained.append(observer)
                raise ValueError('supplier_completion_handoff_changed')
            return original(runtime, handoff)
        with patch.object(processing, '_check_independent_handoff', fail):
            result = _reconcile(runtime)
        assert retained and not any(value['complete'] for value in result['operations']), result
        try:
            retained[0].execute('SELECT 1')
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError('fallback retained proof observer')
        with closing(source.readonly(runtime.db_path)) as conn:
            assert conn.execute('SELECT count(*) FROM '+processing.COMPLETIONS).fetchone()[0] == 0
            assert conn.execute('SELECT count(*) FROM '+processing.ATTEMPTS).fetchone()[0] == len(identities)
        assert all(value['complete'] for value in _reconcile(runtime)['operations'])
    print('supplier completion: released own readers, transient BEGIN/COMMIT only, bounded atomic rollback, fallback observer cleanup: OK')


def guarded_commit_restoration():
    for outcome in ('success', 'exhaustion', 'guard_error'):
        with TemporaryDirectory(prefix='guarded-commit-timeout-') as raw:
            runtime = SimpleNamespace(db_path=Path(raw)/'op.sqlite3')
            with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
                conn.execute('CREATE TABLE counter(value INTEGER)')
                conn.execute('INSERT INTO counter VALUES(0)')
            with contention(runtime, processing, lock='reader', release_after=.35 if outcome == 'success' else None):
                with closing(processing.connect_sqlite(runtime.db_path)) as conn:
                    conn.execute('PRAGMA busy_timeout=1379')
                    conn.execute('UPDATE counter SET value=value+1')
                    guards = []
                    def guard():
                        guards.append(conn.execute('PRAGMA busy_timeout').fetchone()[0])
                        assert guards[-1] == 0, guards
                        if outcome == 'guard_error' and len(guards) == 2:
                            raise ValueError('independent_source_changed')
                    try:
                        conn.commit_with_guard(guard)
                    except SQLiteContentionExhausted:
                        assert outcome == 'exhaustion'
                    except ValueError as exc:
                        assert outcome == 'guard_error' and str(exc) == 'independent_source_changed'
                    else:
                        assert outcome == 'success'
                    assert len(guards) > 1, guards
                    assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 1379
                    assert conn.in_transaction == (outcome != 'success')
            with closing(sqlite3.connect(runtime.db_path)) as conn:
                assert conn.execute('SELECT value FROM counter').fetchone()[0] == (1 if outcome == 'success' else 0)
    print('guarded COMMIT restores exact native timeout on success/exhaustion/guard refusal; no prefix commit: OK')


def commit_drift_cases():
    from packages.application import fbs_accounting_runtime as accounting
    for store in ('raw', 'book'):
        for single in (False, True):
            with TemporaryDirectory(prefix='supplier-guarded-'+store+'-') as raw:
                runtime, identities, _, raw_path = finance_fixture(raw, split=True)
                gc.collect()
                with closing(sqlite3.connect(runtime.db_path)) as conn:
                    assert conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
                targets = identities[:1] if single else identities
                changed, validations, guard_refusals = [], [], []
                drift_committed = threading.Event()
                validate, check = processing._validate_completion, processing._check_independent_handoff
                def counted_validate(*args):
                    validations.append(args[1])
                    return validate(*args)
                def counted_guard(*args):
                    try:
                        return check(*args)
                    except ValueError as exc:
                        if str(exc) == 'supplier_completion_handoff_changed':
                            # SQLite can expose the commit before the holder's
                            # Python context exit/notification has completed.
                            # Observe the native refusal first, then synchronize
                            # its bookkeeping; never serialize the actual check.
                            assert drift_committed.wait(5), 'guard refused before the independent commit'
                            guard_refusals.append(statements.count('COMMIT'))
                        raise
                def drift():
                    path = raw_path if store == 'raw' else accounting.path(runtime.runtime_dir)
                    with closing(sqlite3.connect(path)) as external, external:
                        if store == 'raw':
                            assert external.execute("UPDATE finance_raw_current_rows SET raw_json=json_set(raw_json,'$.retailPriceWithDisc','333')").rowcount > 0
                        else:
                            assert external.execute("UPDATE accounting_revisions SET payload=payload || ' '").rowcount > 0
                    changed.append(store)
                    drift_committed.set()
                started = time.monotonic()
                with patch.object(processing, 'captured_cohort', return_value=targets), patch.object(processing, 'COMPLETION_COHORT_MAX_BYTES', 1 if single else 8*1024**2), patch.object(processing, '_validate_completion', counted_validate), patch.object(processing, '_check_independent_handoff', counted_guard):
                    with contention(runtime, processing, lock='reader', release_after=2.2, on_wait=drift) as statements:
                        result = _reconcile(runtime)
                assert changed and drift_committed.is_set() and time.monotonic()-started >= 2.2
                assert validations == targets, validations
                assert statements.count('COMMIT') > 1
                assert guard_refusals and all(attempts > 0 for attempts in guard_refusals), guard_refusals
                assert result['status'] == 'pending' and not any(value['complete'] for value in result['operations']), result
                with closing(source.readonly(runtime.db_path)) as conn:
                    assert conn.execute('SELECT count(*) FROM '+processing.COMPLETIONS).fetchone()[0] == 0
                    attempts = list(conn.execute('SELECT operation_id,reason FROM '+processing.ATTEMPTS))
                    assert set(row[0] for row in attempts) == set(targets)
                    assert all(row[1] == 'supplier_completion_handoff_changed' for row in attempts), attempts
                print('guarded COMMIT rejects '+store+' drift after 2.2s blocked COMMIT; '+('single' if single else 'batch')+' path; all completions rolled back: OK')


if __name__ == '__main__':
    read_cases()
    history_cases()
    processing_cases()
    guarded_commit_restoration()
    commit_drift_cases()
