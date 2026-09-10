"""Keyword-cleaner operational schema and short transaction boundary.

No business database filename and no network calls live here. Schema installation
is explicit; reads never migrate, dispatch jobs, or contact Wildberries.
"""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Iterator

from packages.application.storage_registry import StoreRegistry

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS cleaner_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);
INSERT OR IGNORE INTO cleaner_schema VALUES(1,1);
CREATE TABLE IF NOT EXISTS cleaner_settings(
 account TEXT PRIMARY KEY, seller_id TEXT NOT NULL, account_scope TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN(0,1)), revision INTEGER NOT NULL DEFAULT 1,
 schedule_time TEXT NOT NULL DEFAULT '07:00', timezone TEXT NOT NULL DEFAULT 'Asia/Yekaterinburg',
 rules_version TEXT NOT NULL, baseline_ready INTEGER NOT NULL DEFAULT 0,
 generation TEXT NOT NULL, restore_hold INTEGER NOT NULL DEFAULT 1,
 heartbeat_at TEXT, cursor INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cleaner_profiles(
 account TEXT NOT NULL, nm_id INTEGER NOT NULL, version INTEGER NOT NULL,
 fingerprint TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL,
 PRIMARY KEY(account,nm_id,version)
);
CREATE TABLE IF NOT EXISTS cleaner_profile_heads(
 account TEXT NOT NULL,nm_id INTEGER NOT NULL,active_version INTEGER,revision INTEGER NOT NULL,
 PRIMARY KEY(account,nm_id)
);
CREATE TABLE IF NOT EXISTS cleaner_auto_decisions(
 decision_id TEXT PRIMARY KEY,account TEXT NOT NULL,target TEXT NOT NULL,query_hash TEXT NOT NULL,
 query TEXT NOT NULL,verdict TEXT NOT NULL CHECK(verdict IN('allow','exclude','review')),
 rule_id TEXT NOT NULL,reason TEXT NOT NULL,facts TEXT NOT NULL,rules_version TEXT NOT NULL,
 profile_version INTEGER,fingerprint TEXT,source TEXT NOT NULL,override_revision INTEGER,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cleaner_baselines(
 account TEXT NOT NULL,target TEXT NOT NULL,advert_id INTEGER NOT NULL,nm_id INTEGER NOT NULL,
 query_hash TEXT NOT NULL,query TEXT NOT NULL,observed_state TEXT NOT NULL,decision_id TEXT,
 provenance TEXT NOT NULL,import_digest TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(account,target,query_hash),FOREIGN KEY(decision_id) REFERENCES cleaner_auto_decisions(decision_id)
);
CREATE TABLE IF NOT EXISTS cleaner_observations(
 account TEXT NOT NULL,target TEXT NOT NULL,advert_id INTEGER NOT NULL,nm_id INTEGER NOT NULL,
 query_hash TEXT NOT NULL,query TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
 observed_state TEXT NOT NULL,sources TEXT NOT NULL,source_times TEXT NOT NULL,
 state TEXT NOT NULL,decision_id TEXT,review_id TEXT,last_run_id TEXT,
 PRIMARY KEY(account,target,query_hash),FOREIGN KEY(decision_id) REFERENCES cleaner_auto_decisions(decision_id)
);
CREATE TABLE IF NOT EXISTS cleaner_manual_overrides(
 account TEXT NOT NULL,nm_id INTEGER NOT NULL,query_hash TEXT NOT NULL,query TEXT NOT NULL,
 revision INTEGER NOT NULL,verdict TEXT NOT NULL CHECK(verdict IN('allow','exclude')),
 fingerprint TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(account,nm_id,query_hash,revision)
);
CREATE TABLE IF NOT EXISTS cleaner_override_heads(
 account TEXT NOT NULL,nm_id INTEGER NOT NULL,query_hash TEXT NOT NULL,
 revision INTEGER NOT NULL,needs_revalidation INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(account,nm_id,query_hash)
);
CREATE TABLE IF NOT EXISTS cleaner_reviews(
 review_id TEXT PRIMARY KEY,account TEXT NOT NULL,nm_id INTEGER NOT NULL,query_hash TEXT NOT NULL,
 query TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,reason TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN('open','resolved')),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 scope_digest TEXT NOT NULL DEFAULT '',
 UNIQUE(account,nm_id,query_hash)
);
CREATE TABLE IF NOT EXISTS cleaner_runs(
 run_id TEXT PRIMARY KEY,account TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN('scan','manual_apply')),
 trigger TEXT NOT NULL,state TEXT NOT NULL,phase TEXT NOT NULL,created_at TEXT NOT NULL,
 started_at TEXT,scan_finished_at TEXT,settled_at TEXT,worker_token TEXT,
 lease_expires_at TEXT,worker_generation TEXT,request_id TEXT,captured_versions TEXT NOT NULL,
 targets TEXT NOT NULL,review_id TEXT,override_revision INTEGER,apply_group_id TEXT,
 continuation_number INTEGER NOT NULL DEFAULT 0,summary TEXT NOT NULL DEFAULT '{}',reason TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS cleaner_active_run ON cleaner_runs(account) WHERE state IN('accepted','running');
