"""Hourly observations in a separate store, with no accounting consumers."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import resource
import sqlite3
import time
from typing import Any
from uuid import uuid4

from packages.adapters.official_api_rate_budget import FileBackedOfficialApiRateBudget
from packages.adapters.wb_fbs_orders import HttpBackedWbFbsOrdersSource
from packages.application.wb_fbs_orders import (
    OBSERVATIONS_TABLE, STATUS_CURRENT_TABLE, STATE_TABLE,
    WbFbsOrdersCollector, ensure_wb_fbs_orders_schema,
)
from packages.application.wb_fbs_shadow_polling import (
    fbs_shadow_poll_lock, WbFbsShadowPollingBusy,
)

CADENCE_SECONDS = 3600
INITIAL_LOOKBACK_SECONDS = 30 * 86400
OVERLAP_SECONDS = 2 * 3600
TERMINAL = "'sold','accepted_by_client','canceled','canceled_by_client','declined_by_client','defect'"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


class WbFbsObserver:
    def __init__(self, *, runtime_dir: Path, canonical_db_path: Path,
                 source: Any = None, enabled: bool = True,
                 max_pages: int = 10, max_status_batches: int = 10,
                 unix_time_factory: Any = time.time, timestamp_factory: Any = _now):
        self.runtime_dir = Path(runtime_dir).resolve()
        self.root = self.runtime_dir / 'fbs_observer'
        self.db_path = self.root / 'observations.sqlite3'
        self.canonical_db_path = Path(canonical_db_path).resolve()
        self.enabled = enabled
        self.max_pages = max(1, min(max_pages, 10))
        self.max_status_batches = max(1, min(max_status_batches, 10))
        self.unix = unix_time_factory
        self.timestamp = timestamp_factory
        self.source = source or HttpBackedWbFbsOrdersSource(
            rate_budget=FileBackedOfficialApiRateBudget(
                runtime_dir=self.runtime_dir, family='wb_fbs_orders', min_interval_seconds=0.22,
            )
        )
        self.collector = WbFbsOrdersCollector(
            db_path=self.db_path, source=self.source, enabled=True,
            timestamp_factory=self.timestamp, unix_time_factory=self.unix,
        )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=2)
        conn.row_factory = sqlite3.Row
        with closing(conn), conn:
            yield conn

    def _initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Refuse aliases/hardlinks to any existing accounting store.
        if self.root.is_symlink() or self.db_path.is_symlink():
            raise RuntimeError('observer storage must not be a symlink')
        if self.db_path.exists() and self.canonical_db_path.exists() and os.path.samefile(self.db_path, self.canonical_db_path):
            raise RuntimeError('observer storage must be separate from accounting')
        with self._connect() as conn:
            ensure_wb_fbs_orders_schema(conn)
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS observer_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observer_status_checks(order_id INTEGER PRIMARY KEY,last_attempt INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS observer_status_check_age ON observer_status_checks(last_attempt,order_id);
                CREATE TABLE IF NOT EXISTS observer_runs(run_id TEXT PRIMARY KEY,started_at TEXT NOT NULL,result_json TEXT NOT NULL);
            ''')
        os.chmod(self.db_path, 0o600)

    def _bootstrap(self) -> tuple[int, int]:
        with self._connect() as target:
            if target.execute("SELECT 1 FROM observer_meta WHERE key='bootstrap'").fetchone():
                return 0, 0
            columns = [r['name'] for r in target.execute(f'PRAGMA table_info({OBSERVATIONS_TABLE})') if r['name'] != 'observation_sequence']
        # Only copy already known unfinished identities. No status/cursor/cost import.
        with closing(_read_only(self.canonical_db_path)) as source:
            tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if OBSERVATIONS_TABLE not in tables or STATUS_CURRENT_TABLE not in tables:
                raise RuntimeError('canonical FBS observations unavailable for initial unfinished scope')
            rows = source.execute(f'''
                SELECT {','.join('o.'+c for c in columns)} FROM {OBSERVATIONS_TABLE} o
                LEFT JOIN {STATUS_CURRENT_TABLE} s ON s.order_id=o.order_id
                WHERE o.observation_sequence=(SELECT MAX(x.observation_sequence) FROM {OBSERVATIONS_TABLE} x WHERE x.order_id=o.order_id)
                  AND COALESCE(s.wb_status,'') NOT IN ({TERMINAL})
                  AND COALESCE(s.supplier_status,'')<>'cancel'
                ORDER BY o.order_id LIMIT 50001
            ''').fetchall()
        if len(rows) > 50000:
            raise RuntimeError('initial unfinished scope exceeds bounded bootstrap limit')
        with self._connect() as target:
            before = target.total_changes
            target.executemany(f"INSERT OR IGNORE INTO {OBSERVATIONS_TABLE}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", [tuple(r) for r in rows])
            inserted = target.total_changes - before
            target.execute("INSERT INTO observer_meta VALUES('bootstrap',?)", (json.dumps({'at':self.timestamp(), 'source':str(self.canonical_db_path), 'known_unfinished':len(rows)}),))
        return inserted, inserted + 1

    def _window(self) -> tuple[int, int, int]:
        now = int(self.unix())
        with self._connect() as conn:
            row = conn.execute(f'SELECT * FROM {STATE_TABLE} WHERE state_id=1').fetchone()
        if row and not row['complete']:
            return int(row['window_date_from']), int(row['window_date_to']), int(row['next_cursor'])
        if row:
            start = max(1, int(row['window_date_to']) - OVERLAP_SECONDS)
            return start, min(now, start + INITIAL_LOOKBACK_SECONDS), 0
        return max(1, now - INITIAL_LOOKBACK_SECONDS), now, 0

    def _pending(self, *, due_before: int, limit: int | None = None) -> list[int]:
        cutoff = datetime.fromtimestamp(int(self.unix()) - INITIAL_LOOKBACK_SECONDS, timezone.utc).isoformat().replace('+00:00','Z')
        with self._connect() as conn:
            return [int(r[0]) for r in conn.execute(f'''
                SELECT o.order_id FROM {OBSERVATIONS_TABLE} o
                LEFT JOIN {STATUS_CURRENT_TABLE} s ON s.order_id=o.order_id
                LEFT JOIN observer_status_checks c ON c.order_id=o.order_id
                WHERE o.observation_sequence=(SELECT MAX(x.observation_sequence) FROM {OBSERVATIONS_TABLE} x WHERE x.order_id=o.order_id)
                  AND COALESCE(c.last_attempt,0)<?
                  AND ((COALESCE(s.wb_status,'') NOT IN ({TERMINAL}) AND COALESCE(s.supplier_status,'')<>'cancel') OR o.source_created_at>=?)
                ORDER BY COALESCE(c.last_attempt,0),o.order_id LIMIT ?
            ''', (due_before,cutoff, limit if limit is not None else -1))]

    def poll_once(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            'contract_name':'wb_fbs_observer_v1', 'mode':'observation_only',
            'run_id':'fbs_observer_'+uuid4().hex, 'started_at':self.timestamp(),
            'cadence_seconds':CADENCE_SECONDS, 'mutates_wb':False,
            'mutates_accounting':False, 'lifecycle_applied':False,
            'page_count':0, 'new_order_observation_count':0,
            'new_status_observation_count':0, 'transition_count':0,
            'missing_status_count':0, 'schema_drift_count':0, 'db_write_count':0,
            'status_checked_count':0,
            'terminal_recheck_created_within_days':30,
            'initial_listing_days':30,
            'unfinished_status_age_limit_days':None,
        }
        if not self.enabled:
            return {**result, 'status':'disabled'}
        started = time.monotonic()
        cpu = time.process_time()
        # Separate lease covers collection, refresh, bootstrap and receipt publication.
        try:
            with fbs_shadow_poll_lock(self.root / 'lease'):
                self._initialize()
                self.source.reset_telemetry() if hasattr(self.source,'reset_telemetry') else None
                try:
                    result['bootstrap_order_count'], bootstrap_writes = self._bootstrap()
                    result['db_write_count'] += bootstrap_writes
                    start, end, cursor = self._window()
                    result.update(window_date_from=start, window_date_to=end, start_cursor=cursor)
                    complete = False
                    for _ in range(self.max_pages):
                        page = self.collector.collect(date_from=start,date_to=end,next_cursor=cursor,page_limit=1000,max_pages=1)
                        result['page_count'] += 1
                        for key in ('new_status_observation_count','transition_count','missing_status_count','schema_drift_count','db_write_count'):
                            result[key] += int(page.get(key,0))
                        result['new_order_observation_count'] += int(page['new_observation_count'])
                        cursor = int(page['next_cursor'])
                        result['next_cursor'] = cursor
                        if page['complete']:
                            complete = True
                            break
                    # Oldest attempts first: missing statuses cannot starve later orders.
                    due_before = int(self.unix()) - 45 * 60
                    for _ in range(self.max_status_batches):
                        ids = self._pending(due_before=due_before, limit=1000)
                        if not ids:
                            break
                        refresh = self.collector.refresh_known_statuses(ids)
                        result['status_checked_count'] += len(ids)
                        for key in ('new_status_observation_count','transition_count','missing_status_count','schema_drift_count','db_write_count'):
                            result[key] += int(refresh.get(key,0))
                        with self._connect() as conn:
                            conn.executemany('INSERT INTO observer_status_checks VALUES(?,?) ON CONFLICT(order_id) DO UPDATE SET last_attempt=excluded.last_attempt', [(oid,int(self.unix())) for oid in ids])
                            result['db_write_count'] += conn.total_changes
                    result['status_backlog_count'] = len(self._pending(due_before=due_before))
                    result['listing_complete'] = complete
                    result['status'] = 'success' if complete and not any(result[k] for k in ('status_backlog_count','missing_status_count','schema_drift_count')) else 'bounded_partial'
                except Exception as exc:
                    # Do not persist upstream bodies, order identities or credentials in receipts.
                    result.update(status='failed', error_type=type(exc).__name__)
                result.update(completed_at=self.timestamp(), duration_ms=round((time.monotonic()-started)*1000), cpu_ms=round((time.process_time()-cpu)*1000), peak_rss_kb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                if hasattr(self.source,'telemetry_snapshot'):
                    result.update(self.source.telemetry_snapshot())
                result['observer_db_bytes'] = self.db_path.stat().st_size
                with self._connect() as conn:
                    result['db_write_count'] += 1
                    conn.execute('INSERT INTO observer_runs VALUES(?,?,?)', (result['run_id'],result['started_at'],json.dumps(result,sort_keys=True)))
                return result
        except WbFbsShadowPollingBusy:
            return {**result, 'status':'single_flight_skipped'}


def observer_status(runtime_dir: Path) -> dict[str, Any]:
    path = Path(runtime_dir).resolve() / 'fbs_observer' / 'observations.sqlite3'
    if not path.exists():
        return {'mode':'observation_only','status':'not_started'}
    with closing(_read_only(path)) as conn:
        row = conn.execute('SELECT result_json FROM observer_runs ORDER BY rowid DESC LIMIT 1').fetchone()
    if not row:
        return {'mode':'observation_only','status':'no_completed_cycle'}
    result = json.loads(row[0])
    age = max(0, int(time.time() - datetime.fromisoformat(result['completed_at'].replace('Z','+00:00')).timestamp()))
    return {**result, 'age_seconds':age,'stale':age > 2*CADENCE_SECONDS,'query_only':True}
