#!/usr/bin/env python3
"""Reconcile the one WBC0027 SQLite rollback already owned by SQLite.

This incident-only contour never opens the operational database read-write.  A
dry-run binds the exact implicit rollback and the two audited operational
writers.  The detached one-submit worker may then publish evidence and the
maintenance marker only when every query-only CAS still matches.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.business_data_maintenance as maintenance  # noqa: E402
import apps.hosted_runtime_deploy_barrier as deploy_barrier  # noqa: E402
import apps.sqlite_hot_journal_recovery as hot  # noqa: E402
from apps.recovery_file_utils import file_sha256  # noqa: E402
from apps.wbc0027_breakglass_last_good import (  # noqa: E402
    _canonicalize_sqlite_scalar,
    _update_digest_with_canonical_json,
)
from packages.application.business_data_write_barrier import (  # noqa: E402
    barrier_status,
)
from packages.application.storage_registry import (  # noqa: E402
    MANIFEST_FILENAME,
    StoreRegistry,
    manifest_payload,
)


CONTRACT_NAME = "wbc0027_s047_reconcile_existing_rollback_v1"
RESULT_CONTRACT_NAME = hot.RESULT_CONTRACT_NAME
MODE = "reconciled_existing"
DEFAULT_BACKUP_ROOT = hot.DEFAULT_BACKUP_ROOT
DEFAULT_RESERVE_BYTES = hot.DEFAULT_RESERVE_BYTES
DEFAULT_EVIDENCE_ENVELOPE_BYTES = hot.DEFAULT_EVIDENCE_ENVELOPE_BYTES
EXPECTED_WINDOW_ID = hot.EXPECTED_WINDOW_ID
EXPECTED_GENERATION_ID = hot.EXPECTED_GENERATION_ID
EXPECTED_DATABASE_DEVICE = 2081
EXPECTED_DATABASE_INODE = 1_835_015
EXPECTED_DATABASE_SIZE = hot.EXPECTED_DATABASE_SIZE
EXPECTED_FIRST_ACTIVATION_SHA = (
    "9d4aa7b5f8a605bed6e3a1dd3287a038eb0256d7"
)
EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE = "2026-09-02T04:04:00Z"
EXPECTED_SEALED_ROLLBACK = {
    "database_sha256": hot.EXPECTED_DATABASE_SHA256,
    "database_size_bytes": hot.EXPECTED_DATABASE_SIZE,
    "journal_sha256": hot.EXPECTED_JOURNAL_SHA256,
    "journal_size_bytes": hot.EXPECTED_JOURNAL_SIZE,
    "magic_hex": hot.EXPECTED_JOURNAL_MAGIC.hex(),
    "record_count": 169,
    "nonce_hex": "5296552f",
    "initial_database_pages": 1_814_968,
    "sector_size": 512,
    "page_size": 4096,
    "record_region_bytes": 694_088,
    "trailing_bytes": 7_255_464,
    "unique_page_count": 169,
    "page_number_min": 1,
    "page_number_max": 1_809_317,
    "page_number_list_sha256": (
        "8ef27ddebf2d2b12dbf0050bd7719d64a18266f38917aa38a4cf170ec7c8a12e"
    ),
    "checksum_mismatch_count": 0,
    "pages_different_from_main": 168,
    "pages_equal_to_main": 1,
    "expected_recovered_database_sha256": hot.EXPECTED_RECOVERED_DATABASE_SHA256,
}
OPERATIONAL_TABLES = (
    "change_registry_observer_jobs",
    "change_registry_observer_job_events",
    "change_registry_checkpoints",
    "change_registry_checkpoint_source_manifests",
    "sheet_vitrina_v1_source_health_status",
)
ACTIVATION_JOB_PREFIX = "crjob_activation_"
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
OPERATION_PATTERN = re.compile(r"[0-9a-f]{64}")


class ReconcileExistingError(RuntimeError):
    """The exact implicit-rollback reconciliation is not admissible."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    digest = hashlib.sha256()
    _update_digest_with_canonical_json(digest, value)
    return "sha256:" + digest.hexdigest()


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    material = json.loads(_canonical_json(plan))
    material.pop("fingerprint", None)
    material.pop("created_at", None)
    return _fingerprint(material)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    return hot._read_json(path, label=label)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    hot._atomic_json(path, payload)


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({json.dumps(table)})"
        )
    ]


