"""Scheduled read-only observer for externally visible seller changes.

Admission owns only the scheduled-slot idempotency key and seller/account
lease. All WB GET calls run without a SQLite transaction. The canonical
baseline engine then persists its checkpoint/facts and observer metadata in
one short atomic transaction through a transaction hook.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Mapping

from packages.application.change_registry import (
    ANNOTATION_REVISIONS_TABLE,
    CHECKPOINTS_TABLE,
    CHECKPOINT_SOURCE_MANIFESTS_TABLE,
    FACTS_TABLE,
    IDENTITY_INCIDENTS_TABLE,
    ITEMS_TABLE,
    MANUAL_PENDING_CURRENT_TABLE,
    MANUAL_PENDING_EVENTS_TABLE,
    OBSERVER_HEALTH_EVENTS_TABLE,
    OBSERVER_JOB_EVENTS_TABLE,
    OBSERVER_JOBS_TABLE,
    OBSERVER_LEASES_TABLE,
    ChangeRegistryRepository,
    canonical_digest,
    canonical_json,
)
from packages.application.change_registry_baseline_engine import (
    SOURCE_SURFACE,
    ChangeRegistryBaselineEngine,
)
from packages.application.change_registry_source_acquisition import (
    ChangeRegistrySourceAcquirer,
    canonical_utc_timestamp,
    canonicalize_acquisition_timestamps,
)
from packages.application.storage_registry import StorageRegistryError, StoreRegistry


CONTRACT_NAME = "wb_change_registry_observer"
CONTRACT_VERSION = 1
DEFAULT_ACCOUNT_SCOPE = "seller-portal-primary"
LEASE_SECONDS = 1800
TERMINAL_JOB_STATES = frozenset({"complete", "partial", "failed", "busy"})
RUNNABLE_JOB_STATES = frozenset({"accepted", "running"})
_DEPLOYED_SHA = re.compile(r"[0-9a-f]{40}")
SOURCE_STATUSES = {"not_observed", "complete", "partial", "failed", "invalid"}
PERSISTENCE_STAGE_BINDINGS = {
    "baseline_ingest": ("change_registry_baseline", "ingest"),
    "baseline_result": ("change_registry_baseline", "transaction_hook_result"),
    "source_manifest_prices": (CHECKPOINT_SOURCE_MANIFESTS_TABLE, "insert_prices"),
    "source_manifest_ads": (CHECKPOINT_SOURCE_MANIFESTS_TABLE, "insert_ads"),
    "terminal_job_event": (OBSERVER_JOB_EVENTS_TABLE, "insert_terminal"),
    "scheduled_health": (OBSERVER_HEALTH_EVENTS_TABLE, "insert_scheduled_health"),
    "lease_release": (OBSERVER_LEASES_TABLE, "cas_release"),
    "transaction_commit": ("operational_store", "commit"),
}
FALLBACK_STAGE_BINDINGS = {
    "fallback_store_open": ("operational_store", "open_failure_evidence_rw"),
    "fallback_begin": ("operational_store", "begin_immediate_failure_evidence"),
    "fallback_terminal_job_event": (OBSERVER_JOB_EVENTS_TABLE, "insert_failed_terminal"),
    "fallback_scheduled_health": (OBSERVER_HEALTH_EVENTS_TABLE, "insert_failed_health"),
    "fallback_lease_release": (OBSERVER_LEASES_TABLE, "cas_release"),
    "fallback_commit": ("operational_store", "commit_failure_evidence"),
}
_SAFE_IDENTIFIER = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?"
    r"(?:,\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)*"
)
_SAFE_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
_TRIGGER_CONSTRAINTS = {
    "change registry canonical row is immutable": "change_registry_immutable",
    "change registry canonical row is append-only": "change_registry_append_only",
    "change registry observer lease CAS mismatch": "change_registry_observer_lease_cas",
    "change registry observer lease rows are retained": "change_registry_observer_lease_retained",
}
_SQLITE_CATEGORIES = {
    "SQLITE_CONSTRAINT_UNIQUE": "unique",
    "SQLITE_CONSTRAINT_PRIMARYKEY": "primary_key",
    "SQLITE_CONSTRAINT_FOREIGNKEY": "foreign_key",
    "SQLITE_CONSTRAINT_NOTNULL": "not_null",
    "SQLITE_CONSTRAINT_CHECK": "check",
    "SQLITE_CONSTRAINT_TRIGGER": "trigger",
    "SQLITE_CONSTRAINT_ROWID": "rowid",
    "SQLITE_CONSTRAINT": "constraint",
    "SQLITE_BUSY": "busy",
    "SQLITE_LOCKED": "locked",
    "SQLITE_READONLY": "readonly",
    "SQLITE_FULL": "full",
    "SQLITE_IOERR": "io",
    "SQLITE_CORRUPT": "corrupt",
    "SQLITE_SCHEMA": "schema",
}


class ChangeRegistryObserverError(ValueError):
    """Fail-closed observer validation error."""


class ChangeRegistryObserverBusy(ChangeRegistryObserverError):
    """Another scan owns the exact seller/account lease."""


class ChangeRegistryObserverTerminalEvidenceError(ChangeRegistryObserverError):
    """Both the primary failure and fallback persistence remain inspectable."""

    def __init__(
        self,
        primary: Mapping[str, Any],
        fallback: Mapping[str, Any],
        rescue: Mapping[str, Any],
    ) -> None:
        super().__init__(
            "observer terminal evidence persistence failed after a primary failure"
        )
        self.primary_evidence = dict(primary)
        self.fallback_evidence = dict(fallback)
        self.rescue_evidence = dict(rescue)


@dataclass
class _PersistenceStage:
    stage: str = ""
    table: str = ""
    operation: str = ""

    def enter(self, stage: str, bindings: Mapping[str, tuple[str, str]]) -> None:
        table, operation = bindings[stage]
        self.stage = stage
        self.table = table
        self.operation = operation


def utc_now() -> str:
    return canonical_utc_timestamp(datetime.now(timezone.utc))


def activation_job_id(deployed_sha: str) -> str:
    exact_sha = str(deployed_sha or "").strip().lower()
    if not _DEPLOYED_SHA.fullmatch(exact_sha):
        raise ChangeRegistryObserverError("deployed_sha must be an exact Git SHA")
    return f"crjob_activation_{exact_sha}"


def scheduled_slot(moment: str | datetime | None = None) -> str:
    parsed = _moment(moment or utc_now())
    hour = parsed.hour - parsed.hour % 2
    return parsed.replace(
        hour=hour, minute=0, second=0, microsecond=0
    ).isoformat().replace("+00:00", "Z")


def _moment(value: str | datetime) -> datetime:
    try:
        rendered = canonical_utc_timestamp(value)
    except Exception as exc:
        raise ChangeRegistryObserverError("timestamp must include a timezone")
    return datetime.fromisoformat(rendered[:-1] + "+00:00")


def _id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return prefix + digest[:32]


def _insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    columns = tuple(row)
    conn.execute(
        f"INSERT INTO {table} ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(row[column] for column in columns),
    )


def _insert_idempotent(
    conn: sqlite3.Connection,
    table: str,
    identity_column: str,
    row: Mapping[str, Any],
) -> None:
    existing = conn.execute(
        f"SELECT * FROM {table} WHERE {identity_column}=?",
        (row[identity_column],),
    ).fetchone()
    if existing is None:
        _insert(conn, table, row)
        return
    if any(existing[column] != value for column, value in row.items()):
        raise ChangeRegistryObserverError(
            f"{table} idempotency identity owns different bytes"
        )


def _source_status(snapshot: Mapping[str, Any]) -> str:
    status = str(snapshot.get("completeness_status") or "").strip().lower()
    return status if status in {"complete", "partial", "failed"} else "invalid"


def _safe_error_code(exc: Exception) -> str:
    name = type(exc).__name__
    return name[:120] if _SAFE_TOKEN.fullmatch(name) else "Exception"


def _sqlite_identity(exc: Exception) -> tuple[int | None, str]:
    raw_code = getattr(exc, "sqlite_errorcode", None)
    errorcode = (
        int(raw_code)
        if isinstance(raw_code, int) and 0 <= int(raw_code) <= 65535
        else None
    )
    raw_name = str(getattr(exc, "sqlite_errorname", "") or "")
    errorname = (
        raw_name[:80]
        if re.fullmatch(r"SQLITE_[A-Z0-9_]+", raw_name)
        else ""
    )
    return errorcode, errorname


def _constraint_evidence(exc: Exception, errorname: str) -> tuple[str, str]:
    category = _SQLITE_CATEGORIES.get(errorname, "")
    if isinstance(exc, sqlite3.Error) and not category:
        category = "sqlite_database"
    raw = " ".join(str(exc).split())
    constraint = ""
    if category in {"unique", "not_null"}:
        prefix = (
            "UNIQUE constraint failed: "
            if category == "unique"
            else "NOT NULL constraint failed: "
        )
        if raw.startswith(prefix):
            candidate = raw[len(prefix) :]
            if len(candidate) <= 320 and _SAFE_IDENTIFIER.fullmatch(candidate):
                constraint = candidate
    elif category == "check" and raw.startswith("CHECK constraint failed: "):
        candidate = raw[len("CHECK constraint failed: ") :]
        if len(candidate) <= 320 and _SAFE_IDENTIFIER.fullmatch(candidate):
            constraint = candidate
    elif category == "trigger":
        constraint = _TRIGGER_CONSTRAINTS.get(raw, "")
    return category, constraint


def _failure_evidence(
    exc: Exception,
    *,
    failure_origin: str,
    source_status: str,
    persistence: _PersistenceStage | None = None,
) -> dict[str, Any]:
    exact_source_status = (
        source_status if source_status in SOURCE_STATUSES else "invalid"
    )
    error_code = _safe_error_code(exc)
    sqlite_errorcode, sqlite_errorname = _sqlite_identity(exc)
    category, constraint = _constraint_evidence(exc, sqlite_errorname)
    if not category:
        if failure_origin == "source_acquisition":
            category = "source_acquisition"
        elif isinstance(exc, ChangeRegistryObserverBusy):
            category = "lease_ownership"
        elif isinstance(exc, ChangeRegistryObserverError):
            category = "observer_validation"
        else:
            category = "local_persistence"
    if isinstance(exc, sqlite3.Error):
        detail = f"; constraint={constraint}" if constraint else ""
        safe_message = f"SQLite failure: {sqlite_errorname or 'SQLITE_ERROR'}; category={category}{detail}."
    elif failure_origin == "source_acquisition":
        safe_message = f"Source acquisition failed: {error_code}."
    else:
        safe_message = f"Local persistence failed: {error_code}; category={category}."
    safe_message = safe_message[:400]
    payload = {
        "error_code": error_code,
        "error_message": safe_message,
        "source_status": exact_source_status,
        "failure_origin": failure_origin,
        "persistence_stage": persistence.stage if persistence else "",
        "persistence_table": persistence.table if persistence else "",
        "persistence_operation": persistence.operation if persistence else "",
        "sqlite_errorcode": sqlite_errorcode,
        "sqlite_errorname": sqlite_errorname,
        "constraint_category": category,
        "constraint_name": constraint,
    }
    payload["error_digest"] = canonical_digest(payload)
    return payload


def _fallback_columns(failure: Mapping[str, Any] | None) -> dict[str, Any]:
    evidence = dict(failure or {})
    return {
        "fallback_persistence_stage": str(evidence.get("persistence_stage") or "")[:80],
        "fallback_persistence_table": str(evidence.get("persistence_table") or "")[:160],
        "fallback_persistence_operation": str(evidence.get("persistence_operation") or "")[:160],
        "fallback_error_code": str(evidence.get("error_code") or "")[:120],
        "fallback_error_message": str(evidence.get("error_message") or "")[:800],
        "fallback_sqlite_errorcode": evidence.get("sqlite_errorcode"),
        "fallback_sqlite_errorname": str(evidence.get("sqlite_errorname") or "")[:80],
        "fallback_constraint_category": str(evidence.get("constraint_category") or "")[:80],
        "fallback_constraint_name": str(evidence.get("constraint_name") or "")[:320],
        "fallback_error_digest": str(evidence.get("error_digest") or ""),
    }


class ChangeRegistryObserver:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        seller_id: str,
        account_scope: str = DEFAULT_ACCOUNT_SCOPE,
        acquirer_factory: Callable[[], ChangeRegistrySourceAcquirer] | None = None,
        now_fn: Callable[[], str] | None = None,
        lease_seconds: int = LEASE_SECONDS,
        persistence_stage_hook: (
            Callable[[str, sqlite3.Connection | None], None] | None
        ) = None,
        fallback_stage_hook: (
            Callable[[str, sqlite3.Connection | None], None] | None
        ) = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.seller_id = str(seller_id or "").strip()
        self.account_scope = str(account_scope or "").strip()
        if not self.seller_id or not self.account_scope:
            raise ChangeRegistryObserverError("seller_id and account_scope are required")
        self.store_registry = StoreRegistry(self.runtime_dir)
        self.now_fn = now_fn or utc_now
        self.lease_seconds = int(lease_seconds)
        if self.lease_seconds <= 0:
            raise ChangeRegistryObserverError("lease_seconds must be positive")
        self.acquirer_factory = acquirer_factory or (
            lambda: ChangeRegistrySourceAcquirer(
                seller_id=self.seller_id,
                account_scope=self.account_scope,
            )
        )
        self.persistence_stage_hook = persistence_stage_hook
        self.fallback_stage_hook = fallback_stage_hook
        self.engine = ChangeRegistryBaselineEngine(
            runtime_dir=self.runtime_dir,
            seller_id=self.seller_id,
            account_scope=self.account_scope,
        )

    def _enter_persistence_stage(
        self,
        state: _PersistenceStage,
        stage: str,
        conn: sqlite3.Connection | None,
    ) -> None:
        state.enter(stage, PERSISTENCE_STAGE_BINDINGS)
        if self.persistence_stage_hook is not None:
            self.persistence_stage_hook(stage, conn)

    def _enter_fallback_stage(
        self,
        state: _PersistenceStage,
        stage: str,
        conn: sqlite3.Connection | None,
    ) -> None:
        state.enter(stage, FALLBACK_STAGE_BINDINGS)
        if self.fallback_stage_hook is not None:
            self.fallback_stage_hook(stage, conn)

    def initialize_schema(self) -> None:
        ChangeRegistryRepository(self.runtime_dir).initialize_schema()

    def run(
        self,
        *,
        trigger_kind: str,
        requested_by: str,
        scheduled_slot_value: str = "",
        job_id: str = "",
        deployed_sha: str = "",
        inject_db_failure: bool = False,
    ) -> dict[str, Any]:
        trigger = str(trigger_kind or "").strip().lower()
        if trigger not in {"scheduled", "manual", "activation"}:
            raise ChangeRegistryObserverError("trigger_kind is invalid")
        now = canonical_utc_timestamp(self.now_fn())
        slot = scheduled_slot_value or (
            scheduled_slot(now) if trigger == "scheduled" else ""
        )
        slot = canonical_utc_timestamp(slot) if slot else ""
        if trigger != "scheduled" and slot:
            raise ChangeRegistryObserverError(
                "only scheduled jobs own a scheduled slot"
            )
        exact_sha = str(deployed_sha or "").strip().lower()
        if trigger == "activation":
            exact_job_id = activation_job_id(exact_sha)
            if job_id and job_id != exact_job_id:
                raise ChangeRegistryObserverError(
                    "activation job_id must be bound to deployed_sha"
                )
        elif exact_sha:
            raise ChangeRegistryObserverError(
                "deployed_sha is only valid for activation jobs"
            )
        else:
            exact_job_id = str(job_id or "").strip()
        if not exact_job_id:
            identity_basis = {
                "seller_id": self.seller_id,
                "account_scope": self.account_scope,
                "trigger_kind": trigger,
                "scheduled_slot": slot,
                "requested_by": str(requested_by or "").strip(),
                "requested_at": slot if trigger == "scheduled" else now,
            }
            exact_job_id = _id("crjob_", identity_basis)
        basis = {
            "seller_id": self.seller_id,
            "account_scope": self.account_scope,
            "trigger_kind": trigger,
            "scheduled_slot": slot,
            "requested_by": str(requested_by or "").strip(),
            "client_job_id": exact_job_id,
            "deployed_sha": exact_sha,
        }
        admitted = self._admit(
            job_id=exact_job_id,
            trigger_kind=trigger,
            scheduled_slot_value=slot,
            requested_by=requested_by,
            requested_at=now,
            request_digest=canonical_digest(basis),
        )
        if admitted["replay"]:
            return self.read_job(exact_job_id)
        try:
            snapshot = self.acquirer_factory().acquire()
        except Exception as exc:
            primary = _failure_evidence(
                exc,
                failure_origin="source_acquisition",
                source_status="failed",
            )
            try:
                self._fail_job(exact_job_id, trigger, slot, primary)
            except ChangeRegistryObserverTerminalEvidenceError as terminal_exc:
                raise terminal_exc from exc
            raise
        source_status = _source_status(snapshot)
        persistence = _PersistenceStage()
        try:
            return self._persist(
                exact_job_id,
                trigger,
                slot,
                snapshot,
                source_status,
                persistence,
                inject_db_failure,
            )
        except Exception as exc:
            primary = _failure_evidence(
                exc,
                failure_origin="local_persistence",
                source_status=source_status,
                persistence=persistence,
            )
            try:
                self._fail_job(exact_job_id, trigger, slot, primary)
            except ChangeRegistryObserverTerminalEvidenceError as terminal_exc:
                raise terminal_exc from exc
            raise

    def submit_manual(self, *, requested_by: str, job_id: str = "") -> dict[str, Any]:
        """Admit a manual job, then perform its read-only scan in background."""

        now = canonical_utc_timestamp(self.now_fn())
        exact_job_id = job_id or _id(
            "crjob_",
            {
                "seller_id": self.seller_id,
                "account_scope": self.account_scope,
                "trigger_kind": "manual",
                "requested_by": requested_by,
                "request_nonce": now,
            },
        )
        admitted = self._admit(
            job_id=exact_job_id,
            trigger_kind="manual",
            scheduled_slot_value="",
            requested_by=requested_by,
            requested_at=now,
            request_digest=canonical_digest(
                {
                    "seller_id": self.seller_id,
                    "account_scope": self.account_scope,
                    "trigger_kind": "manual",
                    "requested_by": str(requested_by or "").strip(),
                    "client_job_id": exact_job_id,
                }
            ),
        )
        if admitted["replay"]:
            return self.read_job(exact_job_id)

        def worker() -> None:
            try:
                snapshot = self.acquirer_factory().acquire()
            except Exception as exc:  # pragma: no cover - integration boundary
                primary = _failure_evidence(
                    exc,
                    failure_origin="source_acquisition",
                    source_status="failed",
                )
                try:
                    self._fail_job(exact_job_id, "manual", "", primary)
                except ChangeRegistryObserverTerminalEvidenceError:
                    return
                return
            source_status = _source_status(snapshot)
            persistence = _PersistenceStage()
            try:
                self._persist(
                    exact_job_id,
                    "manual",
                    "",
                    snapshot,
                    source_status,
                    persistence,
                    False,
                )
            except Exception as exc:  # pragma: no cover - integration boundary
                primary = _failure_evidence(
                    exc,
                    failure_origin="local_persistence",
                    source_status=source_status,
                    persistence=persistence,
                )
                try:
                    self._fail_job(exact_job_id, "manual", "", primary)
                except ChangeRegistryObserverTerminalEvidenceError:
                    return

        threading.Thread(
            target=worker,
            name=f"change-registry-{exact_job_id[-12:]}",
            daemon=True,
        ).start()
        return self.read_job(exact_job_id)

    def _admit(self, **row: Any) -> dict[str, bool]:
        self.initialize_schema()
        now = canonical_utc_timestamp(row["requested_at"])
        requested_by = str(row["requested_by"] or "").strip()[:160]
        if not requested_by:
            raise ChangeRegistryObserverError("requested_by is required")
        expires = (
            _moment(now) + timedelta(seconds=self.lease_seconds)
        ).isoformat().replace("+00:00", "Z")
        with self.store_registry.session(
            "operational", mode="rw", operation="change_registry_observer_admit"
        ) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    f"SELECT * FROM {OBSERVER_JOBS_TABLE} WHERE job_id=?",
                    (row["job_id"],),
                ).fetchone()
                if existing is not None:
                    expected = {
                        "seller_id": self.seller_id,
                        "account_scope": self.account_scope,
                        "trigger_kind": row["trigger_kind"],
                        "scheduled_slot": row["scheduled_slot_value"],
                        "requested_by": requested_by,
                        "request_digest": row["request_digest"],
                    }
                    if any(existing[key] != value for key, value in expected.items()):
                        raise ChangeRegistryObserverError(
                            "observer job id owns a conflicting request binding"
                        )
                    state = self._last_job_state(conn, str(row["job_id"]))
                    if state in TERMINAL_JOB_STATES:
                        conn.commit()
                        return {"replay": True}
                    if state not in RUNNABLE_JOB_STATES:
                        raise ChangeRegistryObserverError(
                            "observer job has no resumable lifecycle state"
                        )
                lease = conn.execute(
                    f"SELECT * FROM {OBSERVER_LEASES_TABLE} "
                    "WHERE seller_id=? AND account_scope=?",
                    (self.seller_id, self.account_scope),
                ).fetchone()
                if lease is not None and str(lease["owner_job_id"]):
                    owner = str(lease["owner_job_id"])
                    if _moment(str(lease["expires_at"])) > _moment(now):
                        if existing is not None and owner == str(row["job_id"]):
                            conn.commit()
                            return {"replay": True}
                        raise ChangeRegistryObserverBusy(
                            "another registry scan owns the seller lease"
                        )
                    if owner != str(row["job_id"]):
                        self._terminalize_stale_owner(conn, lease, now)
                if lease is None:
                    _insert(
                        conn,
                        OBSERVER_LEASES_TABLE,
                        {
                            "seller_id": self.seller_id,
                            "account_scope": self.account_scope,
                            "owner_job_id": row["job_id"],
                            "acquired_at": now,
                            "expires_at": expires,
                            "revision": 1,
                            "updated_at": now,
                        },
                    )
                else:
                    expected_revision = int(lease["revision"])
                    claimed = conn.execute(
                        f"UPDATE {OBSERVER_LEASES_TABLE} SET "
                        "owner_job_id=?,acquired_at=?,expires_at=?,"
                        "revision=revision+1,updated_at=? "
                        "WHERE seller_id=? AND account_scope=? AND revision=?",
                        (
                            row["job_id"], now, expires, now,
                            self.seller_id, self.account_scope, expected_revision,
                        ),
                    )
                    if claimed.rowcount != 1:
                        raise ChangeRegistryObserverBusy(
                            "observer lease changed during CAS recovery"
                        )
                if existing is None:
                    _insert(
                        conn,
                        OBSERVER_JOBS_TABLE,
                        {
                            "job_id": row["job_id"],
                            "seller_id": self.seller_id,
                            "account_scope": self.account_scope,
                            "trigger_kind": row["trigger_kind"],
                            "scheduled_slot": row["scheduled_slot_value"],
                            "requested_by": requested_by,
                            "requested_at": now,
                            "request_digest": row["request_digest"],
                        },
                    )
                    self._append_job_event(
                        conn, str(row["job_id"]), 1, "accepted", now, None, 0
                    )
                self._append_job_event(
                    conn,
                    str(row["job_id"]),
                    self._next_event_sequence(conn, str(row["job_id"])),
                    "running",
                    now,
                    None,
                    0,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"replay": False}

    def _last_job_state(self, conn: sqlite3.Connection, job_id: str) -> str:
        row = conn.execute(
            f"SELECT state FROM {OBSERVER_JOB_EVENTS_TABLE} WHERE job_id=? "
            "ORDER BY sequence_no DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return str(row["state"]) if row is not None else ""

    def _next_event_sequence(self, conn: sqlite3.Connection, job_id: str) -> int:
        row = conn.execute(
            f"SELECT MAX(sequence_no) AS sequence_no FROM {OBSERVER_JOB_EVENTS_TABLE} "
            "WHERE job_id=?",
            (job_id,),
        ).fetchone()
        return int(row["sequence_no"] or 0) + 1

    def _terminalize_stale_owner(
        self,
        conn: sqlite3.Connection,
        lease: sqlite3.Row,
        now: str,
    ) -> None:
        owner = str(lease["owner_job_id"])
        owner_job = conn.execute(
            f"SELECT * FROM {OBSERVER_JOBS_TABLE} WHERE job_id=?",
            (owner,),
        ).fetchone()
        if owner_job is not None and self._last_job_state(conn, owner) in RUNNABLE_JOB_STATES:
            self._append_job_event(
                conn,
                owner,
                self._next_event_sequence(conn, owner),
                "failed",
                now,
                None,
                0,
                error_code="stale_worker_lease",
                error_message="Предыдущий worker не завершился до истечения lease.",
            )
            if str(owner_job["trigger_kind"]) == "scheduled":
                self._append_health(
                    conn,
                    owner,
                    str(owner_job["scheduled_slot"]),
                    "failed",
                    now,
                    None,
                )

    def _persist(
        self,
        job_id: str,
        trigger: str,
        slot: str,
        snapshot: Mapping[str, Any],
        source_status: str,
        persistence: _PersistenceStage,
        inject_db_failure: bool,
    ) -> dict[str, Any]:
        canonical_snapshot = canonicalize_acquisition_timestamps(snapshot)
        if not isinstance(canonical_snapshot, Mapping):
            raise ChangeRegistryObserverError("acquisition snapshot must be an object")
        interval = canonical_snapshot.get("interval") or {}
        completed_at = canonical_utc_timestamp(interval.get("completed_at") or "")
        canonical_utc_timestamp(interval.get("started_at") or "")
        persisted_at = max(_moment(completed_at), _moment(self.now_fn())).isoformat().replace(
            "+00:00", "Z"
        )

        def persist_observer_metadata(
            conn: sqlite3.Connection,
            receipt: Mapping[str, Any],
        ) -> None:
            self._enter_persistence_stage(
                persistence, "baseline_result", conn
            )
            lease = conn.execute(
                f"SELECT * FROM {OBSERVER_LEASES_TABLE} "
                "WHERE seller_id=? AND account_scope=?",
                (self.seller_id, self.account_scope),
            ).fetchone()
            if lease is None or str(lease["owner_job_id"]) != job_id:
                raise ChangeRegistryObserverBusy(
                    "registry observer lease ownership was lost"
                )
            checkpoint_id = str(receipt["checkpoint_id"])
            outcome = str(receipt["completeness_status"])
            fact_count = int(receipt["row_counts"]["facts"])
            self._persist_source_manifests(
                conn,
                checkpoint_id,
                completed_at,
                canonical_snapshot,
                persistence,
            )
            self._enter_persistence_stage(
                persistence, "terminal_job_event", conn
            )
            ChangeRegistryRepository(
                self.runtime_dir
            ).reconcile_manual_pending_in_transaction(
                conn,
                seller_id=self.seller_id,
                account_scope=self.account_scope,
                checkpoint_id=checkpoint_id,
                reconciled_at=persisted_at,
            )
            self._append_job_event(
                conn,
                job_id,
                self._next_event_sequence(conn, job_id),
                outcome,
                persisted_at,
                checkpoint_id,
                fact_count,
                source_status=source_status,
            )
            if trigger == "scheduled":
                self._enter_persistence_stage(
                    persistence, "scheduled_health", conn
                )
                self._append_health(
                    conn, job_id, slot, outcome, persisted_at, checkpoint_id
                )
            self._enter_persistence_stage(
                persistence, "lease_release", conn
            )
            self._release(conn, job_id, persisted_at)
            self._enter_persistence_stage(
                persistence, "transaction_commit", conn
            )
            if inject_db_failure:
                raise sqlite3.OperationalError(
                    "injected observer transaction commit failure"
                )

        self._enter_persistence_stage(
            persistence, "baseline_ingest", None
        )
        self.engine.ingest(
            canonical_snapshot, transaction_hook=persist_observer_metadata
        )
        return self.read_job(job_id)

    def _persist_source_manifests(
        self,
        conn: sqlite3.Connection,
        checkpoint_id: str,
        created_at: str,
        snapshot: Mapping[str, Any],
        persistence: _PersistenceStage,
    ) -> None:
        sources = snapshot.get("sources") or {}
        for source_name in ("prices", "ads"):
            self._enter_persistence_stage(
                persistence,
                f"source_manifest_{source_name}",
                conn,
            )
            source = dict(sources.get(source_name) or {})
            counts = dict(source.get("counts") or {})
            observed = int(
                counts.get("goods", 0)
                if source_name == "prices"
                else counts.get("detail_campaigns", 0)
            )
            if source_name == "prices":
                expected = observed
            else:
                expected_raw = (source.get("count_manifest") or {}).get(
                    "expected_all"
                )
                expected = (
                    int(expected_raw)
                    if isinstance(expected_raw, int) and expected_raw >= observed
                    else max(observed, int(counts.get("manifest_campaigns", 0)))
                )
            status = str(source.get("completeness_status") or "failed")
            if status not in {"complete", "partial", "failed"}:
                status = "failed"
            summary = canonicalize_acquisition_timestamps({
                "source": source_name,
                "completeness_status": status,
                "counts": {
                    key: int(value)
                    for key, value in sorted(counts.items())
                    if isinstance(value, int)
                },
                "interval": dict(source.get("interval") or {}),
                "persistence": dict(snapshot.get("persistence") or {}),
                "wb_mutation_calls": dict(snapshot.get("wb_mutation_calls") or {}),
            })
            row = {
                "source_manifest_id": _id("crsm_", [checkpoint_id, source_name]),
                "checkpoint_id": checkpoint_id,
                "source_name": source_name,
                "completeness_status": status,
                "expected_count": expected,
                "observed_count": observed,
                "summary_json": canonical_json(summary),
                "evidence_digest": str(
                    source.get("manifest_digest") or canonical_digest(summary)
                ),
                "created_at": created_at,
            }
            _insert_idempotent(
                conn,
                CHECKPOINT_SOURCE_MANIFESTS_TABLE,
                "source_manifest_id",
                row,
            )

    def _append_job_event(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        sequence: int,
        state: str,
        occurred_at: str,
        checkpoint_id: str | None,
        fact_count: int,
        *,
        error_code: str = "",
        error_message: str = "",
        source_status: str = "not_observed",
        failure: Mapping[str, Any] | None = None,
        fallback_failure: Mapping[str, Any] | None = None,
    ) -> None:
        typed = dict(failure or {})
        exact_source_status = str(
            typed.get("source_status") or source_status or "not_observed"
        )
        if exact_source_status not in SOURCE_STATUSES:
            exact_source_status = "invalid"
        primary = {
            "error_code": str(typed.get("error_code") or error_code)[:120],
            "error_message": str(
                typed.get("error_message") or error_message
            )[:800],
            "source_status": exact_source_status,
            "failure_origin": str(typed.get("failure_origin") or ""),
            "persistence_stage": str(typed.get("persistence_stage") or "")[:80],
            "persistence_table": str(typed.get("persistence_table") or "")[:160],
            "persistence_operation": str(
                typed.get("persistence_operation") or ""
            )[:160],
            "sqlite_errorcode": typed.get("sqlite_errorcode"),
            "sqlite_errorname": str(typed.get("sqlite_errorname") or "")[:80],
            "constraint_category": str(
                typed.get("constraint_category") or ""
            )[:80],
            "constraint_name": str(typed.get("constraint_name") or "")[:320],
            "error_digest": str(typed.get("error_digest") or ""),
        }
        fallback = _fallback_columns(fallback_failure)
        evidence = canonical_digest(
            {
                "job_id": job_id,
                "sequence": sequence,
                "state": state,
                "checkpoint_id": checkpoint_id,
                "fact_count": fact_count,
                **primary,
                **fallback,
            }
        )
        _insert(
            conn,
            OBSERVER_JOB_EVENTS_TABLE,
            {
                "job_event_id": _id("crje_", [job_id, sequence, state]),
                "job_id": job_id,
                "sequence_no": sequence,
                "state": state,
                "occurred_at": occurred_at,
                "checkpoint_id": checkpoint_id,
                "fact_count": fact_count,
                **primary,
                **fallback,
                "evidence_digest": evidence,
            },
        )

    def _append_health(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        slot: str,
        outcome: str,
        occurred_at: str,
        checkpoint_id: str | None,
    ) -> None:
        previous = conn.execute(
            f"SELECT * FROM {OBSERVER_HEALTH_EVENTS_TABLE} "
            "WHERE seller_id=? AND account_scope=? "
            "ORDER BY occurred_at DESC,health_event_id DESC LIMIT 1",
            (self.seller_id, self.account_scope),
        ).fetchone()
        failures = (
            0
            if outcome == "complete"
            else int(previous["consecutive_noncomplete"] if previous else 0) + 1
        )
        health = "degraded" if failures >= 2 else "normal"
        evidence = canonical_digest(
            {
                "job_id": job_id,
                "slot": slot,
                "outcome": outcome,
                "consecutive_noncomplete": failures,
                "health_state": health,
            }
        )
        _insert_idempotent(
            conn,
            OBSERVER_HEALTH_EVENTS_TABLE,
            "health_event_id",
            {
                "health_event_id": _id(
                    "crhe_", [self.seller_id, self.account_scope, slot]
                ),
                "seller_id": self.seller_id,
                "account_scope": self.account_scope,
                "scheduled_slot": slot,
                "outcome": outcome,
                "consecutive_noncomplete": failures,
                "health_state": health,
                "job_id": job_id,
                "checkpoint_id": checkpoint_id,
                "occurred_at": occurred_at,
                "evidence_digest": evidence,
            },
        )

    def _release(self, conn: sqlite3.Connection, job_id: str, now: str) -> None:
        cursor = conn.execute(
            f"UPDATE {OBSERVER_LEASES_TABLE} SET owner_job_id='',"
            "acquired_at='',expires_at='',revision=revision+1,updated_at=? "
            "WHERE seller_id=? AND account_scope=? AND owner_job_id=?",
            (now, self.seller_id, self.account_scope, job_id),
        )
        if cursor.rowcount != 1:
            raise ChangeRegistryObserverBusy(
                "registry observer lease release lost ownership"
            )

    def _fail_job(
        self,
        job_id: str,
        trigger: str,
        slot: str,
        primary_failure: Mapping[str, Any],
    ) -> None:
        now = canonical_utc_timestamp(self.now_fn())
        fallback_stage = _PersistenceStage()
        try:
            self._enter_fallback_stage(
                fallback_stage, "fallback_store_open", None
            )
            with self.store_registry.session(
                "operational",
                mode="rw",
                operation="change_registry_observer_fail",
            ) as conn:
                self._enter_fallback_stage(
                    fallback_stage, "fallback_begin", conn
                )
                conn.execute("BEGIN IMMEDIATE")
                try:
                    state = self._last_job_state(conn, job_id)
                    if state in TERMINAL_JOB_STATES:
                        conn.commit()
                        return
                    sequence = self._next_event_sequence(conn, job_id)
                    self._enter_fallback_stage(
                        fallback_stage,
                        "fallback_terminal_job_event",
                        conn,
                    )
                    self._append_job_event(
                        conn,
                        job_id,
                        sequence,
                        "failed",
                        now,
                        None,
                        0,
                        failure=primary_failure,
                    )
                    if trigger == "scheduled":
                        self._enter_fallback_stage(
                            fallback_stage,
                            "fallback_scheduled_health",
                            conn,
                        )
                        self._append_health(
                            conn, job_id, slot, "failed", now, None
                        )
                    lease = conn.execute(
                        f"SELECT owner_job_id FROM {OBSERVER_LEASES_TABLE} "
                        "WHERE seller_id=? AND account_scope=?",
                        (self.seller_id, self.account_scope),
                    ).fetchone()
                    if (
                        lease is not None
                        and str(lease["owner_job_id"]) == job_id
                    ):
                        self._enter_fallback_stage(
                            fallback_stage,
                            "fallback_lease_release",
                            conn,
                        )
                        self._release(conn, job_id, now)
                    self._enter_fallback_stage(
                        fallback_stage, "fallback_commit", conn
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except Exception as fallback_exc:
            fallback_failure = _failure_evidence(
                fallback_exc,
                failure_origin="local_persistence",
                source_status=str(
                    primary_failure.get("source_status") or "invalid"
                ),
                persistence=fallback_stage,
            )
            rescue_stage = _PersistenceStage()
            try:
                self._rescue_failure_evidence(
                    job_id=job_id,
                    trigger=trigger,
                    slot=slot,
                    now=now,
                    primary_failure=primary_failure,
                    fallback_failure=fallback_failure,
                    stage=rescue_stage,
                )
            except Exception as rescue_exc:
                rescue_failure = _failure_evidence(
                    rescue_exc,
                    failure_origin="local_persistence",
                    source_status=str(
                        primary_failure.get("source_status") or "invalid"
                    ),
                    persistence=rescue_stage,
                )
                raise ChangeRegistryObserverTerminalEvidenceError(
                    primary_failure,
                    fallback_failure,
                    rescue_failure,
                ) from rescue_exc

    def _rescue_failure_evidence(
        self,
        *,
        job_id: str,
        trigger: str,
        slot: str,
        now: str,
        primary_failure: Mapping[str, Any],
        fallback_failure: Mapping[str, Any],
        stage: _PersistenceStage,
    ) -> None:
        stage.stage = "fallback_rescue_store_open"
        stage.table = "operational_store"
        stage.operation = "open_rescue_rw"
        with self.store_registry.session(
            "operational",
            mode="rw",
            operation="change_registry_observer_fail_rescue",
        ) as conn:
            stage.stage = "fallback_rescue_begin"
            stage.operation = "begin_immediate_rescue"
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = self._last_job_state(conn, job_id)
                if state in TERMINAL_JOB_STATES:
                    conn.commit()
                    return
                sequence = self._next_event_sequence(conn, job_id)
                stage.stage = "fallback_rescue_terminal_job_event"
                stage.table = OBSERVER_JOB_EVENTS_TABLE
                stage.operation = "insert_failed_terminal_with_fallback"
                self._append_job_event(
                    conn,
                    job_id,
                    sequence,
                    "failed",
                    now,
                    None,
                    0,
                    failure=primary_failure,
                    fallback_failure=fallback_failure,
                )
                if trigger == "scheduled":
                    stage.stage = "fallback_rescue_scheduled_health"
                    stage.table = OBSERVER_HEALTH_EVENTS_TABLE
                    stage.operation = "ensure_failed_health"
                    existing_health = conn.execute(
                        f"SELECT * FROM {OBSERVER_HEALTH_EVENTS_TABLE} "
                        "WHERE seller_id=? AND account_scope=? "
                        "AND scheduled_slot=?",
                        (self.seller_id, self.account_scope, slot),
                    ).fetchone()
                    if existing_health is None:
                        self._append_health(
                            conn, job_id, slot, "failed", now, None
                        )
                    elif not (
                        str(existing_health["job_id"]) == job_id
                        and str(existing_health["outcome"]) == "failed"
                    ):
                        raise ChangeRegistryObserverError(
                            "scheduled health slot owns different failure evidence"
                        )
                lease = conn.execute(
                    f"SELECT owner_job_id FROM {OBSERVER_LEASES_TABLE} "
                    "WHERE seller_id=? AND account_scope=?",
                    (self.seller_id, self.account_scope),
                ).fetchone()
                if lease is not None and str(lease["owner_job_id"]) == job_id:
                    stage.stage = "fallback_rescue_lease_release"
                    stage.table = OBSERVER_LEASES_TABLE
                    stage.operation = "cas_release"
                    self._release(conn, job_id, now)
                stage.stage = "fallback_rescue_commit"
                stage.table = "operational_store"
                stage.operation = "commit_rescue_failure_evidence"
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def read_job(self, job_id: str) -> dict[str, Any]:
        with self.store_registry.session(
            "operational", mode="ro", operation="change_registry_observer_read_job"
        ) as conn:
            job = conn.execute(
                f"SELECT * FROM {OBSERVER_JOBS_TABLE} WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise ChangeRegistryObserverError("observer job not found")
            events = conn.execute(
                f"SELECT * FROM {OBSERVER_JOB_EVENTS_TABLE} WHERE job_id=? "
                "ORDER BY sequence_no",
                (job_id,),
            ).fetchall()
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "job": dict(job),
            "events": [dict(row) for row in events],
        }


class ChangeRegistryReadSurface:
    """Minimal sanitized read surface used by authenticated Registry UI/API."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        seller_id: str,
        account_scope: str = DEFAULT_ACCOUNT_SCOPE,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.seller_id = str(seller_id)
        self.account_scope = str(account_scope)
        self.store_registry = StoreRegistry(self.runtime_dir)

    def overview(self, *, limit: int = 100) -> dict[str, Any]:
        exact_limit = max(1, min(int(limit), 200))
        try:
            operational_path = self.store_registry.resolve("operational")
        except StorageRegistryError as exc:
            return self._unavailable_overview("storage_unavailable", str(exc))
        if not operational_path.is_file():
            return self._unavailable_overview(
                "schema_missing", "operational generation file is missing"
            )
        with self.store_registry.session(
            "operational", mode="ro", operation="change_registry_overview"
        ) as conn:
            query_only = int(conn.execute("PRAGMA query_only").fetchone()[0])
            if query_only != 1:
                raise ChangeRegistryObserverError(
                    "registry read surface requires PRAGMA query_only=ON"
                )
            required_tables = {
                CHECKPOINTS_TABLE,
                CHECKPOINT_SOURCE_MANIFESTS_TABLE,
                FACTS_TABLE,
                IDENTITY_INCIDENTS_TABLE,
                OBSERVER_HEALTH_EVENTS_TABLE,
                OBSERVER_JOB_EVENTS_TABLE,
                OBSERVER_JOBS_TABLE,
                ANNOTATION_REVISIONS_TABLE,
                ITEMS_TABLE,
                MANUAL_PENDING_CURRENT_TABLE,
                MANUAL_PENDING_EVENTS_TABLE,
            }
            actual_tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing_tables = sorted(required_tables - actual_tables)
            if missing_tables:
                return self._unavailable_overview(
                    "schema_missing",
                    "required registry tables are missing",
                    missing_tables=missing_tables,
                )
            checkpoint = conn.execute(
                f"SELECT * FROM {CHECKPOINTS_TABLE} WHERE seller_id=? "
                "AND account_scope=? AND source_surface=? "
                "ORDER BY completed_at DESC,checkpoint_id DESC LIMIT 1",
                (self.seller_id, self.account_scope, SOURCE_SURFACE),
            ).fetchone()
            health = conn.execute(
                f"SELECT * FROM {OBSERVER_HEALTH_EVENTS_TABLE} "
                "WHERE seller_id=? AND account_scope=? "
                "ORDER BY occurred_at DESC,health_event_id DESC LIMIT 1",
                (self.seller_id, self.account_scope),
            ).fetchone()
            jobs = conn.execute(
                f"""SELECT job.*,event.state,event.occurred_at AS status_at,
                           event.checkpoint_id,event.fact_count,event.error_code,
                           event.error_message,event.source_status,
                           event.failure_origin,event.persistence_stage,
                           event.persistence_table,event.persistence_operation,
                           event.sqlite_errorcode,event.sqlite_errorname,
                           event.constraint_category,event.constraint_name,
                           event.error_digest,event.fallback_persistence_stage,
                           event.fallback_persistence_table,
                           event.fallback_persistence_operation,
                           event.fallback_error_code,
                           event.fallback_error_message,
                           event.fallback_sqlite_errorcode,
                           event.fallback_sqlite_errorname,
                           event.fallback_constraint_category,
                           event.fallback_constraint_name,
                           event.fallback_error_digest
                    FROM {OBSERVER_JOBS_TABLE} job
                    JOIN {OBSERVER_JOB_EVENTS_TABLE} event ON event.job_id=job.job_id
                    WHERE job.seller_id=? AND job.account_scope=?
                      AND event.sequence_no=(
                          SELECT MAX(sequence_no)
                          FROM {OBSERVER_JOB_EVENTS_TABLE} latest
                          WHERE latest.job_id=job.job_id
                      )
                    ORDER BY job.requested_at DESC,job.job_id DESC LIMIT ?""",
                (self.seller_id, self.account_scope, exact_limit),
            ).fetchall()
            facts = conn.execute(
                f"SELECT * FROM {FACTS_TABLE} WHERE seller_id=? "
                "AND account_scope=? ORDER BY proven_at DESC,fact_id DESC LIMIT ?",
                (self.seller_id, self.account_scope, exact_limit),
            ).fetchall()
            incidents = conn.execute(
                f"SELECT * FROM {IDENTITY_INCIDENTS_TABLE} WHERE seller_id=? "
                "AND account_scope=? ORDER BY observed_at DESC,incident_id DESC LIMIT ?",
                (self.seller_id, self.account_scope, exact_limit),
            ).fetchall()
            manual_pending = conn.execute(
                f"""SELECT item.recommendation_item_id,event.pending_id,event.state,
                           event.occurred_at,event.related_fact_id,item.created_at,
                           item.target_kind,item.nm_id,item.advert_id,item.placement,
                           item.parameter_field,item.before_value_integer,
                           item.requested_value_integer,pointer.active
                    FROM {ITEMS_TABLE} item
                    JOIN {MANUAL_PENDING_EVENTS_TABLE} event
                      ON event.change_item_id=item.change_item_id
                    LEFT JOIN {MANUAL_PENDING_CURRENT_TABLE} pointer
                      ON pointer.current_pending_id=event.pending_id
                     AND pointer.current_event_id=event.pending_event_id
                    WHERE item.seller_id=? AND item.account_scope=?
                      AND event.sequence_no=(
                        SELECT MAX(sequence_no)
                        FROM {MANUAL_PENDING_EVENTS_TABLE} latest
                        WHERE latest.pending_id=event.pending_id
                      )
                    ORDER BY item.created_at DESC,item.change_item_id DESC LIMIT ?""",
                (self.seller_id, self.account_scope, exact_limit),
            ).fetchall()
            source_manifests = (
                conn.execute(
                    f"SELECT source_name,completeness_status,expected_count,"
                    "observed_count,summary_json,evidence_digest,created_at "
                    f"FROM {CHECKPOINT_SOURCE_MANIFESTS_TABLE} "
                    "WHERE checkpoint_id=? ORDER BY source_name",
                    (checkpoint["checkpoint_id"],),
                ).fetchall()
                if checkpoint is not None
                else []
            )
            scoped_subjects = {
                ("fact", str(row["fact_id"])) for row in facts
            }
            if checkpoint is not None:
                scoped_subjects.add(
                    ("checkpoint", str(checkpoint["checkpoint_id"]))
                )
            scoped_subjects.update(
                ("identity_incident", str(row["incident_id"]))
                for row in incidents
            )
            scoped_subjects.update(
                ("manual_pending", str(row["pending_id"]))
                for row in manual_pending
            )
            annotations = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM {ANNOTATION_REVISIONS_TABLE} "
                    "ORDER BY created_at DESC,annotation_revision_id DESC LIMIT ?",
                    (exact_limit * 4,),
                ).fetchall()
                if (str(row["subject_kind"]), str(row["subject_id"]))
                in scoped_subjects
            ][:exact_limit]

        fact_payloads = [dict(row) for row in facts]
        interval_state: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int, str, str]] = set()
        for fact in fact_payloads:
            key = (
                str(fact["target_kind"]), int(fact["nm_id"]),
                int(fact["advert_id"]), str(fact["placement"]),
                str(fact["parameter_field"]),
            )
            if key in seen:
                continue
            seen.add(key)
            interval_state.append(
                {
                    "target_kind": key[0],
                    "nm_id": key[1],
                    "advert_id": key[2],
                    "placement": key[3],
                    "parameter_field": key[4],
                    "value_kind": fact["after_value_kind"],
                    "value_integer": fact["after_value_integer"],
                    "value_text": fact["after_value_text"],
                    "observation_window": {
                        "from": fact["observed_from"],
                        "to": fact["observed_to"],
                    },
                    "fact_id": fact["fact_id"],
                }
            )
        pending_payloads = []
        for row in manual_pending:
            requested_at = str(row["created_at"])
            expires_at = (
                _moment(requested_at) + timedelta(hours=24)
            ).isoformat().replace("+00:00", "Z")
            pending_payloads.append(
                {
                    "pending_id": str(row["pending_id"]),
                    "recommendation_item_id": str(row["recommendation_item_id"]),
                    "state": str(row["state"]),
                    "active": bool(row["active"] or 0),
                    "requested_at": requested_at,
                    "expires_at": expires_at,
                    "target": {
                        "target_kind": str(row["target_kind"]),
                        "nm_id": int(row["nm_id"]),
                        "advert_id": int(row["advert_id"]),
                        "placement": str(row["placement"]),
                        "parameter_field": str(row["parameter_field"]),
                    },
                    "before_value": row["before_value_integer"],
                    "requested_value": row["requested_value_integer"],
                    "related_fact_id": str(row["related_fact_id"] or ""),
                }
            )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "storage": {"mode": "ro", "query_only": True},
            "seller_scope": {
                "seller_id": self.seller_id,
                "account_scope": self.account_scope,
            },
            "status": {
                "health_state": (
                    str(health["health_state"])
                    if health is not None else "awaiting_baseline"
                ),
                "consecutive_scheduled_noncomplete": (
                    int(health["consecutive_noncomplete"])
                    if health is not None else 0
                ),
                "last_checkpoint": dict(checkpoint) if checkpoint else None,
                "source_manifests": [dict(row) for row in source_manifests],
            },
            "facts": fact_payloads,
            "interval_state": interval_state,
            "incidents": [dict(row) for row in incidents],
            "jobs": [dict(row) for row in jobs],
            "manual_pending": pending_payloads,
            "annotations": annotations,
            "interval_semantics": (
                "Время изменения известно только внутри окна между двумя наблюдениями."
            ),
        }

    def _unavailable_overview(
        self,
        health_state: str,
        detail: str,
        *,
        missing_tables: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "storage": {"mode": "ro", "query_only": True},
            "seller_scope": {
                "seller_id": self.seller_id,
                "account_scope": self.account_scope,
            },
            "status": {
                "health_state": health_state,
                "health_detail": detail[:300],
                "missing_tables": list(missing_tables or []),
                "consecutive_scheduled_noncomplete": 0,
                "last_checkpoint": None,
                "source_manifests": [],
            },
            "facts": [],
            "interval_state": [],
            "incidents": [],
            "jobs": [],
            "manual_pending": [],
            "annotations": [],
            "interval_semantics": (
                "Время изменения известно только внутри окна между двумя наблюдениями."
            ),
        }

    def annotate(
        self,
        payload: Mapping[str, Any],
        *,
        actor: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        subject_kind = str(payload.get("subject_kind") or "")
        subject_id = str(payload.get("subject_id") or "")
        if not self._owns_subject(subject_kind, subject_id):
            raise ChangeRegistryObserverError(
                "annotation subject is outside the Registry seller scope"
            )
        parent = payload.get("parent_revision_id")
        created_at = now or utc_now()
        revision_id = _id(
            "crann_",
            {
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "parent": parent,
                "actor": actor,
                "created_at": created_at,
                "reason": payload.get("reason"),
                "comment": payload.get("comment"),
            },
        )
        return ChangeRegistryRepository(self.runtime_dir).append_annotation_revision(
            annotation_revision_id=revision_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            actor_principal=actor,
            created_at=created_at,
            reason=str(payload.get("reason") or ""),
            comment=str(payload.get("comment") or ""),
            parent_revision_id=str(parent) if parent else None,
        )

    def _owns_subject(self, subject_kind: str, subject_id: str) -> bool:
        if subject_kind == "manual_pending" and subject_id:
            with self.store_registry.session(
                "operational", mode="ro", operation="change_registry_annotation_scope"
            ) as conn:
                row = conn.execute(
                    f"SELECT 1 FROM {MANUAL_PENDING_EVENTS_TABLE} event "
                    f"JOIN {ITEMS_TABLE} item ON item.change_item_id=event.change_item_id "
                    "WHERE event.pending_id=? AND item.seller_id=? "
                    "AND item.account_scope=? LIMIT 1",
                    (subject_id, self.seller_id, self.account_scope),
                ).fetchone()
            return row is not None
        table_and_id = {
            "fact": (FACTS_TABLE, "fact_id"),
            "checkpoint": (CHECKPOINTS_TABLE, "checkpoint_id"),
            "identity_incident": (IDENTITY_INCIDENTS_TABLE, "incident_id"),
        }.get(subject_kind)
        if table_and_id is None or not subject_id:
            return False
        table, identity = table_and_id
        with self.store_registry.session(
            "operational", mode="ro", operation="change_registry_annotation_scope"
        ) as conn:
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE {identity}=? AND seller_id=? "
                "AND account_scope=? LIMIT 1",
                (subject_id, self.seller_id, self.account_scope),
            ).fetchone()
        return row is not None


__all__ = [
    "ChangeRegistryObserver",
    "ChangeRegistryObserverBusy",
    "ChangeRegistryObserverError",
    "ChangeRegistryObserverTerminalEvidenceError",
    "ChangeRegistryReadSurface",
    "DEFAULT_ACCOUNT_SCOPE",
    "PERSISTENCE_STAGE_BINDINGS",
    "TERMINAL_JOB_STATES",
    "activation_job_id",
    "scheduled_slot",
    "utc_now",
]
