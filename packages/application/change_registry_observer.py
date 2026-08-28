"""Scheduled read-only observer for externally visible seller changes.

Admission owns only the scheduled-slot idempotency key and seller/account
lease. All WB GET calls run without a SQLite transaction. The canonical
baseline engine then persists its checkpoint/facts and observer metadata in
one short atomic transaction through a transaction hook.
"""

from __future__ import annotations

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


class ChangeRegistryObserverError(ValueError):
    """Fail-closed observer validation error."""


class ChangeRegistryObserverBusy(ChangeRegistryObserverError):
    """Another scan owns the exact seller/account lease."""


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
        self.engine = ChangeRegistryBaselineEngine(
            runtime_dir=self.runtime_dir,
            seller_id=self.seller_id,
            account_scope=self.account_scope,
        )

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
            return self._persist(
                exact_job_id, trigger, slot, snapshot, inject_db_failure
            )
        except Exception as exc:
            self._fail_job(exact_job_id, trigger, slot, exc)
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
                self._persist(exact_job_id, "manual", "", snapshot, False)
            except Exception as exc:  # pragma: no cover - integration boundary
                self._fail_job(exact_job_id, "manual", "", exc)

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
        inject_db_failure: bool,
    ) -> dict[str, Any]:
        interval = snapshot.get("interval") or {}
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
                conn, checkpoint_id, completed_at, canonical_snapshot
            )
            self._append_job_event(
                conn,
                job_id,
                self._next_event_sequence(conn, job_id),
                outcome,
                persisted_at,
                checkpoint_id,
                fact_count,
            )
            if trigger == "scheduled":
                self._append_health(
                    conn, job_id, slot, outcome, persisted_at, checkpoint_id
                )
            self._release(conn, job_id, persisted_at)
            if inject_db_failure:
                raise sqlite3.OperationalError("injected observer DB failure")

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
    ) -> None:
        sources = snapshot.get("sources") or {}
        for source_name in ("prices", "ads"):
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
    ) -> None:
        evidence = canonical_digest(
            {
                "job_id": job_id,
                "sequence": sequence,
                "state": state,
                "checkpoint_id": checkpoint_id,
                "fact_count": fact_count,
                "error_code": error_code,
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
                "error_code": error_code[:120],
                "error_message": error_message[:800],
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
        _insert(
            conn,
            OBSERVER_HEALTH_EVENTS_TABLE,
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
        self, job_id: str, trigger: str, slot: str, exc: Exception
    ) -> None:
        now = canonical_utc_timestamp(self.now_fn())
        with self.store_registry.session(
            "operational", mode="rw", operation="change_registry_observer_fail"
        ) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = self._last_job_state(conn, job_id)
                if state in TERMINAL_JOB_STATES:
                    conn.commit()
                    return
                sequence = self._next_event_sequence(conn, job_id)
                self._append_job_event(
                    conn,
                    job_id,
                    sequence,
                    "failed",
                    now,
                    None,
                    0,
                    error_code=type(exc).__name__,
                    error_message=(
                        "Сканирование не завершено; источник или локальное "
                        "сохранение вернули ошибку."
                    ),
                )
                if trigger == "scheduled":
                    self._append_health(
                        conn, job_id, slot, "failed", now, None
                    )
                lease = conn.execute(
                    f"SELECT owner_job_id FROM {OBSERVER_LEASES_TABLE} "
                    "WHERE seller_id=? AND account_scope=?",
                    (self.seller_id, self.account_scope),
                ).fetchone()
                if lease is not None and str(lease["owner_job_id"]) == job_id:
                    self._release(conn, job_id, now)
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
                           event.checkpoint_id,event.fact_count,event.error_code
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
    "ChangeRegistryReadSurface",
    "DEFAULT_ACCOUNT_SCOPE",
    "TERMINAL_JOB_STATES",
    "activation_job_id",
    "scheduled_slot",
    "utc_now",
]
