"""Local passive registry for wb-core Codex task orchestration.

The registry stores durable task/incident facts and exposes a read-only local
dashboard.  It never calls Codex or GitHub by itself; the OpenAI-native Watcher
is the only router and GitHub Release Train remains the release actuator.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
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
    IncidentStatus,
    TaskStatus,
    canonical_digest,
    incident_key,
    report_status,
    transition_allowed,
    validate_digest,
    validate_task_id,
)


DEFAULT_HOME = Path.home() / ".wb-core" / "orchestrator" / "v1"
ACTIVE_INCIDENT_STATES = (
    IncidentStatus.OPEN.value,
    IncidentStatus.WAITING_RESOURCE.value,
    IncidentStatus.CLAIMED.value,
    IncidentStatus.DECIDED.value,
    IncidentStatus.DELIVERED.value,
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
    UNIQUE(thread_id)
);
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
    arbiter_thread_id TEXT NOT NULL DEFAULT '',
    decision_json TEXT NOT NULL DEFAULT '',
    expected_transition TEXT NOT NULL DEFAULT '',
    evidence_digest TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_incident_per_task
ON incidents(task_id) WHERE status IN ('OPEN','WAITING_RESOURCE','CLAIMED','DECIDED','DELIVERED');
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
    last_run_at TEXT,
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
            watcher_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(watchers)")
            }
            if "run_count" not in watcher_columns:
                connection.execute(
                    "ALTER TABLE watchers ADD COLUMN run_count INTEGER NOT NULL DEFAULT 0"
                )
            if "last_run_at" not in watcher_columns:
                connection.execute("ALTER TABLE watchers ADD COLUMN last_run_at TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')"
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
        last_seq = 0
        if self.event_path.exists():
            with self.event_path.open("rb") as handle:
                for line in handle:
                    if line.strip():
                        try:
                            last_seq = int(json.loads(line).get("seq") or last_seq)
                        except (ValueError, json.JSONDecodeError):
                            raise RuntimeError("events.jsonl is corrupt")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq", (last_seq,)
            ).fetchall()
        if not rows:
            return 0
        descriptor = os.open(self.event_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                descriptor = -1
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
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return len(rows)

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
        if not all(value.strip() for value in (title, repo, objective, curator_thread_id, executor_thread_id)):
            raise ValueError("task registration requires title, repo, objective and exact thread ids")
        timestamp = _now()
        digest = canonical_digest(passport)
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id,title,repo,project_id,objective,passport_json,"
                "passport_digest,status,curator_thread_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    title.strip(),
                    repo.strip(),
                    project_id.strip(),
                    objective.strip(),
                    _json(passport),
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
        if role not in {"curator", "executor", "arbiter"} or generation <= 0 or not thread_id.strip():
            raise ValueError("invalid thread identity")
        timestamp = _now()
        with self.transaction() as connection:
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
            }
            if not 0 <= int(values["progress_percent"]) <= 100:
                raise ValueError("progress must be between 0 and 100")
            if status != TaskStatus.AWAITING_HUMAN and blocker is None:
                values["blocker"] = ""
            connection.execute(
                "UPDATE tasks SET status=?,revision=?,progress_percent=?,eta_text=?,"
                "last_delta=?,current_action=?,blocker=?,updated_at=? WHERE task_id=?",
                (
                    status.value,
                    next_revision,
                    values["progress_percent"],
                    values["eta_text"],
                    values["last_delta"],
                    values["current_action"],
                    values["blocker"],
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
            if TaskStatus(row["status"]) != TaskStatus.DONE_AWAITING_ACCEPTANCE:
                raise RuntimeError("only a completed task can be accepted")
            revision = expected_revision + 1
            timestamp = _now()
            connection.execute(
                "UPDATE tasks SET status=?,revision=?,accepted_at=?,updated_at=? WHERE task_id=?",
                (TaskStatus.ACCEPTED.value, revision, timestamp, timestamp, validate_task_id(task_id)),
            )
            self.event(connection, "task", validate_task_id(task_id), "accepted", {"revision": revision})
        self.flush_events()
        return {"task_id": validate_task_id(task_id), "status": TaskStatus.ACCEPTED.value, "revision": revision}

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
        normalized_resources = sorted({item.strip() for item in resources if item.strip()}) or [
            f"task:{identity}"
        ]
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
                "SELECT case_id,status FROM incidents WHERE task_id=? AND status IN (?,?,?,?,?)",
                (identity, *ACTIVE_INCIDENT_STATES),
            ).fetchone()
            if active is not None:
                return {
                    "case_id": active["case_id"],
                    "status": active["status"],
                    "deduplicated": True,
                    "incident_key": key,
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
        arbiter_thread_id: str,
    ) -> dict[str, object]:
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
                return {"case_id": case_id, "status": IncidentStatus.STALE.value}
            if case["status"] not in {IncidentStatus.OPEN.value, IncidentStatus.WAITING_RESOURCE.value}:
                return {"case_id": case_id, "status": case["status"], "idempotent": True}
            resources = json.loads(case["resources_json"])
            conflicts = []
            for resource in resources:
                locked = connection.execute(
                    "SELECT case_id FROM resource_locks WHERE resource=?", (resource,)
                ).fetchone()
                if locked is not None and locked["case_id"] != case_id:
                    conflicts.append({"resource": resource, "case_id": locked["case_id"]})
            if conflicts:
                connection.execute(
                    "UPDATE incidents SET status=?,updated_at=? WHERE case_id=?",
                    (IncidentStatus.WAITING_RESOURCE.value, _now(), case_id),
                )
                return {"case_id": case_id, "status": IncidentStatus.WAITING_RESOURCE.value, "conflicts": conflicts}
            for resource in resources:
                connection.execute(
                    "INSERT OR REPLACE INTO resource_locks(resource,case_id,acquired_at) VALUES(?,?,?)",
                    (resource, case_id, _now()),
                )
            connection.execute(
                "UPDATE incidents SET status=?,arbiter_thread_id=?,updated_at=? WHERE case_id=?",
                (IncidentStatus.CLAIMED.value, arbiter_thread_id.strip(), _now(), case_id),
            )
            self.event(connection, "incident", case_id, "claimed", {"arbiter_thread_id": arbiter_thread_id})
        self.flush_events()
        return {"case_id": case_id, "status": IncidentStatus.CLAIMED.value}

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
        with self.transaction() as connection:
            case = connection.execute("SELECT * FROM incidents WHERE case_id=?", (case_id,)).fetchone()
            if case is None:
                raise KeyError(f"unknown incident: {case_id}")
            task = self.task(case["task_id"], connection)
            if int(task["revision"]) != expected_task_revision or int(case["task_revision"]) != expected_task_revision:
                connection.execute("UPDATE incidents SET status=?,updated_at=? WHERE case_id=?", (IncidentStatus.STALE.value, _now(), case_id))
                connection.execute("DELETE FROM resource_locks WHERE case_id=?", (case_id,))
                return {"case_id": case_id, "status": IncidentStatus.STALE.value}
            if case["status"] not in {IncidentStatus.CLAIMED.value, IncidentStatus.DECIDED.value}:
                raise RuntimeError("incident must be claimed before decision")
            connection.execute(
                "UPDATE incidents SET status=?,decision_json=?,expected_transition=?,evidence_digest=?,updated_at=? WHERE case_id=?",
                (IncidentStatus.DECIDED.value, _json(decision), expected_transition.strip(), digest, _now(), case_id),
            )
            self.event(connection, "incident", case_id, "decided", {"expected_transition": expected_transition, "evidence_digest": digest})
        self.flush_events()
        return {"case_id": case_id, "status": IncidentStatus.DECIDED.value}

    def deliver(self, *, case_id: str) -> dict[str, object]:
        with self.transaction() as connection:
            case = connection.execute("SELECT * FROM incidents WHERE case_id=?", (case_id,)).fetchone()
            if case is None:
                raise KeyError(f"unknown incident: {case_id}")
            task = self.task(case["task_id"], connection)
            if int(task["revision"]) != int(case["task_revision"]):
                connection.execute("UPDATE incidents SET status=?,updated_at=? WHERE case_id=?", (IncidentStatus.STALE.value, _now(), case_id))
                connection.execute("DELETE FROM resource_locks WHERE case_id=?", (case_id,))
                return {"case_id": case_id, "status": IncidentStatus.STALE.value}
            if case["status"] != IncidentStatus.DECIDED.value:
                raise RuntimeError("only a current decision can be delivered")
            connection.execute("UPDATE incidents SET status=?,updated_at=? WHERE case_id=?", (IncidentStatus.DELIVERED.value, _now(), case_id))
            self.event(connection, "incident", case_id, "delivered", {"task_revision": case["task_revision"]})
        self.flush_events()
        return {"case_id": case_id, "status": IncidentStatus.DELIVERED.value}

    def verify(self, *, case_id: str, observed_transition: str) -> dict[str, object]:
        with self.transaction() as connection:
            case = connection.execute("SELECT * FROM incidents WHERE case_id=?", (case_id,)).fetchone()
            if case is None:
                raise KeyError(f"unknown incident: {case_id}")
            if case["status"] != IncidentStatus.DELIVERED.value:
                raise RuntimeError("only a delivered decision can be verified")
            if observed_transition.strip() != case["expected_transition"]:
                raise RuntimeError("observed transition does not match the arbiter decision")
            connection.execute(
                "UPDATE incidents SET status=?,updated_at=? WHERE case_id=?",
                (IncidentStatus.CLOSED.value, _now(), case_id),
            )
            connection.execute("DELETE FROM resource_locks WHERE case_id=?", (case_id,))
            self.event(connection, "incident", case_id, "closed", {"observed_transition": observed_transition})
        self.flush_events()
        return {"case_id": case_id, "status": IncidentStatus.CLOSED.value}

    def prepare_watcher(self, *, generation: int, thread_id: str, host_id: str, automation_id: str) -> dict[str, object]:
        if generation <= 0 or not thread_id.strip():
            raise ValueError("watcher generation and thread_id are required")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO watchers(generation,thread_id,host_id,automation_id,status,created_at) VALUES(?,?,?,?,?,?)",
                (generation, thread_id.strip(), host_id.strip(), automation_id.strip(), "PREPARED", _now()),
            )
            self.event(connection, "watcher", str(generation), "prepared", {"thread_id": thread_id})
        self.flush_events()
        return {"generation": generation, "status": "PREPARED"}

    def activate_watcher(self, *, generation: int) -> dict[str, object]:
        with self.transaction() as connection:
            prepared = connection.execute("SELECT * FROM watchers WHERE generation=?", (generation,)).fetchone()
            if prepared is None or prepared["status"] not in {"PREPARED", "ACTIVE"}:
                raise RuntimeError("watcher must be prepared before activation")
            connection.execute("UPDATE watchers SET status='RETIRED',retired_at=? WHERE status='ACTIVE' AND generation<>?", (_now(), generation))
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
            connection.execute(
                "INSERT INTO runtime_leases(name,owner,generation,expires_at) VALUES('watcher-run',?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,generation=excluded.generation,expires_at=excluded.expires_at",
                (owner, generation, now + lease_seconds),
            )
            connection.execute(
                "UPDATE watchers SET run_count=run_count+1,last_run_at=? WHERE generation=?",
                (_now(), generation),
            )
        return {"acquired": True, "generation": generation, "owner": owner}

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
                    "SELECT * FROM incidents WHERE status IN (?,?,?,?,?) ORDER BY created_at",
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
                "",
                f"Задача: {task['title']}",
                f"Прогресс: ≈{task['progress_percent']}% · Осталось: ≈{task['eta_text']}",
                f"С прошлого отчёта: {task['last_delta']}",
                f"Сейчас: {task['current_action']}",
            ]
            if task["blocker"]:
                lines.append(f"Блокер: {task['blocker']}")
            blocks.append("\n".join(lines))
        return "\n\n---\n\n".join(blocks) if blocks else "Активных задач нет."

    def integrity(self) -> dict[str, object]:
        with self.connect() as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            active_watchers = int(connection.execute("SELECT count(*) FROM watchers WHERE status='ACTIVE'").fetchone()[0])
            stale_locks = int(
                connection.execute(
                    "SELECT count(*) FROM resource_locks l LEFT JOIN incidents i ON i.case_id=l.case_id "
                    "WHERE i.case_id IS NULL OR i.status NOT IN ('OPEN','WAITING_RESOURCE','CLAIMED','DECIDED','DELIVERED')"
                ).fetchone()[0]
            )
            event_count = int(connection.execute("SELECT count(*) FROM events").fetchone()[0])
        return {
            "ok": quick == "ok" and active_watchers <= 1 and stale_locks == 0,
            "sqlite": quick,
            "active_watchers": active_watchers,
            "stale_locks": stale_locks,
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
    register.add_argument("--host-id", default="")

    add_thread = commands.add_parser("add-thread")
    add_thread.add_argument("--task-id", required=True)
    add_thread.add_argument("--role", choices=("curator", "executor", "arbiter"), required=True)
    add_thread.add_argument("--generation", type=int, required=True)
    add_thread.add_argument("--thread-id", required=True)
    add_thread.add_argument("--host-id", default="")

    update = commands.add_parser("update-task")
    update.add_argument("--task-id", required=True)
    update.add_argument("--expected-revision", type=int, required=True)
    update.add_argument("--status", choices=[item.value for item in TaskStatus], required=True)
    update.add_argument("--progress", type=int)
    update.add_argument("--eta")
    update.add_argument("--delta")
    update.add_argument("--current")
    update.add_argument("--blocker")

    link = commands.add_parser("link-pr")
    link.add_argument("--task-id", required=True)
    link.add_argument("--pr", type=int, required=True)
    link.add_argument("--role", default="implementation")
    link.add_argument("--head-sha", default="")
    link.add_argument("--state", default="open")

    accept = commands.add_parser("accept")
    accept.add_argument("--task-id", required=True)
    accept.add_argument("--expected-revision", type=int, required=True)

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
    claim.add_argument("--arbiter-thread", required=True)

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

    watcher = commands.add_parser("prepare-watcher")
    watcher.add_argument("--generation", type=int, required=True)
    watcher.add_argument("--thread-id", required=True)
    watcher.add_argument("--host-id", default="")
    watcher.add_argument("--automation-id", default="")
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
            result = registry.update_task(task_id=args.task_id, expected_revision=args.expected_revision, status=TaskStatus(args.status), progress=args.progress, eta=args.eta, delta=args.delta, current=args.current, blocker=args.blocker)
        elif args.command == "link-pr":
            result = registry.link_pr(task_id=args.task_id, pr=args.pr, role=args.role, head_sha=args.head_sha, state=args.state)
        elif args.command == "accept":
            result = registry.accept(task_id=args.task_id, expected_revision=args.expected_revision)
        elif args.command == "open-incident":
            result = registry.open_incident(task_id=args.task_id, task_revision=args.task_revision, phase=args.phase, error_class=args.error_class, evidence_fingerprint=args.evidence_fingerprint, resources=args.resource)
        elif args.command == "claim-incident":
            result = registry.claim_incident(case_id=args.case_id, expected_task_revision=args.expected_task_revision, arbiter_thread_id=args.arbiter_thread)
        elif args.command == "decide":
            decision = _load_object(Path(args.decision_file).read_text(encoding="utf-8"), field="decision")
            result = registry.decide(case_id=args.case_id, expected_task_revision=args.expected_task_revision, decision=decision, expected_transition=args.expected_transition, evidence_digest=args.evidence_digest)
        elif args.command == "deliver":
            result = registry.deliver(case_id=args.case_id)
        elif args.command == "verify":
            result = registry.verify(case_id=args.case_id, observed_transition=args.observed_transition)
        elif args.command == "prepare-watcher":
            result = registry.prepare_watcher(generation=args.generation, thread_id=args.thread_id, host_id=args.host_id, automation_id=args.automation_id)
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