CREATE UNIQUE INDEX IF NOT EXISTS cleaner_apply_continuation ON cleaner_runs(apply_group_id,continuation_number) WHERE apply_group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS cleaner_run_fifo ON cleaner_runs(account,state,created_at);
CREATE TABLE IF NOT EXISTS cleaner_schedule_dates(
 account TEXT NOT NULL,local_date TEXT NOT NULL,run_id TEXT NOT NULL,due_at TEXT NOT NULL,
 PRIMARY KEY(account,local_date),FOREIGN KEY(run_id) REFERENCES cleaner_runs(run_id)
);
CREATE TABLE IF NOT EXISTS cleaner_scan_queue(
 account TEXT NOT NULL,target TEXT NOT NULL,advert_id INTEGER NOT NULL,nm_id INTEGER NOT NULL,
 scan_order INTEGER NOT NULL,metadata TEXT NOT NULL,available INTEGER NOT NULL DEFAULT 1,
 last_attempted_at TEXT,retry_not_before TEXT,PRIMARY KEY(account,target),UNIQUE(account,scan_order)
);
CREATE TABLE IF NOT EXISTS cleaner_requests(
 account TEXT NOT NULL,actor TEXT NOT NULL,request_id TEXT NOT NULL,route TEXT NOT NULL,
 digest TEXT NOT NULL,outcome TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(account,actor,request_id)
);
CREATE TABLE IF NOT EXISTS cleaner_run_targets(
 run_id TEXT NOT NULL,target TEXT NOT NULL,metadata TEXT NOT NULL,state TEXT NOT NULL,
 complete INTEGER NOT NULL,reason TEXT NOT NULL,observed_at TEXT,counters TEXT NOT NULL,
 source_times TEXT NOT NULL,PRIMARY KEY(run_id,target),FOREIGN KEY(run_id) REFERENCES cleaner_runs(run_id)
);
CREATE TABLE IF NOT EXISTS cleaner_target_holds(
 account TEXT NOT NULL,target TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(account,target)
);
CREATE TABLE IF NOT EXISTS cleaner_write_operations(
 operation_id TEXT PRIMARY KEY,account TEXT NOT NULL,target TEXT NOT NULL,run_id TEXT NOT NULL,
 state TEXT NOT NULL,before_json TEXT NOT NULL,expected_json TEXT NOT NULL,additions TEXT NOT NULL,
 candidate_digest TEXT NOT NULL,versions TEXT NOT NULL,worker_token TEXT,worker_generation TEXT,
 dispatch_count INTEGER NOT NULL DEFAULT 0 CHECK(dispatch_count BETWEEN 0 AND 1),
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,preflight_at TEXT,evidence TEXT NOT NULL DEFAULT '{}',
 FOREIGN KEY(run_id) REFERENCES cleaner_runs(run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS cleaner_unresolved_target ON cleaner_write_operations(account,target)
 WHERE state IN('prepared','dispatching','submitted','unresolved');
CREATE TABLE IF NOT EXISTS cleaner_write_items(
 operation_id TEXT NOT NULL,query_hash TEXT NOT NULL,query TEXT NOT NULL,decision_id TEXT NOT NULL,
 override_revision INTEGER,state TEXT NOT NULL,confirmed_at TEXT,registry_item_id TEXT,
 PRIMARY KEY(operation_id,query_hash),FOREIGN KEY(operation_id) REFERENCES cleaner_write_operations(operation_id),
 FOREIGN KEY(decision_id) REFERENCES cleaner_auto_decisions(decision_id)
);
CREATE TABLE IF NOT EXISTS cleaner_readback_jobs(
 operation_id TEXT PRIMARY KEY,account TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0,retry_not_before TEXT,worker_token TEXT,lease_expires_at TEXT,
 FOREIGN KEY(operation_id) REFERENCES cleaner_write_operations(operation_id)
);
CREATE TABLE IF NOT EXISTS cleaner_events(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,account TEXT NOT NULL,
 run_id TEXT,operation_id TEXT,kind TEXT NOT NULL,created_at TEXT NOT NULL,facts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cleaner_reviews_page ON cleaner_reviews(account,state,created_at,review_id);
CREATE INDEX IF NOT EXISTS cleaner_observation_review ON cleaner_observations(account,review_id,state);
CREATE INDEX IF NOT EXISTS cleaner_events_page ON cleaner_events(account,sequence DESC);
"""
IMMUTABLE_TABLES = ("cleaner_profiles", "cleaner_auto_decisions", "cleaner_baselines", "cleaner_manual_overrides", "cleaner_events", "cleaner_requests", "cleaner_schedule_dates")


def install_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    if conn.execute("SELECT version FROM cleaner_schema WHERE singleton=1").fetchone()[0] != SCHEMA_VERSION:
        raise RuntimeError("unsupported cleaner schema version")
    for table in IMMUTABLE_TABLES:
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable cleaner evidence'); END")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS cleaner_operation_dispatch_monotonic
      BEFORE UPDATE ON cleaner_write_operations WHEN NEW.dispatch_count < OLD.dispatch_count
      OR OLD.state IN('confirmed','rejected','cancelled_before_send')
      OR (OLD.dispatch_count=1 AND NEW.state IN('queued','prepared'))
      BEGIN SELECT RAISE(ABORT,'cleaner operation cannot regain dispatch right'); END""")
    conn.commit()


class CleanerStore:
    def __init__(self, registry: StoreRegistry):
        self.registry = registry

    def initialize(self) -> None:
        with self.registry.session("operational", mode="rw", operation="cleaner_schema") as conn:
            install_schema(conn)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self.registry.session("operational", mode="ro", operation="cleaner_read", timeout_ms=1000) as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.registry.session("operational", mode="rw", operation="cleaner_transaction", timeout_ms=1000, isolation_level=None) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
