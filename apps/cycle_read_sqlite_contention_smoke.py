"""Real DELETE contention at checkpoint, overhead and ready RO boundaries."""
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sqlite3
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application import warehouse_recovery_policy as recovery
from packages.application import operator_ff_overhead as overhead
from packages.application import fbs_current_snapshot_policy as policy
from packages.application import ready_publication as ready
from packages.application.sqlite_contention import (
    connect_sqlite, current_sqlite_contention_state, SQLiteContentionExhausted,
)
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock


@contextmanager
def exclusive(path, *, transient):
    acquired, stop = threading.Event(), threading.Event()
    errors = []
    def hold():
        try:
            with closing(sqlite3.connect(path)) as conn:
                assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
                conn.execute('BEGIN EXCLUSIVE')
                acquired.set()
                stop.wait(.65 if transient else 10)
        except BaseException as exc:
            errors.append(exc)
            acquired.set()
    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert acquired.wait(5) and not errors, errors
        yield
    finally:
        stop.set()
        worker.join(12)
        assert not worker.is_alive() and not errors, errors


def bounded_connect(*args, **kwargs):
    return connect_sqlite(*args, **kwargs, timeout_ms=1200)


def assert_readonly(conn, table):
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
    try:
        conn.execute('DELETE FROM ' + table)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError('RO factory accepted DML')
    # query_only alone must not hide an accidentally writable URI.
    conn.execute('PRAGMA query_only=OFF')
    try:
        conn.execute('DELETE FROM ' + table)
    except sqlite3.OperationalError as exc:
        assert 'readonly' in str(exc), exc
    else:
        raise AssertionError('mode=ro lost')


def checkpoint_cases():
    for boundary in ('SELECT sql FROM sqlite_master', 'PRAGMA table_info'):
        for transient in (True, False):
            with TemporaryDirectory(prefix='checkpoint-read-lock-') as raw:
                path = Path(raw) / 'source ?#.sqlite3'
                final = Path(raw) / 'copy.sqlite3'
                temporary = Path(str(final) + '.tmp')
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.executescript('CREATE TABLE source_rows(id INTEGER PRIMARY KEY,value TEXT);'
                                       "INSERT INTO source_rows VALUES(1,'before');"
                                       'CREATE TABLE finance_raw_secret(value TEXT);')
                registry = recovery.WarehouseRecoveryRegistry(runtime_dir=Path(raw), db_path=path)
                native_readonly = recovery._connect_readonly
                opened, statements, states = [], [], []
                @contextmanager
                def reader(db_path):
                    with native_readonly(db_path) as conn:
                        opened.append(conn)
                        conn.set_trace_callback(statements.append)
                        class BoundaryReader:
                            def execute(self, sql, *args):
                                if ' '.join(sql.split()).startswith(boundary):
                                    assert not conn.in_transaction
                                    with exclusive(path, transient=transient):
                                        result = conn.execute(sql, *args)
                                    states.append(current_sqlite_contention_state())
                                    return result
                                return conn.execute(sql, *args)
                        yield BoundaryReader()
                started = time.monotonic()
                with patch.object(recovery, 'connect_sqlite', bounded_connect), patch.object(recovery, '_connect_readonly', reader):
                    try:
                        registry._write_domain_checkpoint(
                            temporary=temporary, final=final, table_names=['source_rows'],
                            operation_id='test', plan_fingerprint='test', source_digest='source',
                            source_watermarks={}, schema_revision='1')
                    except SQLiteContentionExhausted as exc:
                        assert not transient and exc.phase == ('schema' if boundary.startswith('PRAGMA') else 'read_statement'), exc.phase
                        assert 1 <= time.monotonic() - started < 3
                        assert not final.exists()
                    else:
                        assert transient and final.exists()
                        assert states and all(state.retries > 0 and not state.exhausted for state in states)
                        with closing(sqlite3.connect(final)) as copied:
                            assert copied.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                            assert copied.execute('SELECT * FROM source_rows').fetchall() == [(1, 'before')]
                            assert copied.execute('SELECT count(*) FROM recovery_checkpoint_metadata').fetchone()[0] == 1
                            assert copied.execute("SELECT 1 FROM sqlite_master WHERE name='finance_raw_secret'").fetchone() is None
                assert not any(sql.startswith(('BEGIN', 'INSERT', 'UPDATE', 'DELETE')) for sql in statements), statements
                try:
                    opened[0].execute('SELECT 1')
                except sqlite3.ProgrammingError:
                    pass
                else:
                    raise AssertionError('checkpoint source connection leaked')
                if not transient:
                    # The failed private temp is disposable. A later explicit
                    # preparation starts clean, without publishing a prefix.
                    registry._write_domain_checkpoint(
                        temporary=temporary, final=final, table_names=['source_rows'],
                        operation_id='test', plan_fingerprint='test', source_digest='source',
                        source_watermarks={}, schema_revision='1')
                    with closing(sqlite3.connect(final)) as copied:
                        assert copied.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                        assert copied.execute('SELECT * FROM source_rows').fetchall() == [(1, 'before')]
                        assert copied.execute('SELECT count(*) FROM recovery_checkpoint_metadata').fetchone()[0] == 1
                with closing(sqlite3.connect(path, timeout=.2)) as conn, conn:
                    assert conn.execute('SELECT * FROM source_rows').fetchall() == [(1, 'before')]
                    conn.execute("UPDATE source_rows SET value='after'")  # No own reader blocks the next writer.
                with native_readonly(path) as conn:
                    assert_readonly(conn, 'source_rows')
    print('checkpoint schema/PRAGMA: recovered real DELETE lock, bounded failure, RO URI, no replay/new pinned transaction or finalized failed artifact: OK')


