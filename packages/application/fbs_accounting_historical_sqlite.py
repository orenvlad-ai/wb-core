"""Bounded native SQLite waits for historical publication's write boundaries.

SQLite waits on the same statement/COMMIT; the business operation is never
replayed. Native busy handling also covers schema executescript and connection
context commits, which do not pass through Python execute/commit overrides.
"""
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import sqlite3
import time

from packages.application.sqlite_contention import SQLiteContentionExhausted

BUSY_TIMEOUT_SECONDS = 30.0
_LOG = logging.getLogger(__name__)


@contextmanager
def phase(db_path, name, statement_kind=None):
    started = time.monotonic()
    try:
        yield
    except sqlite3.Error as exc:
        if isinstance(exc, SQLiteContentionExhausted) or getattr(exc, "historical_phase_recorded", False):
            raise
        exc.historical_phase_recorded = True
        code = getattr(exc, 'sqlite_errorcode', None)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        # No SQL, parameters, document payloads or database contents in logs.
        _LOG.error('historical_sqlite_failure %s', json.dumps(dict(
            phase=name, database=Path(db_path).name, statement_kind=statement_kind,
            sqlite_errorcode=code,
            sqlite_errorname=getattr(exc, 'sqlite_errorname', None),
            elapsed_ms=elapsed_ms,
        ), sort_keys=True), exc_info=True)
        if code is not None and code & 0xff == sqlite3.SQLITE_BUSY:
            raise SQLiteContentionExhausted(
                wait_ms=elapsed_ms, retries=0, phase=name,
            ) from exc
        raise


@contextmanager
def connection(db_path, *, phase_name):
    db_path = Path(db_path)
    with phase(db_path, phase_name):
        conn = sqlite3.connect(db_path.as_uri() + '?mode=rw', uri=True,
                              timeout=BUSY_TIMEOUT_SECONDS)
        try:
            yield conn
        finally:
            conn.close()


@contextmanager
def transaction(conn, *, db_path, phase_name):
    """Commit the original transaction once; roll it back on any failure."""
    with phase(db_path, phase_name):
        try:
            with phase(db_path, phase_name, 'begin'):
                conn.execute('BEGIN IMMEDIATE')
            with phase(db_path, phase_name, 'body'):
                yield conn
            with phase(db_path, phase_name, 'commit'):
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
