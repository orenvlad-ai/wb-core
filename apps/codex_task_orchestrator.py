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
    AcceptanceStatus,
    AttentionKind,
    AttentionStatus,
    CANONICAL_REPOSITORY,
    IncidentDisposition,
    IncidentStatus,
    RetryObservation,
    STRICT_HUMAN_REASONS,
    SuccessionStatus,
    TaskStatus,
    canonical_digest,
    classify_incident,
    incident_key,
    report_status,
    transition_allowed,
    validate_arbiter_decision,
    validate_attention_event_id,
    validate_digest,
    validate_envelope_id,
    validate_task_passport,
    validate_task_id,
    validate_visible_text,
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
    pin_readback_digest TEXT NOT NULL DEFAULT '',
    pin_confirmed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, role, generation),
    UNIQUE(task_id, thread_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_execution_thread
ON task_threads(thread_id) WHERE active=1 AND role IN ('executor','arbiter');
CREATE TABLE IF NOT EXISTS acceptance_envelopes (
    envelope_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    curator_thread_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','DONE_PENDING_HANDOFF','AWAITING_ACCEPTANCE','ACCEPTED')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    owner_notification_digest TEXT NOT NULL DEFAULT '',
    owner_notification_revision INTEGER NOT NULL DEFAULT 0 CHECK(owner_notification_revision >= 0),
    owner_notified_at TEXT,
    prepared_handoff_text TEXT NOT NULL DEFAULT '',
    prepared_handoff_digest TEXT NOT NULL DEFAULT '',
    prepared_handoff_revision INTEGER NOT NULL DEFAULT 0 CHECK(prepared_handoff_revision >= 0),
    prepared_handoff_at TEXT,
    accepted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS acceptance_envelope_members (
    envelope_id TEXT NOT NULL REFERENCES acceptance_envelopes(envelope_id),
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
    role TEXT NOT NULL CHECK(role IN ('root','corrective','required-child')),
    required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(envelope_id,task_id)
);
CREATE TABLE IF NOT EXISTS attention_events (
    event_id TEXT PRIMARY KEY,
    event_digest TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    source_revision INTEGER NOT NULL CHECK(source_revision > 0),
    task_revision INTEGER NOT NULL CHECK(task_revision > 0),
    event_kind TEXT NOT NULL CHECK(event_kind IN ('TECHNICAL_COMPLETION','TERMINAL_FAILURE','STRICT_HUMAN_GATE','SERIOUS_STALL')),
    curator_thread_id TEXT NOT NULL,
    envelope_id TEXT REFERENCES acceptance_envelopes(envelope_id),
    evidence_summary TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('PENDING','LEASED','SENT','RETRY','ACKED','STALE')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at REAL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    transport_receipt_digest TEXT NOT NULL DEFAULT '',
    ack_evidence_digest TEXT NOT NULL DEFAULT '',
    acked_by_thread_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    first_sent_at TEXT,
    last_sent_at TEXT,
    acked_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id,source_revision,event_kind)
);
CREATE INDEX IF NOT EXISTS attention_delivery_queue
ON attention_events(state,next_attempt_at,created_at);
CREATE TABLE IF NOT EXISTS visible_report_state (
    envelope_id TEXT PRIMARY KEY,
    last_fingerprint TEXT NOT NULL,
    last_rendered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executor_successions (
    succession_id TEXT PRIMARY KEY,
    envelope_id TEXT NOT NULL REFERENCES acceptance_envelopes(envelope_id),
    predecessor_task_id TEXT NOT NULL REFERENCES tasks(task_id),
    predecessor_thread_id TEXT NOT NULL,
    predecessor_generation INTEGER NOT NULL CHECK(predecessor_generation > 0),
    successor_task_id TEXT NOT NULL REFERENCES tasks(task_id),
    successor_thread_id TEXT NOT NULL,
    successor_generation INTEGER NOT NULL CHECK(successor_generation > 0),
    reason TEXT NOT NULL,
    checkpoint_digest TEXT NOT NULL,
    target_readback_digest TEXT NOT NULL,
    prompt_delivery_digest TEXT NOT NULL,
    registry_link_digest TEXT NOT NULL,
    successor_active_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('READY_TO_ARCHIVE','ARCHIVED')),
    archive_readback_digest TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    archived_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(predecessor_thread_id,successor_thread_id)
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
    title_readback_digest TEXT NOT NULL DEFAULT '',
    pin_readback_digest TEXT NOT NULL DEFAULT '',
    automation_readback_digest TEXT NOT NULL DEFAULT '',
    retirement_required INTEGER NOT NULL DEFAULT 0 CHECK(retirement_required IN (0,1)),
    successor_generation INTEGER,
    automation_paused_digest TEXT NOT NULL DEFAULT '',
    archive_readback_digest TEXT NOT NULL DEFAULT '',
    archived_at TEXT,
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
            envelope_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(acceptance_envelopes)"
                )
            }
            if "owner_notification_digest" not in envelope_columns:
                connection.execute(
                    "ALTER TABLE acceptance_envelopes ADD COLUMN owner_notification_digest TEXT NOT NULL DEFAULT ''"
                )
            if "owner_notified_at" not in envelope_columns:
                connection.execute(
                    "ALTER TABLE acceptance_envelopes ADD COLUMN owner_notified_at TEXT"
                )
            if "owner_notification_revision" not in envelope_columns:
                connection.execute(
                    "ALTER TABLE acceptance_envelopes ADD COLUMN owner_notification_revision "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "prepared_handoff_text" not in envelope_columns:
                connection.execute(
                    "ALTER TABLE acceptance_envelopes ADD COLUMN prepared_handoff_text "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "prepared_handoff_digest" not in envelope_columns:
                connection.execute(
                    "ALTER TABLE acceptance_envelopes ADD COLUMN prepared_handoff_digest "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "prepared_handoff_revision" not in envelope_columns:
                connection.execute(
                    "ALTER TABLE acceptance_envelopes ADD COLUMN prepared_handoff_revision "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "prepared_handoff_at" not in envelope_columns:
                connection.execute(
                    "ALTER TABLE acceptance_envelopes ADD COLUMN prepared_handoff_at TEXT"
                )
            for column, definition in (
                ("title_readback_digest", "TEXT NOT NULL DEFAULT ''"),
                ("pin_readback_digest", "TEXT NOT NULL DEFAULT ''"),
                ("automation_readback_digest", "TEXT NOT NULL DEFAULT ''"),
                ("retirement_required", "INTEGER NOT NULL DEFAULT 0"),
                ("successor_generation", "INTEGER"),
                ("automation_paused_digest", "TEXT NOT NULL DEFAULT ''"),
                ("archive_readback_digest", "TEXT NOT NULL DEFAULT ''"),
                ("archived_at", "TEXT"),
            ):
                if column not in watcher_columns:
                    connection.execute(
                        f"ALTER TABLE watchers ADD COLUMN {column} {definition}"
                    )
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
            task_thread_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(task_threads)")
            }
            if "pin_readback_digest" not in task_thread_columns:
                connection.execute(
                    "ALTER TABLE task_threads ADD COLUMN pin_readback_digest "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "pin_confirmed_at" not in task_thread_columns:
                connection.execute(
                    "ALTER TABLE task_threads ADD COLUMN pin_confirmed_at TEXT"
                )
            connection.execute("DROP INDEX IF EXISTS one_active_incident_per_task")
            connection.execute(
                "CREATE UNIQUE INDEX one_active_incident_per_task ON incidents(task_id) "
                "WHERE status IN ('OPEN','WAITING_RESOURCE','CLAIMED','DECIDED','DELIVERED','VERIFIED')"
            )
            connection.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version','5') "
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
        curator_pin_readback_digest: str,
        executor_pin_readback_digest: str,
        acceptance_envelope_id: str = "",
        acceptance_title: str = "",
        acceptance_role: str = "root",
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
        role_pin_digests = {
            "curator": validate_digest(curator_pin_readback_digest),
            "executor": validate_digest(executor_pin_readback_digest),
        }
        timestamp = _now()
        digest = canonical_digest(validated_passport)
        visible_task_title = validate_visible_text(
            title, field="task title", task_id=identity
        )
        envelope_id = validate_envelope_id(acceptance_envelope_id or identity)
        if acceptance_role not in {"root", "corrective", "required-child"}:
            raise ValueError("invalid acceptance envelope member role")
        envelope_title = (acceptance_title or visible_task_title).strip()
        validate_visible_text(envelope_title, field="acceptance title")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (identity,)
            ).fetchone()
            if existing is not None:
                expected = (
                    visible_task_title,
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
                    row["role"]: (
                        row["thread_id"],
                        row["host_id"],
                        row["pin_readback_digest"],
                    )
                    for row in connection.execute(
                        "SELECT role,thread_id,host_id,pin_readback_digest FROM task_threads "
                        "WHERE task_id=? AND generation=1 AND role IN ('curator','executor')",
                        (identity,),
                    ).fetchall()
                }
                expected_threads = {
                    "curator": (
                        curator_thread_id.strip(),
                        host_id.strip(),
                        role_pin_digests["curator"],
                    ),
                    "executor": (
                        executor_thread_id.strip(),
                        host_id.strip(),
                        role_pin_digests["executor"],
                    ),
                }
                membership = connection.execute(
                    "SELECT envelope_id,role FROM acceptance_envelope_members WHERE task_id=?",
                    (identity,),
                ).fetchone()
                expected_membership = (envelope_id, acceptance_role)
                actual_membership = (
                    None if membership is None else membership["envelope_id"],
                    None if membership is None else membership["role"],
                )
                if (
                    actual != expected
                    or initial_threads != expected_threads
                    or actual_membership != expected_membership
                ):
                    raise RuntimeError(
                        "task id is already registered with a different immutable identity"
                    )
                return {
                    "task_id": identity,
                    "revision": int(existing["revision"]),
                    "passport_digest": digest,
                    "acceptance_envelope_id": envelope_id,
                    "idempotent": True,
                }
            connection.execute(
                "INSERT INTO tasks(task_id,title,repo,project_id,objective,passport_json,"
                "passport_digest,status,curator_thread_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    visible_task_title,
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
                    "INSERT INTO task_threads(task_id,role,generation,thread_id,host_id,"
                    "pin_readback_digest,pin_confirmed_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        role,
                        1,
                        thread_id.strip(),
                        host_id.strip(),
                        role_pin_digests[role],
                        timestamp,
                        timestamp,
                    ),
                )
            envelope = connection.execute(
                "SELECT * FROM acceptance_envelopes WHERE envelope_id=?",
                (envelope_id,),
            ).fetchone()
            if envelope is None:
                if acceptance_role != "root":
                    raise RuntimeError(
                        "a corrective task requires an existing acceptance envelope root"
                    )
                connection.execute(
                    "INSERT INTO acceptance_envelopes(envelope_id,title,curator_thread_id,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        envelope_id,
                        envelope_title,
                        curator_thread_id.strip(),
                        AcceptanceStatus.OPEN.value,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                if (
                    envelope["curator_thread_id"] != curator_thread_id.strip()
                    or envelope["status"] == AcceptanceStatus.ACCEPTED.value
                ):
                    raise RuntimeError(
                        "acceptance envelope curator/status does not allow this task"
                    )
                if acceptance_title and envelope["title"] != envelope_title:
                    raise RuntimeError("acceptance envelope title does not match")
            connection.execute(
                "INSERT INTO acceptance_envelope_members(envelope_id,task_id,role,created_at) "
                "VALUES(?,?,?,?)",
                (envelope_id, identity, acceptance_role, timestamp),
            )
            if envelope is not None:
                self._recompute_envelope(
                    connection,
                    envelope_id,
                    required_membership_changed=True,
                )
            self.event(
                connection,
                "task",
                identity,
                "registered",
                {
                    "passport_digest": digest,
                    "executor_thread_id": executor_thread_id,
                    "acceptance_envelope_id": envelope_id,
                    "acceptance_role": acceptance_role,
                },
            )
        self.flush_events()
        return {
            "task_id": identity,
            "revision": 1,
            "passport_digest": digest,
            "acceptance_envelope_id": envelope_id,
        }

    def add_thread(
        self,
        *,
        task_id: str,
        role: str,
        generation: int,
        thread_id: str,
        host_id: str,
        pin_readback_digest: str = "",
    ) -> dict[str, object]:
        identity = validate_task_id(task_id)
        if (
            role not in {"curator", "executor", "arbiter"}
            or generation <= 0
            or not thread_id.strip()
            or not host_id.strip()
        ):
            raise ValueError("invalid thread identity")
        pin_digest = ""
        if role in {"curator", "executor"}:
            pin_digest = validate_digest(pin_readback_digest)
        elif pin_readback_digest:
            raise ValueError("arbiter threads do not use persistent role pin evidence")
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
                    or existing["pin_readback_digest"] != pin_digest
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
                "INSERT INTO task_threads(task_id,role,generation,thread_id,host_id,"
                "pin_readback_digest,pin_confirmed_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity,
                    role,
                    generation,
                    thread_id.strip(),
                    host_id.strip(),
                    pin_digest,
                    timestamp if pin_digest else None,
                    timestamp,
                ),
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

    def bind_acceptance_envelope(
        self,
        *,
        envelope_id: str,
        title: str,
        curator_thread_id: str,
        root_task_id: str,
        corrective_task_ids: Iterable[str],
    ) -> dict[str, object]:
        identity = validate_envelope_id(envelope_id)
        root = validate_task_id(root_task_id)
        corrective = [validate_task_id(item) for item in corrective_task_ids]
        if len(set(corrective)) != len(corrective) or root in corrective:
            raise ValueError("acceptance envelope members must be unique")
        if not curator_thread_id.strip():
            raise ValueError("acceptance envelope requires the exact curator thread")
        visible_title = validate_visible_text(title, field="acceptance title")
        timestamp = _now()
        members = [(root, "root"), *((item, "corrective") for item in corrective)]
        for task_id, _ in members:
            visible_title = validate_visible_text(
                visible_title, field="acceptance title", task_id=task_id
            )
        with self.transaction() as connection:
            rows = {
                item["task_id"]: item
                for item in connection.execute(
                    "SELECT * FROM tasks WHERE task_id IN ({})".format(
                        ",".join("?" for _ in members)
                    ),
                    tuple(item[0] for item in members),
                ).fetchall()
            }
            if set(rows) != {item[0] for item in members}:
                raise RuntimeError("acceptance envelope contains an unknown task")
            if any(
                row["curator_thread_id"] != curator_thread_id.strip()
                for row in rows.values()
            ):
                raise RuntimeError("all acceptance envelope members must share one curator")
            existing_memberships = connection.execute(
                "SELECT task_id,envelope_id,role FROM acceptance_envelope_members "
                "WHERE task_id IN ({})".format(",".join("?" for _ in members)),
                tuple(item[0] for item in members),
            ).fetchall()
            for membership in existing_memberships:
                expected_role = dict(members)[membership["task_id"]]
                if (
                    membership["envelope_id"] != identity
                    or membership["role"] != expected_role
                ):
                    raise RuntimeError(
                        "a task is already bound to a different acceptance envelope"
                    )
            envelope = connection.execute(
                "SELECT * FROM acceptance_envelopes WHERE envelope_id=?", (identity,)
            ).fetchone()
            if envelope is None:
                connection.execute(
                    "INSERT INTO acceptance_envelopes(envelope_id,title,curator_thread_id,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        identity,
                        visible_title,
                        curator_thread_id.strip(),
                        AcceptanceStatus.OPEN.value,
                        timestamp,
                        timestamp,
                    ),
                )
            elif (
                envelope["title"] != visible_title
                or envelope["curator_thread_id"] != curator_thread_id.strip()
                or envelope["status"] == AcceptanceStatus.ACCEPTED.value
            ):
                raise RuntimeError("acceptance envelope identity does not match")
            inserted = 0
            for task_id, role in members:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO acceptance_envelope_members(envelope_id,task_id,role,created_at) "
                    "VALUES(?,?,?,?)",
                    (identity, task_id, role, timestamp),
                )
                inserted += cursor.rowcount
            self._recompute_envelope(
                connection,
                identity,
                required_membership_changed=inserted > 0,
            )
            self.event(
                connection,
                "acceptance-envelope",
                identity,
                "members-bound",
                {"root_task_id": root, "corrective_task_ids": corrective},
            )
        self.flush_events()
        return {
            "acceptance_envelope_id": identity,
            "root_task_id": root,
            "corrective_task_ids": corrective,
            "members_inserted": inserted,
            "idempotent": inserted == 0,
        }

    def confirm_role_pin(
        self,
        *,
        thread_id: str,
        role: str,
        pin_readback_digest: str,
    ) -> dict[str, object]:
        if role not in {"curator", "executor"} or not thread_id.strip():
            raise ValueError("role pin confirmation requires an exact curator/executor thread")
        digest = validate_digest(pin_readback_digest)
        timestamp = _now()
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT tt.* FROM task_threads tt JOIN tasks t ON t.task_id=tt.task_id "
                "WHERE tt.thread_id=? AND tt.role=? AND tt.active=1 AND t.status<>? "
                "ORDER BY tt.task_id,tt.generation",
                (thread_id.strip(), role, TaskStatus.ACCEPTED.value),
            ).fetchall()
            if not rows:
                raise RuntimeError("role pin readback does not match an active assignment")
            conflicting = {
                str(row["pin_readback_digest"])
                for row in rows
                if row["pin_readback_digest"]
                and row["pin_readback_digest"] != digest
            }
            if conflicting:
                raise RuntimeError("active role already has different assignment-time pin evidence")
            updated = 0
            for row in rows:
                if row["pin_readback_digest"] == digest and row["pin_confirmed_at"]:
                    continue
                connection.execute(
                    "UPDATE task_threads SET pin_readback_digest=?,pin_confirmed_at=? WHERE id=?",
                    (digest, timestamp, int(row["id"])),
                )
                updated += 1
                self.event(
                    connection,
                    "task",
                    str(row["task_id"]),
                    "role-pin-confirmed",
                    {
                        "role": role,
                        "thread_id": thread_id.strip(),
                        "pin_readback_digest": digest,
                    },
                )
        self.flush_events()
        return {
            "thread_id": thread_id.strip(),
            "role": role,
            "pin_readback_digest": digest,
            "active_assignments": len(rows),
            "updated_assignments": updated,
            "idempotent": updated == 0,
        }

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

    def _task_envelope(
        self, connection: sqlite3.Connection, task_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT e.* FROM acceptance_envelopes e "
            "JOIN acceptance_envelope_members m ON m.envelope_id=e.envelope_id "
            "WHERE m.task_id=?",
            (validate_task_id(task_id),),
        ).fetchone()

    def _recompute_envelope(
        self,
        connection: sqlite3.Connection,
        envelope_id: str,
        *,
        required_membership_changed: bool = False,
    ) -> sqlite3.Row:
        identity = validate_envelope_id(envelope_id)
        envelope = connection.execute(
            "SELECT * FROM acceptance_envelopes WHERE envelope_id=?", (identity,)
        ).fetchone()
        if envelope is None:
            raise KeyError(f"unknown acceptance envelope: {identity}")
        if envelope["status"] == AcceptanceStatus.ACCEPTED.value:
            return envelope
        members = connection.execute(
            "SELECT t.status FROM acceptance_envelope_members m "
            "JOIN tasks t ON t.task_id=m.task_id "
            "WHERE m.envelope_id=? AND m.required=1 ORDER BY m.task_id",
            (identity,),
        ).fetchall()
        if not members:
            raise RuntimeError("acceptance envelope must contain a required task")
        statuses = {TaskStatus(row["status"]) for row in members}
        terminal = {
            TaskStatus.DONE_AWAITING_ACCEPTANCE,
            TaskStatus.TERMINAL_FAILURE,
        }
        pending = {
            TaskStatus.DONE_PENDING_HANDOFF,
            TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF,
        }
        if statuses.issubset(terminal):
            target = AcceptanceStatus.AWAITING_ACCEPTANCE
        elif statuses.issubset(terminal | pending) and statuses & pending:
            target = AcceptanceStatus.DONE_PENDING_HANDOFF
        else:
            target = AcceptanceStatus.OPEN
        notification_stale = bool(envelope["owner_notified_at"]) and (
            required_membership_changed
            or target != AcceptanceStatus.AWAITING_ACCEPTANCE
            or int(envelope["owner_notification_revision"])
            != int(envelope["revision"])
        )
        prepared_handoff_stale = bool(envelope["prepared_handoff_digest"]) and (
            required_membership_changed
            or target != AcceptanceStatus.AWAITING_ACCEPTANCE
            or int(envelope["prepared_handoff_revision"])
            != int(envelope["revision"])
        )
        if (
            envelope["status"] != target.value
            or required_membership_changed
            or notification_stale
            or prepared_handoff_stale
        ):
            next_revision = int(envelope["revision"]) + 1
            connection.execute(
                "UPDATE acceptance_envelopes SET status=?,revision=?,"
                "owner_notification_digest='',owner_notification_revision=0,owner_notified_at=NULL,"
                "prepared_handoff_text='',prepared_handoff_digest='',"
                "prepared_handoff_revision=0,prepared_handoff_at=NULL,updated_at=? "
                "WHERE envelope_id=?",
                (target.value, next_revision, _now(), identity),
            )
            self.event(
                connection,
                "acceptance-envelope",
                identity,
                "state-changed",
                {
                    "from": envelope["status"],
                    "to": target.value,
                    "revision": next_revision,
                    "required_membership_changed": required_membership_changed,
                    "owner_notification_invalidated": notification_stale,
                },
            )
            envelope = connection.execute(
                "SELECT * FROM acceptance_envelopes WHERE envelope_id=?", (identity,)
            ).fetchone()
        return envelope

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
            attention_managed = {
                TaskStatus.DONE_PENDING_HANDOFF,
                TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF,
                TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
                TaskStatus.DONE_AWAITING_ACCEPTANCE,
                TaskStatus.TERMINAL_FAILURE,
            }
            if status in attention_managed and status != current_status:
                raise RuntimeError(
                    "attention-managed task states require enqueue-attention or curator acknowledgement"
                )
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
            values["eta_text"] = validate_visible_text(
                str(values["eta_text"]), field="eta", task_id=validate_task_id(task_id)
            )
            values["last_delta"] = validate_visible_text(
                str(values["last_delta"]),
                field="delta",
                task_id=validate_task_id(task_id),
            )
            values["current_action"] = validate_visible_text(
                str(values["current_action"]),
                field="current action",
                task_id=validate_task_id(task_id),
            )
            if status in {
                TaskStatus.AWAITING_HUMAN,
                TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
            }:
                if values["human_reason"] not in STRICT_HUMAN_REASONS:
                    raise ValueError("awaiting-human requires a strict v1 human reason")
                if not values["blocker"]:
                    raise ValueError("awaiting-human requires an exact blocker")
                values["blocker"] = validate_visible_text(
                    str(values["blocker"]),
                    field="blocker",
                    task_id=validate_task_id(task_id),
                )
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

    def reconcile_acceptance(self, *, envelope_id: str) -> dict[str, object]:
        identity = validate_envelope_id(envelope_id)
        with self.transaction() as connection:
            before = connection.execute(
                "SELECT * FROM acceptance_envelopes WHERE envelope_id=?", (identity,)
            ).fetchone()
            if before is None:
                raise KeyError(f"unknown acceptance envelope: {identity}")
            after = self._recompute_envelope(connection, identity)
            changed = int(after["revision"]) != int(before["revision"])
            if changed:
                self.event(
                    connection,
                    "acceptance-envelope",
                    identity,
                    "reconciled",
                    {
                        "from_revision": int(before["revision"]),
                        "to_revision": int(after["revision"]),
                    },
                )
        self.flush_events()
        return {
            "acceptance_envelope_id": identity,
            "status": after["status"],
            "revision": int(after["revision"]),
            "owner_notification_current": bool(after["owner_notified_at"])
            and int(after["owner_notification_revision"]) == int(after["revision"]),
            "changed": changed,
        }

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

    def _ensure_task_envelope(
        self, connection: sqlite3.Connection, task: sqlite3.Row
    ) -> sqlite3.Row:
        envelope = self._task_envelope(connection, task["task_id"])
        if envelope is not None:
            return envelope
        envelope_id = validate_envelope_id(task["task_id"])
        timestamp = _now()
        connection.execute(
            "INSERT INTO acceptance_envelopes(envelope_id,title,curator_thread_id,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                envelope_id,
                validate_visible_text(task["title"], field="acceptance title"),
                task["curator_thread_id"],
                AcceptanceStatus.OPEN.value,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO acceptance_envelope_members(envelope_id,task_id,role,created_at) "
            "VALUES(?,?,?,?)",
            (envelope_id, task["task_id"], "root", timestamp),
        )
        self.event(
            connection,
            "acceptance-envelope",
            envelope_id,
            "default-created",
            {"root_task_id": task["task_id"]},
        )
        return connection.execute(
            "SELECT * FROM acceptance_envelopes WHERE envelope_id=?", (envelope_id,)
        ).fetchone()

    def enqueue_attention(
        self,
        *,
        task_id: str,
        expected_revision: int,
        kind: AttentionKind,
        evidence_summary: str,
        evidence_digest: str,
        backfill: bool = False,
        eta: str | None = None,
        delta: str | None = None,
        current: str | None = None,
        blocker: str = "",
        human_reason: str = "",
        repo_owned_remediation_available: bool = False,
        remediation_exhausted: bool = False,
    ) -> dict[str, object]:
        identity = validate_task_id(task_id)
        digest = validate_digest(evidence_digest)
        summary = evidence_summary.strip()
        if not summary:
            raise ValueError("attention event requires an evidence summary")
        if expected_revision <= 0:
            raise ValueError("expected_revision must be positive")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM attention_events WHERE task_id=? AND source_revision=? AND event_kind=?",
                (identity, expected_revision, kind.value),
            ).fetchone()
            if existing is None and backfill:
                existing = connection.execute(
                    "SELECT * FROM attention_events WHERE task_id=? AND event_kind=? "
                    "ORDER BY created_at,event_id LIMIT 1",
                    (identity, kind.value),
                ).fetchone()
            if existing is not None:
                if (
                    existing["evidence_summary"] != summary
                    or existing["evidence_digest"] != digest
                ):
                    raise RuntimeError("attention event identity already has different evidence")
                return {
                    "event_id": existing["event_id"],
                    "event_digest": existing["event_digest"],
                    "state": existing["state"],
                    "task_revision": int(existing["task_revision"]),
                    "idempotent": True,
                }
            task = self.task(identity, connection)
            if int(task["revision"]) != expected_revision:
                raise RuntimeError(
                    f"stale task revision: expected {expected_revision}, actual {task['revision']}"
                )
            before_status = TaskStatus(task["status"])
            pending_by_kind = {
                AttentionKind.TECHNICAL_COMPLETION: TaskStatus.DONE_PENDING_HANDOFF,
                AttentionKind.TERMINAL_FAILURE: TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF,
                AttentionKind.STRICT_HUMAN_GATE: TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
                AttentionKind.SERIOUS_STALL: TaskStatus.RECOVERING,
            }
            target_status = pending_by_kind[kind]
            if backfill:
                allowed_backfill = {
                    AttentionKind.TECHNICAL_COMPLETION: TaskStatus.DONE_AWAITING_ACCEPTANCE,
                    AttentionKind.TERMINAL_FAILURE: TaskStatus.TERMINAL_FAILURE,
                    AttentionKind.STRICT_HUMAN_GATE: TaskStatus.AWAITING_HUMAN,
                }
                if kind not in allowed_backfill or before_status != allowed_backfill[kind]:
                    raise RuntimeError("backfill requires the matching legacy terminal state")
            elif not transition_allowed(before_status, target_status):
                raise RuntimeError(
                    f"forbidden attention transition: {before_status.value} -> {target_status.value}"
                )
            if kind == AttentionKind.STRICT_HUMAN_GATE:
                if human_reason not in STRICT_HUMAN_REASONS or not blocker.strip():
                    raise ValueError("strict HumanGate attention requires reason and blocker")
                if repo_owned_remediation_available or not remediation_exhausted:
                    raise ValueError(
                        "strict HumanGate requires exhausted remediation and no repo-owned action"
                    )
            elif blocker.strip() or human_reason.strip():
                raise ValueError("blocker and human reason are only valid for strict HumanGate")
            values = {
                "eta": task["eta_text"] if eta is None else eta,
                "delta": task["last_delta"] if delta is None else delta,
                "current": task["current_action"] if current is None else current,
            }
            for field, value in values.items():
                values[field] = validate_visible_text(
                    str(value), field=field, task_id=identity
                )
            visible_blocker = ""
            if blocker.strip():
                visible_blocker = validate_visible_text(
                    blocker, field="blocker", task_id=identity
                )
            envelope = self._ensure_task_envelope(connection, task)
            next_revision = expected_revision + 1
            event_payload = {
                "schema": "wb-core-attention-event/v1",
                "task_id": identity,
                "source_revision": expected_revision,
                "task_revision": next_revision,
                "event_kind": kind.value,
                "curator_thread_id": task["curator_thread_id"],
                "acceptance_envelope_id": envelope["envelope_id"],
                "evidence_summary": summary,
                "evidence_digest": digest,
            }
            event_digest = canonical_digest(event_payload)
            event_id = "evt-" + event_digest.removeprefix("sha256:")[:24]
            progress = 100 if kind == AttentionKind.TECHNICAL_COMPLETION else int(task["progress_percent"])
            connection.execute(
                "UPDATE tasks SET status=?,revision=?,progress_percent=?,eta_text=?,last_delta=?,"
                "current_action=?,blocker=?,human_reason=?,updated_at=? WHERE task_id=?",
                (
                    target_status.value,
                    next_revision,
                    progress,
                    values["eta"],
                    values["delta"],
                    values["current"],
                    visible_blocker,
                    human_reason.strip(),
                    _now(),
                    identity,
                ),
            )
            timestamp = _now()
            connection.execute(
                "INSERT INTO attention_events(event_id,event_digest,task_id,source_revision,task_revision,"
                "event_kind,curator_thread_id,envelope_id,evidence_summary,evidence_digest,state,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    event_digest,
                    identity,
                    expected_revision,
                    next_revision,
                    kind.value,
                    task["curator_thread_id"],
                    envelope["envelope_id"],
                    summary,
                    digest,
                    AttentionStatus.PENDING.value,
                    timestamp,
                    timestamp,
                ),
            )
            self.event(
                connection,
                "attention-event",
                event_id,
                "backfilled" if backfill else "enqueued",
                {
                    "event_digest": event_digest,
                    "task_id": identity,
                    "task_revision": next_revision,
                    "event_kind": kind.value,
                    "acceptance_envelope_id": envelope["envelope_id"],
                },
            )
            self._recompute_envelope(connection, envelope["envelope_id"])
        self.flush_events()
        return {
            "event_id": validate_attention_event_id(event_id),
            "event_digest": event_digest,
            "state": AttentionStatus.PENDING.value,
            "task_id": identity,
            "task_revision": next_revision,
            "acceptance_envelope_id": envelope["envelope_id"],
        }

    def reserve_attention(
        self,
        *,
        generation: int,
        owner: str,
        lease_seconds: int,
        limit: int,
    ) -> dict[str, object]:
        if not owner.strip() or lease_seconds <= 0 or lease_seconds > 600:
            raise ValueError("attention lease requires owner and 1-600 seconds")
        if limit <= 0 or limit > 8:
            raise ValueError("attention reservation limit must be between 1 and 8")
        now = time.time()
        reserved: list[dict[str, object]] = []
        with self.transaction() as connection:
            active = connection.execute(
                "SELECT generation FROM watchers WHERE status='ACTIVE'"
            ).fetchone()
            if active is None or int(active["generation"]) != generation:
                return {"reserved": [], "reason": "stale-watcher-generation"}
            rows = connection.execute(
                "SELECT * FROM attention_events WHERE "
                "(state IN ('PENDING','RETRY','SENT') AND next_attempt_at<=?) OR "
                "(state='LEASED' AND (lease_expires_at IS NULL OR lease_expires_at<=?)) "
                "ORDER BY created_at,event_id LIMIT ?",
                (now, now, limit),
            ).fetchall()
            for row in rows:
                task = self.task(row["task_id"], connection)
                if int(task["revision"]) != int(row["task_revision"]):
                    connection.execute(
                        "UPDATE attention_events SET state='STALE',lease_owner='',lease_expires_at=NULL,updated_at=? "
                        "WHERE event_id=?",
                        (_now(), row["event_id"]),
                    )
                    self.event(
                        connection,
                        "attention-event",
                        row["event_id"],
                        "stale",
                        {"actual_task_revision": int(task["revision"])},
                    )
                    continue
                attempt = int(row["attempt_count"]) + 1
                connection.execute(
                    "UPDATE attention_events SET state='LEASED',attempt_count=?,lease_owner=?,"
                    "lease_expires_at=?,updated_at=? WHERE event_id=?",
                    (attempt, owner.strip(), now + lease_seconds, _now(), row["event_id"]),
                )
                reserved.append(
                    {
                        "event_id": row["event_id"],
                        "event_digest": row["event_digest"],
                        "task_id": row["task_id"],
                        "task_revision": int(row["task_revision"]),
                        "event_kind": row["event_kind"],
                        "curator_thread_id": row["curator_thread_id"],
                        "acceptance_envelope_id": row["envelope_id"],
                        "evidence_summary": row["evidence_summary"],
                        "evidence_digest": row["evidence_digest"],
                        "attempt": attempt,
                    }
                )
                self.event(
                    connection,
                    "attention-event",
                    row["event_id"],
                    "reserved",
                    {"owner": owner.strip(), "attempt": attempt},
                )
        self.flush_events()
        return {"reserved": reserved, "generation": generation}

    def mark_attention_sent(
        self,
        *,
        event_id: str,
        owner: str,
        transport_receipt_digest: str,
        ack_timeout_seconds: int,
    ) -> dict[str, object]:
        identity = validate_attention_event_id(event_id)
        receipt = validate_digest(transport_receipt_digest)
        if ack_timeout_seconds <= 0 or ack_timeout_seconds > 86400:
            raise ValueError("ack timeout must be between 1 and 86400 seconds")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attention_events WHERE event_id=?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown attention event: {identity}")
            if row["state"] == AttentionStatus.ACKED.value:
                if (
                    row["transport_receipt_digest"]
                    and row["transport_receipt_digest"] != receipt
                ):
                    raise RuntimeError(
                        "attention event already has a different transport receipt"
                    )
                if not row["transport_receipt_digest"]:
                    timestamp = _now()
                    connection.execute(
                        "UPDATE attention_events SET transport_receipt_digest=?,"
                        "first_sent_at=COALESCE(first_sent_at,?),last_sent_at=?,updated_at=? "
                        "WHERE event_id=?",
                        (receipt, timestamp, timestamp, timestamp, identity),
                    )
                    self.event(
                        connection,
                        "attention-event",
                        identity,
                        "send-confirmed-after-ack",
                        {
                            "attempt": int(row["attempt_count"]),
                            "transport_receipt_digest": receipt,
                        },
                    )
                result = {
                    "event_id": identity,
                    "state": row["state"],
                    "idempotent": True,
                }
            else:
                if row["state"] == AttentionStatus.SENT.value:
                    if row["transport_receipt_digest"] != receipt:
                        raise RuntimeError(
                            "attention event already has a different transport receipt"
                        )
                    return {
                        "event_id": identity,
                        "state": row["state"],
                        "idempotent": True,
                    }
                if (
                    row["state"] != AttentionStatus.LEASED.value
                    or row["lease_owner"] != owner.strip()
                ):
                    raise RuntimeError("attention event is not leased by this owner")
                timestamp = _now()
                connection.execute(
                    "UPDATE attention_events SET state='SENT',lease_owner='',lease_expires_at=NULL,"
                    "next_attempt_at=?,transport_receipt_digest=?,first_sent_at=COALESCE(first_sent_at,?),"
                    "last_sent_at=?,updated_at=? WHERE event_id=?",
                    (
                        time.time() + ack_timeout_seconds,
                        receipt,
                        timestamp,
                        timestamp,
                        timestamp,
                        identity,
                    ),
                )
                self.event(
                    connection,
                    "attention-event",
                    identity,
                    "sent",
                    {
                        "attempt": int(row["attempt_count"]),
                        "transport_receipt_digest": receipt,
                    },
                )
                result = {"event_id": identity, "state": AttentionStatus.SENT.value}
        self.flush_events()
        return result

    def retry_attention(
        self,
        *,
        event_id: str,
        owner: str,
        error: str,
        retry_after_seconds: int,
    ) -> dict[str, object]:
        identity = validate_attention_event_id(event_id)
        if not error.strip() or retry_after_seconds < 0 or retry_after_seconds > 86400:
            raise ValueError("attention retry requires error and bounded delay")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attention_events WHERE event_id=?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown attention event: {identity}")
            if row["state"] == AttentionStatus.ACKED.value:
                return {"event_id": identity, "state": row["state"], "idempotent": True}
            if row["state"] != AttentionStatus.LEASED.value or row["lease_owner"] != owner.strip():
                raise RuntimeError("attention event is not leased by this owner")
            connection.execute(
                "UPDATE attention_events SET state='RETRY',lease_owner='',lease_expires_at=NULL,"
                "next_attempt_at=?,last_error=?,updated_at=? WHERE event_id=?",
                (time.time() + retry_after_seconds, error.strip(), _now(), identity),
            )
            self.event(
                connection,
                "attention-event",
                identity,
                "send-failed",
                {"attempt": int(row["attempt_count"]), "error": error.strip()},
            )
        self.flush_events()
        return {"event_id": identity, "state": AttentionStatus.RETRY.value}

    def attention_event(self, event_id: str) -> dict[str, object]:
        identity = validate_attention_event_id(event_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM attention_events WHERE event_id=?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown attention event: {identity}")
            envelope = connection.execute(
                "SELECT envelope_id,title,status,revision,curator_thread_id,owner_notified_at "
                "FROM acceptance_envelopes WHERE envelope_id=?",
                (row["envelope_id"],),
            ).fetchone()
        return {"event": dict(row), "acceptance_envelope": dict(envelope)}

    def ack_attention(
        self,
        *,
        event_id: str,
        event_digest: str,
        curator_thread_id: str,
        expected_task_revision: int,
        ack_evidence_digest: str,
    ) -> dict[str, object]:
        identity = validate_attention_event_id(event_id)
        digest = validate_digest(event_digest)
        ack_digest = validate_digest(ack_evidence_digest)
        if not curator_thread_id.strip():
            raise ValueError("attention acknowledgement requires exact curator identity")
        result: dict[str, object]
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attention_events WHERE event_id=?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown attention event: {identity}")
            if (
                row["event_digest"] != digest
                or row["curator_thread_id"] != curator_thread_id.strip()
                or int(row["task_revision"]) != expected_task_revision
            ):
                raise RuntimeError("attention acknowledgement identity does not match")
            if row["state"] == AttentionStatus.ACKED.value:
                if (
                    row["ack_evidence_digest"] != ack_digest
                    or row["acked_by_thread_id"] != curator_thread_id.strip()
                ):
                    raise RuntimeError("attention event already has different acknowledgement")
                envelope = connection.execute(
                    "SELECT * FROM acceptance_envelopes WHERE envelope_id=?",
                    (row["envelope_id"],),
                ).fetchone()
                return {
                    "event_id": identity,
                    "state": AttentionStatus.ACKED.value,
                    "acceptance_envelope_id": envelope["envelope_id"],
                    "acceptance_envelope_revision": int(envelope["revision"]),
                    "acceptance_envelope_state": envelope["status"],
                    "owner_notification_required": (
                        envelope["status"] == AcceptanceStatus.AWAITING_ACCEPTANCE.value
                        and not envelope["owner_notified_at"]
                    ),
                    "idempotent": True,
                }
            task = self.task(row["task_id"], connection)
            if int(task["revision"]) != expected_task_revision:
                connection.execute(
                    "UPDATE attention_events SET state='STALE',lease_owner='',lease_expires_at=NULL,updated_at=? "
                    "WHERE event_id=?",
                    (_now(), identity),
                )
                self.event(
                    connection,
                    "attention-event",
                    identity,
                    "stale",
                    {"actual_task_revision": int(task["revision"]), "stage": "curator-ack"},
                )
                result = {"event_id": identity, "state": AttentionStatus.STALE.value}
            elif row["state"] == AttentionStatus.STALE.value:
                result = {"event_id": identity, "state": AttentionStatus.STALE.value}
            else:
                final_by_kind = {
                    AttentionKind.TECHNICAL_COMPLETION.value: TaskStatus.DONE_AWAITING_ACCEPTANCE,
                    AttentionKind.TERMINAL_FAILURE.value: TaskStatus.TERMINAL_FAILURE,
                    AttentionKind.STRICT_HUMAN_GATE.value: TaskStatus.AWAITING_HUMAN,
                }
                target_status = final_by_kind.get(row["event_kind"])
                next_revision = int(task["revision"])
                if target_status is not None:
                    if not transition_allowed(TaskStatus(task["status"]), target_status):
                        raise RuntimeError("task is not in the matching pending-handoff state")
                    next_revision += 1
                    connection.execute(
                        "UPDATE tasks SET status=?,revision=?,updated_at=? WHERE task_id=?",
                        (target_status.value, next_revision, _now(), row["task_id"]),
                    )
                    self.event(
                        connection,
                        "task",
                        row["task_id"],
                        "handoff-acknowledged",
                        {
                            "event_id": identity,
                            "status": target_status.value,
                            "revision": next_revision,
                        },
                    )
                timestamp = _now()
                connection.execute(
                    "UPDATE attention_events SET state='ACKED',lease_owner='',lease_expires_at=NULL,"
                    "ack_evidence_digest=?,acked_by_thread_id=?,acked_at=?,updated_at=? WHERE event_id=?",
                    (ack_digest, curator_thread_id.strip(), timestamp, timestamp, identity),
                )
                envelope = self._recompute_envelope(connection, row["envelope_id"])
                self.event(
                    connection,
                    "attention-event",
                    identity,
                    "curator-acknowledged",
                    {
                        "ack_evidence_digest": ack_digest,
                        "curator_thread_id": curator_thread_id.strip(),
                        "task_revision": next_revision,
                        "acceptance_envelope_revision": int(envelope["revision"]),
                    },
                )
                result = {
                    "event_id": identity,
                    "state": AttentionStatus.ACKED.value,
                    "task_id": row["task_id"],
                    "task_revision": next_revision,
                    "acceptance_envelope_id": envelope["envelope_id"],
                    "acceptance_envelope_revision": int(envelope["revision"]),
                    "acceptance_envelope_state": envelope["status"],
                    "owner_notification_required": (
                        envelope["status"] == AcceptanceStatus.AWAITING_ACCEPTANCE.value
                        and not envelope["owner_notified_at"]
                    ),
                }
        self.flush_events()
        return result

    def confirm_owner_notification(
        self,
        *,
        curator_thread_id: str,
        envelope_id: str,
        expected_revision: int,
        notification_evidence_digest: str,
    ) -> dict[str, object]:
        identity = validate_envelope_id(envelope_id)
        digest = validate_digest(notification_evidence_digest)
        with self.transaction() as connection:
            envelope = connection.execute(
                "SELECT * FROM acceptance_envelopes WHERE envelope_id=?", (identity,)
            ).fetchone()
            if envelope is None:
                raise KeyError(f"unknown acceptance envelope: {identity}")
            if envelope["curator_thread_id"] != curator_thread_id.strip():
                raise RuntimeError("owner notification came from the wrong curator")
            if int(envelope["revision"]) != expected_revision:
                raise RuntimeError("stale acceptance envelope revision")
            if envelope["status"] != AcceptanceStatus.AWAITING_ACCEPTANCE.value:
                raise RuntimeError("owner notification requires a completed acceptance envelope")
            if (
                int(envelope["prepared_handoff_revision"]) != expected_revision
                or not envelope["prepared_handoff_text"]
                or envelope["prepared_handoff_digest"] != digest
            ):
                raise RuntimeError(
                    "owner notification must match the prepared handoff for this envelope revision"
                )
            if envelope["owner_notified_at"]:
                if (
                    envelope["owner_notification_digest"] != digest
                    or int(envelope["owner_notification_revision"])
                    != expected_revision
                ):
                    raise RuntimeError("acceptance envelope already has different notification evidence")
                return {
                    "acceptance_envelope_id": identity,
                    "status": envelope["status"],
                    "revision": int(envelope["revision"]),
                    "idempotent": True,
                }
            connection.execute(
                "UPDATE acceptance_envelopes SET owner_notification_digest=?,"
                "owner_notification_revision=?,owner_notified_at=?,updated_at=? "
                "WHERE envelope_id=?",
                (digest, expected_revision, _now(), _now(), identity),
            )
            self.event(
                connection,
                "acceptance-envelope",
                identity,
                "owner-notified",
                {"notification_evidence_digest": digest, "revision": expected_revision},
            )
        self.flush_events()
        return {
            "acceptance_envelope_id": identity,
            "status": AcceptanceStatus.AWAITING_ACCEPTANCE.value,
            "revision": expected_revision,
        }

    def prepare_owner_handoff(
        self,
        *,
        curator_thread_id: str,
        envelope_id: str,
        expected_revision: int,
        done: Iterable[str],
        verified: str,
        limitations: str = "",
    ) -> dict[str, object]:
        identity = validate_envelope_id(envelope_id)
        done_items = [item.strip() for item in done if item.strip()]
        if not 1 <= len(done_items) <= 2:
            raise ValueError("owner handoff requires one or two concise done statements")
        if expected_revision <= 0:
            raise ValueError("owner handoff requires a positive envelope revision")
        for item in done_items:
            validate_visible_text(item, field="owner handoff done")
        verified_text = validate_visible_text(verified, field="owner handoff verification")
        limitations_text = ""
        if limitations.strip():
            limitations_text = validate_visible_text(
                limitations, field="owner handoff limitations"
            )
        lines = [
            "Статус: Завершена — требуется приёмка",
            "Сделано: " + " ".join(done_items),
            "Проверено: " + verified_text,
        ]
        if limitations_text:
            lines.append("Ограничения: " + limitations_text)
        lines.append("Ответьте ровно: «Задача принята»")
        handoff_text = "\n".join(lines)
        with self.transaction() as connection:
            envelope = connection.execute(
                "SELECT * FROM acceptance_envelopes WHERE envelope_id=?", (identity,)
            ).fetchone()
            if envelope is None:
                raise KeyError(f"unknown acceptance envelope: {identity}")
            if envelope["curator_thread_id"] != curator_thread_id.strip():
                raise RuntimeError("owner handoff came from the wrong curator")
            if int(envelope["revision"]) != expected_revision:
                raise RuntimeError("stale acceptance envelope revision")
            if envelope["status"] != AcceptanceStatus.AWAITING_ACCEPTANCE.value:
                raise RuntimeError("owner handoff requires a completed acceptance envelope")
            member_task_ids = [
                str(row["task_id"])
                for row in connection.execute(
                    "SELECT task_id FROM acceptance_envelope_members "
                    "WHERE envelope_id=? AND required=1 ORDER BY task_id",
                    (identity,),
                ).fetchall()
            ]
            for member_task_id in member_task_ids:
                for item in done_items:
                    validate_visible_text(
                        item,
                        field="owner handoff done",
                        task_id=member_task_id,
                    )
                validate_visible_text(
                    verified_text,
                    field="owner handoff verification",
                    task_id=member_task_id,
                )
                if limitations_text:
                    validate_visible_text(
                        limitations_text,
                        field="owner handoff limitations",
                        task_id=member_task_id,
                    )
            payload = {
                "schema": "wb-core-owner-handoff/v1",
                "acceptance_envelope_id": identity,
                "acceptance_envelope_revision": expected_revision,
                "handoff_text": handoff_text,
            }
            digest = canonical_digest(payload)
            if envelope["owner_notified_at"]:
                if (
                    int(envelope["owner_notification_revision"])
                    == expected_revision
                    and envelope["owner_notification_digest"] == digest
                ):
                    return {
                        "acceptance_envelope_id": identity,
                        "revision": expected_revision,
                        "handoff_text": handoff_text,
                        "handoff_digest": digest,
                        "owner_notified": True,
                        "idempotent": True,
                    }
                raise RuntimeError("owner handoff revision already has different notification")
            if envelope["prepared_handoff_digest"]:
                if (
                    int(envelope["prepared_handoff_revision"]) == expected_revision
                    and envelope["prepared_handoff_text"] == handoff_text
                    and envelope["prepared_handoff_digest"] == digest
                ):
                    return {
                        "acceptance_envelope_id": identity,
                        "revision": expected_revision,
                        "handoff_text": handoff_text,
                        "handoff_digest": digest,
                        "owner_notified": False,
                        "idempotent": True,
                    }
                raise RuntimeError("owner handoff revision already has a different prepared summary")
            timestamp = _now()
            connection.execute(
                "UPDATE acceptance_envelopes SET prepared_handoff_text=?,"
                "prepared_handoff_digest=?,prepared_handoff_revision=?,prepared_handoff_at=?,"
                "updated_at=? WHERE envelope_id=?",
                (handoff_text, digest, expected_revision, timestamp, timestamp, identity),
            )
            self.event(
                connection,
                "acceptance-envelope",
                identity,
                "owner-handoff-prepared",
                {"handoff_digest": digest, "revision": expected_revision},
            )
        self.flush_events()
        return {
            "acceptance_envelope_id": identity,
            "revision": expected_revision,
            "handoff_text": handoff_text,
            "handoff_digest": digest,
            "owner_notified": False,
        }

    def accept_curator(
        self, *, curator_thread_id: str, expected_envelope_revision: int
    ) -> dict[str, object]:
        if not curator_thread_id.strip() or expected_envelope_revision <= 0:
            raise ValueError("curator acceptance requires exact identity and positive revision")
        with self.transaction() as connection:
            envelopes = connection.execute(
                "SELECT * FROM acceptance_envelopes WHERE curator_thread_id=? AND status=? "
                "ORDER BY created_at",
                (
                    curator_thread_id.strip(),
                    AcceptanceStatus.AWAITING_ACCEPTANCE.value,
                ),
            ).fetchall()
            if len(envelopes) != 1:
                raise RuntimeError(
                    "owner phrase is ambiguous: curator must have exactly one awaiting acceptance envelope"
                )
            envelope = envelopes[0]
            if int(envelope["revision"]) != expected_envelope_revision:
                raise RuntimeError("stale acceptance envelope revision")
            if (
                not envelope["owner_notified_at"]
                or not envelope["owner_notification_digest"]
                or int(envelope["owner_notification_revision"])
                != expected_envelope_revision
                or int(envelope["prepared_handoff_revision"])
                != expected_envelope_revision
                or envelope["prepared_handoff_digest"]
                != envelope["owner_notification_digest"]
            ):
                raise RuntimeError("owner acceptance requires proven curator notification")
            members = connection.execute(
                "SELECT t.* FROM acceptance_envelope_members m JOIN tasks t ON t.task_id=m.task_id "
                "WHERE m.envelope_id=? AND m.required=1 ORDER BY t.task_id",
                (envelope["envelope_id"],),
            ).fetchall()
            if not members or any(
                TaskStatus(member["status"])
                not in {TaskStatus.DONE_AWAITING_ACCEPTANCE, TaskStatus.TERMINAL_FAILURE}
                for member in members
            ):
                raise RuntimeError("acceptance envelope still has a non-terminal required member")
            timestamp = _now()
            accepted_tasks: list[str] = []
            for member in members:
                revision = int(member["revision"]) + 1
                connection.execute(
                    "UPDATE tasks SET status=?,revision=?,accepted_at=?,updated_at=? WHERE task_id=?",
                    (
                        TaskStatus.ACCEPTED.value,
                        revision,
                        timestamp,
                        timestamp,
                        member["task_id"],
                    ),
                )
                accepted_tasks.append(member["task_id"])
                self.event(
                    connection,
                    "task",
                    member["task_id"],
                    "accepted-through-envelope",
                    {"envelope_id": envelope["envelope_id"], "revision": revision},
                )
            envelope_revision = int(envelope["revision"]) + 1
            connection.execute(
                "UPDATE acceptance_envelopes SET status=?,revision=?,accepted_at=?,updated_at=? "
                "WHERE envelope_id=?",
                (
                    AcceptanceStatus.ACCEPTED.value,
                    envelope_revision,
                    timestamp,
                    timestamp,
                    envelope["envelope_id"],
                ),
            )
            self.event(
                connection,
                "acceptance-envelope",
                envelope["envelope_id"],
                "accepted",
                {"revision": envelope_revision, "task_ids": accepted_tasks},
            )
        self.flush_events()
        return {
            "acceptance_envelope_id": envelope["envelope_id"],
            "status": AcceptanceStatus.ACCEPTED.value,
            "revision": envelope_revision,
            "accepted_task_ids": accepted_tasks,
        }

    def accept(self, *, task_id: str, expected_revision: int) -> dict[str, object]:
        with self.transaction() as connection:
            row = self.task(task_id, connection)
            if self._task_envelope(connection, task_id) is not None:
                raise RuntimeError(
                    "task belongs to an acceptance envelope; use curator-owned envelope acceptance"
                )
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

    def register_executor_succession(
        self,
        *,
        envelope_id: str,
        predecessor_task_id: str,
        successor_task_id: str,
        reason: str,
        checkpoint_digest: str,
        target_readback_digest: str,
        prompt_delivery_digest: str,
        registry_link_digest: str,
        successor_active_digest: str,
    ) -> dict[str, object]:
        envelope_identity = validate_envelope_id(envelope_id)
        predecessor_task = validate_task_id(predecessor_task_id)
        successor_task = validate_task_id(successor_task_id)
        if predecessor_task == successor_task or not reason.strip():
            raise ValueError("executor succession requires distinct tasks and a reason")
        digests = {
            "checkpoint_digest": validate_digest(checkpoint_digest),
            "target_readback_digest": validate_digest(target_readback_digest),
            "prompt_delivery_digest": validate_digest(prompt_delivery_digest),
            "registry_link_digest": validate_digest(registry_link_digest),
            "successor_active_digest": validate_digest(successor_active_digest),
        }
        timestamp = _now()
        with self.transaction() as connection:
            envelope = connection.execute(
                "SELECT * FROM acceptance_envelopes WHERE envelope_id=?",
                (envelope_identity,),
            ).fetchone()
            if envelope is None or envelope["status"] == AcceptanceStatus.ACCEPTED.value:
                raise RuntimeError("executor succession requires an active acceptance envelope")
            memberships = connection.execute(
                "SELECT task_id FROM acceptance_envelope_members WHERE envelope_id=? "
                "AND task_id IN (?,?)",
                (envelope_identity, predecessor_task, successor_task),
            ).fetchall()
            if {row["task_id"] for row in memberships} != {
                predecessor_task,
                successor_task,
            }:
                raise RuntimeError("executor succession tasks must share the exact envelope")
            prior = connection.execute(
                "SELECT * FROM executor_successions WHERE envelope_id=? AND predecessor_task_id=? "
                "AND successor_task_id=?",
                (envelope_identity, predecessor_task, successor_task),
            ).fetchone()
            if prior is not None:
                expected_evidence = (
                    reason.strip(),
                    digests["checkpoint_digest"],
                    digests["target_readback_digest"],
                    digests["prompt_delivery_digest"],
                    digests["registry_link_digest"],
                    digests["successor_active_digest"],
                )
                actual_evidence = (
                    prior["reason"],
                    prior["checkpoint_digest"],
                    prior["target_readback_digest"],
                    prior["prompt_delivery_digest"],
                    prior["registry_link_digest"],
                    prior["successor_active_digest"],
                )
                if actual_evidence != expected_evidence:
                    raise RuntimeError("executor succession already has different evidence")
                return {
                    "succession_id": prior["succession_id"],
                    "status": prior["status"],
                    "predecessor_thread_id": prior["predecessor_thread_id"],
                    "successor_thread_id": prior["successor_thread_id"],
                    "idempotent": True,
                }
            active_rows = connection.execute(
                "SELECT tt.task_id,tt.thread_id,tt.generation,tt.pin_readback_digest,"
                "tt.pin_confirmed_at FROM task_threads tt "
                "JOIN acceptance_envelope_members m ON m.task_id=tt.task_id "
                "WHERE m.envelope_id=? AND tt.role='executor' AND tt.active=1 "
                "ORDER BY tt.task_id,tt.generation",
                (envelope_identity,),
            ).fetchall()
            by_task: dict[str, list[sqlite3.Row]] = {}
            for row in active_rows:
                by_task.setdefault(row["task_id"], []).append(row)
            if set(by_task) != {predecessor_task, successor_task} or any(
                len(rows) != 1 for rows in by_task.values()
            ):
                raise RuntimeError("executor succession is ambiguous")
            predecessor = by_task[predecessor_task][0]
            successor = by_task[successor_task][0]
            if (
                not successor["pin_readback_digest"]
                or not successor["pin_confirmed_at"]
            ):
                raise RuntimeError(
                    "successor must have assignment-time pin readback before succession"
                )
            payload = {
                "schema": "wb-core-executor-succession/v1",
                "acceptance_envelope_id": envelope_identity,
                "predecessor_task_id": predecessor_task,
                "predecessor_thread_id": predecessor["thread_id"],
                "predecessor_generation": int(predecessor["generation"]),
                "successor_task_id": successor_task,
                "successor_thread_id": successor["thread_id"],
                "successor_generation": int(successor["generation"]),
                "reason": reason.strip(),
                **digests,
            }
            succession_digest = canonical_digest(payload)
            succession_id = "succ-" + succession_digest.removeprefix("sha256:")[:20]
            existing = connection.execute(
                "SELECT * FROM executor_successions WHERE succession_id=?",
                (succession_id,),
            ).fetchone()
            if existing is not None:
                return {
                    "succession_id": succession_id,
                    "status": existing["status"],
                    "predecessor_thread_id": existing["predecessor_thread_id"],
                    "successor_thread_id": existing["successor_thread_id"],
                    "idempotent": True,
                }
            other = connection.execute(
                "SELECT succession_id FROM executor_successions WHERE predecessor_thread_id=?",
                (predecessor["thread_id"],),
            ).fetchone()
            if other is not None:
                raise RuntimeError("predecessor already has a different successor")
            connection.execute(
                "UPDATE task_threads SET active=0 WHERE task_id=? AND role='executor' AND thread_id=?",
                (predecessor_task, predecessor["thread_id"]),
            )
            connection.execute(
                "INSERT INTO executor_successions(succession_id,envelope_id,predecessor_task_id,"
                "predecessor_thread_id,predecessor_generation,successor_task_id,successor_thread_id,"
                "successor_generation,reason,checkpoint_digest,target_readback_digest,prompt_delivery_digest,"
                "registry_link_digest,successor_active_digest,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    succession_id,
                    envelope_identity,
                    predecessor_task,
                    predecessor["thread_id"],
                    int(predecessor["generation"]),
                    successor_task,
                    successor["thread_id"],
                    int(successor["generation"]),
                    reason.strip(),
                    digests["checkpoint_digest"],
                    digests["target_readback_digest"],
                    digests["prompt_delivery_digest"],
                    digests["registry_link_digest"],
                    digests["successor_active_digest"],
                    SuccessionStatus.READY_TO_ARCHIVE.value,
                    timestamp,
                    timestamp,
                ),
            )
            self.event(
                connection,
                "executor-succession",
                succession_id,
                "ready-to-archive",
                payload,
            )
        self.flush_events()
        return {
            "succession_id": succession_id,
            "status": SuccessionStatus.READY_TO_ARCHIVE.value,
            "predecessor_thread_id": predecessor["thread_id"],
            "successor_thread_id": successor["thread_id"],
        }

    def pending_executor_archives(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM executor_successions WHERE status=? ORDER BY created_at",
                    (SuccessionStatus.READY_TO_ARCHIVE.value,),
                ).fetchall()
            ]

    def confirm_executor_archive(
        self,
        *,
        succession_id: str,
        predecessor_thread_id: str,
        archive_readback_digest: str,
    ) -> dict[str, object]:
        digest = validate_digest(archive_readback_digest)
        if not succession_id.strip() or not predecessor_thread_id.strip():
            raise ValueError("executor archive confirmation requires exact identities")
        with self.transaction() as connection:
            succession = connection.execute(
                "SELECT * FROM executor_successions WHERE succession_id=?",
                (succession_id.strip(),),
            ).fetchone()
            if succession is None:
                raise KeyError(f"unknown executor succession: {succession_id}")
            if succession["predecessor_thread_id"] != predecessor_thread_id.strip():
                raise RuntimeError("archive readback belongs to the wrong predecessor")
            if succession["status"] == SuccessionStatus.ARCHIVED.value:
                if succession["archive_readback_digest"] != digest:
                    raise RuntimeError("executor succession already has different archive evidence")
                return {
                    "succession_id": succession_id,
                    "status": SuccessionStatus.ARCHIVED.value,
                    "idempotent": True,
                }
            predecessor = connection.execute(
                "SELECT active FROM task_threads WHERE task_id=? AND role='executor' AND thread_id=?",
                (
                    succession["predecessor_task_id"],
                    succession["predecessor_thread_id"],
                ),
            ).fetchone()
            successor = connection.execute(
                "SELECT active FROM task_threads WHERE task_id=? AND role='executor' AND thread_id=?",
                (
                    succession["successor_task_id"],
                    succession["successor_thread_id"],
                ),
            ).fetchone()
            if predecessor is None or int(predecessor["active"]) != 0:
                raise RuntimeError("predecessor is not an inactive legacy executor")
            if successor is None or int(successor["active"]) != 1:
                raise RuntimeError("successor active readback is no longer current")
            timestamp = _now()
            connection.execute(
                "UPDATE executor_successions SET status=?,archive_readback_digest=?,archived_at=?,updated_at=? "
                "WHERE succession_id=?",
                (
                    SuccessionStatus.ARCHIVED.value,
                    digest,
                    timestamp,
                    timestamp,
                    succession_id.strip(),
                ),
            )
            self.event(
                connection,
                "executor-succession",
                succession_id.strip(),
                "archived",
                {
                    "predecessor_thread_id": predecessor_thread_id.strip(),
                    "archive_readback_digest": digest,
                },
            )
        self.flush_events()
        return {"succession_id": succession_id, "status": SuccessionStatus.ARCHIVED.value}

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
        title_readback_digest: str,
        pin_readback_digest: str,
        automation_readback_digest: str,
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
        readback_digests = {
            "title_readback_digest": validate_digest(title_readback_digest),
            "pin_readback_digest": validate_digest(pin_readback_digest),
            "automation_readback_digest": validate_digest(automation_readback_digest),
        }
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
                    readback_digests["title_readback_digest"],
                    readback_digests["pin_readback_digest"],
                    readback_digests["automation_readback_digest"],
                )
                actual = (
                    existing["thread_id"],
                    existing["host_id"],
                    existing["automation_id"],
                    int(existing["max_runs"]),
                    existing["title_readback_digest"],
                    existing["pin_readback_digest"],
                    existing["automation_readback_digest"],
                )
                if actual != expected:
                    raise RuntimeError("watcher generation already has different immutable identity")
                return {
                    "generation": generation,
                    "status": existing["status"],
                    "idempotent": True,
                }
            connection.execute(
                "INSERT INTO watchers(generation,thread_id,host_id,automation_id,status,max_runs,"
                "title_readback_digest,pin_readback_digest,automation_readback_digest,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    generation,
                    thread_id.strip(),
                    host_id.strip(),
                    automation_id.strip(),
                    "PREPARED",
                    max_runs,
                    readback_digests["title_readback_digest"],
                    readback_digests["pin_readback_digest"],
                    readback_digests["automation_readback_digest"],
                    _now(),
                ),
            )
            self.event(connection, "watcher", str(generation), "prepared", {"thread_id": thread_id})
        self.flush_events()
        return {"generation": generation, "status": "PREPARED"}

    def confirm_watcher_readback(
        self,
        *,
        generation: int,
        thread_id: str,
        automation_id: str,
        title_readback_digest: str,
        pin_readback_digest: str,
        automation_readback_digest: str,
    ) -> dict[str, object]:
        if generation <= 0 or not thread_id.strip() or not automation_id.strip():
            raise ValueError("watcher readback requires exact generation identities")
        evidence = {
            "title_readback_digest": validate_digest(title_readback_digest),
            "pin_readback_digest": validate_digest(pin_readback_digest),
            "automation_readback_digest": validate_digest(automation_readback_digest),
        }
        with self.transaction() as connection:
            watcher = connection.execute(
                "SELECT * FROM watchers WHERE generation=?", (generation,)
            ).fetchone()
            if watcher is None:
                raise KeyError(f"unknown watcher generation: {generation}")
            if (
                watcher["thread_id"] != thread_id.strip()
                or watcher["automation_id"] != automation_id.strip()
            ):
                raise RuntimeError("watcher readback belongs to a different generation")
            existing = {
                key: str(watcher[key])
                for key in evidence
                if watcher[key]
            }
            if any(existing[key] != evidence[key] for key in existing):
                raise RuntimeError("watcher generation already has different UI readback evidence")
            updated = any(not watcher[key] for key in evidence)
            if updated:
                connection.execute(
                    "UPDATE watchers SET title_readback_digest=?,pin_readback_digest=?,"
                    "automation_readback_digest=? WHERE generation=?",
                    (
                        evidence["title_readback_digest"],
                        evidence["pin_readback_digest"],
                        evidence["automation_readback_digest"],
                        generation,
                    ),
                )
                self.event(
                    connection,
                    "watcher",
                    str(generation),
                    "ui-readback-confirmed",
                    evidence,
                )
        self.flush_events()
        return {
            "generation": generation,
            "status": watcher["status"],
            **evidence,
            "idempotent": not updated,
        }

    def smoke_watcher(self, *, generation: int, evidence_digest: str) -> dict[str, object]:
        digest = validate_digest(evidence_digest)
        with self.transaction() as connection:
            watcher = connection.execute(
                "SELECT * FROM watchers WHERE generation=?", (generation,)
            ).fetchone()
            if watcher is None or watcher["status"] not in {"PREPARED", "ACTIVE"}:
                raise RuntimeError("watcher smoke requires a prepared generation")
            if any(
                not watcher[field]
                for field in (
                    "title_readback_digest",
                    "pin_readback_digest",
                    "automation_readback_digest",
                )
            ):
                raise RuntimeError(
                    "watcher smoke requires title, pin and automation readback evidence"
                )
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
            if any(
                not prepared[field]
                for field in (
                    "title_readback_digest",
                    "pin_readback_digest",
                    "automation_readback_digest",
                )
            ):
                raise RuntimeError("watcher activation requires UI readback evidence")
            connection.execute(
                "UPDATE watchers SET status='RETIRED',retired_at=?,retirement_required=1,"
                "successor_generation=? WHERE status='ACTIVE' AND generation<>?",
                (_now(), generation, generation),
            )
            connection.execute(
                "DELETE FROM runtime_leases WHERE name='watcher-run' AND generation<>?",
                (generation,),
            )
            connection.execute("UPDATE watchers SET status='ACTIVE',activated_at=?,retired_at=NULL WHERE generation=?", (_now(), generation))
            self.event(connection, "watcher", str(generation), "activated", {"previous_retired": True})
        self.flush_events()
        return {"generation": generation, "status": "ACTIVE"}

    def pending_watcher_retirements(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM watchers WHERE retirement_required=1 AND archived_at IS NULL "
                    "ORDER BY generation"
                ).fetchall()
            ]

    def confirm_watcher_retirement(
        self,
        *,
        generation: int,
        successor_generation: int,
        automation_paused_digest: str,
        archive_readback_digest: str,
    ) -> dict[str, object]:
        paused_digest = validate_digest(automation_paused_digest)
        archive_digest = validate_digest(archive_readback_digest)
        with self.transaction() as connection:
            watcher = connection.execute(
                "SELECT * FROM watchers WHERE generation=?", (generation,)
            ).fetchone()
            successor = connection.execute(
                "SELECT * FROM watchers WHERE generation=?", (successor_generation,)
            ).fetchone()
            if watcher is None or successor is None:
                raise KeyError("watcher retirement requires both exact generations")
            if (
                watcher["status"] != "RETIRED"
                or int(watcher["retirement_required"]) != 1
                or int(watcher["successor_generation"] or 0) != successor_generation
                or successor["status"] != "ACTIVE"
            ):
                raise RuntimeError("watcher retirement does not follow an active successor")
            if watcher["archived_at"]:
                if (
                    watcher["automation_paused_digest"] != paused_digest
                    or watcher["archive_readback_digest"] != archive_digest
                ):
                    raise RuntimeError("watcher retirement already has different evidence")
                return {
                    "generation": generation,
                    "successor_generation": successor_generation,
                    "status": "ARCHIVED",
                    "idempotent": True,
                }
            timestamp = _now()
            connection.execute(
                "UPDATE watchers SET automation_paused_digest=?,archive_readback_digest=?,"
                "archived_at=? WHERE generation=?",
                (paused_digest, archive_digest, timestamp, generation),
            )
            self.event(
                connection,
                "watcher",
                str(generation),
                "retirement-confirmed",
                {
                    "successor_generation": successor_generation,
                    "automation_paused_digest": paused_digest,
                    "archive_readback_digest": archive_digest,
                },
            )
        self.flush_events()
        return {
            "generation": generation,
            "successor_generation": successor_generation,
            "status": "ARCHIVED",
        }

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
            attention_events = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM attention_events ORDER BY created_at,event_id"
                ).fetchall()
            ]
            acceptance_envelopes = [
                {
                    **dict(row),
                    "members": [
                        dict(member)
                        for member in connection.execute(
                            "SELECT task_id,role,required FROM acceptance_envelope_members "
                            "WHERE envelope_id=? ORDER BY role,task_id",
                            (row["envelope_id"],),
                        ).fetchall()
                    ],
                }
                for row in connection.execute(
                    "SELECT * FROM acceptance_envelopes ORDER BY created_at"
                ).fetchall()
            ]
            executor_successions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM executor_successions ORDER BY created_at"
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
            "attention_events": attention_events,
            "acceptance_envelopes": acceptance_envelopes,
            "executor_successions": executor_successions,
            "integrity": self.integrity(),
        }

    def _visible_envelopes(
        self, connection: sqlite3.Connection
    ) -> list[dict[str, Any]]:
        envelopes: list[dict[str, Any]] = []
        bound_task_ids: set[str] = set()
        for envelope in connection.execute(
            "SELECT * FROM acceptance_envelopes WHERE status<>? ORDER BY created_at",
            (AcceptanceStatus.ACCEPTED.value,),
        ).fetchall():
            members = connection.execute(
                "SELECT t.*,m.role,m.required FROM acceptance_envelope_members m "
                "JOIN tasks t ON t.task_id=m.task_id WHERE m.envelope_id=? AND m.required=1 "
                "ORDER BY t.created_at,t.task_id",
                (envelope["envelope_id"],),
            ).fetchall()
            if not members:
                continue
            bound_task_ids.update(str(member["task_id"]) for member in members)
            envelopes.append({"envelope": envelope, "members": members})
        for task in connection.execute(
            "SELECT * FROM tasks WHERE status<>? ORDER BY created_at",
            (TaskStatus.ACCEPTED.value,),
        ).fetchall():
            if task["task_id"] in bound_task_ids:
                continue
            envelopes.append(
                {
                    "envelope": {
                        "envelope_id": task["task_id"],
                        "title": task["title"],
                        "status": AcceptanceStatus.OPEN.value,
                        "revision": int(task["revision"]),
                    },
                    "members": [task],
                }
            )
        return envelopes

    @staticmethod
    def _visible_envelope_status(
        envelope: Mapping[str, object], members: list[sqlite3.Row]
    ) -> TaskStatus:
        statuses = [TaskStatus(member["status"]) for member in members]
        if envelope["status"] == AcceptanceStatus.AWAITING_ACCEPTANCE.value:
            if TaskStatus.TERMINAL_FAILURE in statuses:
                return TaskStatus.TERMINAL_FAILURE
            return TaskStatus.DONE_AWAITING_ACCEPTANCE
        if envelope["status"] == AcceptanceStatus.DONE_PENDING_HANDOFF.value:
            if TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF in statuses:
                return TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF
            return TaskStatus.DONE_PENDING_HANDOFF
        for preferred in (
            TaskStatus.AWAITING_HUMAN,
            TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
            TaskStatus.RECOVERING,
            TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF,
            TaskStatus.READY_FOR_RELEASE,
            TaskStatus.RELEASE_OWNED,
            TaskStatus.VERIFYING,
            TaskStatus.WORKING,
            TaskStatus.DISPATCHING,
            TaskStatus.DISCUSSION,
        ):
            if preferred in statuses:
                return preferred
        if all(status == TaskStatus.TERMINAL_FAILURE for status in statuses):
            return TaskStatus.TERMINAL_FAILURE
        critical = min(
            members,
            key=lambda member: (
                int(member["progress_percent"]),
                member["updated_at"],
            ),
        )
        return TaskStatus(critical["status"])

    def report(self, *, record: bool = False) -> str:
        blocks: list[str] = []
        context = self.transaction() if record else self.connect()
        with context as connection:
            for group in self._visible_envelopes(connection):
                envelope = group["envelope"]
                members = list(group["members"])
                minimum_progress = min(
                    int(member["progress_percent"]) for member in members
                )
                critical_candidates = [
                    member
                    for member in members
                    if int(member["progress_percent"]) == minimum_progress
                ]
                critical = max(
                    critical_candidates, key=lambda member: member["updated_at"]
                )
                status = self._visible_envelope_status(envelope, members)
                title = str(envelope["title"])
                for member in members:
                    title = validate_visible_text(
                        title,
                        field="task title",
                        task_id=str(member["task_id"]),
                    )
                eta = validate_visible_text(
                    str(critical["eta_text"]),
                    field="eta",
                    task_id=str(critical["task_id"]),
                )
                delta = validate_visible_text(
                    str(critical["last_delta"]),
                    field="delta",
                    task_id=str(critical["task_id"]),
                )
                current = validate_visible_text(
                    str(critical["current_action"]),
                    field="current action",
                    task_id=str(critical["task_id"]),
                )
                owner_notification_current = (
                    envelope["status"] == AcceptanceStatus.AWAITING_ACCEPTANCE.value
                    and bool(envelope["owner_notified_at"])
                    and int(envelope["owner_notification_revision"])
                    == int(envelope["revision"])
                    and bool(envelope["owner_notification_digest"])
                )
                if envelope["status"] == AcceptanceStatus.AWAITING_ACCEPTANCE.value:
                    current = (
                        "Ожидается приёмка владельца."
                        if owner_notification_current
                        else "Куратор готовит короткий итог владельцу."
                    )
                for member in members:
                    member_task_id = str(member["task_id"])
                    eta = validate_visible_text(
                        eta, field="eta", task_id=member_task_id
                    )
                    delta = validate_visible_text(
                        delta, field="delta", task_id=member_task_id
                    )
                    current = validate_visible_text(
                        current, field="current action", task_id=member_task_id
                    )
                blocker = ""
                if status in {
                    TaskStatus.AWAITING_HUMAN,
                    TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
                }:
                    blocked = next(
                        (member for member in members if member["blocker"]), None
                    )
                    if blocked is not None:
                        blocker = validate_visible_text(
                            str(blocked["blocker"]),
                            field="blocker",
                            task_id=str(blocked["task_id"]),
                        )
                        for member in members:
                            blocker = validate_visible_text(
                                blocker,
                                field="blocker",
                                task_id=str(member["task_id"]),
                            )
                fingerprint = canonical_digest(
                    {
                        "envelope_status": envelope["status"],
                        "visible_status": status.value,
                        "progress": minimum_progress,
                        "eta": eta,
                        "delta": delta,
                        "current": current,
                        "blocker": blocker,
                        "owner_notification_current": owner_notification_current,
                    }
                )
                previous = connection.execute(
                    "SELECT last_fingerprint FROM visible_report_state WHERE envelope_id=?",
                    (envelope["envelope_id"],),
                ).fetchone()
                if previous is not None and previous["last_fingerprint"] == fingerprint:
                    delta = f"Изменений нет; работа продолжается: {current}"
                lines = [
                    f"Статус: {report_status(status)}",
                    f"Задача: {title}",
                    f"Прогресс: ≈{minimum_progress}% · Осталось: ≈{eta}",
                    f"С прошлого отчёта: {delta}",
                    f"Сейчас: {current}",
                ]
                if blocker:
                    lines.append(f"Блокер: {blocker}")
                blocks.append("\n".join(lines))
                if record:
                    connection.execute(
                        "INSERT INTO visible_report_state(envelope_id,last_fingerprint,last_rendered_at) "
                        "VALUES(?,?,?) ON CONFLICT(envelope_id) DO UPDATE SET "
                        "last_fingerprint=excluded.last_fingerprint,last_rendered_at=excluded.last_rendered_at",
                        (envelope["envelope_id"], fingerprint, _now()),
                    )
        if record:
            self.flush_events()
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
                    "(status IN ('AWAITING_HUMAN','AWAITING_HUMAN_PENDING_HANDOFF') "
                    "AND (blocker='' OR human_reason='')) OR "
                    "(status NOT IN ('AWAITING_HUMAN','AWAITING_HUMAN_PENDING_HANDOFF') "
                    "AND (blocker<>'' OR human_reason<>''))"
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
            invalid_attention_events = int(
                connection.execute(
                    "SELECT count(*) FROM attention_events WHERE "
                    "(state='LEASED' AND (lease_owner='' OR lease_expires_at IS NULL)) OR "
                    "(state<>'LEASED' AND (lease_owner<>'' OR lease_expires_at IS NOT NULL)) OR "
                    "(state='ACKED' AND (ack_evidence_digest='' OR acked_by_thread_id='' OR acked_at IS NULL))"
                ).fetchone()[0]
            )
            unproven_terminal_tasks = int(
                connection.execute(
                    "SELECT count(*) FROM tasks t JOIN acceptance_envelope_members m ON m.task_id=t.task_id "
                    "WHERE t.status IN ('DONE_AWAITING_ACCEPTANCE','TERMINAL_FAILURE') "
                    "AND NOT EXISTS (SELECT 1 FROM attention_events a WHERE a.task_id=t.task_id "
                    "AND a.state='ACKED' AND a.event_kind IN ('TECHNICAL_COMPLETION','TERMINAL_FAILURE'))"
                ).fetchone()[0]
            )
            invalid_envelopes = int(
                connection.execute(
                    "SELECT count(*) FROM acceptance_envelopes e WHERE "
                    "(e.status='AWAITING_ACCEPTANCE' AND EXISTS ("
                    "SELECT 1 FROM acceptance_envelope_members m JOIN tasks t ON t.task_id=m.task_id "
                    "WHERE m.envelope_id=e.envelope_id AND m.required=1 "
                    "AND t.status NOT IN ('DONE_AWAITING_ACCEPTANCE','TERMINAL_FAILURE'))) OR "
                    "(e.status='ACCEPTED' AND EXISTS ("
                    "SELECT 1 FROM acceptance_envelope_members m JOIN tasks t ON t.task_id=m.task_id "
                    "WHERE m.envelope_id=e.envelope_id AND m.required=1 AND t.status<>'ACCEPTED'))"
                ).fetchone()[0]
            )
            invalid_successions = int(
                connection.execute(
                    "SELECT count(*) FROM executor_successions s WHERE "
                    "(s.status='ARCHIVED' AND (s.archive_readback_digest='' OR s.archived_at IS NULL)) OR "
                    "NOT EXISTS (SELECT 1 FROM acceptance_envelope_members m "
                    "WHERE m.envelope_id=s.envelope_id AND m.task_id=s.predecessor_task_id) OR "
                    "NOT EXISTS (SELECT 1 FROM acceptance_envelope_members m "
                    "WHERE m.envelope_id=s.envelope_id AND m.task_id=s.successor_task_id)"
                ).fetchone()[0]
            )
            invalid_active_role_pins = int(
                connection.execute(
                    "SELECT count(*) FROM task_threads tt JOIN tasks t ON t.task_id=tt.task_id "
                    "WHERE tt.active=1 AND tt.role IN ('curator','executor') AND t.status<>'ACCEPTED' "
                    "AND (tt.pin_readback_digest='' OR tt.pin_confirmed_at IS NULL)"
                ).fetchone()[0]
            )
            invalid_owner_notifications = int(
                connection.execute(
                    "SELECT count(*) FROM acceptance_envelopes WHERE status<>'ACCEPTED' AND ("
                    "(owner_notified_at IS NOT NULL AND (owner_notification_digest='' "
                    "OR owner_notification_revision<>revision "
                    "OR prepared_handoff_digest<>owner_notification_digest "
                    "OR prepared_handoff_revision<>revision)) OR "
                    "(owner_notified_at IS NULL AND (owner_notification_digest<>'' "
                    "OR owner_notification_revision<>0)) OR "
                    "(prepared_handoff_digest<>'' AND (prepared_handoff_text='' "
                    "OR prepared_handoff_revision<>revision OR prepared_handoff_at IS NULL "
                    "OR status<>'AWAITING_ACCEPTANCE')) OR "
                    "(prepared_handoff_digest='' AND (prepared_handoff_text<>'' "
                    "OR prepared_handoff_revision<>0 OR prepared_handoff_at IS NOT NULL)))"
                ).fetchone()[0]
            )
            invalid_active_watcher_readbacks = int(
                connection.execute(
                    "SELECT count(*) FROM watchers WHERE status='ACTIVE' AND "
                    "(title_readback_digest='' OR pin_readback_digest='' "
                    "OR automation_readback_digest='')"
                ).fetchone()[0]
            )
            incomplete_watcher_retirements = int(
                connection.execute(
                    "SELECT count(*) FROM watchers WHERE retirement_required=1 AND "
                    "(successor_generation IS NULL OR automation_paused_digest='' "
                    "OR archive_readback_digest='' OR archived_at IS NULL)"
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
                and invalid_attention_events == 0
                and unproven_terminal_tasks == 0
                and invalid_envelopes == 0
                and invalid_successions == 0
                and invalid_active_role_pins == 0
                and invalid_owner_notifications == 0
                and invalid_active_watcher_readbacks == 0
                and incomplete_watcher_retirements == 0
            ),
            "sqlite": quick,
            "active_watchers": active_watchers,
            "stale_locks": stale_locks,
            "invalid_human_blocks": invalid_human_blocks,
            "unsmoked_active_watchers": unsmoked_active_watchers,
            "unproven_verified_incidents": unproven_verified_incidents,
            "invalid_attention_events": invalid_attention_events,
            "unproven_terminal_tasks": unproven_terminal_tasks,
            "invalid_envelopes": invalid_envelopes,
            "invalid_successions": invalid_successions,
            "invalid_active_role_pins": invalid_active_role_pins,
            "invalid_owner_notifications": invalid_owner_notifications,
            "invalid_active_watcher_readbacks": invalid_active_watcher_readbacks,
            "incomplete_watcher_retirements": incomplete_watcher_retirements,
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
    register.add_argument("--curator-pin-readback-digest", required=True)
    register.add_argument("--executor-pin-readback-digest", required=True)
    register.add_argument("--acceptance-envelope", default="")
    register.add_argument("--acceptance-title", default="")
    register.add_argument(
        "--acceptance-role",
        choices=("root", "corrective", "required-child"),
        default="root",
    )

    add_thread = commands.add_parser("add-thread")
    add_thread.add_argument("--task-id", required=True)
    add_thread.add_argument("--role", choices=("curator", "executor", "arbiter"), required=True)
    add_thread.add_argument("--generation", type=int, required=True)
    add_thread.add_argument("--thread-id", required=True)
    add_thread.add_argument("--host-id", required=True)
    add_thread.add_argument("--pin-readback-digest", default="")

    confirm_role_pin = commands.add_parser("confirm-role-pin")
    confirm_role_pin.add_argument("--thread-id", required=True)
    confirm_role_pin.add_argument(
        "--role", choices=("curator", "executor"), required=True
    )
    confirm_role_pin.add_argument("--pin-readback-digest", required=True)

    bind_acceptance = commands.add_parser("bind-acceptance")
    bind_acceptance.add_argument("--envelope-id", required=True)
    bind_acceptance.add_argument("--title", required=True)
    bind_acceptance.add_argument("--curator-thread", required=True)
    bind_acceptance.add_argument("--root-task", required=True)
    bind_acceptance.add_argument("--corrective-task", action="append", default=[])
    reconcile_acceptance = commands.add_parser("reconcile-acceptance")
    reconcile_acceptance.add_argument("--envelope-id", required=True)

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

    enqueue_attention = commands.add_parser("enqueue-attention")
    enqueue_attention.add_argument("--task-id", required=True)
    enqueue_attention.add_argument("--expected-revision", type=int, required=True)
    enqueue_attention.add_argument(
        "--kind", choices=[item.value for item in AttentionKind], required=True
    )
    enqueue_attention.add_argument("--evidence-summary", required=True)
    enqueue_attention.add_argument("--evidence-digest", required=True)
    enqueue_attention.add_argument("--backfill", action="store_true")
    enqueue_attention.add_argument("--eta")
    enqueue_attention.add_argument("--delta")
    enqueue_attention.add_argument("--current")
    enqueue_attention.add_argument("--blocker", default="")
    enqueue_attention.add_argument(
        "--human-reason", choices=sorted(STRICT_HUMAN_REASONS), default=""
    )
    enqueue_attention.add_argument(
        "--repo-owned-remediation-available", action="store_true"
    )
    enqueue_attention.add_argument("--remediation-exhausted", action="store_true")

    reserve_attention = commands.add_parser("reserve-attention")
    reserve_attention.add_argument("--generation", type=int, required=True)
    reserve_attention.add_argument("--owner", required=True)
    reserve_attention.add_argument("--lease-seconds", type=int, default=120)
    reserve_attention.add_argument("--limit", type=int, default=8)

    sent_attention = commands.add_parser("mark-attention-sent")
    sent_attention.add_argument("--event-id", required=True)
    sent_attention.add_argument("--owner", required=True)
    sent_attention.add_argument("--transport-receipt-digest", required=True)
    sent_attention.add_argument("--ack-timeout-seconds", type=int, default=600)

    retry_attention = commands.add_parser("retry-attention")
    retry_attention.add_argument("--event-id", required=True)
    retry_attention.add_argument("--owner", required=True)
    retry_attention.add_argument("--error", required=True)
    retry_attention.add_argument("--retry-after-seconds", type=int, default=60)

    attention = commands.add_parser("attention")
    attention.add_argument("--event-id", required=True)

    ack_attention = commands.add_parser("ack-attention")
    ack_attention.add_argument("--event-id", required=True)
    ack_attention.add_argument("--event-digest", required=True)
    ack_attention.add_argument("--curator-thread", required=True)
    ack_attention.add_argument("--expected-task-revision", type=int, required=True)
    ack_attention.add_argument("--ack-evidence-digest", required=True)

    notify_owner = commands.add_parser("confirm-owner-notification")
    notify_owner.add_argument("--curator-thread", required=True)
    notify_owner.add_argument("--envelope-id", required=True)
    notify_owner.add_argument("--expected-revision", type=int, required=True)
    notify_owner.add_argument("--notification-evidence-digest", required=True)

    prepare_handoff = commands.add_parser("prepare-owner-handoff")
    prepare_handoff.add_argument("--curator-thread", required=True)
    prepare_handoff.add_argument("--envelope-id", required=True)
    prepare_handoff.add_argument("--expected-revision", type=int, required=True)
    prepare_handoff.add_argument("--done", action="append", required=True)
    prepare_handoff.add_argument("--verified", required=True)
    prepare_handoff.add_argument("--limitations", default="")

    accept_curator = commands.add_parser("accept-curator")
    accept_curator.add_argument("--curator-thread", required=True)
    accept_curator.add_argument("--expected-envelope-revision", type=int, required=True)

    succession = commands.add_parser("register-executor-succession")
    succession.add_argument("--envelope-id", required=True)
    succession.add_argument("--predecessor-task", required=True)
    succession.add_argument("--successor-task", required=True)
    succession.add_argument("--reason", required=True)
    succession.add_argument("--checkpoint-digest", required=True)
    succession.add_argument("--target-readback-digest", required=True)
    succession.add_argument("--prompt-delivery-digest", required=True)
    succession.add_argument("--registry-link-digest", required=True)
    succession.add_argument("--successor-active-digest", required=True)

    commands.add_parser("pending-executor-archives")
    confirm_archive = commands.add_parser("confirm-executor-archive")
    confirm_archive.add_argument("--succession-id", required=True)
    confirm_archive.add_argument("--predecessor-thread", required=True)
    confirm_archive.add_argument("--archive-readback-digest", required=True)

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
    watcher.add_argument("--title-readback-digest", required=True)
    watcher.add_argument("--pin-readback-digest", required=True)
    watcher.add_argument("--automation-readback-digest", required=True)
    watcher.add_argument("--max-runs", type=int, default=720)
    watcher_readback = commands.add_parser("confirm-watcher-readback")
    watcher_readback.add_argument("--generation", type=int, required=True)
    watcher_readback.add_argument("--thread-id", required=True)
    watcher_readback.add_argument("--automation-id", required=True)
    watcher_readback.add_argument("--title-readback-digest", required=True)
    watcher_readback.add_argument("--pin-readback-digest", required=True)
    watcher_readback.add_argument("--automation-readback-digest", required=True)
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
    commands.add_parser("pending-watcher-retirements")
    watcher_retirement = commands.add_parser("confirm-watcher-retirement")
    watcher_retirement.add_argument("--generation", type=int, required=True)
    watcher_retirement.add_argument(
        "--successor-generation", type=int, required=True
    )
    watcher_retirement.add_argument("--automation-paused-digest", required=True)
    watcher_retirement.add_argument("--archive-readback-digest", required=True)

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
                curator_pin_readback_digest=args.curator_pin_readback_digest,
                executor_pin_readback_digest=args.executor_pin_readback_digest,
                acceptance_envelope_id=args.acceptance_envelope,
                acceptance_title=args.acceptance_title,
                acceptance_role=args.acceptance_role,
            )
        elif args.command == "add-thread":
            result = registry.add_thread(
                task_id=args.task_id,
                role=args.role,
                generation=args.generation,
                thread_id=args.thread_id,
                host_id=args.host_id,
                pin_readback_digest=args.pin_readback_digest,
            )
        elif args.command == "confirm-role-pin":
            result = registry.confirm_role_pin(
                thread_id=args.thread_id,
                role=args.role,
                pin_readback_digest=args.pin_readback_digest,
            )
        elif args.command == "bind-acceptance":
            result = registry.bind_acceptance_envelope(
                envelope_id=args.envelope_id,
                title=args.title,
                curator_thread_id=args.curator_thread,
                root_task_id=args.root_task,
                corrective_task_ids=args.corrective_task,
            )
        elif args.command == "reconcile-acceptance":
            result = registry.reconcile_acceptance(envelope_id=args.envelope_id)
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
        elif args.command == "enqueue-attention":
            result = registry.enqueue_attention(
                task_id=args.task_id,
                expected_revision=args.expected_revision,
                kind=AttentionKind(args.kind),
                evidence_summary=args.evidence_summary,
                evidence_digest=args.evidence_digest,
                backfill=args.backfill,
                eta=args.eta,
                delta=args.delta,
                current=args.current,
                blocker=args.blocker,
                human_reason=args.human_reason,
                repo_owned_remediation_available=args.repo_owned_remediation_available,
                remediation_exhausted=args.remediation_exhausted,
            )
        elif args.command == "reserve-attention":
            result = registry.reserve_attention(
                generation=args.generation,
                owner=args.owner,
                lease_seconds=args.lease_seconds,
                limit=args.limit,
            )
        elif args.command == "mark-attention-sent":
            result = registry.mark_attention_sent(
                event_id=args.event_id,
                owner=args.owner,
                transport_receipt_digest=args.transport_receipt_digest,
                ack_timeout_seconds=args.ack_timeout_seconds,
            )
        elif args.command == "retry-attention":
            result = registry.retry_attention(
                event_id=args.event_id,
                owner=args.owner,
                error=args.error,
                retry_after_seconds=args.retry_after_seconds,
            )
        elif args.command == "attention":
            result = registry.attention_event(args.event_id)
        elif args.command == "ack-attention":
            result = registry.ack_attention(
                event_id=args.event_id,
                event_digest=args.event_digest,
                curator_thread_id=args.curator_thread,
                expected_task_revision=args.expected_task_revision,
                ack_evidence_digest=args.ack_evidence_digest,
            )
        elif args.command == "confirm-owner-notification":
            result = registry.confirm_owner_notification(
                curator_thread_id=args.curator_thread,
                envelope_id=args.envelope_id,
                expected_revision=args.expected_revision,
                notification_evidence_digest=args.notification_evidence_digest,
            )
        elif args.command == "prepare-owner-handoff":
            result = registry.prepare_owner_handoff(
                curator_thread_id=args.curator_thread,
                envelope_id=args.envelope_id,
                expected_revision=args.expected_revision,
                done=args.done,
                verified=args.verified,
                limitations=args.limitations,
            )
        elif args.command == "accept-curator":
            result = registry.accept_curator(
                curator_thread_id=args.curator_thread,
                expected_envelope_revision=args.expected_envelope_revision,
            )
        elif args.command == "register-executor-succession":
            result = registry.register_executor_succession(
                envelope_id=args.envelope_id,
                predecessor_task_id=args.predecessor_task,
                successor_task_id=args.successor_task,
                reason=args.reason,
                checkpoint_digest=args.checkpoint_digest,
                target_readback_digest=args.target_readback_digest,
                prompt_delivery_digest=args.prompt_delivery_digest,
                registry_link_digest=args.registry_link_digest,
                successor_active_digest=args.successor_active_digest,
            )
        elif args.command == "pending-executor-archives":
            result = registry.pending_executor_archives()
        elif args.command == "confirm-executor-archive":
            result = registry.confirm_executor_archive(
                succession_id=args.succession_id,
                predecessor_thread_id=args.predecessor_thread,
                archive_readback_digest=args.archive_readback_digest,
            )
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
                title_readback_digest=args.title_readback_digest,
                pin_readback_digest=args.pin_readback_digest,
                automation_readback_digest=args.automation_readback_digest,
                max_runs=args.max_runs,
            )
        elif args.command == "confirm-watcher-readback":
            result = registry.confirm_watcher_readback(
                generation=args.generation,
                thread_id=args.thread_id,
                automation_id=args.automation_id,
                title_readback_digest=args.title_readback_digest,
                pin_readback_digest=args.pin_readback_digest,
                automation_readback_digest=args.automation_readback_digest,
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
        elif args.command == "pending-watcher-retirements":
            result = registry.pending_watcher_retirements()
        elif args.command == "confirm-watcher-retirement":
            result = registry.confirm_watcher_retirement(
                generation=args.generation,
                successor_generation=args.successor_generation,
                automation_paused_digest=args.automation_paused_digest,
                archive_readback_digest=args.archive_readback_digest,
            )
        elif args.command == "list":
            result = registry.active_tasks()
        elif args.command == "snapshot":
            result = registry.snapshot()
        elif args.command == "report":
            print(registry.report(record=True))
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