def overhead_cases():
    with TemporaryDirectory(prefix='overhead-read-lock-') as raw:
        runtime = SimpleNamespace(runtime_dir=Path(raw), db_path=Path(raw) / 'source ?#.sqlite3')
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute('CREATE TABLE fixture(value INTEGER)')
        for transient in (True, False):
            with patch.object(overhead, 'connect_sqlite', bounded_connect), warehouse_functional_job_lock(runtime.runtime_dir):
                with exclusive(runtime.db_path, transient=transient):
                    started = time.monotonic()
                    try:
                        result = overhead.reconcile(runtime)
                    except SQLiteContentionExhausted as exc:
                        assert not transient and exc.phase == 'read_statement'
                        assert 1 <= time.monotonic() - started < 3
                    else:
                        assert transient and result == {'processed_count': 0}, result
                        state = current_sqlite_contention_state()
                        assert state.retries > 0 and not state.exhausted
        with closing(overhead.readonly(runtime.db_path)) as conn:
            assert conn.in_transaction  # Existing caller-owned snapshot semantics.
            assert_readonly(conn, 'fixture')
    print('overhead reconcile->_exists: transient recovery/permanent exhaustion, existing BEGIN and RO URI preserved: OK')


def ready_cases():
    with TemporaryDirectory(prefix='ready-read-lock-') as raw:
        path = Path(raw) / 'source ?#.sqlite3'
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE fixture(value INTEGER)')
            conn.execute('INSERT INTO fixture VALUES(1)')
        factory = policy.open_current_snapshot_readonly
        for transient in (True, False):
            def bounded(path):
                return factory(path, timeout=1.2)
            with patch.object(policy, 'open_current_snapshot_readonly', bounded), exclusive(path, transient=transient):
                started = time.monotonic()
                try:
                    with ready.readonly(path) as conn:
                        assert conn.in_transaction
                        assert policy._opened_generation(conn)[1] == str(path.resolve())
                        assert conn.execute('SELECT value FROM fixture').fetchone()[0] == 1
                        state = current_sqlite_contention_state()
                        assert state.retries > 0 and not state.exhausted
                except SQLiteContentionExhausted as exc:
                    assert not transient and exc.phase == 'read_statement'
                    assert 1 <= time.monotonic() - started < 3
                else:
                    assert transient
        with closing(factory(path)) as conn:
            assert not conn.in_transaction  # Factory must leave BEGIN ownership with ready/caller.
            assert type(conn) is policy._OpenedReadConnection
            assert_readonly(conn, 'fixture')
        # A replacement during the new bounded wait cannot inherit old-inode authority.
        original_connect = policy.sqlite3.connect
        opened = []
        def recorded(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            if kwargs.get('factory') is policy._OpenedReadConnection:
                opened.append(conn)
            return conn
        def replace():
            old = path.with_name('old.sqlite3')
            path.rename(old)
            with closing(original_connect(path)) as conn, conn:
                conn.execute('CREATE TABLE replacement(value)')
        with exclusive(path, transient=True):
            timer = threading.Timer(.35, replace)
            timer.start()
            try:
                with patch.object(policy.sqlite3, 'connect', recorded):
                    try:
                        factory(path, timeout=1.2)
                    except policy.FbsSnapshotPolicyError as exc:
                        assert str(exc) == 'fbs_policy_operational_file_changed_during_open', exc
                    else:
                        raise AssertionError('wait accepted replacement inode')
            finally:
                timer.join(5)
        try:
            opened[0].execute('SELECT 1')
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError('failed inode proof leaked its reader')
    print('ready->FBS open: bounded DELETE wait, exact subtype/inode proof, closed rejected reader, caller BEGIN ownership unchanged: OK')


if __name__ == '__main__':
    checkpoint_cases()
    overhead_cases()
    ready_cases()