def _stream_tables(
    connection: sqlite3.Connection,
    tables: Sequence[str],
) -> dict[str, Any]:
    """Stream the canonical {table:[rows]} byte sequence into SHA-256."""

    digest = hashlib.sha256()
    digest.update(b"{")
    table_counts: dict[str, int] = {}
    total_rows = 0
    try:
        for table_index, table in enumerate(sorted(tables)):
            if table_index:
                digest.update(b",")
            _update_digest_with_canonical_json(digest, table)
            digest.update(b":[")
            columns = _table_columns(connection, table)
            if not columns:
                raise ReconcileExistingError(f"table has no columns: {table}")
            select = ",".join(f'"{item}"' for item in columns)
            count = 0
            for row in connection.execute(
                f'SELECT {select} FROM "{table}" ORDER BY {select}'
            ):
                if count:
                    digest.update(b",")
                _update_digest_with_canonical_json(
                    digest,
                    [_canonicalize_sqlite_scalar(value) for value in row],
                )
                count += 1
            digest.update(b"]")
            table_counts[table] = count
            total_rows += count
    except Exception as exc:
        if isinstance(exc, ReconcileExistingError):
            raise
        raise ReconcileExistingError("canonical SQLite table streaming failed") from exc
    digest.update(b"}")
    return {
        "contract": "sqlite_canonical_table_stream_v1",
        "digest": "sha256:" + digest.hexdigest(),
        "table_count": len(table_counts),
        "row_count": total_rows,
        "table_row_counts": table_counts,
    }


