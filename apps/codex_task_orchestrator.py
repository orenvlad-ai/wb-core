"""Local passive registry for wb-core Codex task orchestration.

The registry stores durable task/incident facts and exposes a read-only local
dashboard.  It never calls Codex or GitHub by itself; the OpenAI-native Watcher
is the only router and GitHub Release Train remains the release actuator.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator, Mapping
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.codex_task_orchestrator_spec import (  # noqa: E402
    CANONICAL_REPOSITORY,
    IncidentDisposition,
    IncidentStatus,
    RetryObservation,
    STRICT_HUMAN_REASONS,
    TaskStatus,
    canonical_digest,
    classify_incident,
    incident_key,
    report_status,
    transition_allowed,
    validate_arbiter_decision,
    validate_digest,
    validate_task_passport,
    validate_task_id,
)


DEFAULT_HOME = Path.home() / ".wb-core" / "orchestrator" / "v1"
ACTIVE_INCIDENT_STATES = (
    IncidentStatus.OPEN.value,
    IncidentStatus.WAITING_RESOURCE.value,
    IncidentStatus.CLAIMED.value,
    IncidentStatus.DECIDED.value,
    IncidentStatus.DELIVERED.value,
    IncidentStatus.VERIFIED.value,
)


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    repo TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    objective TEXT NOT NULL,
    passport_json TEXT NOT NULL,
    passport_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    progress_percent INTEGER NOT NULL DEFAULT 0 CHECK(progress_percent BETWEEN 0 AND 100),
    eta_text TEXT NOT NULL DEFAULT 'уточняется',
    last_delta TEXT NOT NULL DEFAULT 'Задача зарегистрирована.',
    current_action TEXT NOT NULL DEFAULT 'Исполнитель начинает работу.',
    blocker TEXT NOT NULL DEFAULT '',
    human_reason TEXT NOT NULL DEFAULT '',
    curator_thread_id TEXT NOT NULL,
    accepted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    role TEXT NOT NULL CHECK(role IN ('curator','executor','arbiter')),
    generation INTEGER NOT NULL CHECK(generation > 0),
    thread_id TEXT NOT NULL,
    host_id TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(task_id, role, generation),
    UNIQUE(task_id, thread_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_execution_thread
ON task_threads(thread_id) WHERE active=1 AND role IN ('executor','arbiter');
CREATE TABLE IF NOT EXISTS task_prs (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    pr_number INTEGER NOT NULL CHECK(pr_number > 0),
    role TEXT NOT NULL DEFAULT 'implementation',
    head_sha TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(task_id, pr_number)
);
CREATE TABLE IF NOT EXISTS incidents (
    case_id TEXT PRIMARY KEY,
    incident_key TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    task_revision INTEGER NOT NULL,
    phase TEXT NOT NULL,
    error_class TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    status TEXT NOT NULL,
    reservation_owner TEXT NOT NULL DEFAULT '',
    arbiter_thread_id TEXT NOT NULL DEFAULT '',
    decision_json TEXT NOT NULL DEFAULT '',
    expected_transition TEXT NOT NULL DEFAULT '',
    evidence_digest TEXT NOT NULL DEFAULT '',
    verification_evidence_digest TEXT NOT NULL DEFAULT '',
    archive_evidence_digest TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_incident_per_task
ON incidents(task_id) WHERE status IN ('OPEN','WAITING_RESOURCE','CLAIMED','DECIDED','DELIVERED','VERIFIED');
CREATE TABLE IF NOT EXISTS retry_observations (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    phase TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    error_class TEXT NOT NULL,
    observation_count INTEGER NOT NULL CHECK(observation_count > 0),
    empty_system_error INTEGER NOT NULL DEFAULT 0 CHECK(empty_system_error IN (0,1)),
    transient INTEGER NOT NULL DEFAULT 0 CHECK(transient IN (0,1)),
    last_disposition TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY(task_id, phase, evidence_fingerprint)
);
CREATE TABLE IF NOT EXISTS resource_locks (
    resource TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES incidents(case_id),
    acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchers (
    generation INTEGER PRIMARY KEY CHECK(generation > 0),
    thread_id TEXT NOT NULL UNIQUE,
    host_id TEXT NOT NULL DEFAULT '',
    automation_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('PREPARED','ACTIVE','RETIRED')),
    run_count INTEGER NOT NULL DEFAULT 0,
    max_runs INTEGER NOT NULL DEFAULT 720 CHECK(max_runs > 0),
    last_run_at TEXT,
    smoke_digest TEXT NOT NULL DEFAULT '',
    smoke_at TEXT,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    retired_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_watcher
ON watchers(status) WHERE status = 'ACTIVE';
CREATE TABLE IF NOT EXISTS runtime_leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    generation INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_object(raw: str, *, field: str) -> dict[str, Any]:
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError(f"{field} must be a JSON object")
    return loaded


class Registry:
    def __init__(self, home: Path) -> None:
        self.home = home.expanduser().resolve()
        self.db_path = self.home / "registry.sqlite3"
        self.event_path = self.home / "events.jsonl"

    def initialize(self) -> None:
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            task_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(tasks)")
            }
            if "human_reason" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN human_reason TEXT NOT NULL DEFAULT ''"
                )
            incident_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(incidents)")
            }
            if "reservation_owner" not in incident_columns:
                connection.execute(
                    "ALTER TABLE incidents ADD COLUMN reservation_owner TEXT NOT NULL DEFAULT ''"
                )
            if "archive_evidence_digest" not in incident_columns:
                connection.execute(
                    "ALTER TABLE incidents ADD COLUMN archive_evidence_digest TEXT NOT NULL DEFAULT ''"
                )
            if "verification_evidence_digest" not in incident_columns:
                connection.execute(
                    "ALTER TABLE incidents ADD COLUMN verification_evidence_digest TEXT NOT NULL DEFAULT ''"
                )
            watcher_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(watchers)")
            }
            if "run_count" not in watcher_columns:
                connection.execute(
                    "ALTER TABLE watchers ADD COLUMN run_count INTEGER NOT NULL DEFAULT 0"
                )
            if "last_run_at" not in watcher_columns:
                connection.execute("ALTER TABLE watchers ADD COLUMN last_run_at TEXT")
            if "max_runs" not in watcher_columns:
                connection.execute(
                    "ALTER TABLE watchers ADD COLUMN max_runs INTEGER NOT NULL DEFAULT 720"
                )
            if "smoke_digest" not in watcher_columns:
                connection.execute(
                    "ALTER TABLE watchers ADD COLUMN smoke_digest TEXT NOT NULL DEFAULT ''"
                )
            if "smoke_at" not in watcher_columns:
                connection.execute("ALTER TABLE watchers ADD COLUMN smoke_at TEXT")
            task_threads_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_threads'"
            ).fetchone()
            task_threads_sql = "" if task_threads_sql_row is None else str(task_threads_sql_row[0])
            compact_task_threads_sql = "".join(task_threads_sql.lower().split())
            if (
                (
                    "unique(thread_id)" in compact_task_threads_sql
                    or "thread_idtextnotnullunique" in compact_task_threads_sql
                )
                and "unique(task_id,thread_id)" not in compact_task_threads_sql
            ):
                connection.execute("DROP INDEX IF EXISTS one_active_execution_thread")
                connection.execute("ALTER TABLE task_threads RENAME TO task_threads_v1")
                connection.execute(
                    "CREATE TABLE task_threads ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "task_id TEXT NOT NULL REFERENCES tasks(task_id),"
                    "role TEXT NOT NULL CHECK(role IN ('curator','executor','arbiter')) ,"
                    "generation INTEGER NOT NULL CHECK(generation > 0),"
                    "thread_id TEXT NOT NULL,"
                    "host_id TEXT NOT NULL DEFAULT '',"
                    "active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),"
                    "created_at TEXT NOT NULL,"
                    "UNIQUE(task_id,role,generation),"
                    "UNIQUE(task_id,thread_id))"
                )
                connection.execute(
                    "INSERT INTO task_threads(id,task_id,role,generation,thread_id,host_id,active,created_at) "
                    "SELECT id,task_id,role,generation,thread_id,host_id,active,created_at "
                    "FROM task_threads_v1"
                )
                connection.execute("DROP TABLE task_threads_v1")
                connection.execute(
                    "CREATE UNIQUE INDEX one_active_execution_thread "
                    "ON task_threads(thread_id) "
                    "WHERE active=1 AND role IN ('executor','arbiter')"
                )
            connection.execute("DROP INDEX IF EXISTS one_active_incident_per_task")
            connection.execute(
                "CREATE UNIQUE INDEX one_active_incident_per_task ON incidents(task_id) "
                "WHERE status IN ('OPEN','WAITING_RESOURCE','CLAIMED','DECIDED','DELIVERED','VERIFIED')"
            )
            connection.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version','3') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        if self.db_path.exists():
            os.chmod(self.db_path, 0o600)
        self.event_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.event_path, 0o600)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def event(
        self,
        connection: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        event_type: str,
        payload: Mapping[str, object],
    ) -> int:
        occurred_at = _now()
        rendered = _json(payload)
        cursor = connection.execute(
            "INSERT INTO events(occurred_at,entity_type,entity_id,event_type,payload_json) "
            "VALUES(?,?,?,?,?)",
            (occurred_at, entity_type, entity_id, event_type, rendered),
        )
        return int(cursor.lastrowid)

    def flush_events(self) -> int:
        descriptor = os.open(self.event_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
                descriptor = -1
                handle.seek(0)
                last_seq = 0
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        last_seq = int(json.loads(line).get("seq") or last_seq)
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise RuntimeError("events.jsonl is corrupt") from exc
                with self.connect() as connection:
                    rows = connection.execute(
                        "SELECT * FROM events WHERE seq > ? ORDER BY seq", (last_seq,)
                    ).fetchall()
                handle.seek(0, os.SEEK_END)
                for row in rows:
                    handle.write(
                        _json(
                            {
                                "seq": row["seq"],
                                "occurred_at": row["occurred_at"],
                                "entity_type": row["entity_type"],
                                "entity_id": row["entity_id"],
                                "event_type": row["event_type"],
                                "payload": json.loads(row["payload_json"]),
                            }
                        )
                        + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
                return len(rows)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def register_task(
        self,
        *,
        task_id: str,
        title: str,
        repo: str,
        project_id: str,
        objective: str,
        passport: Mapping[str, object],
        curator_thread_id: str,
        executor_thread_id: str,
        host_id: str,
    ) -> dict[str, object]:
        identity = validate_task_id(task_id)
        if not all(
            value.strip()
            for value in (
                title,
                repo,
                project_id,
                objective,
                curator_thread_id,
                executor_thread_id,
                host_id,
            )
        ):
            raise ValueError(
                "task registration requires title, repo, project, objective and exact thread/host ids"
            )
        if repo.strip().casefold() != CANONICAL_REPOSITORY:
            raise ValueError(
                f"the v1 registry accepts only {CANONICAL_REPOSITORY} tasks"
            )
        validated_passport = validate_task_passport(
            passport,
            task_id=identity,
            title=title,
            objective=objective,
            curator_thread_id=curator_thread_id,
            executor_thread_id=executor_thread_id,
        )
        timestamp = _now()
        digest = canonical_digest(validated_passport)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (identity,)
            ).fetchone()
            if existing is not None:
                expected = (
                    title.strip(),
                    CANONICAL_REPOSITORY,
                    project_id.strip(),
                    objective.strip(),
                    digest,
                    curator_thread_id.strip(),
                )
                actual = (
                    existing["title"],
                    existing["repo"],
                    existing["project_id"],
                    existing["objective"],
                    existing["passport_digest"],
                    existing["curator_thread_id"],
                )
                initial_threads = {
                    row["role"]: (row["thread_id"], row["host_id"])
                    for row in connection.execute(
                        "SELECT role,thread_id,host_id FROM task_threads "
                        "WHERE task_id=? AND generation=1 AND role IN ('curator','executor')",
                        (identity,),
                    ).fetchall()
                }
                expected_threads = {
                    "curator": (curator_thread_id.strip(), host_id.strip()),
                    "executor": (executor_thread_id.strip(), host_id.strip()),
                }
                if actual != expected or initial_threads != expected_threads:
                    raise RuntimeError(
                        "task id is already registered with a different immutable identity"
                    )
                return {
                    "task_id": identity,
                    "revision": int(existing["revision"]),
                    "passport_digest": digest,
                    "idempotent": True,
                }
            connection.execute(
                "INSERT INTO tasks(task_id,title,repo,project_id,objective,passport_json,"
                "passport_digest,status,curator_thread_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    title.strip(),
                    CANONICAL_REPOSITORY,
                    project_id.strip(),
                    objective.strip(),
                    _json(validated_passport),
                    digest,
                    TaskStatus.WORKING.value,
                    curator_thread_id.strip(),
                    timestamp,
                    timestamp,
                ),
            )
            for role, thread_id in (
                ("curator", curator_thread_id),
                ("executor", executor_thread_id),
            ):
                connection.execute(
                    "INSERT INTO task_threads(task_id,role,generation,thread_id,host_id,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (identity, role, 1, thread_id.strip(), host_id.strip(), timestamp),
                )
            self.event(
                connection,
                "task",
                identity,
                "registered",
                {"passport_digest": digest, "executor_thread_id": executor_thread_id},
            )
        self.flush_events()
        return {"task_id": identity, "revision": 1, "passport_digest": digest}

    def add_thread(
        self,
        *,
        task_id: str,
        role: str,
        generation: int,
        thread_id: str,
        host_id: str,
    ) -> dict[str, object]:
        identity = validate_task_id(task_id)
        if (
            role not in {"curator", "executor", "arbiter"}
            or generation <= 0
            or not thread_id.strip()
            or not host_id.strip()
        ):
            raise ValueError("invalid thread identity")
        timestamp = _now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM task_threads WHERE task_id=? AND role=? AND generation=?",
                (identity, role, generation),
            ).fetchone()
            if existing is not None:
                if (
                    existing["thread_id"] != thread_id.strip()
                    or existing["host_id"] != host_id.strip()
                ):
                    raise RuntimeError(
                        "thread generation is already registered with a different identity"
                    )
                return {
                    "task_id": identity,
                    "role": role,
                    "generation": generation,
                    "idempotent": True,
                }
            if role in {"executor", "arbiter"}:
                connection.execute(
                    "UPDATE task_threads SET active=0 WHERE task_id=? AND role=?",
                    (identity, role),
                )
            connection.execute(
                "INSERT INTO task_threads(task_id,role,generation,thread_id,host_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (identity, role, generation, thread_id.strip(), host_id.strip(), timestamp),
            )
            self.event(
                connection,
                "task",
                identity,
                "thread-added",
                {"role": role, "generation": generation, "thread_id": thread_id},
            )
        self.flush_events()
        return {"task_id": identity, "role": role, "generation": generation}

    def task(self, task_id: str, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        identity = validate_task_id(task_id)
        if connection is not None:
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (identity,)).fetchone()
        else:
            with self.connect() as own:
                row = own.execute("SELECT * FROM tasks WHERE task_id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {identity}")
        return row

    def update_task(
        self,
        *,
        task_id: str,
        expected_revision: int,
        status: TaskStatus,
        progress: int | None,
        eta: str | None,
        delta: str | None,
        current: str | None,
        blocker: str | None,
        human_reason: str | None = None,
        repo_owned_remediation_available: bool = False,
        remediation_exhausted: bool = False,
    ) -> dict[str, object]:
        if expected_revision <= 0:
            raise ValueError("expected_revision must be positive")
        with self.transaction() as connection:
            before = self.task(task_id, connection)
            if int(before["revision"]) != expected_revision:
                raise RuntimeError(
                    f"stale task revision: expected {expected_revision}, actual {before['revision']}"
                )
            current_status = TaskStatus(before["status"])
            if not transition_allowed(current_status, status):
                raise RuntimeError(f"forbidden task transition: {current_status.value} -> {status.value}")
            next_revision = expected_revision + 1
            values = {
                "progress_percent": int(before["progress_percent"]) if progress is None else progress,
                "eta_text": before["eta_text"] if eta is None else eta.strip(),
                "last_delta": before["last_delta"] if delta is None else delta.strip(),
                "current_action": before["current_action"] if current is None else current.strip(),
                "blocker": before["blocker"] if blocker is None else blocker.strip(),
                "human_reason": (
                    before["human_reason"]
                    if human_reason is None
                    else human_reason.strip()
                ),
            }
            if not 0 <= int(values["progress_percent"]) <= 100:
                raise ValueError("progress must be between 0 and 100")
            if any(
                not str(values[field]).strip()
                for field in ("eta_text", "last_delta", "current_action")
            ):
                raise ValueError("eta, delta and current action must be non-empty")
            if status == TaskStatus.AWAITING_HUMAN:
                if values["human_reason"] not in STRICT_HUMAN_REASONS:
                    raise ValueError("awaiting-human requires a strict v1 human reason")
                if not values["blocker"]:
                    raise ValueError("awaiting-human requires an exact blocker")
                if repo_owned_remediation_available or not remediation_exhausted:
                    raise ValueError(
                        "awaiting-human requires exhausted remediation and no repo-owned action"
                    )
            else:
                if blocker is not None and blocker.strip():
                    raise ValueError("blocker text is allowed only for awaiting-human")
                values["blocker"] = ""
                values["human_reason"] = ""
            connection.execute(
                "UPDATE tasks SET status=?,revision=?,progress_percent=?,eta_text=?,"
                "last_delta=?,current_action=?,blocker=?,human_reason=?,updated_at=? WHERE task_id=?",
                (
                    status.value,
                    next_revision,
                    values["progress_percent"],
                    values["eta_text"],
                    values["last_delta"],
                    values["current_action"],
                    values["blocker"],
                    values["human_reason"],
                    _now(),
                    validate_task_id(task_id),
                ),
            )
            self.event(
                connection,
                "task",
                validate_task_id(task_id),
                "state-changed",
                {"from": current_status.value, "to": status.value, "revision": next_revision},
            )
        self.flush_events()
        return {"task_id": validate_task_id(task_id), "status": status.value, "revision": next_revision}

    def link_pr(self, *, task_id: str, pr: int, role: str, head_sha: str, state: str) -> dict[str, object]:
        if pr <= 0:
            raise ValueError("pr must be positive")
        identity = validate_task_id(task_id)
        timestamp = _now()
        with self.transaction() as connection:
            self.task(identity, connection)
            connection.execute(
                "INSERT INTO task_prs(task_id,pr_number,role,head_sha,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id,pr_number) DO UPDATE SET "
                "role=excluded.role,head_sha=excluded.head_sha,state=excluded.state,updated_at=excluded.updated_at",
                (identity, pr, role.strip(), head_sha.strip().lower(), state.strip(), timestamp, timestamp),
            )
            self.event(connection, "task", identity, "pr-linked", {"pr": pr, "state": state})
        self.flush_events()
        return {"task_id": identity, "pr": pr, "state": state}

    def accept(self, *, task_id: str, expected_revision: int) -> dict[str, object]:
        with self.transaction() as connection:
            row = self.task(task_id, connection)
            if int(row["revision"]) != expected_revision:
                raise RuntimeError("stale acceptance revision")
            if TaskStatus(row["status"]) not in {
                TaskStatus.DONE_AWAITING_ACCEPTANCE,
                TaskStatus.TERMINAL_FAILURE,
            }:
                raise RuntimeError("only a terminal task can be accepted")
            revision = expected_revision + 1
            timestamp = _now()
            connection.execute(
                "UPDATE tasks SET status=?,revision=?,accepted_at=?,updated_at=? WHERE task_id=?",
                (TaskStatus.ACCEPTED.value, revision, timestamp, timestamp, validate_task_id(task_id)),
            )
            self.event(connection, "task", validate_task_id(task_id), "accepted", {"revision": revision})
        self.flush_events()
        return {"task_id": validate_task_id(task_id), "status": TaskStatus.ACCEPTED.value, "revision": revision}

    def record_failure(
        self,
        *,
        task_id: str,
        task_revision: int,
        phase: str,
        error_class: str,
        evidence_fingerprint: str,
        transient: bool,
        empty_system_error: bool,
        repo_owned_remediation_available: bool,
        remediation_exhausted: bool,
        human_reason: str,
    ) -> dict[str, object]:
        identity = validate_task_id(task_id)
        normalized_phase = phase.strip()
        fingerprint = evidence_fingerprint.strip()
        normalized_error = error_class.strip()
        if not normalized_phase or not fingerprint or not normalized_error:
            raise ValueError("failure observation requires phase, error class and fingerprint")
        timestamp = _now()
        with self.transaction() as connection:
            task = self.task(identity, connection)
            if int(task["revision"]) != task_revision:
                raise RuntimeError("failure was observed on a stale task revision")
            row = connection.execute(
                "SELECT observation_count,resolved_at FROM retry_observations "
                "WHERE task_id=? AND phase=? AND evidence_fingerprint=?",
                (identity, normalized_phase, fingerprint),
            ).fetchone()
            count = 1 if row is None or row["resolved_at"] else int(row["observation_count"]) + 1
            observation = RetryObservation(
                error_class=normalized_error,
                identical_fingerprint_count=count,
                transient=transient,
                empty_system_error=empty_system_error,
                repo_owned_remediation_available=repo_owned_remediation_available,
                remediation_exhausted=remediation_exhausted,
                human_reason=human_reason.strip(),
            )
            disposition = classify_incident(observation)
            connection.execute(
                "INSERT INTO retry_observations(task_id,phase,evidence_fingerprint,error_class,"
                "observation_count,empty_system_error,transient,last_disposition,first_seen_at,"
                "last_seen_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,NULL) "
                "ON CONFLICT(task_id,phase,evidence_fingerprint) DO UPDATE SET "
                "error_class=excluded.error_class,observation_count=excluded.observation_count,"
                "empty_system_error=excluded.empty_system_error,transient=excluded.transient,"
                "last_disposition=excluded.last_disposition,last_seen_at=excluded.last_seen_at,"
                "resolved_at=NULL",
                (
                    identity,
                    normalized_phase,
                    fingerprint,
                    normalized_error,
                    count,
                    int(empty_system_error),
                    int(transient),
                    disposition.value,
                    timestamp,
                    timestamp,
                ),
            )
            self.event(
                connection,
                "task",
                identity,
                "failure-observed",
                {
                    "phase": normalized_phase,
                    "fingerprint": fingerprint,
                    "count": count,
                    "disposition": disposition.value,
                },
            )
        self.flush_events()
        return {
            "task_id": identity,
            "task_revision": task_revision,
            "phase": normalized_phase,
            "evidence_fingerprint": fingerprint,
            "identical_fingerprint_count": count,
            "disposition": disposition.value,
            "incident_required": disposition
            in {
                IncidentDisposition.REPLACE_EXECUTOR,
                IncidentDisposition.OPEN_ARBITER,
            },
        }

    def resolve_failure(
        self,
        *,
        task_id: str,
        phase: str,
        evidence_fingerprint: str,
    ) -> dict[str, object]:
        identity = validate_task_id(task_id)
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE retry_observations SET resolved_at=?,last_seen_at=? "
                "WHERE task_id=? AND phase=? AND evidence_fingerprint=? AND resolved_at IS NULL",
                (_now(), _now(), identity, phase.strip(), evidence_fingerprint.strip()),
            )
            stale_cases: list[sqlite3.Row] = []
            if cursor.rowcount:
                stale_cases = connection.execute(
                    "SELECT case_id FROM incidents WHERE task_id=? AND phase=? "
                    "AND evidence_fingerprint=? AND status IN (?,?)",
                    (
                        identity,
                        phase.strip(),
                        evidence_fingerprint.strip(),
                        IncidentStatus.OPEN.value,
                        IncidentStatus.WAITING_RESOURCE.value,
                    ),
                ).fetchall()
                for case in stale_cases:
                    connection.execute(
                        "UPDATE incidents SET status=?,updated_at=? WHERE case_id=?",
                        (IncidentStatus.STALE.value, _now(), case["case_id"]),
                    )
                    connection.execute(
                        "DELETE FROM resource_locks WHERE case_id=?",
                        (case["case_id"],),
                    )
                    self.event(
                        connection,
                        "incident",
                        str(case["case_id"]),
                        "resolved-before-arbitration",
                        {"task_id": identity},
                    )
                self.event(
                    connection,
                    "task",
                    identity,
                    "failure-resolved",
                    {"phase": phase.strip(), "fingerprint": evidence_fingerprint.strip()},
                )
        self.flush_events()
        return {
            "task_id": identity,
            "resolved": cursor.rowcount == 1,
            "incidents_staled": len(stale_cases),
        }

    def open_incident(
        self,
        *,
        task_id: str,
        task_revision: int,
        phase: str,
        error_class: str,
        evidence_fingerprint: str,
        resources: Iterable[str],
    ) -> dict[str, object]:
        identity = validate_task_id(task_id)
        resource_set = {item.strip() for item in resources if item.strip()}
        resource_set.add(f"task:{identity}")
        normalized_resources = sorted(resource_set)
        key = incident_key(
            task_id=identity,
            task_revision=task_revision,
            phase=phase,
            error_class=error_class,
            evidence_fingerprint=evidence_fingerprint,
            resources=normalized_resources,
        )
        timestamp = _now()
        with self.transaction() as connection:
            task = self.task(identity, connection)
            if int(task["revision"]) != task_revision:
                raise RuntimeError("incident was observed on a stale task revision")
            existing = connection.execute(
                "SELECT * FROM incidents WHERE incident_key=?", (key,)
            ).fetchone()
            if existing is not None:
                return {
                    "case_id": existing["case_id"],
                    "status": existing["status"],
                    "deduplicated": True,
                    "incident_key": key,
                }
            active = connection.execute(
                "SELECT case_id,status,incident_key FROM incidents "
                "WHERE task_id=? AND status IN (?,?,?,?,?,?)",
                (identity, *ACTIVE_INCIDENT_STATES),
            ).fetchone()
            if active is not None:
                return {
                    "case_id": active["case_id"],
                    "status": active["status"],
                    "deduplicated": True,
                    "incident_key": active["incident_key"],
                    "requested_incident_key": key,
                    "reason": "one-active-case-per-task",
                }
            case_id = "a-" + uuid.uuid4().hex[:12]
            connection.execute(
                "INSERT INTO incidents(case_id,incident_key,task_id,task_revision,phase,error_class,"
                "evidence_fingerprint,resources_json,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    key,
                    identity,
                    task_revision,
                    phase.strip(),
                    error_class.strip(),
                    evidence_fingerprint.strip(),
                    _json(normalized_resources),
                    IncidentStatus.OPEN.value,
                    timestamp,
                    timestamp,
                ),
            )
            self.event(connection, "incident", case_id, "opened", {"task_id": identity, "resources": normalized_resources})
        self.flush_events()
        return {"case_id": case_id, "status": IncidentStatus.OPEN.value, "deduplicated": False, "incident_key": key}

    def claim_incident(
        self,
        *,
        case_id: str,
        expected_task_revision: int,
        reservation_owner: str,
    ) -> dict[str, object]:
        if not reservation_owner.strip():
            raise ValueError("incident claim requires a reservation owner before arbiter creation")
        result: dict[str, object]
        with self.transaction() as connection:
            case = connection.execute("SELECT * FROM incidents WHERE case_id=?", (case_id,)).fetchone()
            if case is None:
                raise KeyError(f"unknown incident: {case_id}")
            task = self.task(case["task_id"], connection)
            if int(task["revision"]) != expected_task_revision or int(case["task_revision"]) != expected_task_revision:
                connection.execute(
                    "UPDATE incidents SET status=?,updated_at=? WHERE case_id=?",
                    (IncidentStatus.STALE.value, _now(), case_id),
                )
                connection.execute("DELETE FROM resource_locks WHERE case_id=?", (case_id,))
                connection.execute(
                    "UPDATE task_threads SET active=0 WHERE task_id=? AND role='arbiter'",
                    (case["task_id"],),
                )
                self.event(
                    connection,
                    "incident",
                    case_id,
                    "stale",
                    {"stage": "claim", "expected_task_revision": expected_task_revision},
                )
                result = {"case_id": case_id, "status": IncidentStatus.STALE.value}
            elif case["status"] == IncidentStatus.CLAIMED.value:
                if case["reservation_owner"] != reservation_owner.strip():
                    raise RuntimeError("incident is already reserved by a different watcher run")
                result = {
                    "case_id": case_id,
                    "status": case["status"],
                    "idempotent": True,
                }
            elif case["status"] not in {
                IncidentStatus.OPEN.value,
                IncidentStatus.WAITING_RESOURCE.value,
            }:
                result = {
                    "case_id": case_id,
                    "status": case["status"],
                    "idempotent": True,
                }
            else:
                resources = json.loads(case["resources_json"])
                conflicts = []
                for resource in resources:
                    locked = connection.execute(
                        "SELECT case_id FROM resource_locks WHERE resource=?", (resource,)
                    ).fetchone()
                    if locked is not None and locked["case_id"] != case_id:
                        conflicts.append({"resource": resource, "case_id": locked["case_id"]})
                if conflicts:
                    if case["status"] != IncidentStatus.WAITING_RESOURCE.value:
                        self.event(
                            connection,
                            "incident",
                            case_id,
                            "waiting-resource",
                            {"conflicts": conflicts},
                        )
                    connection.execute(
                        "UPDATE incidents SET status=?,updated_at=? WHERE case_id=?",
                        (IncidentStatus.WAITING_RESOURCE.value, _now(), case_id),
                    )
                    result = {
                        "case_id": case_id,
                        "status": IncidentStatus.WAITING_RESOURCE.value,
                        "conflicts": conflicts,
                    }
                else:
                    for resource in resources:
                        connection.execute(
                            "INSERT OR REPLACE INTO resource_locks(resource,case_id,acquired_at) VALUES(?,?,?)",
                            (resource, case_id, _now()),
                        )
                    connection.execute(
                        "UPDATE incidents SET status=?,reservation_owner=?,arbiter_thread_id=?,updated_at=? "
                        "WHERE case_id=?",
                        (
                            IncidentStatus.CLAIMED.value,
                            reservation_owner.strip(),
                            "",
                            _now(),
                            case_id,
                        ),
                    )
                    self.event(
                        connection,
                        "incident",
                        case_id,
                        "claimed",
                        {"reservation_owner": reservation_owner.strip()},
                    )
                    result = {
                        "case_id": case_id,
                        "status": IncidentStatus.CLAIMED.value,
                    }
        self.flush_events()
        return result

    def attach_arbiter(
        self,
        *,
        case_id: str,
        expected_task_revision: int,
        thread_id: str,
        host_id: str,
        generation: int,
        reservation_owner: str,
    ) -> dict[str, object]:
        if (
            not thread_id.strip()
            or not host_id.strip()
            or generation <= 0
            or not reservation_owner.strip()
        ):
            raise ValueError(
                "arbiter attachment requires exact thread identity, generation and reservation owner"
            )
        timestamp = _now()
        result: dict[str, object]
        with self.transaction() as connection:
            case = connection.execute(
                "SELECT * FROM incidents WHERE case_id=?", (case_id,)
            ).fetchone()
            if case is None:
                raise KeyError(f"unknown incident: {case_id}")
            task = self.task(case["task_id"], connection)
            if (
                int(task["revision"]) != expected_task_revision
                or int(case["task_revision"]) != expected_task_revision
            ):
                connection.execute(
                    "UPDATE incidents SET status=?,updated_at=? WHERE case_id=?",
                    (IncidentStatus.STALE.value, timestamp, case_id),
                )
                connection.execute("DELETE FROM resource_locks WHERE case_id=?", (case_id,))
                connection.execute(
                    "UPDATE task_threads SET active=0 WHERE task_id=? AND role='arbiter'",
                    (case["task_id"],),
                )
                self.event(
                    connection,
                    "incident",
                    case_id,
                    "stale",
                    {"stage": "attach-arbiter", "expected_task_revision": expected_task_revision},
                )
                result = {"case_id": case_id, "status": IncidentStatus.STALE.value}
            elif case["status"] != IncidentStatus.CLAIMED.value:
                raise RuntimeError("incident must be claimed before arbiter attachment")
            elif not case["reservation_owner"]:
                raise RuntimeError("incident must hold a reservation before arbiter attachment")
            elif case["reservation_owner"] != reservation_owner.strip():
                raise RuntimeError("arbiter attachment does not own the incident reservation")
            elif case["arbiter_thread_id"]:
                if case["arbiter_thread_id"] == thread_id.strip():
                    result = {
                        "case_id": case_id,
                        "status": IncidentStatus.CLAIMED.value,
                        "idempotent": True,
                    }
                else:
                    raise RuntimeError("incident already has a different arbiter")
            else:
                connection.execute(
                    "UPDATE task_threads SET active=0 WHERE task_id=? AND role='arbiter'",
                    (case["task_id"],),
                )
                connection.execute(
                    "INSERT INTO task_threads(task_id,role,generation,thread_id,host_id,created_at) "
                    "VALUES(?,'arbiter',?,?,?,?)",
                    (
                        case["task_id"],
                        generation,
                        thread_id.strip(),
                        host_id.strip(),
                        timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE incidents SET arbiter_thread_id=?,updated_at=? WHERE case_id=?",
                    (thread_id.strip(), timestamp, case_id),
                )
                self.event(
                    connection,
                    "incident",
                    case_id,
                    "arbiter-attached",
                    {"thread_id": thread_id.strip(), "generation": generation},
                )
                result = {
                    "case_id": case_id,
                    "status": IncidentStatus.CLAIMED.value,
                    "arbiter_thread_id": thread_id.strip(),
                }
        self.flush_events()
        return result

    def decide(
        self,
        *,
        case_id: str,
        expected_task_revision: int,
        decision: Mapping[str, object],
        expected_transition: str,
        evidence_digest: str,
    ) -> dict[str, object]:
        digest = validate_digest(evidence_digest)
        result: dict[str, object]
        with self.transaction() as connection:
            case = connection.execute("SELECT * FROM incidents WHERE case_id=?", (case_id,)).fetchone()
            if case is None:
                raise KeyError(f"unknown incident: {case_id}")
            task = self.task(case["task_id"], connection)
            if int(task["revision"]) != expected_task_revision or int(case["task_revision"]) != expected_task_revision:
                connection.execute("UPDATE incidents SET status=?,updated_at=? WHERE case_id=?", (IncidentStatus.STALE.value, _now(), case_id))
                connection.execute("DELETE FROM resource_locks WHERE case_id=?", (case_id,))
                connection.execute(
                    "UPDATE task_threads SET active=0 WHERE task_id=? AND role='arbiter'",
                    (case["task_id"],),
                )
                self.event(
                    connection,
                    "incident",
                    case_id,
                    "stale",
                    {"stage": "decide", "expected_task_revision": expected_task_revision},
                )
                result = {"case_id": case_id, "status": IncidentStatus.STALE.value}
            elif case["status"] not in {IncidentStatus.CLAIMED.value, IncidentStatus.DECIDED.value}:
                raise RuntimeError("incident must be claimed before decision")
            else:
                validated_decision = validate_arbiter_decision(
                    decision,
                    task_id=case["task_id"],
                    task_revision=expected_task_revision,
                    incident_key_value=case["incident_key"],
                    allowed_resources=json.loads(case["resources_json"]),
                    expected_transition=expected_transition,
                    evidence_digest=digest,
                )
                if case["status"] == IncidentStatus.DECIDED.value:
                    if (
                        json.loads(case["decision_json"]) == validated_decision
                        and case["expected_transition"] == expected_transition.strip()
                        and case["evidence_digest"] == digest
                    ):
                        result = {
                            "case_id": case_id,
                            "status": IncidentStatus.DECIDED.value,
                            "idempotent": True,
                        }
                    else:
                        raise RuntimeError("incident already has a different bounded decision")
                else:
                    connection.execute(
                        "UPDATE incidents SET status=?,decision_json=?,expected_transition=?,evidence_digest=?,updated_at=? WHERE case_id=?",
                        (IncidentStatus.DECIDED.value, _json(validated_decision), expected_transition.strip(), digest, _now(), case_id),
                    )
                    self.event(connection, "incident", case_id, "decided", {"expected_transition": expected_transition, "evidence_digest": digest})
                    result = {"case_id": case_id, "status": IncidentStatus.DECIDED.value}
        self.flush_events()
        return result

    def deliver(self, *, case_id: str) -> dict[str, object]:
        result: dict[str, object]
        with self.transaction() as connection:
            case = connection.execute("SELECT * FROM incidents WHERE case_id=?", (case_id,)).fetchone()
            if case is None:
                raise KeyError(f"unknown incident: {case_id}")
            task = self.task(case["task_id"], connection)
            if int(task["revision"]) != int(case["task_revision"]):
                connection.execute("UPDATE incidents SET status=?,updated_at=? WHERE case_id=?", (IncidentStatus.STALE.value, _now(), case_id))
                connection.execute("DELETE FROM resource_locks WHERE case_id=?", (case_id,))
                connection.execute(
                    "UPDATE task_threads SET active=0 WHERE task_id=? AND role='arbiter'",
                    (case["task_id"],),
                )
                self.event(
                    connection,
                    "incident",
                    case_id,
                    "stale",
                    {"stage": "deliver", "task_revision": int(task["revision"])},
                )
                result = {"case_id": case_id, "status": IncidentStatus.STALE.value}
            elif case["status"] != IncidentStatus.DECIDED.value:
                raise RuntimeError("only a current decision can be delivered")
            else:
                connection.execute("UPDATE incidents SET status=?,updated_at=? WHERE case_id=?", (IncidentStatus.DELIVERED.value, _now(), case_id))
                self.event(connection, "incident", case_id, "delivered", {"task_revision": case["task_revision"]})
                result = {"case_id": case_id, "status": IncidentStatus.DELIVERED.value}
        self.flush_events()
        return result

    def verify(
        self,
        *,
        case_id: str,
        observed_transition: str,
        verification_evidence_digest: str,
    ) -> dict[str, object]:
        verification_digest = validate_digest(verification_evidence_digest)
        with self.transaction() as connection:
            case = connection.execute("SELECT * FROM incidents WHERE case_id=?", (case_id,)).fetchone()
            if case is None:
                raise KeyError(f"unknown incident: {case_id}")
            if case["status"] == IncidentStatus.VERIFIED.value:
                if (
                    observed_transition.strip() == case["expected_transition"]
                    and verification_digest == case["verification_evidence_digest"]
                ):
                    return {
                        "case_id": case_id,
                        "status": IncidentStatus.VERIFIED.value,
                        "idempotent": True,
                    }
                raise RuntimeError("incident already has different verification evidence")
            if case["status"] != IncidentStatus.DELIVERED.value:
                raise RuntimeError("only a delivered decision can be verified")
            if observed_transition.strip() != case["expected_transition"]:
                raise RuntimeError("observed transition does not match the arbiter decision")
            connection.execute(
                "UPDATE incidents SET status=?,verification_evidence_digest=?,updated_at=? "
                "WHERE case_id=?",
                (
                    IncidentStatus.VERIFIED.value,
                    verification_digest,
                    _now(),
                    case_id,
                ),
            )
            self.event(
                connection,
                "incident",
                case_id,
                "verified",
                {
                    "observed_transition": observed_transition,
                    "verification_evidence_digest": verification_digest,
                },
            )
        self.flush_events()
        return {"case_id": case_id, "status": IncidentStatus.VERIFIED.value}

    def close_incident(
        self, *, case_id: str, archive_evidence_digest: str
    ) -> dict[str, object]:
        archive_digest = validate_digest(archive_evidence_digest)
        with self.transaction() as connection:
            case = connection.execute(
                "SELECT * FROM incidents WHERE case_id=?", (case_id,)
            ).fetchone()
            if case is None:
                raise KeyError(f"unknown incident: {case_id}")
            if case["status"] == IncidentStatus.CLOSED.value:
                if case["archive_evidence_digest"] != archive_digest:
                    raise RuntimeError("incident already has different archive evidence")
                return {"case_id": case_id, "status": IncidentStatus.CLOSED.value, "idempotent": True}
            if case["status"] != IncidentStatus.VERIFIED.value:
                raise RuntimeError("incident can close only after verified transition and arbiter archive")
            if not case["verification_evidence_digest"]:
                raise RuntimeError("incident verification evidence is missing")
            connection.execute(
                "UPDATE incidents SET status=?,archive_evidence_digest=?,updated_at=? WHERE case_id=?",
                (IncidentStatus.CLOSED.value, archive_digest, _now(), case_id),
            )
            connection.execute("DELETE FROM resource_locks WHERE case_id=?", (case_id,))
            if case["arbiter_thread_id"]:
                connection.execute(
                    "UPDATE task_threads SET active=0 WHERE task_id=? AND role='arbiter' AND thread_id=?",
                    (case["task_id"], case["arbiter_thread_id"]),
                )
            self.event(
                connection,
                "incident",
                case_id,
                "closed",
                {"archive_evidence_digest": archive_digest},
            )
        self.flush_events()
        return {"case_id": case_id, "status": IncidentStatus.CLOSED.value}

    def prepare_watcher(
        self,
        *,
        generation: int,
        thread_id: str,
        host_id: str,
        automation_id: str,
        max_runs: int = 720,
    ) -> dict[str, object]:
        if (
            generation <= 0
            or not thread_id.strip()
            or not host_id.strip()
            or not automation_id.strip()
            or max_runs <= 0
        ):
            raise ValueError(
                "watcher generation, exact thread/host identity and automation id are required"
            )
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM watchers WHERE generation=?", (generation,)
            ).fetchone()
            if existing is not None:
                expected = (
                    thread_id.strip(),
                    host_id.strip(),
                    automation_id.strip(),
                    max_runs,
                )
                actual = (
                    existing["thread_id"],
                    existing["host_id"],
                    existing["automation_id"],
                    int(existing["max_runs"]),
                )
                if actual != expected:
                    raise RuntimeError("watcher generation already has different immutable identity")
                return {
                    "generation": generation,
                    "status": existing["status"],
                    "idempotent": True,
                }
            connection.execute(
                "INSERT INTO watchers(generation,thread_id,host_id,automation_id,status,max_runs,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    generation,
                    thread_id.strip(),
                    host_id.strip(),
                    automation_id.strip(),
                    "PREPARED",
                    max_runs,
                    _now(),
                ),
            )
            self.event(connection, "watcher", str(generation), "prepared", {"thread_id": thread_id})
        self.flush_events()
        return {"generation": generation, "status": "PREPARED"}

    def smoke_watcher(self, *, generation: int, evidence_digest: str) -> dict[str, object]:
        digest = validate_digest(evidence_digest)
        with self.transaction() as connection:
            watcher = connection.execute(
                "SELECT * FROM watchers WHERE generation=?", (generation,)
            ).fetchone()
            if watcher is None or watcher["status"] not in {"PREPARED", "ACTIVE"}:
                raise RuntimeError("watcher smoke requires a prepared generation")
            connection.execute(
                "UPDATE watchers SET smoke_digest=?,smoke_at=? WHERE generation=?",
                (digest, _now(), generation),
            )
            self.event(
                connection,
                "watcher",
                str(generation),
                "smoke-passed",
                {"evidence_digest": digest},
            )
        self.flush_events()
        return {"generation": generation, "status": "SMOKE_PASSED", "evidence_digest": digest}

    def activate_watcher(self, *, generation: int) -> dict[str, object]:
        with self.transaction() as connection:
            prepared = connection.execute("SELECT * FROM watchers WHERE generation=?", (generation,)).fetchone()
            if prepared is None or prepared["status"] not in {"PREPARED", "ACTIVE"}:
                raise RuntimeError("watcher must be prepared before activation")
            if not prepared["smoke_digest"] or not prepared["smoke_at"]:
                raise RuntimeError("watcher must pass a recorded smoke before activation")
            connection.execute("UPDATE watchers SET status='RETIRED',retired_at=? WHERE status='ACTIVE' AND generation<>?", (_now(), generation))
            connection.execute(
                "DELETE FROM runtime_leases WHERE name='watcher-run' AND generation<>?",
                (generation,),
            )
            connection.execute("UPDATE watchers SET status='ACTIVE',activated_at=?,retired_at=NULL WHERE generation=?", (_now(), generation))
            self.event(connection, "watcher", str(generation), "activated", {"previous_retired": True})
        self.flush_events()
        return {"generation": generation, "status": "ACTIVE"}

    def begin_run(self, *, generation: int, owner: str, lease_seconds: int) -> dict[str, object]:
        if lease_seconds <= 0 or lease_seconds > 600:
            raise ValueError("lease_seconds must be between 1 and 600")
        now = time.time()
        with self.transaction() as connection:
            active = connection.execute("SELECT generation FROM watchers WHERE status='ACTIVE'").fetchone()
            if active is None or int(active["generation"]) != generation:
                return {"acquired": False, "reason": "stale-watcher-generation"}
            lease = connection.execute("SELECT * FROM runtime_leases WHERE name='watcher-run'").fetchone()
            if lease is not None and float(lease["expires_at"]) > now and lease["owner"] != owner:
                return {"acquired": False, "reason": "overlapping-run", "owner": lease["owner"]}
            if (
                lease is not None
                and float(lease["expires_at"]) > now
                and lease["owner"] == owner
                and int(lease["generation"]) == generation
            ):
                watcher = connection.execute(
                    "SELECT run_count,max_runs FROM watchers WHERE generation=?",
                    (generation,),
                ).fetchone()
                return {
                    "acquired": True,
                    "generation": generation,
                    "owner": owner,
                    "run_count": int(watcher["run_count"]),
                    "max_runs": int(watcher["max_runs"]),
                    "rotation_due": int(watcher["run_count"]) >= int(watcher["max_runs"]),
                    "idempotent": True,
                }
            connection.execute(
                "INSERT INTO runtime_leases(name,owner,generation,expires_at) VALUES('watcher-run',?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,generation=excluded.generation,expires_at=excluded.expires_at",
                (owner, generation, now + lease_seconds),
            )
            connection.execute(
                "UPDATE watchers SET run_count=run_count+1,last_run_at=? WHERE generation=?",
                (_now(), generation),
            )
            watcher = connection.execute(
                "SELECT run_count,max_runs FROM watchers WHERE generation=?", (generation,)
            ).fetchone()
        return {
            "acquired": True,
            "generation": generation,
            "owner": owner,
            "run_count": int(watcher["run_count"]),
            "max_runs": int(watcher["max_runs"]),
            "rotation_due": int(watcher["run_count"]) >= int(watcher["max_runs"]),
        }

    def end_run(self, *, generation: int, owner: str) -> dict[str, object]:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM runtime_leases WHERE name='watcher-run' AND owner=? AND generation=?",
                (owner, generation),
            )
        return {"released": cursor.rowcount == 1}

    def active_tasks(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status<>? ORDER BY created_at",
                (TaskStatus.ACCEPTED.value,),
            ).fetchall()
            result = []
            for row in rows:
                payload = dict(row)
                payload["threads"] = [
                    dict(item)
                    for item in connection.execute(
                        "SELECT role,generation,thread_id,host_id,active FROM task_threads WHERE task_id=? ORDER BY role,generation",
                        (row["task_id"],),
                    ).fetchall()
                ]
                payload["prs"] = [
                    dict(item)
                    for item in connection.execute(
                        "SELECT pr_number,role,head_sha,state FROM task_prs WHERE task_id=? ORDER BY pr_number",
                        (row["task_id"],),
                    ).fetchall()
                ]
                result.append(payload)
            return result

    def snapshot(self) -> dict[str, object]:
        with self.connect() as connection:
            watcher_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM watchers ORDER BY generation"
                ).fetchall()
            ]
            incidents = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM incidents WHERE status IN (?,?,?,?,?,?) ORDER BY created_at",
                    ACTIVE_INCIDENT_STATES,
                ).fetchall()
            ]
        for item in incidents:
            item["resources"] = json.loads(item.pop("resources_json"))
            if item.get("decision_json"):
                item["decision"] = json.loads(item["decision_json"])
            item.pop("decision_json", None)
        return {
            "tasks": self.active_tasks(),
            "incidents": incidents,
            "watchers": watcher_rows,
            "integrity": self.integrity(),
        }

    def report(self) -> str:
        blocks = []
        for task in self.active_tasks():
            status = TaskStatus(task["status"])
            lines = [
                f"Статус: {report_status(status)}",
                f"Задача: {task['title']}",
                f"Прогресс: ≈{task['progress_percent']}% · Осталось: ≈{task['eta_text']}",
                f"С прошлого отчёта: {task['last_delta']}",
                f"Сейчас: {task['current_action']}",
            ]
            if status == TaskStatus.AWAITING_HUMAN and task["blocker"]:
                lines.append(f"Блокер: {task['blocker']}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) if blocks else "Активных задач нет."

    def integrity(self) -> dict[str, object]:
        with self.connect() as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            active_watchers = int(connection.execute("SELECT count(*) FROM watchers WHERE status='ACTIVE'").fetchone()[0])
            stale_locks = int(
                connection.execute(
                    "SELECT count(*) FROM resource_locks l LEFT JOIN incidents i ON i.case_id=l.case_id "
                    "WHERE i.case_id IS NULL OR i.status NOT IN "
                    "('OPEN','WAITING_RESOURCE','CLAIMED','DECIDED','DELIVERED','VERIFIED')"
                ).fetchone()[0]
            )
            event_count = int(connection.execute("SELECT count(*) FROM events").fetchone()[0])
            invalid_human_blocks = int(
                connection.execute(
                    "SELECT count(*) FROM tasks WHERE "
                    "(status='AWAITING_HUMAN' AND (blocker='' OR human_reason='')) OR "
                    "(status<>'AWAITING_HUMAN' AND (blocker<>'' OR human_reason<>''))"
                ).fetchone()[0]
            )
            unsmoked_active_watchers = int(
                connection.execute(
                    "SELECT count(*) FROM watchers WHERE status='ACTIVE' "
                    "AND (smoke_digest='' OR smoke_at IS NULL)"
                ).fetchone()[0]
            )
            unproven_verified_incidents = int(
                connection.execute(
                    "SELECT count(*) FROM incidents WHERE status='VERIFIED' "
                    "AND verification_evidence_digest=''"
                ).fetchone()[0]
            )
        return {
            "ok": (
                quick == "ok"
                and active_watchers <= 1
                and stale_locks == 0
                and invalid_human_blocks == 0
                and unsmoked_active_watchers == 0
                and unproven_verified_incidents == 0
            ),
            "sqlite": quick,
            "active_watchers": active_watchers,
            "stale_locks": stale_locks,
            "invalid_human_blocks": invalid_human_blocks,
            "unsmoked_active_watchers": unsmoked_active_watchers,
            "unproven_verified_incidents": unproven_verified_incidents,
            "event_count": event_count,
        }


def _registry(args: argparse.Namespace) -> Registry:
    home = Path(args.home or os.environ.get("WB_CORE_ORCHESTRATOR_HOME") or DEFAULT_HOME)
    registry = Registry(home)
    registry.initialize()
    return registry


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _passport(args: argparse.Namespace) -> dict[str, Any]:
    if args.passport_file:
        return _load_object(Path(args.passport_file).read_text(encoding="utf-8"), field="passport")
    return _load_object(args.passport_json, field="passport")


def _serve(registry: Registry, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("dashboard is read-only localhost only")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in {"/", "/index.html", "/api/tasks"}:
                self.send_error(404)
                return
            tasks = registry.active_tasks()
            if self.path == "/api/tasks":
                body = json.dumps(tasks, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                cards = []
                for task in tasks:
                    cards.append(
                        "<article><h2>" + html.escape(task["title"]) + "</h2>"
                        "<p><strong>" + html.escape(report_status(TaskStatus(task["status"]))) + "</strong></p>"
                        "<p>Прогресс: " + str(task["progress_percent"]) + "% · " + html.escape(task["eta_text"]) + "</p>"
                        "<p>Сейчас: " + html.escape(task["current_action"]) + "</p></article>"
                    )
                body = (
                    "<!doctype html><meta charset=utf-8><title>wb-core tasks</title>"
                    "<style>body{font:16px system-ui;max-width:900px;margin:40px auto;background:#f5f5f5}"
                    "article{background:white;padding:18px;margin:12px 0;border-radius:12px}</style>"
                    "<h1>Активные задачи wb-core</h1>" + ("".join(cards) or "<p>Активных задач нет.</p>")
                ).encode("utf-8")
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *values: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"http://{host}:{port}")
    server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="wb-core local Codex task registry")
    parser.add_argument("--home", default="")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")

    register = commands.add_parser("register-task")
    register.add_argument("--task-id", default="")
    register.add_argument("--title", required=True)
    register.add_argument("--repo", default="orenvlad-ai/wb-core")
    register.add_argument("--project-id", default="")
    register.add_argument("--objective", required=True)
    register.add_argument("--passport-file")
    register.add_argument("--passport-json", default="{}")
    register.add_argument("--curator-thread", required=True)
    register.add_argument("--executor-thread", required=True)
    register.add_argument("--host-id", required=True)

    add_thread = commands.add_parser("add-thread")
    add_thread.add_argument("--task-id", required=True)
    add_thread.add_argument("--role", choices=("curator", "executor", "arbiter"), required=True)
    add_thread.add_argument("--generation", type=int, required=True)
    add_thread.add_argument("--thread-id", required=True)
    add_thread.add_argument("--host-id", required=True)

    update = commands.add_parser("update-task")
    update.add_argument("--task-id", required=True)
    update.add_argument("--expected-revision", type=int, required=True)
    update.add_argument("--status", choices=[item.value for item in TaskStatus], required=True)
    update.add_argument("--progress", type=int)
    update.add_argument("--eta")
    update.add_argument("--delta")
    update.add_argument("--current")
    update.add_argument("--blocker")
    update.add_argument("--human-reason", choices=sorted(STRICT_HUMAN_REASONS))
    update.add_argument("--repo-owned-remediation-available", action="store_true")
    update.add_argument("--remediation-exhausted", action="store_true")

    link = commands.add_parser("link-pr")
    link.add_argument("--task-id", required=True)
    link.add_argument("--pr", type=int, required=True)
    link.add_argument("--role", default="implementation")
    link.add_argument("--head-sha", default="")
    link.add_argument("--state", default="open")

    accept = commands.add_parser("accept")
    accept.add_argument("--task-id", required=True)
    accept.add_argument("--expected-revision", type=int, required=True)

    observe = commands.add_parser("record-failure")
    observe.add_argument("--task-id", required=True)
    observe.add_argument("--task-revision", type=int, required=True)
    observe.add_argument("--phase", required=True)
    observe.add_argument("--error-class", required=True)
    observe.add_argument("--evidence-fingerprint", required=True)
    observe.add_argument("--transient", action="store_true")
    observe.add_argument("--empty-system-error", action="store_true")
    observe.add_argument("--repo-owned-remediation-available", action="store_true")
    observe.add_argument("--remediation-exhausted", action="store_true")
    observe.add_argument("--human-reason", choices=sorted(STRICT_HUMAN_REASONS), default="")

    resolve = commands.add_parser("resolve-failure")
    resolve.add_argument("--task-id", required=True)
    resolve.add_argument("--phase", required=True)
    resolve.add_argument("--evidence-fingerprint", required=True)

    incident = commands.add_parser("open-incident")
    incident.add_argument("--task-id", required=True)
    incident.add_argument("--task-revision", type=int, required=True)
    incident.add_argument("--phase", required=True)
    incident.add_argument("--error-class", required=True)
    incident.add_argument("--evidence-fingerprint", required=True)
    incident.add_argument("--resource", action="append", default=[])

    claim = commands.add_parser("claim-incident")
    claim.add_argument("--case-id", required=True)
    claim.add_argument("--expected-task-revision", type=int, required=True)
    claim.add_argument("--reservation-owner", required=True)

    attach = commands.add_parser("attach-arbiter")
    attach.add_argument("--case-id", required=True)
    attach.add_argument("--expected-task-revision", type=int, required=True)
    attach.add_argument("--thread-id", required=True)
    attach.add_argument("--host-id", required=True)
    attach.add_argument("--generation", type=int, required=True)
    attach.add_argument("--reservation-owner", required=True)

    decide = commands.add_parser("decide")
    decide.add_argument("--case-id", required=True)
    decide.add_argument("--expected-task-revision", type=int, required=True)
    decide.add_argument("--decision-file", required=True)
    decide.add_argument("--expected-transition", required=True)
    decide.add_argument("--evidence-digest", required=True)

    deliver = commands.add_parser("deliver")
    deliver.add_argument("--case-id", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--case-id", required=True)
    verify.add_argument("--observed-transition", required=True)
    verify.add_argument("--verification-evidence-digest", required=True)
    close = commands.add_parser("close-incident")
    close.add_argument("--case-id", required=True)
    close.add_argument("--archive-evidence-digest", required=True)

    watcher = commands.add_parser("prepare-watcher")
    watcher.add_argument("--generation", type=int, required=True)
    watcher.add_argument("--thread-id", required=True)
    watcher.add_argument("--host-id", required=True)
    watcher.add_argument("--automation-id", required=True)
    watcher.add_argument("--max-runs", type=int, default=720)
    smoke = commands.add_parser("smoke-watcher")
    smoke.add_argument("--generation", type=int, required=True)
    smoke.add_argument("--evidence-digest", required=True)
    activate = commands.add_parser("activate-watcher")
    activate.add_argument("--generation", type=int, required=True)
    begin = commands.add_parser("begin-run")
    begin.add_argument("--generation", type=int, required=True)
    begin.add_argument("--owner", required=True)
    begin.add_argument("--lease-seconds", type=int, default=540)
    end = commands.add_parser("end-run")
    end.add_argument("--generation", type=int, required=True)
    end.add_argument("--owner", required=True)

    commands.add_parser("list")
    commands.add_parser("snapshot")
    commands.add_parser("report")
    commands.add_parser("integrity")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    registry = _registry(args)
    try:
        if args.command == "init":
            result: object = {"status": "initialized", "home": str(registry.home), **registry.integrity()}
        elif args.command == "register-task":
            task_id = args.task_id or "t-" + uuid.uuid4().hex[:12]
            result = registry.register_task(
                task_id=task_id,
                title=args.title,
                repo=args.repo,
                project_id=args.project_id,
                objective=args.objective,
                passport=_passport(args),
                curator_thread_id=args.curator_thread,
                executor_thread_id=args.executor_thread,
                host_id=args.host_id,
            )
        elif args.command == "add-thread":
            result = registry.add_thread(task_id=args.task_id, role=args.role, generation=args.generation, thread_id=args.thread_id, host_id=args.host_id)
        elif args.command == "update-task":
            result = registry.update_task(
                task_id=args.task_id,
                expected_revision=args.expected_revision,
                status=TaskStatus(args.status),
                progress=args.progress,
                eta=args.eta,
                delta=args.delta,
                current=args.current,
                blocker=args.blocker,
                human_reason=args.human_reason,
                repo_owned_remediation_available=args.repo_owned_remediation_available,
                remediation_exhausted=args.remediation_exhausted,
            )
        elif args.command == "link-pr":
            result = registry.link_pr(task_id=args.task_id, pr=args.pr, role=args.role, head_sha=args.head_sha, state=args.state)
        elif args.command == "accept":
            result = registry.accept(task_id=args.task_id, expected_revision=args.expected_revision)
        elif args.command == "record-failure":
            result = registry.record_failure(
                task_id=args.task_id,
                task_revision=args.task_revision,
                phase=args.phase,
                error_class=args.error_class,
                evidence_fingerprint=args.evidence_fingerprint,
                transient=args.transient,
                empty_system_error=args.empty_system_error,
                repo_owned_remediation_available=args.repo_owned_remediation_available,
                remediation_exhausted=args.remediation_exhausted,
                human_reason=args.human_reason,
            )
        elif args.command == "resolve-failure":
            result = registry.resolve_failure(
                task_id=args.task_id,
                phase=args.phase,
                evidence_fingerprint=args.evidence_fingerprint,
            )
        elif args.command == "open-incident":
            result = registry.open_incident(task_id=args.task_id, task_revision=args.task_revision, phase=args.phase, error_class=args.error_class, evidence_fingerprint=args.evidence_fingerprint, resources=args.resource)
        elif args.command == "claim-incident":
            result = registry.claim_incident(
                case_id=args.case_id,
                expected_task_revision=args.expected_task_revision,
                reservation_owner=args.reservation_owner,
            )
        elif args.command == "attach-arbiter":
            result = registry.attach_arbiter(
                case_id=args.case_id,
                expected_task_revision=args.expected_task_revision,
                thread_id=args.thread_id,
                host_id=args.host_id,
                generation=args.generation,
                reservation_owner=args.reservation_owner,
            )
        elif args.command == "decide":
            decision = _load_object(Path(args.decision_file).read_text(encoding="utf-8"), field="decision")
            result = registry.decide(case_id=args.case_id, expected_task_revision=args.expected_task_revision, decision=decision, expected_transition=args.expected_transition, evidence_digest=args.evidence_digest)
        elif args.command == "deliver":
            result = registry.deliver(case_id=args.case_id)
        elif args.command == "verify":
            result = registry.verify(
                case_id=args.case_id,
                observed_transition=args.observed_transition,
                verification_evidence_digest=args.verification_evidence_digest,
            )
        elif args.command == "close-incident":
            result = registry.close_incident(
                case_id=args.case_id,
                archive_evidence_digest=args.archive_evidence_digest,
            )
        elif args.command == "prepare-watcher":
            result = registry.prepare_watcher(
                generation=args.generation,
                thread_id=args.thread_id,
                host_id=args.host_id,
                automation_id=args.automation_id,
                max_runs=args.max_runs,
            )
        elif args.command == "smoke-watcher":
            result = registry.smoke_watcher(
                generation=args.generation,
                evidence_digest=args.evidence_digest,
            )
        elif args.command == "activate-watcher":
            result = registry.activate_watcher(generation=args.generation)
        elif args.command == "begin-run":
            result = registry.begin_run(generation=args.generation, owner=args.owner, lease_seconds=args.lease_seconds)
        elif args.command == "end-run":
            result = registry.end_run(generation=args.generation, owner=args.owner)
        elif args.command == "list":
            result = registry.active_tasks()
        elif args.command == "snapshot":
            result = registry.snapshot()
        elif args.command == "report":
            print(registry.report())
            return 0
        elif args.command == "integrity":
            result = registry.integrity()
        elif args.command == "serve":
            _serve(registry, args.host, args.port)
            return 0
        else:
            raise RuntimeError(f"unsupported command: {args.command}")
        _print(result)
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