def _rows(
    connection: sqlite3.Connection,
    table: str,
    *,
    where: str = "",
    params: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    columns = _table_columns(connection, table)
    select = ",".join(f'"{item}"' for item in columns)
    suffix = f" WHERE {where}" if where else ""
    order = ",".join(f'"{item}"' for item in columns)
    return [
        {
            column: _canonicalize_sqlite_scalar(value)
            for column, value in zip(columns, row)
        }
        for row in connection.execute(
            f'SELECT {select} FROM "{table}"{suffix} ORDER BY {order}',
            tuple(params),
        )
    ]


def _validate_operational_rows(
    connection: sqlite3.Connection,
    *,
    deployed_sha: str,
) -> dict[str, Any]:
    expected_shas = {EXPECTED_FIRST_ACTIVATION_SHA, deployed_sha}
    expected_job_ids = {ACTIVATION_JOB_PREFIX + value for value in expected_shas}
    jobs = _rows(
        connection,
        "change_registry_observer_jobs",
        where="requested_at>=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,),
    )
    if {str(row.get("job_id") or "") for row in jobs} != expected_job_ids:
        raise ReconcileExistingError("post-recovery activation job set is not exact")
    if any(
        row.get("trigger_kind") != "activation"
        or row.get("scheduled_slot") != ""
        or not str(row.get("requested_by") or "")
        for row in jobs
    ):
        raise ReconcileExistingError("post-recovery activation job identity drifted")

    placeholders = ",".join("?" for _ in expected_job_ids)
    events = _rows(
        connection,
        "change_registry_observer_job_events",
        where=f"job_id IN ({placeholders})",
        params=tuple(sorted(expected_job_ids)),
    )
    events_since_recovery = _rows(
        connection,
        "change_registry_observer_job_events",
        where="occurred_at>=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,),
    )
    if events_since_recovery != events:
        raise ReconcileExistingError("post-recovery activation event set is not exact")
    checkpoints: set[str] = set()
    for job_id in sorted(expected_job_ids):
        own = [row for row in events if row.get("job_id") == job_id]
        if [int(row.get("sequence_no") or 0) for row in own] != [1, 2, 3]:
            raise ReconcileExistingError("activation event sequence is not exact")
        if [row.get("state") for row in own] != ["accepted", "running", "complete"]:
            raise ReconcileExistingError("activation event states are not exact")
        complete = own[-1]
        if int(complete.get("fact_count") or 0) != 0:
            raise ReconcileExistingError("activation produced business facts")
        checkpoint_id = str(complete.get("checkpoint_id") or "")
        if not checkpoint_id:
            raise ReconcileExistingError("activation checkpoint is absent")
        checkpoints.add(checkpoint_id)
    if len(checkpoints) != len(expected_job_ids):
        raise ReconcileExistingError("activation checkpoint identity is ambiguous")

    checkpoint_rows = _rows(
        connection,
        "change_registry_checkpoints",
        where=f"checkpoint_id IN ({','.join('?' for _ in checkpoints)})",
        params=tuple(sorted(checkpoints)),
    )
    if {str(row.get("checkpoint_id") or "") for row in checkpoint_rows} != checkpoints:
        raise ReconcileExistingError("activation checkpoint rows are incomplete")
    checkpoints_since_recovery = _rows(
        connection,
        "change_registry_checkpoints",
        where="completed_at>=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,),
    )
    if checkpoints_since_recovery != checkpoint_rows:
        raise ReconcileExistingError("post-recovery checkpoint set is not exact")
    manifests = _rows(
        connection,
        "change_registry_checkpoint_source_manifests",
        where=f"checkpoint_id IN ({','.join('?' for _ in checkpoints)})",
        params=tuple(sorted(checkpoints)),
    )
    if len(manifests) != 2 * len(checkpoints):
        raise ReconcileExistingError("activation source manifest count is not exact")
    manifests_since_recovery = _rows(
        connection,
        "change_registry_checkpoint_source_manifests",
        where="created_at>=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,),
    )
    if manifests_since_recovery != manifests:
        raise ReconcileExistingError("post-recovery source manifest set is not exact")
    for checkpoint_id in checkpoints:
        sources = {
            str(row.get("source_name") or "")
            for row in manifests
            if row.get("checkpoint_id") == checkpoint_id
        }
        if sources != {"prices", "ads"}:
            raise ReconcileExistingError("activation source manifest set drifted")

    source_health = _rows(
        connection,
        "sheet_vitrina_v1_source_health_status",
        where="checked_at>=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,),
    )
    if len(source_health) != 1 or source_health[0].get("source_key") != "seller_portal_auth":
        raise ReconcileExistingError("post-recovery source-health writer set drifted")
    try:
        payload = json.loads(str(source_health[0].get("payload_json") or ""))
    except json.JSONDecodeError as exc:
        raise ReconcileExistingError("seller source-health payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ReconcileExistingError("seller source-health payload is not an object")

    return {
        "not_before": EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,
        "activation_deployed_shas": sorted(expected_shas),
        "activation_jobs": jobs,
        "activation_job_events": events,
        "activation_checkpoints": checkpoint_rows,
        "activation_source_manifests": manifests,
        "source_health_status": source_health,
        "code_paths": {
            "activation": (
                "apps/change_registry_observer.py:run -> "
                "packages.application.change_registry_observer.ChangeRegistryObserver.run"
            ),
            "source_health": (
                "RegistryUploadHttpApplication.handle_seller_portal_session_check_request "
                "-> RegistryUploadDbBackedRuntime.save_source_health_status"
            ),
        },
    }


def database_evidence(database: Path, *, deployed_sha: str) -> dict[str, Any]:
    with closing(
        sqlite3.connect(
            f"file:{database.resolve().as_posix()}?mode=ro&immutable=1",
            uri=True,
            timeout=120,
        )
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not set(OPERATIONAL_TABLES) <= tables:
            raise ReconcileExistingError("operational reconciliation table set is incomplete")
        operational_rows = _validate_operational_rows(
            connection,
            deployed_sha=deployed_sha,
        )
        non_operational = _stream_tables(
            connection,
            sorted(tables - set(OPERATIONAL_TABLES)),
        )
        operational = _stream_tables(connection, OPERATIONAL_TABLES)
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        if integrity != ["ok"] or foreign_keys:
            raise ReconcileExistingError("query-only SQLite integrity readback failed")
        sqlite_readback = {
            "query_only": int(connection.execute("PRAGMA query_only").fetchone()[0]),
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
            "integrity_check": "ok",
            "foreign_key_violation_count": 0,
            "schema_version": int(connection.execute("PRAGMA schema_version").fetchone()[0]),
            "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        }
    return {
        "non_operational": non_operational,
        "operational": operational,
        "operational_rows": operational_rows,
        "sqlite_readback": sqlite_readback,
    }


def _business_writer_timeline() -> dict[str, Any]:
    command = [
        "journalctl",
        "--no-pager",
        "--output=json",
        "--since",
        EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,
    ]
    for unit in maintenance.ALL_BUSINESS_SERVICE_UNITS:
        command.extend(["--unit", unit])
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise ReconcileExistingError("business writer journal timeline is unavailable")
    rows = [line for line in completed.stdout.splitlines() if line.strip()]
    if rows:
        raise ReconcileExistingError("business writer timeline is not empty")
    return {
        "since": EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,
        "units": list(maintenance.ALL_BUSINESS_SERVICE_UNITS),
        "event_count": 0,
        "digest": "sha256:" + hashlib.sha256(b"").hexdigest(),
    }


def _incident_control_preflight(
    *,
    runtime_dir: Path,
    backup_root: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
    operation_id: str,
    window_id: str,
    plan_fingerprint: str,
    allow_reconcile_job: bool,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve()
    backup_root = backup_root.resolve()
    if runtime_dir != Path("/opt/wb-core-runtime/state"):
        raise ReconcileExistingError("reconciliation requires canonical runtime dir")
    if backup_root != DEFAULT_BACKUP_ROOT or backup_root.is_symlink():
        raise ReconcileExistingError("reconciliation requires canonical backup mount")
    if OPERATION_PATTERN.fullmatch(operation_id) is None:
        raise ReconcileExistingError("operation id must be exact 64-hex")
    if window_id != EXPECTED_WINDOW_ID or DIGEST_PATTERN.fullmatch(plan_fingerprint) is None:
        raise ReconcileExistingError("exact barrier identity is invalid")
    current_sha = hot._deployed_sha(deployed_sha_file, deployed_sha)
    barrier = barrier_status(runtime_dir)
    if (
        barrier.get("active") is not True
        or barrier.get("phase") != "acquiring"
        or barrier.get("hold_confirmed") is not False
        or barrier.get("window_id") != window_id
        or barrier.get("plan_fingerprint") != plan_fingerprint
    ):
        raise ReconcileExistingError("exact acquiring barrier drifted")
    state = _read_json(runtime_dir / maintenance.STATE_FILENAME, label="maintenance state")
    epoch = dict(state.get("prepared_abort_partial_restore_recovery_epoch") or {})
    if (
        state.get("phase") != "abort_quiescing"
        or state.get("exact_prior_state_restored") is True
        or epoch.get("schema_version") != hot.EXPECTED_PARTIAL_EPOCH_SCHEMA
        or int(epoch.get("epoch") or 0) != 2
        or epoch.get("deployed_sha") != hot.EXPECTED_SOURCE_EPOCH_SHA
        or epoch.get("window_id") != window_id
        or epoch.get("plan_fingerprint") != plan_fingerprint
        or epoch.get("barrier_state_fingerprint") != barrier.get("state_fingerprint")
        or sorted(epoch.get("timer_units_to_disable") or [])
        != sorted(hot.RECOVERY_PAUSE_OWNED_TIMERS)
        or sorted(epoch.get("disabled_timer_units") or [])
        != sorted(hot.RECOVERY_PAUSE_OWNED_TIMERS)
        or str(epoch.get("pending_disable_unit") or "")
    ):
        raise ReconcileExistingError("partial abort recovery epoch drifted")
    try:
        preserved = deploy_barrier._preserved_units(runtime_dir)
    except Exception as exc:
        raise ReconcileExistingError("pause-owned deploy barrier validation failed") from exc
    if preserved != set(hot.RECOVERY_PAUSE_OWNED_TIMERS):
        raise ReconcileExistingError("pause-owned unit preservation set drifted")
    systemd = maintenance.SystemdClient()
    timer_states = {
        unit: systemd.unit_state(unit) for unit in maintenance.ALL_BUSINESS_TIMER_UNITS
    }
    service_states = {
        unit: systemd.unit_state(unit) for unit in maintenance.ALL_BUSINESS_SERVICE_UNITS
    }
    if any(hot._timer_pair(value) != ("disabled", "inactive") for value in timer_states.values()):
        raise ReconcileExistingError("a business timer is not paused")
    if any(
        str(value.get("is_active") or "") not in maintenance.QUIESCENT_SERVICE_STATES
        or int((value.get("properties") or {}).get("MainPID") or 0) != 0
        for value in service_states.values()
    ):
        raise ReconcileExistingError("a business writer service is not terminal")
    processes = maintenance._writer_processes()
    if processes:
        raise ReconcileExistingError("a business writer process is present")
    jobs = hot._systemd_jobs()
    allowed_marker = f"wb-core-storage-recovery-sanitation@{operation_id}.service"
    unexpected_jobs = [
        row for row in jobs
        if not (allow_reconcile_job and allowed_marker in row)
    ]
    if unexpected_jobs:
        raise ReconcileExistingError("a systemd job is active")
    locks = maintenance._lock_summary(runtime_dir)
    if any(
        bool(value.get("held"))
        for key, value in locks.items()
        if key != "seller_portal"
    ) or bool((locks.get("seller_portal") or {}).get("busy")):
        raise ReconcileExistingError("a business writer lock is held")
    counters = maintenance._prepared_abort_breakglass_counters(runtime_dir)
    if any(int(value) != 0 for value in counters.values()):
        raise ReconcileExistingError("WBC0027 business operation counters are nonzero")
    return {
        "deployed_sha": current_sha,
        "source_epoch_deployed_sha": str(epoch["deployed_sha"]),
        "barrier": {
            key: barrier.get(key)
            for key in (
                "active", "phase", "hold_confirmed", "window_id",
                "plan_fingerprint", "state_fingerprint",
            )
        },
        "maintenance": {
            "phase": state.get("phase"),
            "partial_epoch_fingerprint": _fingerprint(epoch),
            "timer_states": timer_states,
            "service_states": service_states,
            "writer_processes": processes,
            "writer_locks": locks,
            "business_operation_counters": counters,
        },
        "systemd_jobs": jobs,
        "business_writer_timeline": _business_writer_timeline(),
    }


def _preflight(
    *,
    runtime_dir: Path,
    backup_root: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
    operation_id: str,
    window_id: str,
    plan_fingerprint: str,
    reserve_bytes: int,
    evidence_envelope_bytes: int,
    allow_existing_operation: bool = False,
    allow_reconcile_job: bool = False,
    stable_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    if reserve_bytes != DEFAULT_RESERVE_BYTES:
        raise ReconcileExistingError("Finance reserve must remain exact")
    if evidence_envelope_bytes != DEFAULT_EVIDENCE_ENVELOPE_BYTES:
        raise ReconcileExistingError("evidence envelope must remain exact")
    control = _incident_control_preflight(
        runtime_dir=runtime_dir,
        backup_root=backup_root,
        deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
        operation_id=operation_id,
        window_id=window_id,
        plan_fingerprint=plan_fingerprint,
        allow_reconcile_job=allow_reconcile_job,
    )
    runtime_dir = runtime_dir.resolve()
    registry = StoreRegistry(runtime_dir)
    manifest = registry.load(require_files=True)
    if manifest.state != "cutover" or manifest.canonical_source != "split":
        raise ReconcileExistingError("canonical storage is not exact split cutover")
    database = registry.resolve("operational", manifest=manifest)
    raw = registry.resolve("finance_raw", manifest=manifest)
    expected_database = runtime_dir / "generations" / EXPECTED_GENERATION_ID / "operational.sqlite3"
    if database != expected_database:
        raise ReconcileExistingError("operational generation identity drifted")
    journal = Path(str(database) + "-journal")
    if journal.exists():
        raise ReconcileExistingError("rollback journal reappeared")
    first = hot._file_identity(database)
    if stable_interval_seconds > 0:
        time.sleep(stable_interval_seconds)
    if journal.exists():
        raise ReconcileExistingError("rollback journal reappeared during stable read")
    second = hot._file_identity(database)
    if first != second:
        raise ReconcileExistingError("implicit-recovery database is not physically stable")
    if (
        first["device"] != EXPECTED_DATABASE_DEVICE
        or first["inode"] != EXPECTED_DATABASE_INODE
        or first["size_bytes"] != EXPECTED_DATABASE_SIZE
        or first["sha256"] == hot.EXPECTED_DATABASE_SHA256
        or first["sha256"] == hot.EXPECTED_RECOVERED_DATABASE_SHA256
    ):
        raise ReconcileExistingError("implicit-recovery database identity is outside incident")
    openers = hot._openers({database})
    kernel_locks = hot._kernel_locks({database})
    if kernel_locks:
        raise ReconcileExistingError("operational database has a kernel lock")
    for opener in openers:
        if opener["access"] != "read_only":
            raise ReconcileExistingError("operational database has a writer opener")
        if (
            "apps/registry_upload_http_entrypoint_live.py" not in opener["command"]
            or "wb-core-registry-http.service" not in opener["cgroup"]
        ):
            raise ReconcileExistingError("operational database opener is unknown")
    database_snapshot = database_evidence(database, deployed_sha=control["deployed_sha"])
    if journal.exists() or hot._file_identity(database) != second:
        raise ReconcileExistingError("database drifted across logical reconciliation")
    manifest_file = hot._file_identity(runtime_dir / MANIFEST_FILENAME)
    raw_file = hot._file_identity(raw)
    if (
        manifest_file["sha256"] != hot.EXPECTED_MANIFEST_FILE_SHA256
        or raw_file["sha256"] != hot.EXPECTED_RAW_SHA256
    ):
        raise ReconcileExistingError("Finance raw/generation manifest identity drifted")
    capacity = hot._filesystem(backup_root)
    if int(capacity["available_bytes"]) < reserve_bytes + evidence_envelope_bytes:
        raise ReconcileExistingError("reconciliation evidence would breach Finance reserve")
    backup_directory = (
        backup_root / "private-evidence" / "production-goals"
        / f"wbc0027-s047-reconcile-existing-{control['deployed_sha'][:8]}"
        / operation_id
    )
    if backup_directory.exists() and not allow_existing_operation:
        raise ReconcileExistingError("reconciliation operation directory already exists")
    return {
        "contract_name": CONTRACT_NAME,
        "mode": MODE,
        "read_only": True,
        "operation_id": operation_id,
        "deployed_sha": control["deployed_sha"],
        "source_epoch_deployed_sha": control["source_epoch_deployed_sha"],
        "runtime_dir": str(runtime_dir),
        "barrier": control["barrier"],
        "maintenance": control["maintenance"],
        "systemd_jobs": control["systemd_jobs"],
        "business_writer_timeline": control["business_writer_timeline"],
        "storage_generation_manifest": manifest_payload(manifest),
        "storage_generation_manifest_file": manifest_file,
        "raw_database": raw_file,
        "database": first,
        "journal": {
            "path": str(journal),
            "absent": True,
            "sealed_before": EXPECTED_SEALED_ROLLBACK,
        },
        "database_reconciliation": database_snapshot,
        "openers": openers,
        "kernel_locks": kernel_locks,
        "backup": {
            "directory": str(backup_directory),
            "reserve_bytes": reserve_bytes,
            "evidence_envelope_bytes": evidence_envelope_bytes,
            "capacity_before": capacity,
            "projected_available_bytes": int(capacity["available_bytes"]) - evidence_envelope_bytes,
            "projected_reserve_headroom_bytes": int(capacity["available_bytes"]) - evidence_envelope_bytes - reserve_bytes,
        },
        "expected_effect": {
            "logical_business_delta": 0,
            "sqlite_write": False,
            "physical_database_change": False,
            "journal_removed_by_this_operation": False,
            "evidence_marker_only": True,
        },
    }


def build_plan(**kwargs: Any) -> dict[str, Any]:
    plan = {**_preflight(**kwargs), "created_at": _now()}
    plan["fingerprint"] = _plan_fingerprint(plan)
    return plan


def _fresh_matches_plan(plan: Mapping[str, Any], fresh: Mapping[str, Any]) -> bool:
    fresh_material = json.loads(_canonical_json(fresh))
    plan_material = json.loads(_canonical_json(plan))
    for material in (fresh_material, plan_material):
        material.pop("created_at", None)
        material.pop("fingerprint", None)
    return fresh_material == plan_material


def apply_plan(
    *,
    plan_path: Path,
    plan_sha256: str,
    fingerprint: str,
    deployed_sha_file: Path,
) -> dict[str, Any]:
    if DIGEST_PATTERN.fullmatch(plan_sha256) is None or DIGEST_PATTERN.fullmatch(fingerprint) is None:
        raise ReconcileExistingError("reviewed reconciliation identity is invalid")
    plan_path = plan_path.resolve()
    plan = _read_json(plan_path, label="reviewed reconcile-existing plan")
    if "sha256:" + file_sha256(plan_path) != plan_sha256:
        raise ReconcileExistingError("reviewed reconciliation plan bytes drifted")
    if (
        plan.get("contract_name") != CONTRACT_NAME
        or plan.get("mode") != MODE
        or plan.get("read_only") is not True
        or plan.get("fingerprint") != fingerprint
        or _plan_fingerprint(plan) != fingerprint
    ):
        raise ReconcileExistingError("reviewed reconciliation plan identity drifted")
    fresh = build_plan(
        runtime_dir=Path(str(plan["runtime_dir"])),
        backup_root=DEFAULT_BACKUP_ROOT,
        deployed_sha=str(plan["deployed_sha"]),
        deployed_sha_file=deployed_sha_file,
        operation_id=str(plan["operation_id"]),
        window_id=str(plan["barrier"]["window_id"]),
        plan_fingerprint=str(plan["barrier"]["plan_fingerprint"]),
        reserve_bytes=int(plan["backup"]["reserve_bytes"]),
        evidence_envelope_bytes=int(plan["backup"]["evidence_envelope_bytes"]),
        allow_existing_operation=True,
        allow_reconcile_job=True,
    )
    if not _fresh_matches_plan(plan, fresh):
        raise ReconcileExistingError("reviewed reconciliation plan is stale")
    runtime_dir = Path(str(plan["runtime_dir"]))
    backup_dir = Path(str(plan["backup"]["directory"]))
    state_path = backup_dir / "reconciliation-state.json"
    result_path = backup_dir / "result.json"
    if backup_dir.exists():
        state = _read_json(state_path, label="reconciliation operation state")
        if any(
            state.get(key) != value
            for key, value in {
                "contract_name": RESULT_CONTRACT_NAME,
                "mode": MODE,
                "operation_id": plan["operation_id"],
                "deployed_sha": plan["deployed_sha"],
                "plan_sha256": plan_sha256,
                "plan_fingerprint": fingerprint,
            }.items()
        ):
            raise ReconcileExistingError("reconciliation operation state drifted")
    else:
        backup_dir.mkdir(parents=True, mode=0o700)
        os.chmod(backup_dir, 0o700)
        hot._fsync_directory(backup_dir.parent)
        state = {
            "contract_name": RESULT_CONTRACT_NAME,
            "mode": MODE,
            "operation_id": plan["operation_id"],
            "deployed_sha": plan["deployed_sha"],
            "plan_sha256": plan_sha256,
            "plan_fingerprint": fingerprint,
            "phase": "evidence_intent",
            "started_at": _now(),
        }
        _atomic_json(state_path, state)
    if result_path.exists():
        result = _read_json(result_path, label="reconciliation result")
        material = dict(result)
        observed = str(material.pop("result_fingerprint", ""))
        if observed != _fingerprint(material):
            raise ReconcileExistingError("reconciliation result drifted")
    else:
        result = {
            "contract_name": RESULT_CONTRACT_NAME,
            "mode": MODE,
            "status": "reconciled_existing",
            "operation_id": plan["operation_id"],
            "deployed_sha": plan["deployed_sha"],
            "source_epoch_deployed_sha": plan["source_epoch_deployed_sha"],
            "plan_sha256": plan_sha256,
            "plan_fingerprint": fingerprint,
            "barrier": plan["barrier"],
            "maintenance_partial_epoch_fingerprint": plan["maintenance"]["partial_epoch_fingerprint"],
            "database_after": plan["database"],
            "journal_absent": True,
            "sqlite_readback": plan["database_reconciliation"]["sqlite_readback"],
            "non_operational_digest": plan["database_reconciliation"]["non_operational"],
            "operational_digest": plan["database_reconciliation"]["operational"],
            "operational_rows": plan["database_reconciliation"]["operational_rows"],
            "business_writer_timeline": plan["business_writer_timeline"],
            "business_operation_counters": plan["maintenance"]["business_operation_counters"],
            "logical_business_delta": 0,
            "sqlite_write": False,
            "completed_at": _now(),
        }
        result["result_fingerprint"] = _fingerprint(result)
        _atomic_json(result_path, result)
    marker = {
        "contract_name": RESULT_CONTRACT_NAME,
        "mode": MODE,
        "operation_id": result["operation_id"],
        "source_epoch_deployed_sha": result["source_epoch_deployed_sha"],
        "deployed_sha": result["deployed_sha"],
        "barrier": result["barrier"],
        "maintenance_partial_epoch_fingerprint": result["maintenance_partial_epoch_fingerprint"],
        "database_after": result["database_after"],
        "journal_absent": True,
        "sqlite_readback": result["sqlite_readback"],
        "non_operational_digest": result["non_operational_digest"],
        "operational_digest": result["operational_digest"],
        "business_operation_counters": result["business_operation_counters"],
        "result_path": str(result_path),
        "result_fingerprint": result["result_fingerprint"],
        "completed_at": result["completed_at"],
    }
    marker["marker_fingerprint"] = _fingerprint(marker)
    marker_path = runtime_dir / hot.MARKER_FILENAME
    if marker_path.exists():
        if _read_json(marker_path, label="reconciliation marker") != marker:
            raise ReconcileExistingError("reconciliation marker drifted")
    else:
        _atomic_json(marker_path, marker)
    state.update(
        {
            "phase": "completed",
            "completed_at": result["completed_at"],
            "result_fingerprint": result["result_fingerprint"],
        }
    )
    _atomic_json(state_path, state)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--deployed-sha-file", type=Path, default=ROOT / ".wb-core-runtime-sha")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--window-id", required=True)
    parser.add_argument("--plan-fingerprint", required=True)
    parser.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    parser.add_argument("--evidence-envelope-bytes", type=int, default=DEFAULT_EVIDENCE_ENVELOPE_BYTES)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    plan = build_plan(
        runtime_dir=args.runtime_dir,
        backup_root=args.backup_root,
        deployed_sha=args.deployed_sha,
        deployed_sha_file=args.deployed_sha_file,
        operation_id=args.operation_id,
        window_id=args.window_id,
        plan_fingerprint=args.plan_fingerprint,
        reserve_bytes=args.reserve_bytes,
        evidence_envelope_bytes=args.evidence_envelope_bytes,
    )
    if args.output:
        if args.output.resolve().exists():
            raise ReconcileExistingError("dry-run output already exists")
        _atomic_json(args.output.resolve(), plan)
    else:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
