"""Dry-run-first Finance storage split planner and resumable candidate builder."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence

from packages.application.finance_raw_storage import (
    FinanceRawLiveTailBridge,
    OPERATIONAL_SCHEMA_TABLES,
    RAW_SCHEMA_TABLES,
    bind_generation_identity,
    ensure_operational_schema,
    ensure_raw_schema,
    shadow_compare_week,
    storage_health,
)
from packages.application.business_data_write_barrier import barrier_status
from packages.application.storage_registry import (
    MANIFEST_FILENAME,
    MONOLITH_FILENAME,
    StoreRegistry,
    atomic_write_manifest,
    build_manifest,
    explain_query_plan,
    manifest_payload,
    parse_manifest,
)
PLAN_CONTRACT = "wb_core_finance_storage_split_plan_v1"
MIGRATION_CONTRACT = "wb_core_finance_storage_split_candidate_v1"
SNAPSHOT_PLAN_CONTRACT = "wb_core_finance_storage_snapshot_plan_v1"
SNAPSHOT_CONTRACT = "wb_core_finance_storage_coherent_snapshot_v1"
SHADOW_STATE_CONTRACT = "wb_core_finance_shadow_ingest_state_v1"
SHADOW_STATE_FILENAME = ".finance-storage-shadow-ingest.json"
SHADOW_VERIFICATION_CONTRACT = "wb_core_finance_shadow_verification_v1"
SHADOW_VERIFICATION_FILENAME = "shadow_verification.json"
CUTOVER_PLAN_CONTRACT = "wb_core_finance_storage_cutover_plan_v1"
CUTOVER_RESULT_CONTRACT = "wb_core_finance_storage_cutover_result_v1"
ROLLBACK_PLAN_CONTRACT = "wb_core_finance_storage_rollback_plan_v1"
ROLLBACK_CANDIDATE_CONTRACT = "wb_core_finance_storage_rollback_candidate_v1"
ROLLBACK_RESULT_CONTRACT = "wb_core_finance_storage_rollback_result_v1"
LEGACY_RAW_TABLE = "wb_finance_weekly_raw_rows"
RAW_LEGACY_OBJECTS = frozenset(
    {
        LEGACY_RAW_TABLE,
        "wb_finance_raw_by_week",
        "wb_finance_raw_by_sku_week",
        "sqlite_autoindex_wb_finance_weekly_raw_rows_1",
    }
)
_SYSTEMD_UNITS = (
    "wb-core-registry-http.service",
    "wb-core-wb-finance-weekly.service",
    "wb-core-wb-finance-weekly.timer",
    "wb-core-warehouse-functional-sync.service",
    "wb-core-warehouse-functional-sync.timer",
    "wb-core-sheet-vitrina-refresh.service",
    "wb-core-sheet-vitrina-refresh.timer",
    "wb-core-sheet-vitrina-closure-retry.service",
    "wb-core-sheet-vitrina-closure-retry.timer",
    "wb-core-feedbacks-auto-complaints-tick.service",
    "wb-core-feedbacks-auto-complaints-tick.timer",
    "wb-core-spp-tester-schedule-tick.service",
    "wb-core-spp-tester-schedule-tick.timer",
    "wb-core-autoanswers-readonly-sync.service",
    "wb-core-autoanswers-readonly-sync.timer",
    "wb-core-autoanswers-worker.service",
    "wb-core-autoanswers-worker.timer",
)
_GIB = 1024**3


class FinanceStorageMigrationError(ValueError):
    """Fail-closed migration planner or candidate-builder error."""


class InjectedMigrationFault(RuntimeError):
    """Test-only deterministic interruption."""


@dataclass(frozen=True)
class LogicalDigest:
    row_count: int
    digest: str
    payload_digest: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical_json(value)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    """Bind approval to stable source/scope facts, not volatile capacity counters.

    Free bytes, PIDs and timer timestamps are still emitted in every plan and
    are rechecked immediately before any candidate creation. They cannot be
    part of an idempotent review fingerprint because unrelated filesystem use
    or a service restart would otherwise invalidate an unchanged source plan.
    """

    stable = json.loads(_canonical_json(plan))
    stable.pop("fingerprint", None)
    stable.pop("created_at", None)
    stable.pop("performance", None)
    capacity = stable.get("capacity", {})
    for key in (
        "available_bytes",
        "shortfall_bytes",
        "remaining_bytes_after_reservation",
        "sufficient",
    ):
        capacity.pop(key, None)
    writers = stable.get("writers_and_timers", {})
    writers["database_openers"] = sorted(
        {
            (
                str(item.get("comm") or ""),
                str(item.get("access_mode") or ""),
            )
            for item in writers.get("database_openers", [])
        }
    )
    for item in writers.get("systemd_units", []):
        for key in ("main_pid", "last_trigger", "next_trigger"):
            item.pop(key, None)
    return _digest(stable)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        data = (_canonical_json(payload) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    return value


def _table_info(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return list(conn.execute(f"PRAGMA table_info({_quoted(table)})").fetchall())


def _table_order(info: Sequence[sqlite3.Row]) -> list[str]:
    primary = sorted(
        (
            (int(row["pk"]), str(row["name"]))
            for row in info
            if int(row["pk"] or 0) > 0
        ),
        key=lambda item: item[0],
    )
    return [name for _position, name in primary]


def logical_table_digest(conn: sqlite3.Connection, table: str) -> LogicalDigest:
    info = _table_info(conn, table)
    if not info:
        raise FinanceStorageMigrationError(f"table schema is unavailable: {table}")
    columns = [str(row["name"]) for row in info]
    order = _table_order(info)
    select = ",".join(_quoted(column) for column in columns)
    sql = f"SELECT {select} FROM {_quoted(table)}"
    if order:
        sql += " ORDER BY " + ",".join(_quoted(column) for column in order)
    else:
        sql += " ORDER BY rowid"
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(sql):
        values = [_sqlite_value(row[column]) for column in columns]
        digest.update((_canonical_json(values) + "\n").encode("utf-8"))
        count += 1
    return LogicalDigest(row_count=count, digest="sha256:" + digest.hexdigest())


def _schema_inventory(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "type": str(row["type"]),
            "name": str(row["name"]),
            "table": str(row["tbl_name"]),
            "sql": str(row["sql"] or ""),
        }
        for row in conn.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%'
               ORDER BY type,name"""
        ).fetchall()
    ]


def _table_owner(table: str) -> dict[str, Any]:
    if table == LEGACY_RAW_TABLE:
        return {
            "owner": "finance_raw",
            "readers": ["finance_ingest", "finance_shadow", "migration_runner"],
            "writers": ["finance_ingest"],
            "migration_action": "copy_to_finance_raw_rows",
        }
    if table in RAW_SCHEMA_TABLES:
        return {
            "owner": "finance_raw",
            "readers": ["finance_ingest", "outbox_consumer", "finance_shadow"],
            "writers": ["finance_ingest", "outbox_ack"],
            "migration_action": "split_control_table",
        }
    if table.startswith("wb_finance_weekly_reports"):
        return {
            "owner": "operational",
            "readers": ["finance_projection", "finance_ingest_compatibility"],
            "writers": ["finance_ingest_compatibility"],
            "migration_action": (
                "copy_operational_until_ingest_batch_projection_is_cutover"
            ),
        }
    if table.startswith("wb_finance_") or table in OPERATIONAL_SCHEMA_TABLES:
        return {
            "owner": "operational",
            "readers": ["finance_projection", "partner_report", "operator_ui"],
            "writers": ["finance_outbox_consumer", "reviewed_finance_runner"],
            "migration_action": "copy_operational",
        }
    if (
        "warehouse" in table
        or table.startswith("supplier_")
        or table.startswith("sheet_vitrina_v1_supplier")
        or table.startswith("ff_stock_")
        or table.startswith("cny_")
        or table.startswith("own_product_capital")
    ):
        return {
            "owner": "operational",
            "readers": ["warehouse_cost", "economics", "operator_ui"],
            "writers": ["bounded_operational_owner"],
            "migration_action": "copy_operational",
        }
    return {
        "owner": "operational",
        "readers": ["owning_runtime_module"],
        "writers": ["owning_runtime_module"],
        "migration_action": "copy_operational",
    }


def _table_matrix(
    conn: sqlite3.Connection,
    *,
    allocations: Mapping[str, int],
    logical_overrides: Mapping[str, LogicalDigest] | None = None,
) -> list[dict[str, Any]]:
    tables = [
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
        ).fetchall()
    ]
    matrix: list[dict[str, Any]] = []
    overrides = dict(logical_overrides or {})
    for table in tables:
        digest = overrides.get(table) or logical_table_digest(conn, table)
        matrix.append(
            {
                "table": table,
                **_table_owner(table),
                "current_store": "monolith",
                "row_count": digest.row_count,
                "logical_digest": digest.digest,
                "allocated_bytes": int(allocations.get(table, 0)),
            }
        )
    return matrix


def _dbstat_allocations(
    conn: sqlite3.Connection,
) -> tuple[dict[str, int], dict[str, Any]]:
    try:
        allocations = {
            str(row["name"]): int(row["bytes"] or 0)
            for row in conn.execute(
                "SELECT name,SUM(pgsize) AS bytes FROM dbstat GROUP BY name ORDER BY name"
            ).fetchall()
        }
        return allocations, {
            "method": "sqlite_dbstat",
            "exact_per_object": True,
            "conservative_fallback": False,
        }
    except sqlite3.DatabaseError as exc:
        tables = [
            str(row["name"])
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
            ).fetchall()
        ]
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        total_bytes = page_count * page_size
        # Some minimal SQLite builds omit the dbstat virtual table.  Keep local
        # fixture planning available while charging the complete file to raw,
        # which is conservative for the pre-creation capacity gate. Production
        # dry-runs fail closed below unless exact per-object dbstat is available.
        allocations = {table: 0 for table in tables}
        allocations[LEGACY_RAW_TABLE] = total_bytes
        return allocations, {
            "method": "whole_file_charged_to_raw",
            "exact_per_object": False,
            "conservative_fallback": True,
            "reason": f"{type(exc).__name__}: dbstat unavailable",
            "page_count": page_count,
            "page_size": page_size,
            "whole_file_bytes": total_bytes,
        }


def _raw_chunk_manifest(
    conn: sqlite3.Connection,
    *,
    chunk_size: int,
) -> tuple[list[dict[str, Any]], LogicalDigest, dict[str, Any]]:
    sql = f"""SELECT rowid,seller_id,report_id,rrd_id,row_hash,raw_json
              FROM {LEGACY_RAW_TABLE} ORDER BY rowid"""
    cursor = conn.execute(sql)
    chunks: list[dict[str, Any]] = []
    full_identity = hashlib.sha256()
    full_payload = hashlib.sha256()
    count = 0
    chunk_count = 0
    chunk_first_rowid = 0
    chunk_last_rowid = 0
    chunk_identity = hashlib.sha256()
    chunk_payload = hashlib.sha256()

    def flush() -> None:
        nonlocal chunk_count, chunk_first_rowid, chunk_last_rowid
        nonlocal chunk_identity, chunk_payload
        if chunk_count == 0:
            return
        identity_digest = "sha256:" + chunk_identity.hexdigest()
        payload_digest = "sha256:" + chunk_payload.hexdigest()
        chunks.append(
            {
                "chunk_id": f"raw-{len(chunks) + 1:06d}",
                "first_rowid": chunk_first_rowid,
                "last_rowid": chunk_last_rowid,
                "row_count": chunk_count,
                "logical_digest": identity_digest,
                "raw_json_digest": payload_digest,
                "verification_digest": _digest(
                    {
                        "logical_digest": identity_digest,
                        "raw_json_digest": payload_digest,
                    }
                ),
                "status": "planned",
            }
        )
        chunk_count = 0
        chunk_first_rowid = 0
        chunk_last_rowid = 0
        chunk_identity = hashlib.sha256()
        chunk_payload = hashlib.sha256()

    for row in cursor:
        identity = [
            str(row["seller_id"]),
            str(row["report_id"]),
            str(row["rrd_id"]),
            str(row["row_hash"]),
        ]
        identity_bytes = (_canonical_json(identity) + "\n").encode("utf-8")
        raw_json_bytes = str(row["raw_json"]).encode("utf-8")
        payload_bytes = len(raw_json_bytes).to_bytes(8, "big") + raw_json_bytes
        if chunk_count == 0:
            chunk_first_rowid = int(row["rowid"])
        chunk_last_rowid = int(row["rowid"])
        chunk_identity.update(identity_bytes)
        full_identity.update(identity_bytes)
        chunk_payload.update(payload_bytes)
        full_payload.update(payload_bytes)
        chunk_count += 1
        count += 1
        if chunk_count >= chunk_size:
            flush()
    flush()
    watermarks = dict(
        conn.execute(
            f"""SELECT MIN(week_start) AS min_week_start,MAX(week_end) AS max_week_end,
                       MIN(CAST(rrd_id AS INTEGER)) AS min_rrd_id,
                       MAX(CAST(rrd_id AS INTEGER)) AS max_rrd_id,
                       MIN(first_seen_at) AS min_first_seen_at,
                       MAX(updated_at) AS max_updated_at
                FROM {LEGACY_RAW_TABLE}"""
        ).fetchone()
    )
    return (
        chunks,
        LogicalDigest(
            row_count=count,
            digest="sha256:" + full_identity.hexdigest(),
            payload_digest="sha256:" + full_payload.hexdigest(),
        ),
        watermarks,
    )


def _candidate_raw_digest(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    first_sequence: int | None = None,
    last_sequence: int | None = None,
) -> LogicalDigest:
    conditions = ["batch_id=?"]
    params: list[Any] = [batch_id]
    if first_sequence is not None:
        conditions.append("batch_sequence_no>=?")
        params.append(int(first_sequence))
    if last_sequence is not None:
        conditions.append("batch_sequence_no<=?")
        params.append(int(last_sequence))
    digest = hashlib.sha256()
    payload_digest = hashlib.sha256()
    count = 0
    for row in conn.execute(
        f"""SELECT seller_id,report_id,rrd_id,row_hash,raw_json
            FROM finance_raw_rows
            WHERE {' AND '.join(conditions)}
            ORDER BY batch_sequence_no""",
        tuple(params),
    ):
        identity = [
            str(row["seller_id"]),
            str(row["report_id"]),
            str(row["rrd_id"]),
            str(row["row_hash"]),
        ]
        digest.update((_canonical_json(identity) + "\n").encode("utf-8"))
        raw_json_bytes = str(row["raw_json"]).encode("utf-8")
        payload_digest.update(
            len(raw_json_bytes).to_bytes(8, "big") + raw_json_bytes
        )
        count += 1
    return LogicalDigest(
        row_count=count,
        digest="sha256:" + digest.hexdigest(),
        payload_digest="sha256:" + payload_digest.hexdigest(),
    )


def _accessible_fd_paths(fd_dir: Any) -> list[Path]:
    try:
        return list(fd_dir.iterdir())
    except OSError:
        # Containerized CI and hardened hosts can expose the PID directory
        # while denying another process' fd directory. Those inaccessible
        # entries are not evidence of an opener and must not make the
        # query-only planner unavailable.
        return []


def _process_openers(source: Path) -> list[dict[str, Any]]:
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    source_stat = source.stat()
    openers: list[dict[str, Any]] = []
    for pid_dir in sorted(
        (item for item in proc.iterdir() if item.name.isdigit()),
        key=lambda item: int(item.name),
    ):
        fd_dir = pid_dir / "fd"
        if not fd_dir.is_dir():
            continue
        for fd_path in _accessible_fd_paths(fd_dir):
            try:
                target = fd_path.resolve(strict=True)
                target_stat = target.stat()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if (
                target_stat.st_dev != source_stat.st_dev
                or target_stat.st_ino != source_stat.st_ino
            ):
                continue
            flags: int | None = None
            try:
                for line in (pid_dir / "fdinfo" / fd_path.name).read_text().splitlines():
                    if line.startswith("flags:"):
                        flags = int(line.split(":", 1)[1].strip(), 8)
                        break
            except (OSError, ValueError):
                pass
            try:
                comm = (pid_dir / "comm").read_text().strip()[:120]
            except OSError:
                comm = ""
            openers.append(
                {
                    "pid": int(pid_dir.name),
                    "fd": int(fd_path.name),
                    "access_mode": (
                        {0: "read_only", 1: "write_only", 2: "read_write"}.get(
                            flags & os.O_ACCMODE,
                            "unknown",
                        )
                        if flags is not None
                        else "unknown"
                    ),
                    "comm": comm,
                }
            )
    return openers


def _systemd_inventory() -> list[dict[str, Any]]:
    if shutil.which("systemctl") is None:
        return [{"status": "unavailable", "reason": "systemctl_not_found"}]
    result: list[dict[str, Any]] = []
    for unit in _SYSTEMD_UNITS:
        completed = subprocess.run(
            [
                "systemctl",
                "show",
                unit,
                "--property=Id,LoadState,ActiveState,SubState,UnitFileState,MainPID,Result,ExecMainStatus,LastTriggerUSec,NextElapseUSecRealtime",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        values: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        result.append(
            {
                "unit": unit,
                "return_code": completed.returncode,
                "load_state": values.get("LoadState", ""),
                "active_state": values.get("ActiveState", ""),
                "sub_state": values.get("SubState", ""),
                "unit_file_state": values.get("UnitFileState", ""),
                "main_pid": int(values.get("MainPID") or 0),
                "result": values.get("Result", ""),
                "exec_main_status": values.get("ExecMainStatus", ""),
                "last_trigger": values.get("LastTriggerUSec", ""),
                "next_trigger": values.get("NextElapseUSecRealtime", ""),
            }
        )
    return result


def _unknown_snapshot_writers(
    openers: Sequence[Mapping[str, Any]],
    systemd_units: Sequence[Mapping[str, Any]],
    *,
    hold_confirmed: bool,
) -> list[dict[str, Any]]:
    """Classify writable monolith openers against exact systemd ownership."""

    known_unit_by_pid = {
        int(item.get("main_pid") or 0): str(item.get("unit") or "")
        for item in systemd_units
        if int(item.get("main_pid") or 0) > 0
    }
    current_pid = os.getpid()
    unknown: list[dict[str, Any]] = []
    for raw in openers:
        item = dict(raw)
        access_mode = str(item.get("access_mode") or "")
        if access_mode == "read_only":
            continue
        pid = int(item.get("pid") or 0)
        owning_unit = known_unit_by_pid.get(pid, "")
        item["owning_unit"] = owning_unit
        if pid == current_pid and access_mode == "read_only":
            continue
        if not hold_confirmed and owning_unit:
            continue
        if (
            hold_confirmed
            and owning_unit == "wb-core-registry-http.service"
            and access_mode == "read_write"
        ):
            item["write_guard"] = "active_http_write_barrier"
            continue
        unknown.append(item)
    return unknown


def _source_identity(path: Path, conn: sqlite3.Connection) -> dict[str, Any]:
    stat = path.stat()
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    schema = _schema_inventory(conn)
    return {
        "path": str(path),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist,
        "allocated_page_bytes": page_size * page_count,
        "schema_digest": _digest(schema),
        "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
        "query_only": int(conn.execute("PRAGMA query_only").fetchone()[0]),
    }


def _destination_path_identity(path: Path) -> dict[str, Any]:
    target = Path(path)
    payload: dict[str, Any] = {
        "path": str(target),
        "exists": target.exists(),
        "is_file": target.is_file(),
        "sidecars": [],
    }
    if target.exists():
        stat = target.stat()
        payload.update(
            {
                "device": int(stat.st_dev),
                "inode": int(stat.st_ino),
                "size_bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            stat = sidecar.stat()
            payload["sidecars"].append(
                {
                    "path": str(sidecar),
                    "size_bytes": int(stat.st_size),
                    "inode": int(stat.st_ino),
                }
            )
    return payload


def _capacity_plan(
    *,
    runtime_dir: Path,
    raw_allocated_bytes: int,
    raw_index_bytes: int,
    non_raw_allocated_bytes: int,
) -> dict[str, Any]:
    vfs = os.statvfs(runtime_dir)
    free_bytes = int(vfs.f_bavail * vfs.f_frsize)
    projected_raw_bytes = math.ceil(raw_allocated_bytes * 1.08)
    projected_operational_bytes = math.ceil(non_raw_allocated_bytes * 1.10)
    index_build_overhead_bytes = max(
        512 * 1024 * 1024,
        math.ceil(raw_index_bytes * 1.25),
    )
    verification_scratch_bytes = max(_GIB, math.ceil(raw_allocated_bytes * 0.12))
    operational_reserve_bytes = max(2 * _GIB, math.ceil(projected_operational_bytes * 0.50))
    required_bytes = (
        projected_raw_bytes
        + projected_operational_bytes
        + index_build_overhead_bytes
        + verification_scratch_bytes
        + operational_reserve_bytes
    )
    return {
        "filesystem_device": int(runtime_dir.stat().st_dev),
        "filesystem_block_size": int(vfs.f_frsize),
        "available_bytes": free_bytes,
        "projected_raw_destination_bytes": projected_raw_bytes,
        "projected_operational_destination_bytes": projected_operational_bytes,
        "index_build_overhead_bytes": index_build_overhead_bytes,
        "verification_scratch_bytes": verification_scratch_bytes,
        "operational_reserve_bytes": operational_reserve_bytes,
        "required_bytes": required_bytes,
        "shortfall_bytes": max(0, required_bytes - free_bytes),
        "remaining_bytes_after_reservation": max(0, free_bytes - required_bytes),
        "sufficient": free_bytes >= required_bytes,
        "checked_before_destination_creation": True,
    }


def _direct_open_inventory(repo_root: Path | None) -> dict[str, Any]:
    if repo_root is None:
        return {"status": "unavailable", "reason": "repo_root_not_supplied"}
    try:
        from apps.finance_storage_sqlite_open_inventory import inventory

        return inventory(repo_root)
    except Exception as exc:
        return {
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {str(exc)[:300]}",
    }


def _snapshot_plan_fingerprint(plan: Mapping[str, Any]) -> str:
    stable = json.loads(_canonical_json(plan))
    stable.pop("fingerprint", None)
    stable.pop("created_at", None)
    capacity = stable.get("capacity", {})
    for key in (
        "available_bytes",
        "shortfall_bytes",
        "remaining_bytes_after_snapshot",
        "sufficient",
    ):
        capacity.pop(key, None)
    writers = stable.get("writers_and_timers", {})
    writers["database_openers"] = sorted(
        [
            {
                "access_mode": str(item.get("access_mode") or ""),
                "comm": str(item.get("comm") or ""),
            }
            for item in writers.get("database_openers", [])
        ],
        key=lambda item: (item["comm"], item["access_mode"]),
    )
    for item in writers.get("systemd_units", []):
        for key in ("main_pid", "last_trigger", "next_trigger"):
            item.pop(key, None)
    return _digest(stable)


def _load_private_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FinanceStorageMigrationError(f"{label} is missing: {path}")
    if path.stat().st_mode & 0o077:
        raise FinanceStorageMigrationError(f"{label} must be private mode 0600")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinanceStorageMigrationError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise FinanceStorageMigrationError(f"{label} is not a JSON object")
    return payload


class FinanceStorageCoherentSnapshot:
    """Create and verify one immutable migration source under a short hold."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        deployed_sha: str,
        repo_root: Path | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.registry = StoreRegistry(self.runtime_dir)
        self.deployed_sha = str(deployed_sha or "").strip()
        self.repo_root = Path(repo_root).resolve() if repo_root else None

    def build_plan(self) -> dict[str, Any]:
        manifest = self.registry.load()
        if manifest.state != "monolith" or manifest.canonical_source != "monolith":
            raise FinanceStorageMigrationError(
                "coherent snapshot requires the canonical monolith generation"
            )
        source = self.registry.resolve("operational", manifest=manifest)
        with self.registry.session(
            "operational",
            mode="ro",
            operation="finance_storage_snapshot_plan",
            timeout_ms=30_000,
            isolation_level=None,
        ) as conn:
            conn.execute("BEGIN")
            source_identity = _source_identity(source, conn)
            conn.rollback()
        source_bytes = max(
            int(source_identity["size_bytes"]),
            int(source_identity["allocated_page_bytes"]),
        )
        vfs = os.statvfs(self.runtime_dir)
        free_bytes = int(vfs.f_bavail * vfs.f_frsize)
        reserve_bytes = max(2 * _GIB, math.ceil(source_bytes * 0.10))
        required_bytes = source_bytes + reserve_bytes
        identity_fingerprint = _digest(
            {
                "deployed_sha": self.deployed_sha,
                "source_identity": source_identity,
            }
        )
        epoch = identity_fingerprint.removeprefix("sha256:")[:20]
        snapshot_id = f"finance-split-{epoch}"
        snapshot_root = (
            self.runtime_dir / "finance-storage-split-snapshots" / snapshot_id
        ).resolve()
        database_path = snapshot_root / "monolith.sqlite3"
        manifest_path = snapshot_root / "snapshot_manifest.json"
        openers = _process_openers(source)
        systemd_units = _systemd_inventory()
        unknown_openers = [
            item
            for item in openers
            if str(item.get("access_mode") or "") == "unknown"
        ]
        unknown_writers = _unknown_snapshot_writers(
            openers,
            systemd_units,
            hold_confirmed=False,
        )
        active_business_services = [
            dict(item)
            for item in systemd_units
            if str(item.get("unit") or "").endswith(".service")
            and str(item.get("unit") or "")
            != "wb-core-registry-http.service"
            and str(item.get("active_state") or "")
            not in {"inactive", "failed"}
        ]
        blockers: list[dict[str, Any]] = []
        if re.fullmatch(r"[0-9a-f]{40}", self.deployed_sha) is None:
            blockers.append(
                {
                    "code": "deployed_sha_unavailable",
                    "detail": "exact 40-hex deployed SHA is required",
                }
            )
        if not bool(free_bytes >= required_bytes):
            blockers.append(
                {
                    "code": "snapshot_capacity_shortfall",
                    "required_bytes": required_bytes,
                    "available_bytes": free_bytes,
                }
            )
        if unknown_openers:
            blockers.append(
                {
                    "code": "unknown_database_opener",
                    "openers": unknown_openers,
                }
            )
        if unknown_writers:
            blockers.append(
                {
                    "code": "unknown_database_writer",
                    "openers": unknown_writers,
                }
            )
        if active_business_services:
            blockers.append(
                {
                    "code": "active_business_writer_service",
                    "services": active_business_services,
                }
            )
        direct_inventory = _direct_open_inventory(self.repo_root)
        if direct_inventory.get("status") != "ok":
            blockers.append(
                {
                    "code": "direct_sqlite_open_inventory_blocked",
                    "detail": str(
                        direct_inventory.get("reason")
                        or direct_inventory.get("violations")
                        or direct_inventory.get("parse_errors")
                        or "inventory unavailable"
                    )[:1000],
                }
            )
        if snapshot_root.exists() and not manifest_path.is_file():
            blockers.append(
                {
                    "code": "snapshot_path_collision",
                    "path": str(snapshot_root),
                }
            )
        low_seconds = max(
            15,
            math.ceil(source_bytes / (200 * 1024 * 1024)) + 15,
        )
        high_seconds = max(
            60,
            math.ceil(source_bytes / (60 * 1024 * 1024)) + 60,
        )
        plan: dict[str, Any] = {
            "contract_version": SNAPSHOT_PLAN_CONTRACT,
            "mode": "snapshot_dry_run",
            "deployed_sha": self.deployed_sha,
            "source": {
                "logical_store": "monolith",
                "identity": source_identity,
                "sidecars": _destination_path_identity(source)["sidecars"],
                "identity_fingerprint": identity_fingerprint,
            },
            "target_snapshot": {
                "snapshot_id": snapshot_id,
                "window_id": f"snapshot-{epoch}",
                "snapshot_root": str(snapshot_root),
                "database_path": str(database_path),
                "manifest_path": str(manifest_path),
                "exists": snapshot_root.exists(),
            },
            "capacity": {
                "available_bytes": free_bytes,
                "snapshot_bytes": source_bytes,
                "post_snapshot_reserve_bytes": reserve_bytes,
                "required_bytes": required_bytes,
                "shortfall_bytes": max(0, required_bytes - free_bytes),
                "remaining_bytes_after_snapshot": max(
                    0,
                    free_bytes - source_bytes,
                ),
                "sufficient": free_bytes >= required_bytes,
            },
            "writers_and_timers": {
                "database_openers": openers,
                "systemd_units": systemd_units,
                "unknown_database_openers": unknown_openers,
                "unknown_database_writers": unknown_writers,
                "active_business_services": active_business_services,
                "hold_contract": [
                    "manual HTTP/API write barrier active before drain",
                    "business-data maintenance exact hold confirmed",
                    "warehouse and Finance writers quiescent",
                    "unrelated services remain available for reads",
                ],
            },
            "direct_sqlite_open_inventory": direct_inventory,
            "integrity_gate": {
                "live_full_scan_allowed": False,
                "coherent_copy_required": True,
                "snapshot_full_integrity_check_required": True,
                "candidate_build_allowed_before_integrity": False,
                "incident_window_utc": (
                    "2026-07-27T14:29:50Z/2026-07-27T14:31:18Z"
                ),
            },
            "critical_window": {
                "estimated_seconds_low": low_seconds,
                "estimated_seconds_high": high_seconds,
                "reads_available": True,
                "manual_business_writes_blocked": True,
                "automatic_writers_drained": True,
                "full_integrity_runs_after_restore": True,
            },
            "blockers": blockers,
            "snapshot_allowed_by_machine_preflight": not blockers,
            "production_business_mutation_count": 0,
            "human_candidate_backfill_approval_required_after_integrity": True,
        }
        plan["fingerprint"] = _snapshot_plan_fingerprint(plan)
        plan["created_at"] = _utc_now()
        return plan

    def _hold_evidence(
        self,
        *,
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        barrier = barrier_status(self.runtime_dir)
        maintenance = _load_private_json(
            self.runtime_dir / ".business-data-maintenance.json",
            label="business-data maintenance state",
        )
        if (
            barrier.get("active") is not True
            or str(barrier.get("phase") or "") != "held"
            or barrier.get("hold_confirmed") is not True
            or str(barrier.get("window_id") or "")
            != str(plan["target_snapshot"]["window_id"])
        ):
            raise FinanceStorageMigrationError(
                "exact confirmed HTTP write barrier is required for snapshot"
            )
        if (
            str(maintenance.get("schema_version") or "")
            != "business_data_maintenance_v1"
            or str(maintenance.get("phase") or "") != "held"
            or not bool((maintenance.get("hold_readback") or {}).get("quiet"))
        ):
            raise FinanceStorageMigrationError(
                "exact quiet writer/timer hold is required for snapshot"
            )
        return {
            "barrier": barrier,
            "maintenance_state_fingerprint": _digest(maintenance),
            "maintenance_held_at": str(maintenance.get("held_at") or ""),
        }

    def create(
        self,
        *,
        reviewed_plan: Mapping[str, Any],
        expected_fingerprint: str,
        approval_reference: str,
    ) -> dict[str, Any]:
        if (
            str(reviewed_plan.get("contract_version") or "")
            != SNAPSHOT_PLAN_CONTRACT
            or str(reviewed_plan.get("mode") or "") != "snapshot_dry_run"
            or str(reviewed_plan.get("fingerprint") or "")
            != str(expected_fingerprint or "")
            or not bool(
                reviewed_plan.get("snapshot_allowed_by_machine_preflight")
            )
        ):
            raise FinanceStorageMigrationError(
                "reviewed coherent snapshot plan is invalid or blocked"
            )
        if not str(approval_reference or "").strip():
            raise FinanceStorageMigrationError(
                "audited snapshot authorization reference is required"
            )
        current_plan = dict(reviewed_plan)
        hold_evidence = self._hold_evidence(plan=current_plan)
        target = current_plan["target_snapshot"]
        snapshot_root = Path(str(target["snapshot_root"])).resolve()
        database_path = Path(str(target["database_path"])).resolve()
        manifest_path = Path(str(target["manifest_path"])).resolve()
        if manifest_path.is_file():
            existing = _load_private_json(
                manifest_path,
                label="coherent snapshot manifest",
            )
            if (
                str(existing.get("snapshot_plan_fingerprint") or "")
                == str(expected_fingerprint)
                and Path(str(existing.get("database_path") or "")).is_file()
            ):
                return {
                    "contract_version": SNAPSHOT_CONTRACT,
                    "status": str(existing.get("status") or "captured_unverified"),
                    "idempotent": True,
                    "snapshot_manifest_path": str(manifest_path),
                    "snapshot": existing,
                }
            raise FinanceStorageMigrationError(
                "existing snapshot manifest does not match reviewed plan"
            )
        fresh_vfs = os.statvfs(self.runtime_dir)
        fresh_free = int(fresh_vfs.f_bavail * fresh_vfs.f_frsize)
        if fresh_free < int(current_plan["capacity"]["required_bytes"]):
            raise FinanceStorageMigrationError(
                "snapshot capacity raced and is now insufficient"
            )
        snapshot_root.mkdir(parents=True, exist_ok=True)
        source_path = Path(
            str(current_plan["source"]["identity"]["path"])
        ).resolve()
        temporary = snapshot_root / ".monolith.sqlite3.partial"
        if temporary.exists() or database_path.exists():
            raise FinanceStorageMigrationError(
                "snapshot destination already exists without matching manifest"
            )
        source = sqlite3.connect(
            f"file:{source_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        destination = sqlite3.connect(
            temporary,
            timeout=60,
            isolation_level=None,
        )
        started = time.monotonic()
        try:
            source.row_factory = sqlite3.Row
            destination.row_factory = sqlite3.Row
            source.execute("PRAGMA query_only=ON")
            source_before = _source_identity(source_path, source)
            if source_before != current_plan["source"]["identity"]:
                raise FinanceStorageMigrationError(
                    "source identity drifted before coherent snapshot"
                )
            fresh_openers = _process_openers(source_path)
            fresh_systemd = _systemd_inventory()
            unknown_writers = _unknown_snapshot_writers(
                fresh_openers,
                fresh_systemd,
                hold_confirmed=True,
            )
            if unknown_writers:
                raise FinanceStorageMigrationError(
                    "unknown or undrained database writer appeared before "
                    f"coherent snapshot: {unknown_writers}"
                )
            source.backup(destination, pages=16_384, sleep=0.01)
            destination.commit()
            destination_identity = _source_identity(temporary, destination)
            source_after = _source_identity(source_path, source)
            if source_after != source_before:
                raise FinanceStorageMigrationError(
                    "source identity changed during coherent snapshot hold"
                )
            if (
                int(destination_identity["page_count"])
                != int(source_before["page_count"])
                or int(destination_identity["page_size"])
                != int(source_before["page_size"])
                or str(destination_identity["schema_digest"])
                != str(source_before["schema_digest"])
            ):
                raise FinanceStorageMigrationError(
                    "coherent snapshot structural readback mismatch"
                )
        finally:
            destination.close()
            source.close()
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, database_path)
        snapshot_manifest: dict[str, Any] = {
            "contract_version": SNAPSHOT_CONTRACT,
            "status": "captured_unverified",
            "snapshot_id": str(target["snapshot_id"]),
            "snapshot_plan_fingerprint": str(expected_fingerprint),
            "approval_reference": str(approval_reference).strip(),
            "deployed_sha": self.deployed_sha,
            "database_path": str(database_path),
            "source_identity": current_plan["source"]["identity"],
            "snapshot_identity": _destination_path_identity(database_path),
            "hold_evidence": hold_evidence,
            "captured_at": _utc_now(),
            "capture_duration_seconds": round(time.monotonic() - started, 3),
            "full_integrity_check": {
                "status": "pending",
                "runs_outside_maintenance_hold": True,
            },
            "candidate_build_allowed": False,
        }
        snapshot_manifest["evidence_fingerprint"] = _digest(snapshot_manifest)
        _atomic_write_json(manifest_path, snapshot_manifest)
        return {
            "contract_version": SNAPSHOT_CONTRACT,
            "status": "captured_unverified",
            "idempotent": False,
            "snapshot_manifest_path": str(manifest_path),
            "snapshot": snapshot_manifest,
        }

    def verify_integrity(self, manifest_path: Path) -> dict[str, Any]:
        path = Path(manifest_path).expanduser().resolve()
        manifest = _load_private_json(
            path,
            label="coherent snapshot manifest",
        )
        if (
            str(manifest.get("contract_version") or "") != SNAPSHOT_CONTRACT
            or str(manifest.get("deployed_sha") or "") != self.deployed_sha
        ):
            raise FinanceStorageMigrationError(
                "coherent snapshot manifest identity is invalid"
            )
        database_path = Path(str(manifest.get("database_path") or "")).resolve()
        database_path.relative_to(self.runtime_dir)
        if (
            manifest.get("status") == "integrity_verified"
            and manifest.get("candidate_build_allowed") is True
            and manifest.get("snapshot_identity")
            == _destination_path_identity(database_path)
        ):
            return {
                "contract_version": SNAPSHOT_CONTRACT,
                "status": "integrity_verified",
                "idempotent": True,
                "snapshot_manifest_path": str(path),
                "snapshot": manifest,
            }
        if not database_path.is_file():
            raise FinanceStorageMigrationError(
                "coherent snapshot database is missing"
            )
        if (
            manifest.get("snapshot_identity")
            != _destination_path_identity(database_path)
        ):
            raise FinanceStorageMigrationError(
                "coherent snapshot identity drifted before integrity check"
            )
        started = time.monotonic()
        conn = sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            integrity_rows = [
                str(row[0])
                for row in conn.execute("PRAGMA integrity_check").fetchall()
            ]
            foreign_key_rows = [
                tuple(row)
                for row in conn.execute("PRAGMA foreign_key_check").fetchmany(101)
            ]
            identity = _source_identity(database_path, conn)
        finally:
            conn.close()
        if integrity_rows != ["ok"]:
            raise FinanceStorageMigrationError(
                "coherent snapshot full integrity_check failed: "
                + "; ".join(integrity_rows[:20])
            )
        if foreign_key_rows:
            raise FinanceStorageMigrationError(
                "coherent snapshot foreign_key_check found violations"
            )
        updated = dict(manifest)
        updated.update(
            {
                "status": "integrity_verified",
                "snapshot_identity": _destination_path_identity(database_path),
                "sqlite_identity": identity,
                "full_integrity_check": {
                    "status": "ok",
                    "pragma": "integrity_check",
                    "result_rows": integrity_rows,
                    "foreign_key_check_rows": 0,
                    "query_only": True,
                    "completed_at": _utc_now(),
                    "duration_seconds": round(
                        time.monotonic() - started,
                        3,
                    ),
                    "runs_outside_maintenance_hold": True,
                },
                "candidate_build_allowed": True,
            }
        )
        updated["evidence_fingerprint"] = _digest(
            {
                key: value
                for key, value in updated.items()
                if key != "evidence_fingerprint"
            }
        )
        _atomic_write_json(path, updated)
        return {
            "contract_version": SNAPSHOT_CONTRACT,
            "status": "integrity_verified",
            "idempotent": False,
            "snapshot_manifest_path": str(path),
            "snapshot": updated,
        }


class FinanceStorageMigrationPlanner:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        chunk_size: int = 10_000,
        deployed_sha: str = "",
        repo_root: Path | None = None,
        require_exact_allocations: bool = True,
        source_snapshot_manifest: Path | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.registry = StoreRegistry(self.runtime_dir)
        self.chunk_size = int(chunk_size)
        self.deployed_sha = str(deployed_sha or "").strip()
        self.repo_root = Path(repo_root).resolve() if repo_root else None
        self.require_exact_allocations = bool(require_exact_allocations)
        self.source_snapshot_manifest = (
            Path(source_snapshot_manifest).expanduser().resolve()
            if source_snapshot_manifest is not None
            else None
        )
        if self.chunk_size <= 0 or self.chunk_size > 500_000:
            raise FinanceStorageMigrationError("chunk_size must be within 1..500000")

    @contextmanager
    def _source_session(
        self,
        source: Path,
        *,
        use_registry: bool,
    ) -> Iterator[sqlite3.Connection]:
        if use_registry:
            with self.registry.session(
                "operational",
                mode="ro",
                operation="finance_storage_split_dry_run",
                timeout_ms=60_000,
                isolation_level=None,
            ) as conn:
                yield conn
            return
        conn = sqlite3.connect(
            f"file:{Path(source).resolve()}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise FinanceStorageMigrationError(
                    "coherent snapshot query_only could not be enabled"
                )
            yield conn
        finally:
            conn.close()

    def build_plan(self) -> dict[str, Any]:
        plan_started = time.monotonic()
        manifest = self.registry.load()
        if manifest.state != "monolith" or manifest.canonical_source != "monolith":
            raise FinanceStorageMigrationError("dry-run requires the canonical monolith generation")
        live_source = self.registry.resolve("operational", manifest=manifest)
        snapshot_evidence: dict[str, Any] | None = None
        if self.source_snapshot_manifest is not None:
            snapshot_evidence = _load_private_json(
                self.source_snapshot_manifest,
                label="coherent snapshot manifest",
            )
            source = Path(
                str(snapshot_evidence.get("database_path") or "")
            ).resolve()
            source.relative_to(self.runtime_dir)
            if (
                str(snapshot_evidence.get("contract_version") or "")
                != SNAPSHOT_CONTRACT
                or str(snapshot_evidence.get("status") or "")
                != "integrity_verified"
                or snapshot_evidence.get("candidate_build_allowed") is not True
                or not source.is_file()
                or snapshot_evidence.get("snapshot_identity")
                != _destination_path_identity(source)
            ):
                raise FinanceStorageMigrationError(
                    "verified immutable coherent snapshot is required"
                )
        else:
            source = live_source
        before_stat = source.stat()
        with self._source_session(
            source,
            use_registry=snapshot_evidence is None,
        ) as conn:
            conn.execute("BEGIN")
            source_identity = _source_identity(source, conn)
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if LEGACY_RAW_TABLE not in tables:
                raise FinanceStorageMigrationError(f"required source table is missing: {LEGACY_RAW_TABLE}")
            allocations, allocation_evidence = _dbstat_allocations(conn)
            raw_scan_started = time.monotonic()
            chunks, raw_digest, watermarks = _raw_chunk_manifest(
                conn, chunk_size=self.chunk_size
            )
            raw_scan_ms = round(
                (time.monotonic() - raw_scan_started) * 1000,
                3,
            )
            representative = conn.execute(
                f"""SELECT seller_id,report_id,rrd_id,week_start,nm_id
                    FROM {LEGACY_RAW_TABLE} ORDER BY rowid LIMIT 1"""
            ).fetchone()
            query_plans: dict[str, list[str]] = {
                "identity_scan": explain_query_plan(
                    conn,
                    f"""SELECT seller_id,report_id,rrd_id,row_hash
                        FROM {LEGACY_RAW_TABLE} ORDER BY rowid""",
                )
            }
            if representative is not None:
                query_plans["primary_identity_lookup"] = explain_query_plan(
                    conn,
                    f"""SELECT rowid FROM {LEGACY_RAW_TABLE}
                        WHERE seller_id=? AND report_id=? AND rrd_id=?""",
                    (
                        str(representative["seller_id"]),
                        str(representative["report_id"]),
                        str(representative["rrd_id"]),
                    ),
                )
                query_plans["week_lookup"] = explain_query_plan(
                    conn,
                    f"""SELECT rowid FROM {LEGACY_RAW_TABLE}
                        WHERE seller_id=? AND week_start=? ORDER BY rowid LIMIT 100""",
                    (
                        str(representative["seller_id"]),
                        str(representative["week_start"]),
                    ),
                )
                query_plans["sku_week_lookup"] = explain_query_plan(
                    conn,
                    f"""SELECT rowid FROM {LEGACY_RAW_TABLE}
                        WHERE seller_id=? AND nm_id=? AND week_start=?
                        ORDER BY rowid LIMIT 100""",
                    (
                        str(representative["seller_id"]),
                        str(representative["nm_id"] or ""),
                        str(representative["week_start"]),
                    ),
                )
            table_digest_started = time.monotonic()
            table_matrix = _table_matrix(
                conn,
                allocations=allocations,
                logical_overrides={LEGACY_RAW_TABLE: raw_digest},
            )
            table_digest_ms = round(
                (time.monotonic() - table_digest_started) * 1000,
                3,
            )
            conn.rollback()
        after_stat = source.stat()
        source_file_drift_observed = (
            before_stat.st_dev,
            before_stat.st_ino,
            before_stat.st_size,
            before_stat.st_mtime_ns,
        ) != (
            after_stat.st_dev,
            after_stat.st_ino,
            after_stat.st_size,
            after_stat.st_mtime_ns,
        )
        raw_allocated_bytes = sum(
            int(value)
            for name, value in allocations.items()
            if name in RAW_LEGACY_OBJECTS
        )
        raw_index_bytes = sum(
            int(value)
            for name, value in allocations.items()
            if name in RAW_LEGACY_OBJECTS and name != LEGACY_RAW_TABLE
        )
        total_allocated = sum(int(value) for value in allocations.values())
        non_raw_allocated = max(0, total_allocated - raw_allocated_bytes)
        capacity = _capacity_plan(
            runtime_dir=self.runtime_dir,
            raw_allocated_bytes=raw_allocated_bytes,
            raw_index_bytes=raw_index_bytes,
            non_raw_allocated_bytes=non_raw_allocated,
        )
        source_fingerprint = _digest(
            {
                "source_identity": source_identity,
                "raw": {
                    "row_count": raw_digest.row_count,
                    "logical_digest": raw_digest.digest,
                    "raw_json_digest": raw_digest.payload_digest,
                    "watermarks": watermarks,
                },
                "tables": [
                    {
                        "table": item["table"],
                        "row_count": item["row_count"],
                        "logical_digest": item["logical_digest"],
                    }
                    for item in table_matrix
                ],
            }
        )
        epoch = source_fingerprint.removeprefix("sha256:")[:20]
        raw_generation_id = f"finance-raw-{epoch}"
        operational_generation_id = f"operational-{epoch}"
        generation_dir = f"generations/{epoch}"
        candidate_manifest = build_manifest(
            state="shadow",
            canonical_source="monolith",
            generation_epoch=epoch,
            raw_generation_id=raw_generation_id,
            raw_relative_path=f"{generation_dir}/finance_raw.sqlite3",
            raw_watermark=str(watermarks.get("max_rrd_id") or ""),
            operational_generation_id=operational_generation_id,
            operational_relative_path=f"{generation_dir}/operational.sqlite3",
            operational_watermark=source_fingerprint,
            rollback_generation_id="monolith",
            source_fingerprint=source_fingerprint,
            created_at=str(watermarks.get("max_updated_at") or ""),
        )
        candidate_root = (self.runtime_dir / generation_dir).resolve()
        raw_candidate_path = (
            self.runtime_dir / candidate_manifest.raw.relative_path
        ).resolve()
        operational_candidate_path = (
            self.runtime_dir / candidate_manifest.operational.relative_path
        ).resolve()
        destination_preflight = {
            "generation_root": {
                "path": str(candidate_root),
                "exists": candidate_root.exists(),
                "is_directory": candidate_root.is_dir(),
            },
            "raw": _destination_path_identity(raw_candidate_path),
            "operational": _destination_path_identity(operational_candidate_path),
            "candidate_manifest": _destination_path_identity(
                candidate_root / "candidate_generation_manifest.json"
            ),
            "saved_plan": _destination_path_identity(
                candidate_root / "migration_plan.json"
            ),
        }
        direct_open_inventory = _direct_open_inventory(self.repo_root)
        blockers: list[dict[str, Any]] = []
        if re.fullmatch(r"[0-9a-f]{40}", self.deployed_sha) is None:
            blockers.append(
                {
                    "code": "deployed_sha_unavailable",
                    "detail": "exact 40-hex deployed SHA is required",
                }
            )
        if (
            self.require_exact_allocations
            and not allocation_evidence["exact_per_object"]
        ):
            blockers.append(
                {
                    "code": "exact_dbstat_allocation_unavailable",
                    "detail": str(allocation_evidence.get("reason") or ""),
                }
            )
        if not capacity["sufficient"]:
            blockers.append(
                {
                    "code": "capacity_shortfall",
                    "required_bytes": capacity["required_bytes"],
                    "available_bytes": capacity["available_bytes"],
                    "shortfall_bytes": capacity["shortfall_bytes"],
                }
            )
        if candidate_root.exists() and not (
            candidate_root.is_dir()
            and (candidate_root / "migration_plan.json").is_file()
        ):
            blockers.append(
                {
                    "code": "target_generation_path_collision",
                    "path": str(candidate_root),
                    "detail": "existing target generation lacks a resumable saved plan",
                }
            )
        if direct_open_inventory.get("status") != "ok":
            blockers.append(
                {
                    "code": "direct_sqlite_open_inventory_blocked",
                    "detail": str(
                        direct_open_inventory.get("reason")
                        or direct_open_inventory.get("violations")
                        or direct_open_inventory.get("parse_errors")
                        or "inventory unavailable"
                    )[:1000],
                }
            )
        writer_inventory = {
            "database_openers": _process_openers(source),
            "systemd_units": _systemd_inventory(),
            "lock_plan": [
                "business-data-maintenance exact writer inventory",
                "warehouse-functional shared lock",
                "Finance ingestion timer/service exact prior-state hold",
                "no unrelated service stop",
            ],
        }
        plan: dict[str, Any] = {
            "contract_version": PLAN_CONTRACT,
            "mode": "dry_run",
            "query_only_contract": {
                "sqlite_uri_mode": "ro",
                "pragma_query_only": source_identity["query_only"],
                "transaction": "explicit_read_transaction_rolled_back",
                "production_mutation_count": 0,
                "destination_bytes_created": 0,
                "coherent_sqlite_read_transaction": True,
                "external_source_file_activity_observed": source_file_drift_observed,
            },
            "deployed_sha": self.deployed_sha,
            "schema_revisions": {
                "finance_raw": candidate_manifest.raw.schema_revision,
                "operational": candidate_manifest.operational.schema_revision,
                "source_schema_digest": source_identity["schema_digest"],
            },
            "source": {
                "logical_store": (
                    "coherent_snapshot"
                    if snapshot_evidence is not None
                    else "monolith"
                ),
                "identity": source_identity,
                "fingerprint": source_fingerprint,
                "snapshot_manifest_path": (
                    str(self.source_snapshot_manifest)
                    if self.source_snapshot_manifest is not None
                    else ""
                ),
                "snapshot_evidence_fingerprint": (
                    str(snapshot_evidence.get("evidence_fingerprint") or "")
                    if snapshot_evidence is not None
                    else ""
                ),
                "full_integrity_check": (
                    dict(snapshot_evidence.get("full_integrity_check") or {})
                    if snapshot_evidence is not None
                    else {"status": "not_run_on_live_source"}
                ),
            },
            "raw": {
                "source_table": LEGACY_RAW_TABLE,
                "row_count": raw_digest.row_count,
                "logical_digest": raw_digest.digest,
                "raw_json_digest": raw_digest.payload_digest,
                "watermarks": watermarks,
                "allocated_bytes": raw_allocated_bytes,
                "index_allocated_bytes": raw_index_bytes,
                "query_plan": {
                    **query_plans,
                    "required_indexes": [
                        "wb_finance_raw_by_week",
                        "wb_finance_raw_by_sku_week",
                        "PRIMARY KEY(seller_id,report_id,rrd_id)",
                    ],
                },
            },
            "chunks": {
                "chunk_size": self.chunk_size,
                "chunk_count": len(chunks),
                "manifest": chunks,
                "resumable": True,
                "idempotency": "verified source+destination count/digest skips exact chunk",
            },
            "table_owner_read_write_matrix": table_matrix,
            "direct_sqlite_open_inventory": direct_open_inventory,
            "allocations": {
                "evidence": allocation_evidence,
                "sqlite_objects": allocations,
                "total_allocated_bytes": total_allocated,
                "raw_allocated_bytes": raw_allocated_bytes,
                "non_raw_allocated_bytes": non_raw_allocated,
            },
            "capacity": capacity,
            "writers_and_timers": writer_inventory,
            "target_generation": {
                "generation_epoch": epoch,
                "generation_directory": generation_dir,
                "raw_generation_id": raw_generation_id,
                "operational_generation_id": operational_generation_id,
                "candidate_manifest": manifest_payload(candidate_manifest),
                "destination_preflight": destination_preflight,
                "global_manifest_path": MANIFEST_FILENAME,
                "global_manifest_switch_planned": False,
            },
            "non_target_invariants": {
                "runner_did_not_mutate_old_monolith": True,
                "old_monolith_identity_unchanged_during_snapshot": (
                    (
                        str(
                            (
                                snapshot_evidence.get("source_identity") or {}
                            ).get("schema_digest")
                            or ""
                        )
                        == str(source_identity["schema_digest"])
                    )
                    if snapshot_evidence is not None
                    else not source_file_drift_observed
                ),
                "old_monolith_retained_as_rollback": True,
                "tables": [
                    {
                        "table": item["table"],
                        "owner": item["owner"],
                        "row_count": item["row_count"],
                        "logical_digest": item["logical_digest"],
                    }
                    for item in table_matrix
                    if item["table"] != LEGACY_RAW_TABLE
                ],
            },
            "rollback_plan": {
                "pre_cutover": "delete only the exact unselected candidate generation after digest review",
                "post_cutover": (
                    "drain/replay post-cutover raw tail into the immutable old monolith, "
                    "verify raw/derived/warehouse cursors, then atomically restore its exact manifest"
                ),
                "old_monolith_generation_id": "monolith",
                "in_place_delete_or_vacuum": False,
                "retirement_requires_separate_gate": True,
            },
            "shadow_read": {
                "canonical_reader": "monolith",
                "candidate_reader_enabled": False,
                "mass_copy_created": False,
                "comparison_contract": "count+ordered logical digest+query plan+latency",
            },
            "blockers": blockers,
            "apply_allowed_by_machine_preflight": not blockers,
            "human_approval_required": True,
            "approval_scope": {
                "backfill": "NOT_GRANTED",
                "live_tail_apply": "NOT_GRANTED",
                "global_manifest_cutover": "NOT_GRANTED",
                "canonical_reader_writer_switch": "NOT_GRANTED",
                "old_generation_retirement": "NOT_GRANTED",
            },
        }
        plan["fingerprint_contract"] = {
            "version": "wb_core_finance_storage_split_plan_fingerprint_v1",
            "includes": (
                "source identity/schema/logical digests, chunk manifest, "
                "ownership and open inventories, generation ids, required capacity, "
                "writer/timer states and rollback scope"
            ),
            "volatile_fields_rechecked_not_hashed": [
                "available_bytes",
                "capacity_shortfall",
                "process_pid_and_fd",
                "timer_trigger_timestamps",
                "performance_timings",
            ],
        }
        plan["performance"] = {
            "raw_chunk_digest_ms": raw_scan_ms,
            "all_table_logical_digest_ms": table_digest_ms,
            "logical_rows_hashed": sum(
                int(item["row_count"]) for item in table_matrix
            ),
            "total_plan_ms": round(
                (time.monotonic() - plan_started) * 1000,
                3,
            ),
            "acceptance": (
                "compare fresh production timings/query plans with the reviewed "
                "pre-cutover evidence; no hard machine-independent SLA"
            ),
        }
        plan["fingerprint"] = _plan_fingerprint(plan)
        plan["created_at"] = _utc_now()
        return plan


def _copy_rows(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    *,
    table: str,
    chunk_size: int,
) -> Iterator[tuple[int, int]]:
    info = _table_info(source, table)
    columns = [str(row["name"]) for row in info]
    order = _table_order(info)
    select = ",".join(_quoted(column) for column in columns)
    sql = f"SELECT {select} FROM {_quoted(table)}"
    if order:
        sql += " ORDER BY " + ",".join(_quoted(column) for column in order)
    else:
        sql += " ORDER BY rowid"
    cursor = source.execute(sql)
    placeholders = ",".join("?" for _ in columns)
    insert = (
        f"INSERT INTO {_quoted(table)}"
        f"({','.join(_quoted(column) for column in columns)}) VALUES({placeholders})"
    )
    copied = 0
    chunk_no = 0
    while rows := cursor.fetchmany(chunk_size):
        destination.executemany(insert, [tuple(row[column] for column in columns) for row in rows])
        copied += len(rows)
        chunk_no += 1
        yield chunk_no, copied


class FinanceStorageCandidateBuilder:
    """Build both candidate stores without switching the global manifest."""

    def __init__(
        self,
        planner: FinanceStorageMigrationPlanner,
        *,
        expected_fingerprint: str,
        approval_reference: str,
        fault_after_chunks: int = 0,
    ) -> None:
        self.planner = planner
        self.expected_fingerprint = str(expected_fingerprint or "").strip()
        self.approval_reference = str(approval_reference or "").strip()
        self.fault_after_chunks = max(0, int(fault_after_chunks))

    @contextmanager
    def _lock(self) -> Iterator[None]:
        lock_path = self.planner.runtime_dir / ".finance-storage-split.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise FinanceStorageMigrationError("Finance storage migration lock is busy") from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _snapshot_evidence(self) -> dict[str, Any]:
        manifest_path = self.planner.source_snapshot_manifest
        if manifest_path is None:
            raise FinanceStorageMigrationError(
                "candidate apply requires a verified immutable coherent snapshot"
            )
        state = _load_private_json(
            manifest_path,
            label="coherent snapshot manifest",
        )
        database_path = Path(str(state.get("database_path") or "")).resolve()
        database_path.relative_to(self.planner.runtime_dir)
        if (
            str(state.get("contract_version") or "") != SNAPSHOT_CONTRACT
            or str(state.get("status") or "") != "integrity_verified"
            or state.get("candidate_build_allowed") is not True
            or state.get("snapshot_identity")
            != _destination_path_identity(database_path)
        ):
            raise FinanceStorageMigrationError(
                "coherent snapshot full integrity evidence is missing or stale"
            )
        return {
            "contract_version": state["contract_version"],
            "status": state["status"],
            "snapshot_id": str(state.get("snapshot_id") or ""),
            "database_path": str(database_path),
            "manifest_path": str(manifest_path),
            "evidence_fingerprint": str(
                state.get("evidence_fingerprint") or ""
            ),
            "full_integrity_check": dict(
                state.get("full_integrity_check") or {}
            ),
        }

    def apply(self) -> dict[str, Any]:
        if not self.expected_fingerprint.startswith("sha256:"):
            raise FinanceStorageMigrationError("exact plan fingerprint is required")
        if not self.approval_reference:
            raise FinanceStorageMigrationError("fresh human approval reference is required")
        current_plan = self.planner.build_plan()
        generation = current_plan["target_generation"]
        candidate = generation["candidate_manifest"]
        candidate_root = (
            self.planner.runtime_dir / str(generation["generation_directory"])
        ).resolve()
        try:
            candidate_root.relative_to(self.planner.runtime_dir)
        except ValueError as exc:
            raise FinanceStorageMigrationError("candidate generation escapes runtime directory") from exc
        raw_path = self.planner.runtime_dir / str(candidate["raw"]["relative_path"])
        operational_path = self.planner.runtime_dir / str(
            candidate["operational"]["relative_path"]
        )
        source_path = Path(
            str(current_plan["source"]["identity"]["path"])
        ).resolve()
        snapshot_evidence = self._snapshot_evidence()
        return self._apply_under_locks(
            current_plan=current_plan,
            generation=generation,
            candidate=candidate,
            candidate_root=candidate_root,
            raw_path=raw_path,
            operational_path=operational_path,
            source_path=source_path,
            snapshot_evidence=snapshot_evidence,
        )

    def _apply_under_locks(
        self,
        *,
        current_plan: dict[str, Any],
        generation: dict[str, Any],
        candidate: dict[str, Any],
        candidate_root: Path,
        raw_path: Path,
        operational_path: Path,
        source_path: Path,
        snapshot_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock():
            saved_plan_path = candidate_root / "migration_plan.json"
            saved_plan: dict[str, Any] | None = None
            if saved_plan_path.is_file():
                loaded = json.loads(saved_plan_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise FinanceStorageMigrationError("saved migration plan is invalid")
                saved_plan = loaded
            if saved_plan is None:
                if not current_plan["apply_allowed_by_machine_preflight"]:
                    raise FinanceStorageMigrationError("Finance storage candidate apply is blocked")
                if current_plan["fingerprint"] != self.expected_fingerprint:
                    raise FinanceStorageMigrationError("reviewed Finance storage plan is stale")
                plan = current_plan
            else:
                if (
                    str(saved_plan.get("fingerprint") or "") != self.expected_fingerprint
                    or saved_plan.get("source", {}).get("fingerprint")
                    != current_plan.get("source", {}).get("fingerprint")
                    or saved_plan.get("deployed_sha") != current_plan.get("deployed_sha")
                    or saved_plan.get("target_generation", {}).get("generation_epoch")
                    != generation.get("generation_epoch")
                ):
                    raise FinanceStorageMigrationError(
                        "saved migration plan does not match current source/deploy/generation"
                    )
                plan = saved_plan
                generation = plan["target_generation"]
                candidate = generation["candidate_manifest"]
            capacity = plan["capacity"]
            fresh_vfs = os.statvfs(self.planner.runtime_dir)
            fresh_free = int(fresh_vfs.f_bavail * fresh_vfs.f_frsize)
            existing_candidate_bytes = (
                min(
                    raw_path.stat().st_size if raw_path.is_file() else 0,
                    int(capacity["projected_raw_destination_bytes"]),
                )
                + min(
                    operational_path.stat().st_size
                    if operational_path.is_file()
                    else 0,
                    int(capacity["projected_operational_destination_bytes"]),
                )
            )
            required_remaining = max(
                0, int(capacity["required_bytes"]) - existing_candidate_bytes
            )
            if fresh_free < required_remaining:
                raise FinanceStorageMigrationError(
                    "capacity reservation raced and is now insufficient before destination creation"
                )
            candidate_root.mkdir(parents=True, exist_ok=True)
            if saved_plan is None:
                _atomic_write_json(saved_plan_path, plan)
            source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=60)
            source.row_factory = sqlite3.Row
            source.execute("PRAGMA query_only=ON")
            if int(source.execute("PRAGMA query_only").fetchone()[0]) != 1:
                source.close()
                raise FinanceStorageMigrationError("candidate source query_only could not be enabled")
            operational = sqlite3.connect(operational_path, timeout=60, isolation_level=None)
            operational.row_factory = sqlite3.Row
            raw = sqlite3.connect(raw_path, timeout=60, isolation_level=None)
            raw.row_factory = sqlite3.Row
            operational.execute("PRAGMA foreign_keys=ON")
            raw.execute("PRAGMA foreign_keys=ON")
            completed_chunks = 0
            try:
                ensure_operational_schema(operational)
                bind_generation_identity(
                    operational,
                    logical_store="operational",
                    generation_id=str(generation["operational_generation_id"]),
                    generation_epoch=str(generation["generation_epoch"]),
                    source_fingerprint=str(plan["source"]["fingerprint"]),
                )
                operational.commit()
                ensure_raw_schema(raw)
                bind_generation_identity(
                    raw,
                    logical_store="finance_raw",
                    generation_id=str(generation["raw_generation_id"]),
                    generation_epoch=str(generation["generation_epoch"]),
                    source_fingerprint=str(plan["source"]["fingerprint"]),
                )
                raw.commit()
                migration_id = str(generation["generation_epoch"])
                batch_id = _digest(
                    {
                        "migration_id": migration_id,
                        "source_fingerprint": plan["source"]["fingerprint"],
                        "raw_digest": plan["raw"]["logical_digest"],
                    }
                )
                raw.execute("BEGIN IMMEDIATE")
                raw.execute(
                    """INSERT OR IGNORE INTO finance_raw_ingest_batches(
                       batch_id,source_identity,source_sha256,report_period,
                       seller_id,week_start,week_end,row_count,rows_digest,
                       status,created_at,committed_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,'loading',?,NULL)""",
                    (
                        batch_id,
                        plan["source"]["fingerprint"],
                        plan["source"]["fingerprint"],
                        (
                            str(plan["raw"]["watermarks"].get("min_week_start") or "")
                            + "/"
                            + str(plan["raw"]["watermarks"].get("max_week_end") or "")
                        ),
                        "*",
                        str(
                            plan["raw"]["watermarks"].get(
                                "min_week_start"
                            )
                            or ""
                        ),
                        str(
                            plan["raw"]["watermarks"].get(
                                "max_week_end"
                            )
                            or ""
                        ),
                        int(plan["raw"]["row_count"]),
                        plan["raw"]["logical_digest"],
                        _utc_now(),
                    ),
                )
                raw.commit()
                for chunk in plan["chunks"]["manifest"]:
                    existing = operational.execute(
                        """SELECT status,source_row_count,source_digest,destination_row_count,
                                  destination_digest
                           FROM finance_storage_migration_chunks
                           WHERE migration_id=? AND store_name='finance_raw' AND chunk_id=?""",
                        (migration_id, chunk["chunk_id"]),
                    ).fetchone()
                    if (
                        existing is not None
                        and str(existing["status"]) == "verified"
                        and int(existing["source_row_count"]) == int(chunk["row_count"])
                        and str(existing["source_digest"])
                        == str(chunk["verification_digest"])
                        and int(existing["destination_row_count"]) == int(chunk["row_count"])
                        and str(existing["destination_digest"])
                        == str(chunk["verification_digest"])
                    ):
                        verified_readback = _candidate_raw_digest(
                            raw,
                            batch_id=batch_id,
                            first_sequence=int(chunk["first_rowid"]),
                            last_sequence=int(chunk["last_rowid"]),
                        )
                        verified_digest = _digest(
                            {
                                "logical_digest": verified_readback.digest,
                                "raw_json_digest": (
                                    verified_readback.payload_digest
                                ),
                            }
                        )
                        if (
                            verified_readback.row_count
                            == int(chunk["row_count"])
                            and verified_digest
                            == str(chunk["verification_digest"])
                        ):
                            continue
                        raise FinanceStorageMigrationError(
                            f"verified raw chunk drifted: {chunk['chunk_id']}"
                        )
                    rows = source.execute(
                        f"""SELECT rowid,* FROM {LEGACY_RAW_TABLE}
                            WHERE rowid>=? AND rowid<=? ORDER BY rowid""",
                        (int(chunk["first_rowid"]), int(chunk["last_rowid"])),
                    ).fetchall()
                    digest = hashlib.sha256()
                    raw_json_digest = hashlib.sha256()
                    raw.execute("BEGIN IMMEDIATE")
                    for offset, row in enumerate(rows, start=1):
                        identity = [
                            str(row["seller_id"]),
                            str(row["report_id"]),
                            str(row["rrd_id"]),
                            str(row["row_hash"]),
                        ]
                        digest.update((_canonical_json(identity) + "\n").encode("utf-8"))
                        raw_json_bytes = str(row["raw_json"]).encode("utf-8")
                        raw_json_digest.update(
                            len(raw_json_bytes).to_bytes(8, "big")
                            + raw_json_bytes
                        )
                        raw_row_id = _digest(
                            {
                                "seller_id": identity[0],
                                "report_id": identity[1],
                                "rrd_id": identity[2],
                                "row_hash": identity[3],
                            }
                        )
                        raw.execute(
                            """INSERT OR IGNORE INTO finance_raw_rows(
                               raw_row_id,batch_id,batch_sequence_no,seller_id,report_id,rrd_id,
                               report_type,week_start,week_end,nm_id,vendor_code,barcode,
                               doc_type_name,seller_oper_name,row_hash,raw_json,first_seen_at
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                raw_row_id,
                                batch_id,
                                int(chunk["first_rowid"]) + offset - 1,
                                row["seller_id"],
                                row["report_id"],
                                row["rrd_id"],
                                row["report_type"],
                                row["week_start"],
                                row["week_end"],
                                row["nm_id"],
                                row["vendor_code"],
                                row["barcode"],
                                row["doc_type_name"],
                                row["seller_oper_name"],
                                row["row_hash"],
                                row["raw_json"],
                                row["first_seen_at"],
                            ),
                        )
                        raw.execute(
                            """INSERT OR IGNORE INTO finance_raw_batch_rows(
                               batch_id,batch_sequence_no,raw_row_id
                               ) VALUES(?,?,?)""",
                            (
                                batch_id,
                                int(chunk["first_rowid"]) + offset - 1,
                                raw_row_id,
                            ),
                        )
                    destination_digest = "sha256:" + digest.hexdigest()
                    destination_raw_json_digest = (
                        "sha256:" + raw_json_digest.hexdigest()
                    )
                    if (
                        len(rows) != int(chunk["row_count"])
                        or destination_digest != str(chunk["logical_digest"])
                        or destination_raw_json_digest
                        != str(chunk["raw_json_digest"])
                    ):
                        raw.rollback()
                        raise FinanceStorageMigrationError(
                            f"raw chunk source drift: {chunk['chunk_id']}"
                        )
                    raw.commit()
                    destination_readback = _candidate_raw_digest(
                        raw,
                        batch_id=batch_id,
                        first_sequence=int(chunk["first_rowid"]),
                        last_sequence=int(chunk["last_rowid"]),
                    )
                    if (
                        destination_readback.row_count != len(rows)
                        or destination_readback.digest != destination_digest
                        or destination_readback.payload_digest
                        != destination_raw_json_digest
                    ):
                        raise FinanceStorageMigrationError(
                            f"raw chunk destination readback mismatch: {chunk['chunk_id']}"
                        )
                    operational.execute("BEGIN IMMEDIATE")
                    operational.execute(
                        """INSERT INTO finance_storage_migration_chunks(
                           migration_id,store_name,chunk_id,source_first_key,source_last_key,
                           source_row_count,source_digest,destination_row_count,
                           destination_digest,bytes_written,status,error,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,'verified',NULL,?)
                           ON CONFLICT(migration_id,store_name,chunk_id) DO UPDATE SET
                           source_row_count=excluded.source_row_count,
                           source_digest=excluded.source_digest,
                           destination_row_count=excluded.destination_row_count,
                           destination_digest=excluded.destination_digest,
                           bytes_written=excluded.bytes_written,status='verified',
                           error=NULL,updated_at=excluded.updated_at""",
                        (
                            migration_id,
                            "finance_raw",
                            chunk["chunk_id"],
                            str(chunk["first_rowid"]),
                            str(chunk["last_rowid"]),
                            len(rows),
                            chunk["verification_digest"],
                            destination_readback.row_count,
                            _digest(
                                {
                                    "logical_digest": destination_readback.digest,
                                    "raw_json_digest": (
                                        destination_readback.payload_digest
                                    ),
                                }
                            ),
                            max(0, raw_path.stat().st_size),
                            _utc_now(),
                        ),
                    )
                    operational.commit()
                    completed_chunks += 1
                    if self.fault_after_chunks and completed_chunks >= self.fault_after_chunks:
                        raise InjectedMigrationFault(
                            f"after_verified_chunks:{completed_chunks}"
                        )
                raw.execute("BEGIN IMMEDIATE")
                raw.execute(
                    """UPDATE finance_raw_ingest_batches
                       SET status='committed',committed_at=? WHERE batch_id=?""",
                    (_utc_now(), batch_id),
                )
                raw.commit()
                source_schema = _schema_inventory(source)
                excluded = set(RAW_LEGACY_OBJECTS) | set(RAW_SCHEMA_TABLES) | set(
                    OPERATIONAL_SCHEMA_TABLES
                )
                table_names = [
                    str(row["name"])
                    for row in source.execute(
                        """SELECT name FROM sqlite_master
                           WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
                    ).fetchall()
                    if str(row["name"]) not in excluded
                ]
                for table in table_names:
                    already = operational.execute(
                        """SELECT status FROM finance_storage_migration_chunks
                           WHERE migration_id=? AND store_name='operational' AND chunk_id=?""",
                        (migration_id, f"table:{table}"),
                    ).fetchone()
                    expected = next(
                        item
                        for item in plan["table_owner_read_write_matrix"]
                        if item["table"] == table
                    )
                    if already is not None and str(already["status"]) == "verified":
                        current = logical_table_digest(operational, table)
                        if (
                            current.row_count == int(expected["row_count"])
                            and current.digest == str(expected["logical_digest"])
                        ):
                            continue
                        raise FinanceStorageMigrationError(
                            f"verified operational table drifted: {table}"
                        )
                    destination_tables = {
                        str(row[0])
                        for row in operational.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if table in destination_tables:
                        partial = logical_table_digest(operational, table)
                        if (
                            partial.row_count == int(expected["row_count"])
                            and partial.digest == str(expected["logical_digest"])
                        ):
                            operational.execute(
                                """INSERT INTO finance_storage_migration_chunks(
                                   migration_id,store_name,chunk_id,source_first_key,
                                   source_last_key,source_row_count,source_digest,
                                   destination_row_count,destination_digest,bytes_written,
                                   status,error,updated_at
                                   ) VALUES(?,?,?,?,?,?,?,?,?,?,'verified',NULL,?)""",
                                (
                                    migration_id,
                                    "operational",
                                    f"table:{table}",
                                    "",
                                    "",
                                    partial.row_count,
                                    partial.digest,
                                    partial.row_count,
                                    partial.digest,
                                    max(0, operational_path.stat().st_size),
                                    _utc_now(),
                                ),
                            )
                            operational.commit()
                            continue
                        operational.execute(f"DROP TABLE {_quoted(table)}")
                        operational.commit()
                    schema_row = next(
                        (
                            item
                            for item in source_schema
                            if item["type"] == "table" and item["name"] == table
                        ),
                        None,
                    )
                    if schema_row is None or not schema_row["sql"]:
                        raise FinanceStorageMigrationError(
                            f"operational table schema unavailable: {table}"
                        )
                    operational.execute("BEGIN IMMEDIATE")
                    operational.execute(str(schema_row["sql"]))
                    for _chunk_no, _copied in _copy_rows(
                        source,
                        operational,
                        table=table,
                        chunk_size=self.planner.chunk_size,
                    ):
                        pass
                    operational.commit()
                    current = logical_table_digest(operational, table)
                    if (
                        current.row_count != int(expected["row_count"])
                        or current.digest != str(expected["logical_digest"])
                    ):
                        raise FinanceStorageMigrationError(
                            f"operational table readback mismatch: {table}"
                        )
                    operational.execute(
                        """INSERT INTO finance_storage_migration_chunks(
                           migration_id,store_name,chunk_id,source_first_key,source_last_key,
                           source_row_count,source_digest,destination_row_count,
                           destination_digest,bytes_written,status,error,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,'verified',NULL,?)""",
                        (
                            migration_id,
                            "operational",
                            f"table:{table}",
                            "",
                            "",
                            current.row_count,
                            current.digest,
                            current.row_count,
                            current.digest,
                            max(0, operational_path.stat().st_size),
                            _utc_now(),
                        ),
                    )
                    operational.commit()
                for item in source_schema:
                    if item["type"] not in {"index", "trigger"} or not item["sql"]:
                        continue
                    if item["table"] in excluded or item["name"] in excluded:
                        continue
                    operational.execute(str(item["sql"]))
                operational.commit()
                raw_readback = _candidate_raw_digest(raw, batch_id=batch_id)
                if (
                    raw_readback.row_count != int(plan["raw"]["row_count"])
                    or raw_readback.digest != str(plan["raw"]["logical_digest"])
                    or raw_readback.payload_digest
                    != str(plan["raw"]["raw_json_digest"])
                ):
                    raise FinanceStorageMigrationError(
                        "full raw destination count/digest mismatch"
                    )
                candidate_manifest = build_manifest(
                    state="shadow",
                    canonical_source="monolith",
                    generation_epoch=str(generation["generation_epoch"]),
                    raw_generation_id=str(generation["raw_generation_id"]),
                    raw_relative_path=str(candidate["raw"]["relative_path"]),
                    raw_watermark=str(candidate["raw"]["watermark"]),
                    operational_generation_id=str(generation["operational_generation_id"]),
                    operational_relative_path=str(
                        candidate["operational"]["relative_path"]
                    ),
                    operational_watermark=str(
                        candidate["operational"]["watermark"]
                    ),
                    rollback_generation_id="monolith",
                    source_fingerprint=plan["source"]["fingerprint"],
                    created_at=str(candidate.get("created_at") or ""),
                )
                candidate_manifest_path = candidate_root / "candidate_generation_manifest.json"
                atomic_write_manifest(candidate_manifest_path, candidate_manifest)
                result = {
                    "contract_version": MIGRATION_CONTRACT,
                    "status": "candidate_ready",
                    "plan_fingerprint": plan["fingerprint"],
                    "approval_reference": self.approval_reference,
                    "source_snapshot": snapshot_evidence,
                    "business_data_maintenance_hold_required_for_backfill": False,
                    "warehouse_functional_lock_held": False,
                    "candidate_manifest_path": str(candidate_manifest_path),
                    "candidate_manifest": manifest_payload(candidate_manifest),
                    "raw_row_count": raw_readback.row_count,
                    "raw_destination_size_bytes": raw_path.stat().st_size,
                    "operational_destination_size_bytes": operational_path.stat().st_size,
                    "old_monolith_retained": self.planner.registry.resolve(
                        "operational"
                    ).is_file(),
                    "global_manifest_switched": False,
                    "canonical_source": "monolith",
                    "human_cutover_gate_required": True,
                }
                result["fingerprint"] = _digest(result)
                return result
            finally:
                source.close()
                operational.close()
                raw.close()


class FinanceStorageShadowRunner:
    """Enable audited monolith outbox capture and reconcile an unselected raw."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        candidate_manifest_path: Path,
        plan_fingerprint: str,
        approval_reference: str,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.registry = StoreRegistry(self.runtime_dir)
        self.candidate_manifest_path = (
            Path(candidate_manifest_path).expanduser().resolve()
        )
        self.plan_fingerprint = str(plan_fingerprint or "").strip()
        self.approval_reference = str(approval_reference or "").strip()
        if not self.plan_fingerprint.startswith("sha256:"):
            raise FinanceStorageMigrationError(
                "exact candidate plan fingerprint is required"
            )

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / SHADOW_STATE_FILENAME

    def _candidate(self) -> tuple[Any, Path, Path]:
        payload = _load_private_json(
            self.candidate_manifest_path,
            label="candidate generation manifest",
        )
        manifest = parse_manifest(payload)
        if manifest.state != "shadow" or manifest.canonical_source != "monolith":
            raise FinanceStorageMigrationError(
                "shadow runner requires an unselected candidate manifest"
            )
        raw_path = self.registry.resolve("finance_raw", manifest=manifest)
        operational_path = self.registry.resolve(
            "operational",
            manifest=manifest,
        )
        if not raw_path.is_file() or not operational_path.is_file():
            raise FinanceStorageMigrationError(
                "candidate generation files are incomplete"
            )
        return manifest, raw_path, operational_path

    def status(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "contract_version": SHADOW_STATE_CONTRACT,
                "status": "inactive",
                "enabled": False,
                "state_path": str(self.state_path),
            }
        state = _load_private_json(
            self.state_path,
            label="Finance shadow ingest state",
        )
        valid = (
            str(state.get("contract_version") or "")
            == SHADOW_STATE_CONTRACT
            and isinstance(state.get("enabled"), bool)
        )
        if not valid:
            raise FinanceStorageMigrationError(
                "Finance shadow ingest state is invalid"
            )
        return {**state, "state_path": str(self.state_path)}

    def activate(self) -> dict[str, Any]:
        if not self.approval_reference:
            raise FinanceStorageMigrationError(
                "shadow activation requires an approval reference"
            )
        active = self.registry.load()
        if active.state != "monolith" or active.canonical_source != "monolith":
            raise FinanceStorageMigrationError(
                "shadow activation requires the canonical monolith"
            )
        candidate, raw_path, operational_path = self._candidate()
        existing = self.status()
        if existing.get("enabled") is True:
            if (
                str(existing.get("plan_fingerprint") or "")
                == self.plan_fingerprint
                and str(existing.get("candidate_manifest_sha256") or "")
                == candidate.manifest_sha256
            ):
                return {**existing, "idempotent": True}
            raise FinanceStorageMigrationError(
                "a different Finance shadow ingest generation is active"
            )
        with self.registry.session(
            "finance_raw",
            mode="rw",
            operation="finance_shadow_ingest_schema_activate",
            isolation_level=None,
        ) as source:
            ensure_raw_schema(source)
            source.commit()
        state: dict[str, Any] = {
            "contract_version": SHADOW_STATE_CONTRACT,
            "status": "active",
            "enabled": True,
            "plan_fingerprint": self.plan_fingerprint,
            "approval_reference": self.approval_reference,
            "candidate_manifest_path": str(self.candidate_manifest_path),
            "candidate_manifest_sha256": candidate.manifest_sha256,
            "raw_generation_id": candidate.raw.generation_id,
            "operational_generation_id": candidate.operational.generation_id,
            "raw_path": str(raw_path),
            "operational_path": str(operational_path),
            "activated_at": _utc_now(),
        }
        state["state_fingerprint"] = _digest(state)
        _atomic_write_json(self.state_path, state)
        return {**state, "state_path": str(self.state_path), "idempotent": False}

    def deactivate(self, *, reason: str) -> dict[str, Any]:
        state = self.status()
        if state.get("enabled") is False:
            return {**state, "idempotent": True}
        candidate, _raw_path, _operational_path = self._candidate()
        if (
            str(state.get("plan_fingerprint") or "") != self.plan_fingerprint
            or str(state.get("candidate_manifest_sha256") or "")
            != candidate.manifest_sha256
        ):
            raise FinanceStorageMigrationError(
                "shadow deactivate identity does not match"
            )
        updated = {
            key: value
            for key, value in state.items()
            if key not in {"state_path", "state_fingerprint"}
        }
        updated.update(
            {
                "status": "inactive",
                "enabled": False,
                "deactivated_at": _utc_now(),
                "deactivation_reason": str(reason or "")[:1000],
            }
        )
        updated["state_fingerprint"] = _digest(updated)
        _atomic_write_json(self.state_path, updated)
        return {
            **updated,
            "state_path": str(self.state_path),
            "idempotent": False,
        }

    def reconcile_legacy_current(
        self,
        *,
        chunk_size: int = 10_000,
    ) -> dict[str, Any]:
        state = self.status()
        if state.get("enabled") is not True:
            raise FinanceStorageMigrationError(
                "legacy reconciliation requires active shadow ingest"
            )
        _candidate, raw_path, _operational_path = self._candidate()
        source_path = self.registry.resolve("operational")
        source = sqlite3.connect(
            f"file:{source_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        destination = sqlite3.connect(
            raw_path,
            timeout=60,
            isolation_level=None,
        )
        source.row_factory = sqlite3.Row
        destination.row_factory = sqlite3.Row
        started = time.monotonic()
        try:
            source.execute("PRAGMA query_only=ON")
            source.execute("BEGIN")
            ensure_raw_schema(destination)
            source_revision = _digest(
                {
                    "source": _source_identity(source_path, source),
                    "row_count": int(
                        source.execute(
                            f"SELECT COUNT(*) FROM {LEGACY_RAW_TABLE}"
                        ).fetchone()[0]
                    ),
                    "max_updated_at": str(
                        source.execute(
                            f"SELECT COALESCE(MAX(updated_at),'') "
                            f"FROM {LEGACY_RAW_TABLE}"
                        ).fetchone()[0]
                    ),
                    "candidate_plan": self.plan_fingerprint,
                }
            )
            batch_id = _digest(
                {
                    "kind": "legacy_current_reconciliation",
                    "source_revision": source_revision,
                }
            )
            source_scope = source.execute(
                f"""SELECT COALESCE(MIN(week_start),''),
                           COALESCE(MAX(week_end),'')
                    FROM {LEGACY_RAW_TABLE}"""
            ).fetchone()
            destination.execute(
                """INSERT OR IGNORE INTO finance_raw_ingest_batches(
                   batch_id,source_identity,source_sha256,report_period,
                   seller_id,week_start,week_end,row_count,rows_digest,status,
                   created_at,committed_at
                   ) VALUES(?,?,?,'all-history','*',?,?,0,?,'loading',?,NULL)""",
                (
                    batch_id,
                    source_revision,
                    source_revision,
                    str(source_scope[0]),
                    str(source_scope[1]),
                    _digest([]),
                    _utc_now(),
                ),
            )
            destination.commit()
            digest = hashlib.sha256()
            payload_digest = hashlib.sha256()
            row_count = 0
            inserted_count = 0
            reused_count = 0
            cursor = source.execute(
                f"""SELECT rowid,* FROM {LEGACY_RAW_TABLE}
                    ORDER BY rowid"""
            )
            while rows := cursor.fetchmany(max(1, int(chunk_size))):
                destination.execute("BEGIN IMMEDIATE")
                for row in rows:
                    identity = [
                        str(row["seller_id"]),
                        str(row["report_id"]),
                        str(row["rrd_id"]),
                        str(row["row_hash"]),
                    ]
                    digest.update(
                        (_canonical_json(identity) + "\n").encode("utf-8")
                    )
                    raw_json_bytes = str(row["raw_json"]).encode("utf-8")
                    payload_digest.update(
                        len(raw_json_bytes).to_bytes(8, "big")
                        + raw_json_bytes
                    )
                    raw_row_id = _digest(
                        {
                            "seller_id": identity[0],
                            "report_id": identity[1],
                            "rrd_id": identity[2],
                            "row_hash": identity[3],
                        }
                    )
                    inserted = destination.execute(
                        """INSERT OR IGNORE INTO finance_raw_rows(
                           raw_row_id,batch_id,batch_sequence_no,seller_id,
                           report_id,rrd_id,report_type,week_start,week_end,
                           nm_id,vendor_code,barcode,doc_type_name,
                           seller_oper_name,row_hash,raw_json,first_seen_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            raw_row_id,
                            batch_id,
                            int(row["rowid"]),
                            row["seller_id"],
                            row["report_id"],
                            row["rrd_id"],
                            row["report_type"],
                            row["week_start"],
                            row["week_end"],
                            row["nm_id"],
                            row["vendor_code"],
                            row["barcode"],
                            row["doc_type_name"],
                            row["seller_oper_name"],
                            row["row_hash"],
                            row["raw_json"],
                            row["first_seen_at"],
                        ),
                    )
                    if inserted.rowcount:
                        inserted_count += 1
                    else:
                        existing = destination.execute(
                            """SELECT seller_id,report_id,rrd_id,row_hash,raw_json
                               FROM finance_raw_rows WHERE raw_row_id=?""",
                            (raw_row_id,),
                        ).fetchone()
                        if (
                            existing is None
                            or tuple(existing)
                            != (
                                identity[0],
                                identity[1],
                                identity[2],
                                identity[3],
                                row["raw_json"],
                            )
                        ):
                            raise FinanceStorageMigrationError(
                                "legacy reconciliation found conflicting "
                                "immutable raw identity"
                            )
                        reused_count += 1
                    destination.execute(
                        """INSERT OR IGNORE INTO finance_raw_batch_rows(
                           batch_id,batch_sequence_no,raw_row_id
                           ) VALUES(?,?,?)""",
                        (batch_id, int(row["rowid"]), raw_row_id),
                    )
                    row_count += 1
                destination.commit()
            logical_digest = "sha256:" + digest.hexdigest()
            raw_json_digest = "sha256:" + payload_digest.hexdigest()
            destination.execute("BEGIN IMMEDIATE")
            destination.execute(
                """UPDATE finance_raw_ingest_batches
                   SET row_count=?,rows_digest=?,status='committed',
                       committed_at=? WHERE batch_id=?""",
                (row_count, logical_digest, _utc_now(), batch_id),
            )
            destination.commit()
            source.rollback()
        finally:
            source.close()
            destination.close()
        return {
            "contract_version": "wb_core_finance_legacy_reconciliation_v1",
            "status": "reconciled",
            "source_revision": source_revision,
            "batch_id": batch_id,
            "source_row_count": row_count,
            "logical_digest": logical_digest,
            "raw_json_digest": raw_json_digest,
            "inserted_count": inserted_count,
            "reused_count": reused_count,
            "missing_current_rows": 0,
            "duration_seconds": round(time.monotonic() - started, 3),
            "canonical_source": "monolith",
            "global_manifest_switched": False,
        }

    def apply_live_tail(self, *, max_events: int = 100_000) -> dict[str, Any]:
        state = self.status()
        if state.get("enabled") is not True:
            raise FinanceStorageMigrationError(
                "live-tail apply requires active shadow ingest"
            )
        _candidate, raw_path, _operational_path = self._candidate()
        source_path = self.registry.resolve("finance_raw")
        source = sqlite3.connect(
            f"file:{source_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        destination = sqlite3.connect(
            raw_path,
            timeout=60,
            isolation_level=None,
        )
        source.row_factory = sqlite3.Row
        destination.row_factory = sqlite3.Row
        bridge = FinanceRawLiveTailBridge()
        applied = 0
        try:
            source.execute("PRAGMA query_only=ON")
            while applied < max(1, int(max_events)):
                result = bridge.apply_next(
                    source=source,
                    destination=destination,
                )
                if result is None:
                    break
                applied += 1
            source_max = int(
                source.execute(
                    "SELECT COALESCE(MAX(sequence_no),0) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
            cursor_row = destination.execute(
                """SELECT last_sequence_no,last_event_id
                   FROM finance_raw_bridge_cursors WHERE bridge_id=?""",
                (bridge.bridge_id,),
            ).fetchone()
            cursor = int(cursor_row["last_sequence_no"]) if cursor_row else 0
            duplicate_event_ids = int(
                destination.execute(
                    """SELECT COUNT(*)-COUNT(DISTINCT event_id)
                       FROM finance_raw_outbox"""
                ).fetchone()[0]
            )
            duplicate_sequences = int(
                destination.execute(
                    """SELECT COUNT(*)-COUNT(DISTINCT sequence_no)
                       FROM finance_raw_outbox"""
                ).fetchone()[0]
            )
        finally:
            source.close()
            destination.close()
        return {
            "contract_version": "wb_core_finance_live_tail_v1",
            "status": "caught_up" if cursor == source_max else "lagging",
            "applied_events": applied,
            "source_latest_sequence": source_max,
            "destination_cursor": cursor,
            "lag_events": max(0, source_max - cursor),
            "duplicate_event_ids": duplicate_event_ids,
            "duplicate_sequences": duplicate_sequences,
            "global_manifest_switched": False,
        }


class FinanceStorageShadowVerifier:
    """Persist bounded all-week shadow evidence for one exact candidate."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        candidate_manifest_path: Path,
        candidate_plan_fingerprint: str,
        minimum_observation_seconds: int = 3600,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.registry = StoreRegistry(self.runtime_dir)
        self.candidate_manifest_path = (
            Path(candidate_manifest_path).expanduser().resolve()
        )
        self.candidate_plan_fingerprint = str(
            candidate_plan_fingerprint or ""
        ).strip()
        self.minimum_observation_seconds = max(
            0,
            int(minimum_observation_seconds),
        )

    def _candidate(self) -> tuple[Any, Path, Path]:
        manifest = parse_manifest(
            _load_private_json(
                self.candidate_manifest_path,
                label="candidate generation manifest",
            )
        )
        if manifest.state != "shadow" or manifest.canonical_source != "monolith":
            raise FinanceStorageMigrationError(
                "shadow verification requires an unselected candidate"
            )
        return (
            manifest,
            self.registry.resolve("finance_raw", manifest=manifest),
            self.registry.resolve("operational", manifest=manifest),
        )

    @staticmethod
    def _rows_digest(
        conn: sqlite3.Connection,
        *,
        table: str,
    ) -> LogicalDigest:
        digest = hashlib.sha256()
        count = 0
        for row in conn.execute(
            f"""SELECT seller_id,week_start,week_end,report_id,rrd_id,row_hash
                FROM {table}
                ORDER BY seller_id,week_start,week_end,report_id,rrd_id,row_hash"""
        ):
            digest.update(
                (
                    _canonical_json([str(value) for value in row])
                    + "\n"
                ).encode("utf-8")
            )
            count += 1
        return LogicalDigest(
            row_count=count,
            digest="sha256:" + digest.hexdigest(),
        )

    def verify(self) -> dict[str, Any]:
        active = self.registry.load()
        if active.state != "monolith" or active.canonical_source != "monolith":
            raise FinanceStorageMigrationError(
                "shadow verification requires the canonical monolith"
            )
        candidate, raw_path, operational_path = self._candidate()
        shadow_state = FinanceStorageShadowRunner(
            self.runtime_dir,
            candidate_manifest_path=self.candidate_manifest_path,
            plan_fingerprint=self.candidate_plan_fingerprint,
            approval_reference="verification-readback",
        ).status()
        if (
            shadow_state.get("enabled") is not True
            or str(shadow_state.get("candidate_manifest_sha256") or "")
            != candidate.manifest_sha256
        ):
            raise FinanceStorageMigrationError(
                "exact shadow ingest generation is not active"
            )
        source_path = self.registry.resolve("operational", manifest=active)
        source = sqlite3.connect(
            f"file:{source_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        raw = sqlite3.connect(
            f"file:{raw_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        operational = sqlite3.connect(
            operational_path,
            timeout=60,
            isolation_level=None,
        )
        for conn in (source, raw, operational):
            conn.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA query_only=ON")
            raw.execute("PRAGMA query_only=ON")
            ensure_operational_schema(operational)
            weeks = source.execute(
                f"""SELECT DISTINCT seller_id,week_start,week_end
                    FROM {LEGACY_RAW_TABLE}
                    ORDER BY seller_id,week_start,week_end"""
            ).fetchall()
            comparisons = [
                shadow_compare_week(
                    source_conn=source,
                    shadow_conn=raw,
                    seller_id=str(row["seller_id"]),
                    week_start=str(row["week_start"]),
                    week_end=str(row["week_end"]),
                )
                for row in weeks
            ]
            source_digest = self._rows_digest(
                source,
                table=LEGACY_RAW_TABLE,
            )
            shadow_digest = self._rows_digest(
                raw,
                table="finance_raw_current_rows",
            )
            source_max = int(
                source.execute(
                    "SELECT COALESCE(MAX(sequence_no),0) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
            cursor_row = raw.execute(
                """SELECT last_sequence_no FROM finance_raw_bridge_cursors
                   WHERE bridge_id='finance_raw_live_tail_v1'"""
            ).fetchone()
            cursor = int(cursor_row[0]) if cursor_row else 0
            duplicate_events = int(
                raw.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT event_id) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
            duplicate_sequences = int(
                raw.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT sequence_no) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
            mismatch_count = sum(
                1
                for comparison in comparisons
                if comparison["status"] != "match"
            )
            performance_regressions = [
                comparison
                for comparison in comparisons
                if float(comparison["shadow_latency_ms"])
                > max(
                    100.0,
                    float(comparison["source_latency_ms"]) * 4.0,
                )
            ]
            checked_at = _utc_now()
            for comparison in comparisons:
                comparison_id = _digest(
                    {
                        "candidate": candidate.manifest_sha256,
                        "checked_at": checked_at,
                        "scope": comparison["scope"],
                    }
                )
                operational.execute(
                    """INSERT OR REPLACE INTO finance_storage_shadow_comparisons(
                       comparison_id,generation_epoch,scope_json,
                       source_row_count,shadow_row_count,source_digest,
                       shadow_digest,source_query_plan_json,
                       shadow_query_plan_json,source_latency_ms,
                       shadow_latency_ms,status,detail_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        comparison_id,
                        candidate.generation_epoch,
                        _canonical_json(comparison["scope"]),
                        int(comparison["source_row_count"]),
                        int(comparison["shadow_row_count"]),
                        comparison["source_digest"],
                        comparison["shadow_digest"],
                        _canonical_json(comparison["source_query_plan"]),
                        _canonical_json(comparison["shadow_query_plan"]),
                        float(comparison["source_latency_ms"]),
                        float(comparison["shadow_latency_ms"]),
                        comparison["status"],
                        _canonical_json(comparison),
                        checked_at,
                    ),
                )
            operational.commit()
        finally:
            source.close()
            raw.close()
            operational.close()
        evidence_path = operational_path.parent / SHADOW_VERIFICATION_FILENAME
        prior: dict[str, Any] = {}
        if evidence_path.exists():
            prior = _load_private_json(
                evidence_path,
                label="shadow verification evidence",
            )
            if (
                str(prior.get("contract_version") or "")
                != SHADOW_VERIFICATION_CONTRACT
                or str(prior.get("candidate_manifest_sha256") or "")
                != candidate.manifest_sha256
            ):
                raise FinanceStorageMigrationError(
                    "shadow verification evidence identity changed"
                )
        first_verified_at = str(
            prior.get("first_verified_at") or checked_at
        )
        first_dt = datetime.fromisoformat(
            first_verified_at.replace("Z", "+00:00")
        )
        checked_dt = datetime.fromisoformat(
            checked_at.replace("Z", "+00:00")
        )
        observation_seconds = max(
            0,
            int((checked_dt - first_dt).total_seconds()),
        )
        blockers: list[dict[str, Any]] = []
        if (
            source_digest != shadow_digest
            or mismatch_count
        ):
            blockers.append({"code": "shadow_raw_mismatch"})
        if cursor != source_max:
            blockers.append({"code": "live_tail_lag"})
        if duplicate_events or duplicate_sequences:
            blockers.append({"code": "duplicate_outbox_event"})
        if performance_regressions:
            blockers.append({"code": "material_query_regression"})
        if observation_seconds < self.minimum_observation_seconds:
            blockers.append(
                {
                    "code": "shadow_soak_incomplete",
                    "observed_seconds": observation_seconds,
                    "required_seconds": self.minimum_observation_seconds,
                }
            )
        evidence: dict[str, Any] = {
            "contract_version": SHADOW_VERIFICATION_CONTRACT,
            "status": "ready" if not blockers else "soaking",
            "candidate_plan_fingerprint": self.candidate_plan_fingerprint,
            "candidate_manifest_sha256": candidate.manifest_sha256,
            "first_verified_at": first_verified_at,
            "last_verified_at": checked_at,
            "observation_seconds": observation_seconds,
            "minimum_observation_seconds": self.minimum_observation_seconds,
            "comparison_count": len(comparisons),
            "mismatch_count": mismatch_count,
            "source_current": {
                "row_count": source_digest.row_count,
                "logical_digest": source_digest.digest,
            },
            "candidate_current": {
                "row_count": shadow_digest.row_count,
                "logical_digest": shadow_digest.digest,
            },
            "outbox": {
                "source_latest_sequence": source_max,
                "candidate_cursor": cursor,
                "lag_events": max(0, source_max - cursor),
                "duplicate_event_ids": duplicate_events,
                "duplicate_sequences": duplicate_sequences,
            },
            "performance": {
                "regression_count": len(performance_regressions),
                "threshold": "shadow <= max(100ms, source*4)",
            },
            "warehouse_cost_contract": {
                "finance_raw_rows_read": 0,
                "proof": "registry-separated operational store",
            },
            "blockers": blockers,
        }
        evidence["evidence_fingerprint"] = _digest(evidence)
        _atomic_write_json(evidence_path, evidence)
        return {**evidence, "evidence_path": str(evidence_path)}


class FinanceStorageCutover:
    """Fresh operational recopy and atomic split-manifest switch."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        candidate_manifest_path: Path,
        candidate_plan_fingerprint: str,
        deployed_sha: str,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.registry = StoreRegistry(self.runtime_dir)
        self.candidate_manifest_path = (
            Path(candidate_manifest_path).expanduser().resolve()
        )
        self.candidate_plan_fingerprint = str(
            candidate_plan_fingerprint or ""
        ).strip()
        self.deployed_sha = str(deployed_sha or "").strip()

    def _candidate(self) -> tuple[Any, Path, Path]:
        payload = _load_private_json(
            self.candidate_manifest_path,
            label="candidate generation manifest",
        )
        manifest = parse_manifest(payload)
        if manifest.state != "shadow" or manifest.canonical_source != "monolith":
            raise FinanceStorageMigrationError(
                "cutover requires an unselected shadow candidate"
            )
        raw_path = self.registry.resolve("finance_raw", manifest=manifest)
        operational_path = self.registry.resolve(
            "operational",
            manifest=manifest,
        )
        if not raw_path.is_file() or not operational_path.is_file():
            raise FinanceStorageMigrationError(
                "cutover candidate files are incomplete"
            )
        return manifest, raw_path, operational_path

    @staticmethod
    def _fingerprint(plan: Mapping[str, Any]) -> str:
        stable = json.loads(_canonical_json(plan))
        stable.pop("fingerprint", None)
        stable.pop("created_at", None)
        capacity = stable.get("capacity", {})
        for key in (
            "available_bytes",
            "shortfall_bytes",
            "remaining_bytes",
            "sufficient",
        ):
            capacity.pop(key, None)
        return _digest(stable)

    def build_plan(self) -> dict[str, Any]:
        active = self.registry.load()
        candidate, raw_path, operational_path = self._candidate()
        source_path = self.registry.resolve("operational", manifest=active)
        verification_path = (
            operational_path.parent / SHADOW_VERIFICATION_FILENAME
        )
        verification: dict[str, Any] = {}
        if verification_path.exists():
            verification = _load_private_json(
                verification_path,
                label="shadow verification evidence",
            )
        shadow = FinanceStorageShadowRunner(
            self.runtime_dir,
            candidate_manifest_path=self.candidate_manifest_path,
            plan_fingerprint=self.candidate_plan_fingerprint,
            approval_reference="status-only",
        ).status()
        source = sqlite3.connect(
            f"file:{source_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        raw = sqlite3.connect(
            f"file:{raw_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        source.row_factory = sqlite3.Row
        raw.row_factory = sqlite3.Row
        try:
            for conn in (source, raw):
                conn.execute("PRAGMA query_only=ON")
            source_identity = _source_identity(source_path, source)
            legacy_raw_count = int(
                source.execute(
                    f"SELECT COUNT(*) FROM {LEGACY_RAW_TABLE}"
                ).fetchone()[0]
            )
            source_tables = {
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            source_outbox_max = (
                int(
                    source.execute(
                        "SELECT COALESCE(MAX(sequence_no),0) "
                        "FROM finance_raw_outbox"
                    ).fetchone()[0]
                )
                if RAW_SCHEMA_TABLES.issubset(source_tables)
                else 0
            )
            candidate_outbox_max = int(
                raw.execute(
                    "SELECT COALESCE(MAX(sequence_no),0) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
            bridge_row = raw.execute(
                """SELECT last_sequence_no FROM finance_raw_bridge_cursors
                   WHERE bridge_id='finance_raw_live_tail_v1'"""
            ).fetchone()
            bridge_cursor = (
                int(bridge_row["last_sequence_no"]) if bridge_row else 0
            )
            duplicate_event_ids = int(
                raw.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT event_id) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
            duplicate_sequences = int(
                raw.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT sequence_no) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
        finally:
            source.close()
            raw.close()
        cutover_manifest = build_manifest(
            state="cutover",
            canonical_source="split",
            generation_epoch=candidate.generation_epoch,
            raw_generation_id=candidate.raw.generation_id,
            raw_relative_path=candidate.raw.relative_path,
            raw_watermark=str(source_outbox_max),
            operational_generation_id=candidate.operational.generation_id,
            operational_relative_path=candidate.operational.relative_path,
            operational_watermark=_digest(source_identity),
            rollback_generation_id="monolith",
            source_fingerprint=candidate.source_fingerprint,
            created_at=candidate.created_at,
        )
        vfs = os.statvfs(self.runtime_dir)
        free_bytes = int(vfs.f_bavail * vfs.f_frsize)
        recopy_bytes = max(
            int(operational_path.stat().st_size * 1.25),
            2 * _GIB,
        )
        reserve_bytes = 2 * _GIB
        required_bytes = recopy_bytes + reserve_bytes
        blockers: list[dict[str, Any]] = []
        if active.state != "monolith" or active.canonical_source != "monolith":
            blockers.append({"code": "canonical_source_not_monolith"})
        if re.fullmatch(r"[0-9a-f]{40}", self.deployed_sha) is None:
            blockers.append({"code": "deployed_sha_unavailable"})
        if shadow.get("enabled") is not True:
            blockers.append({"code": "shadow_ingest_not_active"})
        if (
            str(shadow.get("candidate_manifest_sha256") or "")
            != candidate.manifest_sha256
        ):
            blockers.append({"code": "shadow_candidate_identity_mismatch"})
        if source_outbox_max != bridge_cursor:
            blockers.append(
                {
                    "code": "live_tail_lag",
                    "source_sequence": source_outbox_max,
                    "candidate_cursor": bridge_cursor,
                }
            )
        if candidate_outbox_max != source_outbox_max:
            blockers.append({"code": "candidate_outbox_sequence_mismatch"})
        if duplicate_event_ids or duplicate_sequences:
            blockers.append({"code": "duplicate_outbox_event"})
        if (
            str(verification.get("contract_version") or "")
            != SHADOW_VERIFICATION_CONTRACT
            or str(verification.get("status") or "") != "ready"
            or str(verification.get("candidate_manifest_sha256") or "")
            != candidate.manifest_sha256
            or str(verification.get("candidate_plan_fingerprint") or "")
            != self.candidate_plan_fingerprint
            or int(verification.get("mismatch_count") or 0) != 0
            or int(
                (verification.get("outbox") or {}).get("lag_events") or 0
            )
            != 0
        ):
            blockers.append({"code": "shadow_soak_evidence_not_ready"})
        if free_bytes < required_bytes:
            blockers.append({"code": "operational_recopy_capacity_shortfall"})
        plan: dict[str, Any] = {
            "contract_version": CUTOVER_PLAN_CONTRACT,
            "mode": "cutover_dry_run",
            "deployed_sha": self.deployed_sha,
            "candidate_plan_fingerprint": self.candidate_plan_fingerprint,
            "candidate_manifest_path": str(self.candidate_manifest_path),
            "candidate_manifest_sha256": candidate.manifest_sha256,
            "source": {
                "path": str(source_path),
                "identity": source_identity,
                "legacy_raw_row_count": legacy_raw_count,
                "outbox_latest_sequence": source_outbox_max,
            },
            "candidate": {
                "raw_path": str(raw_path),
                "operational_path": str(operational_path),
                "raw_identity": _destination_path_identity(raw_path),
                "operational_identity": _destination_path_identity(
                    operational_path
                ),
                "bridge_cursor": bridge_cursor,
                "outbox_latest_sequence": candidate_outbox_max,
                "duplicate_event_ids": duplicate_event_ids,
                "duplicate_sequences": duplicate_sequences,
            },
            "shadow_ingest": shadow,
            "shadow_verification": verification,
            "target_manifest": manifest_payload(cutover_manifest),
            "capacity": {
                "available_bytes": free_bytes,
                "operational_recopy_bytes": recopy_bytes,
                "post_cutover_reserve_bytes": reserve_bytes,
                "required_bytes": required_bytes,
                "shortfall_bytes": max(0, required_bytes - free_bytes),
                "remaining_bytes": max(0, free_bytes - required_bytes),
                "sufficient": free_bytes >= required_bytes,
            },
            "critical_window": {
                "manual_business_writes_blocked": True,
                "automatic_writers_drained": True,
                "fresh_operational_recopy": True,
                "final_raw_tail": True,
                "atomic_manifest_switch": True,
                "unrelated_services_stopped": False,
            },
            "rollback": {
                "old_monolith_retained": True,
                "rollback_generation_id": "monolith",
                "retirement_authorized": False,
            },
            "blockers": blockers,
            "apply_allowed_by_machine_preflight": not blockers,
            "human_approval_required": True,
        }
        plan["fingerprint"] = self._fingerprint(plan)
        plan["created_at"] = _utc_now()
        return plan

    def _hold_evidence(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        barrier = barrier_status(self.runtime_dir)
        maintenance = _load_private_json(
            self.runtime_dir / ".business-data-maintenance.json",
            label="business-data maintenance state",
        )
        if (
            barrier.get("active") is not True
            or str(barrier.get("phase") or "") != "held"
            or barrier.get("hold_confirmed") is not True
            or str(barrier.get("window_kind") or "") != "final_cutover"
            or str(barrier.get("window_id") or "")
            != f"final-cutover-{str(plan['fingerprint']).removeprefix('sha256:')[:20]}"
        ):
            raise FinanceStorageMigrationError(
                "exact final-cutover HTTP write barrier is required"
            )
        if (
            str(maintenance.get("phase") or "") != "held"
            or not bool((maintenance.get("hold_readback") or {}).get("quiet"))
        ):
            raise FinanceStorageMigrationError(
                "exact quiet writer/timer hold is required for cutover"
            )
        return {
            "barrier": barrier,
            "maintenance_fingerprint": _digest(maintenance),
        }

    @staticmethod
    def _drain_candidate_outbox(
        raw: sqlite3.Connection,
        operational: sqlite3.Connection,
    ) -> dict[str, Any]:
        applied = 0
        duplicates = 0
        while True:
            cursor_row = operational.execute(
                """SELECT last_sequence_no FROM finance_operational_consumer_cursors
                   WHERE consumer_id='finance_operational_projection_v1'"""
            ).fetchone()
            cursor = int(cursor_row[0]) if cursor_row else 0
            event = raw.execute(
                """SELECT event_id,sequence_no,event_type,payload_json,
                          payload_sha256
                   FROM finance_raw_outbox
                   WHERE sequence_no>? ORDER BY sequence_no LIMIT 1""",
                (cursor,),
            ).fetchone()
            if event is None:
                break
            if int(event["sequence_no"]) != cursor + 1:
                raise FinanceStorageMigrationError(
                    "candidate outbox sequence gap blocks cutover"
                )
            if _digest(str(event["payload_json"])) != str(
                event["payload_sha256"]
            ):
                raise FinanceStorageMigrationError(
                    "candidate outbox payload digest mismatch"
                )
            payload = json.loads(str(event["payload_json"]))
            operational.execute("BEGIN IMMEDIATE")
            receipt = operational.execute(
                """SELECT 1 FROM finance_operational_receipts
                   WHERE consumer_id='finance_operational_projection_v1'
                     AND event_id=?""",
                (event["event_id"],),
            ).fetchone()
            if receipt is None:
                operational.execute(
                    """INSERT OR IGNORE INTO finance_operational_inbox(
                       event_id,consumer_id,sequence_no,event_type,
                       payload_sha256,received_at
                       ) VALUES(?,'finance_operational_projection_v1',?,?,?,?)""",
                    (
                        event["event_id"],
                        int(event["sequence_no"]),
                        event["event_type"],
                        event["payload_sha256"],
                        _utc_now(),
                    ),
                )
                operational.execute(
                    """INSERT INTO finance_operational_receipts(
                       consumer_id,event_id,sequence_no,source_revision,
                       result_row_count,result_digest,applied_at
                       ) VALUES('finance_operational_projection_v1',?,?,?,?,?,?)""",
                    (
                        event["event_id"],
                        int(event["sequence_no"]),
                        str(payload.get("rows_digest") or ""),
                        int(payload.get("row_count") or 0),
                        str(payload.get("rows_digest") or ""),
                        _utc_now(),
                    ),
                )
                applied += 1
            else:
                duplicates += 1
            operational.execute(
                """INSERT INTO finance_operational_consumer_cursors(
                   consumer_id,last_sequence_no,last_event_id,
                   source_revision,updated_at
                   ) VALUES('finance_operational_projection_v1',?,?,?,?)
                   ON CONFLICT(consumer_id) DO UPDATE SET
                   last_sequence_no=excluded.last_sequence_no,
                   last_event_id=excluded.last_event_id,
                   source_revision=excluded.source_revision,
                   updated_at=excluded.updated_at""",
                (
                    int(event["sequence_no"]),
                    event["event_id"],
                    str(payload.get("rows_digest") or ""),
                    _utc_now(),
                ),
            )
            operational.commit()
            raw.execute("BEGIN IMMEDIATE")
            raw.execute(
                """UPDATE finance_raw_outbox
                   SET published_at=?,attempt_count=attempt_count+1,
                       last_error=NULL WHERE event_id=?""",
                (_utc_now(), event["event_id"]),
            )
            raw.execute(
                """INSERT INTO finance_raw_consumer_cursors(
                   consumer_id,last_sequence_no,last_event_id,updated_at
                   ) VALUES('finance_operational_projection_v1',?,?,?)
                   ON CONFLICT(consumer_id) DO UPDATE SET
                   last_sequence_no=excluded.last_sequence_no,
                   last_event_id=excluded.last_event_id,
                   updated_at=excluded.updated_at""",
                (
                    int(event["sequence_no"]),
                    event["event_id"],
                    _utc_now(),
                ),
            )
            raw.commit()
        latest = int(
            raw.execute(
                "SELECT COALESCE(MAX(sequence_no),0) FROM finance_raw_outbox"
            ).fetchone()[0]
        )
        cursor_row = operational.execute(
            """SELECT last_sequence_no FROM finance_operational_consumer_cursors
               WHERE consumer_id='finance_operational_projection_v1'"""
        ).fetchone()
        final_cursor = int(cursor_row[0]) if cursor_row else 0
        return {
            "applied": applied,
            "duplicate_receipts": duplicates,
            "latest_sequence": latest,
            "operational_cursor": final_cursor,
            "lag_events": max(0, latest - final_cursor),
        }

    def _fresh_operational_recopy(
        self,
        *,
        source_path: Path,
        operational_path: Path,
        candidate: Any,
    ) -> dict[str, Any]:
        temporary = operational_path.with_name(
            ".operational.final-cutover.partial"
        )
        if temporary.exists():
            temporary.unlink()
        source = sqlite3.connect(
            f"file:{source_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        destination = sqlite3.connect(
            temporary,
            timeout=60,
            isolation_level=None,
        )
        source.row_factory = sqlite3.Row
        destination.row_factory = sqlite3.Row
        table_evidence: list[dict[str, Any]] = []
        try:
            source.execute("PRAGMA query_only=ON")
            source.execute("BEGIN")
            source_identity = _source_identity(source_path, source)
            destination.execute("PRAGMA foreign_keys=OFF")
            source_schema = _schema_inventory(source)
            excluded = (
                set(RAW_LEGACY_OBJECTS)
                | set(RAW_SCHEMA_TABLES)
                | set(OPERATIONAL_SCHEMA_TABLES)
            )
            table_names = [
                str(item["name"])
                for item in source_schema
                if item["type"] == "table"
                and item["name"] not in excluded
            ]
            for table in table_names:
                schema_row = next(
                    item
                    for item in source_schema
                    if item["type"] == "table" and item["name"] == table
                )
                if not schema_row["sql"]:
                    raise FinanceStorageMigrationError(
                        f"operational table schema unavailable: {table}"
                    )
                destination.execute(str(schema_row["sql"]))
                for _chunk_no, _copied in _copy_rows(
                    source,
                    destination,
                    table=table,
                    chunk_size=10_000,
                ):
                    pass
                destination.commit()
                source_digest = logical_table_digest(source, table)
                destination_digest = logical_table_digest(
                    destination,
                    table,
                )
                if source_digest != destination_digest:
                    raise FinanceStorageMigrationError(
                        f"fresh operational recopy mismatch: {table}"
                    )
                table_evidence.append(
                    {
                        "table": table,
                        "row_count": source_digest.row_count,
                        "logical_digest": source_digest.digest,
                        "owner": _table_owner(table)["owner"],
                    }
                )
            for item in source_schema:
                if (
                    item["type"] in {"index", "trigger", "view"}
                    and item["sql"]
                    and item["table"] not in excluded
                    and item["name"] not in excluded
                ):
                    destination.execute(str(item["sql"]))
            ensure_operational_schema(destination)
            bind_generation_identity(
                destination,
                logical_store="operational",
                generation_id=candidate.operational.generation_id,
                generation_epoch=candidate.generation_epoch,
                source_fingerprint=candidate.source_fingerprint,
            )
            destination.commit()
            integrity = [
                str(row[0])
                for row in destination.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
            ]
            if integrity != ["ok"]:
                raise FinanceStorageMigrationError(
                    "fresh operational generation integrity_check failed"
                )
            foreign_keys = destination.execute(
                "PRAGMA foreign_key_check"
            ).fetchmany(1)
            if foreign_keys:
                raise FinanceStorageMigrationError(
                    "fresh operational generation foreign_key_check failed"
                )
            source.rollback()
        finally:
            source.close()
            destination.close()
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, operational_path)
        return {
            "status": "reconciled",
            "table_count": len(table_evidence),
            "tables": table_evidence,
            "non_target_digest": _digest(table_evidence),
            "source_identity": source_identity,
            "destination_identity": _destination_path_identity(
                operational_path
            ),
            "integrity_check": "ok",
        }

    @staticmethod
    def _legacy_raw_coverage(
        source_path: Path,
        candidate_raw_path: Path,
    ) -> dict[str, Any]:
        source = sqlite3.connect(
            f"file:{source_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        source.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA query_only=ON")
            source.execute(
                "ATTACH DATABASE ? AS candidate_raw",
                (f"file:{candidate_raw_path}?mode=ro",),
            )
            source_count = int(
                source.execute(
                    f"SELECT COUNT(*) FROM {LEGACY_RAW_TABLE}"
                ).fetchone()[0]
            )
            missing = int(
                source.execute(
                    f"""SELECT COUNT(*) FROM {LEGACY_RAW_TABLE} AS live
                        WHERE NOT EXISTS(
                          SELECT 1 FROM candidate_raw.finance_raw_rows AS raw
                          WHERE raw.seller_id=live.seller_id
                            AND raw.report_id=live.report_id
                            AND raw.rrd_id=live.rrd_id
                            AND raw.row_hash=live.row_hash
                        )"""
                ).fetchone()[0]
            )
        finally:
            source.close()
        return {
            "source_current_row_count": source_count,
            "missing_current_rows": missing,
            "status": "match" if missing == 0 else "mismatch",
        }

    def apply(
        self,
        *,
        reviewed_plan: Mapping[str, Any],
        expected_fingerprint: str,
        approval_reference: str,
    ) -> dict[str, Any]:
        exact_fingerprint = str(expected_fingerprint or "")
        if (
            str(reviewed_plan.get("contract_version") or "")
            != CUTOVER_PLAN_CONTRACT
            or str(reviewed_plan.get("mode") or "") != "cutover_dry_run"
            or str(reviewed_plan.get("fingerprint") or "")
            != exact_fingerprint
            or self._fingerprint(reviewed_plan) != exact_fingerprint
            or not bool(
                reviewed_plan.get("apply_allowed_by_machine_preflight")
            )
        ):
            raise FinanceStorageMigrationError(
                "reviewed Finance cutover plan is invalid or blocked"
            )
        if not str(approval_reference or "").strip():
            raise FinanceStorageMigrationError(
                "Finance cutover approval reference is required"
            )
        if (
            str(reviewed_plan.get("candidate_plan_fingerprint") or "")
            != self.candidate_plan_fingerprint
            or str(reviewed_plan.get("deployed_sha") or "")
            != self.deployed_sha
        ):
            raise FinanceStorageMigrationError(
                "reviewed Finance cutover identity does not match the runner"
            )
        hold_evidence = self._hold_evidence(reviewed_plan)
        candidate, raw_path, operational_path = self._candidate()
        active = self.registry.load()
        if active.state != "monolith" or active.canonical_source != "monolith":
            if (
                active.state == "cutover"
                and active.raw.generation_id
                == candidate.raw.generation_id
                and active.operational.generation_id
                == candidate.operational.generation_id
                and active.source_fingerprint
                == candidate.source_fingerprint
            ):
                return {
                    "contract_version": CUTOVER_RESULT_CONTRACT,
                    "status": "cutover_complete",
                    "idempotent": True,
                    "manifest": manifest_payload(active),
                }
            raise FinanceStorageMigrationError(
                "canonical generation changed before cutover"
            )
        if (
            candidate.manifest_sha256
            != str(reviewed_plan.get("candidate_manifest_sha256") or "")
        ):
            raise FinanceStorageMigrationError(
                "candidate generation changed before cutover"
            )
        shadow = FinanceStorageShadowRunner(
            self.runtime_dir,
            candidate_manifest_path=self.candidate_manifest_path,
            plan_fingerprint=self.candidate_plan_fingerprint,
            approval_reference=str(approval_reference),
        )
        tail = shadow.apply_live_tail(max_events=1_000_000)
        if tail["lag_events"] or tail["duplicate_event_ids"] or tail[
            "duplicate_sequences"
        ]:
            raise FinanceStorageMigrationError(
                "final Finance raw live tail is not clean"
            )
        source_path = self.registry.resolve("operational", manifest=active)
        coverage = self._legacy_raw_coverage(source_path, raw_path)
        if coverage["missing_current_rows"]:
            raise FinanceStorageMigrationError(
                "candidate raw is missing current legacy rows"
            )
        recopy = self._fresh_operational_recopy(
            source_path=source_path,
            operational_path=operational_path,
            candidate=candidate,
        )
        raw = sqlite3.connect(
            raw_path,
            timeout=60,
            isolation_level=None,
        )
        operational = sqlite3.connect(
            operational_path,
            timeout=60,
            isolation_level=None,
        )
        raw.row_factory = sqlite3.Row
        operational.row_factory = sqlite3.Row
        try:
            outbox = self._drain_candidate_outbox(raw, operational)
            if outbox["lag_events"]:
                raise FinanceStorageMigrationError(
                    "candidate operational outbox drain is incomplete"
                )
            raw.execute("PRAGMA wal_checkpoint(FULL)")
            operational.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            raw.close()
            operational.close()
        disabled = shadow.deactivate(
            reason="split generation becoming canonical"
        )
        target_manifest = build_manifest(
            state="cutover",
            canonical_source="split",
            generation_epoch=candidate.generation_epoch,
            raw_generation_id=candidate.raw.generation_id,
            raw_relative_path=candidate.raw.relative_path,
            raw_watermark=str(tail["source_latest_sequence"]),
            operational_generation_id=candidate.operational.generation_id,
            operational_relative_path=candidate.operational.relative_path,
            operational_watermark=_digest(recopy["source_identity"]),
            rollback_generation_id="monolith",
            source_fingerprint=candidate.source_fingerprint,
            created_at=_utc_now(),
        )
        try:
            atomic_write_manifest(
                self.registry.manifest_path,
                target_manifest,
            )
        except Exception:
            shadow.activate()
            raise
        readback = self.registry.load(require_files=True)
        if readback.manifest_sha256 != target_manifest.manifest_sha256:
            raise FinanceStorageMigrationError(
                "atomic cutover manifest readback mismatch"
            )
        result: dict[str, Any] = {
            "contract_version": CUTOVER_RESULT_CONTRACT,
            "status": "cutover_complete",
            "idempotent": False,
            "plan_fingerprint": str(expected_fingerprint),
            "approval_reference": str(approval_reference).strip(),
            "hold_evidence": hold_evidence,
            "raw_live_tail": tail,
            "legacy_raw_coverage": coverage,
            "operational_recopy": recopy,
            "outbox_reconciliation": outbox,
            "shadow_ingest": disabled,
            "manifest": manifest_payload(readback),
            "global_manifest_switched": True,
            "canonical_source": "split",
            "old_monolith_retained": source_path.is_file(),
            "restart_required": ["wb-core-registry-http.service"],
            "retirement_authorized": False,
        }
        result["evidence_fingerprint"] = _digest(result)
        evidence_path = (
            operational_path.parent / "cutover_evidence.json"
        )
        _atomic_write_json(evidence_path, result)
        result["evidence_path"] = str(evidence_path)
        return result


class FinanceStorageRollback:
    """Build and atomically select a reconciled rollback monolith."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        deployed_sha: str,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.registry = StoreRegistry(self.runtime_dir)
        self.deployed_sha = str(deployed_sha or "").strip()

    @staticmethod
    def _fingerprint(plan: Mapping[str, Any]) -> str:
        stable = json.loads(_canonical_json(plan))
        stable.pop("fingerprint", None)
        stable.pop("created_at", None)
        capacity = stable.get("capacity", {})
        for key in (
            "available_bytes",
            "shortfall_bytes",
            "remaining_bytes",
            "sufficient",
        ):
            capacity.pop(key, None)
        return _digest(stable)

    def build_plan(self) -> dict[str, Any]:
        active = self.registry.load(require_files=True)
        raw_path = self.registry.resolve("finance_raw", manifest=active)
        operational_path = self.registry.resolve(
            "operational",
            manifest=active,
        )
        retained_monolith = self.runtime_dir / MONOLITH_FILENAME
        rollback_epoch = "rollback-" + active.generation_epoch[:40]
        rollback_root = self.runtime_dir / "generations" / rollback_epoch
        rollback_path = rollback_root / "monolith.sqlite3"
        raw = sqlite3.connect(
            f"file:{raw_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        raw.row_factory = sqlite3.Row
        try:
            raw.execute("PRAGMA query_only=ON")
            current = FinanceStorageShadowVerifier._rows_digest(
                raw,
                table="finance_raw_current_rows",
            )
            latest_outbox = int(
                raw.execute(
                    "SELECT COALESCE(MAX(sequence_no),0) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
            duplicate_events = int(
                raw.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT event_id) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
            duplicate_sequences = int(
                raw.execute(
                    "SELECT COUNT(*)-COUNT(DISTINCT sequence_no) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
        finally:
            raw.close()
        health = storage_health(self.registry)
        vfs = os.statvfs(self.runtime_dir)
        free_bytes = int(vfs.f_bavail * vfs.f_frsize)
        required_bytes = (
            int(raw_path.stat().st_size)
            + int(operational_path.stat().st_size * 1.25)
            + 2 * _GIB
        )
        blockers: list[dict[str, Any]] = []
        if active.state != "cutover" or active.canonical_source != "split":
            blockers.append({"code": "canonical_source_not_split"})
        if re.fullmatch(r"[0-9a-f]{40}", self.deployed_sha) is None:
            blockers.append({"code": "deployed_sha_unavailable"})
        if not retained_monolith.is_file():
            blockers.append({"code": "retained_monolith_missing"})
        if duplicate_events or duplicate_sequences:
            blockers.append({"code": "duplicate_outbox_event"})
        if int(health.get("consumer_lag_events") or 0):
            blockers.append({"code": "operational_consumer_lag"})
        if int(health.get("actionable_dead_letters") or 0):
            blockers.append({"code": "actionable_dead_letter"})
        if free_bytes < required_bytes:
            blockers.append({"code": "rollback_capacity_shortfall"})
        plan: dict[str, Any] = {
            "contract_version": ROLLBACK_PLAN_CONTRACT,
            "mode": "rollback_dry_run",
            "deployed_sha": self.deployed_sha,
            "active_manifest": manifest_payload(active),
            "raw": {
                "path": str(raw_path),
                "current_row_count": current.row_count,
                "current_logical_digest": current.digest,
                "latest_outbox_sequence": latest_outbox,
                "duplicate_event_ids": duplicate_events,
                "duplicate_sequences": duplicate_sequences,
            },
            "operational": {
                "path": str(operational_path),
                "identity": _destination_path_identity(operational_path),
            },
            "retained_monolith": {
                "path": str(retained_monolith),
                "identity": _destination_path_identity(retained_monolith),
                "mutation_allowed": False,
            },
            "target": {
                "generation_epoch": rollback_epoch,
                "generation_id": rollback_epoch,
                "path": str(rollback_path),
                "relative_path": str(
                    rollback_path.relative_to(self.runtime_dir)
                ),
            },
            "capacity": {
                "available_bytes": free_bytes,
                "required_bytes": required_bytes,
                "remaining_bytes": max(0, free_bytes - required_bytes),
                "shortfall_bytes": max(0, required_bytes - free_bytes),
                "sufficient": free_bytes >= required_bytes,
            },
            "critical_window": {
                "candidate_built_before_hold": True,
                "post_prepare_raw_tail_replayed": True,
                "fresh_operational_recopy": True,
                "atomic_manifest_switch": True,
                "unrelated_services_stopped": False,
            },
            "blockers": blockers,
            "prepare_allowed_by_machine_preflight": not blockers,
            "apply_allowed_after_candidate_readback": not blockers,
            "human_approval_required": True,
            "old_split_and_original_monolith_retained": True,
        }
        plan["fingerprint"] = self._fingerprint(plan)
        plan["created_at"] = _utc_now()
        return plan

    @staticmethod
    def _create_legacy_raw_schema(
        conn: sqlite3.Connection,
    ) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wb_finance_weekly_raw_rows (
                seller_id TEXT NOT NULL,
                report_id TEXT NOT NULL,
                rrd_id TEXT NOT NULL,
                report_type INTEGER,
                week_start TEXT NOT NULL,
                week_end TEXT NOT NULL,
                nm_id TEXT,
                vendor_code TEXT,
                barcode TEXT,
                doc_type_name TEXT,
                seller_oper_name TEXT,
                row_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(seller_id,report_id,rrd_id)
            );
            CREATE INDEX IF NOT EXISTS wb_finance_raw_by_week
            ON wb_finance_weekly_raw_rows(
                seller_id,week_start,week_end
            );
            CREATE INDEX IF NOT EXISTS wb_finance_raw_by_sku_week
            ON wb_finance_weekly_raw_rows(
                seller_id,nm_id,week_start,week_end
            );
            """
        )

    @staticmethod
    def _copy_current_raw(
        source: sqlite3.Connection,
        destination: sqlite3.Connection,
    ) -> LogicalDigest:
        destination.execute("DELETE FROM wb_finance_weekly_raw_rows")
        digest = hashlib.sha256()
        count = 0
        cursor = source.execute(
            """SELECT seller_id,report_id,rrd_id,report_type,week_start,
                      week_end,nm_id,vendor_code,barcode,doc_type_name,
                      seller_oper_name,row_hash,raw_json,first_seen_at,
                      updated_at
               FROM finance_raw_current_rows
               ORDER BY seller_id,week_start,week_end,report_id,rrd_id"""
        )
        while rows := cursor.fetchmany(10_000):
            destination.execute("BEGIN IMMEDIATE")
            for row in rows:
                destination.execute(
                    """INSERT INTO wb_finance_weekly_raw_rows
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(row),
                )
                digest.update(
                    (
                        _canonical_json(
                            [
                                str(row["seller_id"]),
                                str(row["week_start"]),
                                str(row["week_end"]),
                                str(row["report_id"]),
                                str(row["rrd_id"]),
                                str(row["row_hash"]),
                            ]
                        )
                        + "\n"
                    ).encode("utf-8")
                )
                count += 1
            destination.commit()
        return LogicalDigest(
            row_count=count,
            digest="sha256:" + digest.hexdigest(),
        )

    def prepare(
        self,
        *,
        reviewed_plan: Mapping[str, Any],
        expected_fingerprint: str,
        approval_reference: str,
    ) -> dict[str, Any]:
        exact = str(expected_fingerprint or "")
        if (
            str(reviewed_plan.get("contract_version") or "")
            != ROLLBACK_PLAN_CONTRACT
            or str(reviewed_plan.get("mode") or "") != "rollback_dry_run"
            or str(reviewed_plan.get("fingerprint") or "") != exact
            or self._fingerprint(reviewed_plan) != exact
            or not bool(
                reviewed_plan.get("prepare_allowed_by_machine_preflight")
            )
            or not str(approval_reference or "").strip()
        ):
            raise FinanceStorageMigrationError(
                "reviewed Finance rollback plan is invalid or blocked"
            )
        active = self.registry.load(require_files=True)
        if active.manifest_sha256 != str(
            (reviewed_plan.get("active_manifest") or {}).get(
                "manifest_sha256"
            )
            or ""
        ):
            raise FinanceStorageMigrationError(
                "active split generation changed before rollback prepare"
            )
        raw_path = self.registry.resolve("finance_raw", manifest=active)
        operational_path = self.registry.resolve(
            "operational",
            manifest=active,
        )
        target_path = Path(str(reviewed_plan["target"]["path"])).resolve()
        target_path.relative_to(self.runtime_dir)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path = target_path.parent / "rollback_candidate.json"
        if evidence_path.exists() and target_path.is_file():
            existing = _load_private_json(
                evidence_path,
                label="rollback candidate evidence",
            )
            if (
                str(existing.get("plan_fingerprint") or "") == exact
                and str(existing.get("status") or "") == "candidate_ready"
            ):
                return {
                    **existing,
                    "candidate_evidence_path": str(evidence_path),
                    "idempotent": True,
                }
        temporary = target_path.with_name(
            f".{target_path.name}.partial"
        )
        if temporary.exists():
            temporary.unlink()
        operational = sqlite3.connect(
            f"file:{operational_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        candidate = sqlite3.connect(
            temporary,
            timeout=60,
            isolation_level=None,
        )
        raw = sqlite3.connect(
            f"file:{raw_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        for conn in (operational, candidate, raw):
            conn.row_factory = sqlite3.Row
        try:
            operational.execute("PRAGMA query_only=ON")
            raw.execute("PRAGMA query_only=ON")
            operational.backup(candidate)
            self._create_legacy_raw_schema(candidate)
            raw_digest = self._copy_current_raw(raw, candidate)
            latest_sequence = int(
                raw.execute(
                    "SELECT COALESCE(MAX(sequence_no),0) "
                    "FROM finance_raw_outbox"
                ).fetchone()[0]
            )
            candidate.execute("PRAGMA wal_checkpoint(FULL)")
            integrity = [
                str(row[0])
                for row in candidate.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
            ]
            if integrity != ["ok"]:
                raise FinanceStorageMigrationError(
                    "rollback candidate integrity_check failed"
                )
            foreign_keys = candidate.execute(
                "PRAGMA foreign_key_check"
            ).fetchmany(1)
            if foreign_keys:
                raise FinanceStorageMigrationError(
                    "rollback candidate foreign_key_check failed"
                )
        finally:
            operational.close()
            raw.close()
            candidate.close()
        os.replace(temporary, target_path)
        result: dict[str, Any] = {
            "contract_version": ROLLBACK_CANDIDATE_CONTRACT,
            "status": "candidate_ready",
            "idempotent": False,
            "plan_fingerprint": exact,
            "approval_reference": str(approval_reference).strip(),
            "active_manifest_sha256": active.manifest_sha256,
            "candidate_path": str(target_path),
            "captured_outbox_sequence": latest_sequence,
            "raw_current_row_count": raw_digest.row_count,
            "raw_current_logical_digest": raw_digest.digest,
            "integrity_check": "ok",
            "foreign_key_check_rows": 0,
            "prepared_at": _utc_now(),
            "original_monolith_mutated": False,
            "global_manifest_switched": False,
        }
        result["candidate_fingerprint"] = _digest(result)
        _atomic_write_json(evidence_path, result)
        return {**result, "candidate_evidence_path": str(evidence_path)}

    @staticmethod
    def _fresh_operational_recopy(
        source: sqlite3.Connection,
        destination: sqlite3.Connection,
    ) -> dict[str, Any]:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        destination.execute("PRAGMA foreign_keys=OFF")
        source_schema = _schema_inventory(source)
        protected = set(RAW_LEGACY_OBJECTS)
        for item in reversed(
            [
                item
                for item in _schema_inventory(destination)
                if item["type"] in {"view", "trigger", "index"}
                and item["sql"]
                and item["name"] not in protected
                and item["table"] not in protected
            ]
        ):
            destination.execute(
                f"DROP {str(item['type']).upper()} IF EXISTS "
                f"{_quoted(str(item['name']))}"
            )
        destination_tables = [
            str(row[0])
            for row in destination.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
            ).fetchall()
            if str(row[0]) != LEGACY_RAW_TABLE
        ]
        for table in destination_tables:
            destination.execute(f"DROP TABLE {_quoted(table)}")
        destination.commit()
        table_evidence: list[dict[str, Any]] = []
        source_tables = [
            str(item["name"])
            for item in source_schema
            if item["type"] == "table"
            and item["name"] not in RAW_LEGACY_OBJECTS
            and item["name"] not in RAW_SCHEMA_TABLES
        ]
        for table in source_tables:
            schema = next(
                item
                for item in source_schema
                if item["type"] == "table" and item["name"] == table
            )
            if not schema["sql"]:
                raise FinanceStorageMigrationError(
                    f"rollback operational schema unavailable: {table}"
                )
            destination.execute(str(schema["sql"]))
            for _chunk_no, _copied in _copy_rows(
                source,
                destination,
                table=table,
                chunk_size=10_000,
            ):
                pass
            destination.commit()
            source_digest = logical_table_digest(source, table)
            target_digest = logical_table_digest(destination, table)
            if source_digest != target_digest:
                raise FinanceStorageMigrationError(
                    f"rollback operational recopy mismatch: {table}"
                )
            table_evidence.append(
                {
                    "table": table,
                    "row_count": source_digest.row_count,
                    "logical_digest": source_digest.digest,
                }
            )
        for item in source_schema:
            if (
                item["type"] in {"index", "trigger", "view"}
                and item["sql"]
                and item["name"] not in protected
                and item["table"] not in protected
                and item["table"] not in RAW_SCHEMA_TABLES
            ):
                destination.execute(str(item["sql"]))
        destination.commit()
        source.rollback()
        return {
            "status": "reconciled",
            "table_count": len(table_evidence),
            "tables": table_evidence,
            "non_target_digest": _digest(table_evidence),
        }

    @staticmethod
    def _refresh_post_prepare_raw(
        raw: sqlite3.Connection,
        destination: sqlite3.Connection,
        *,
        captured_sequence: int,
    ) -> dict[str, Any]:
        affected = raw.execute(
            """SELECT DISTINCT batch.seller_id,batch.week_start,batch.week_end
               FROM finance_raw_outbox AS event
               JOIN finance_raw_ingest_batches AS batch
                 ON batch.batch_id=event.batch_id
               WHERE event.sequence_no>?
               ORDER BY batch.seller_id,batch.week_start,batch.week_end""",
            (captured_sequence,),
        ).fetchall()
        refreshed_rows = 0
        scopes: list[dict[str, Any]] = []
        for scope in affected:
            seller_id = str(scope["seller_id"])
            week_start = str(scope["week_start"])
            week_end = str(scope["week_end"])
            if not seller_id or seller_id == "*" or not week_start or not week_end:
                raise FinanceStorageMigrationError(
                    "post-prepare raw event has unbounded rollback scope"
                )
            destination.execute("BEGIN IMMEDIATE")
            destination.execute(
                f"""DELETE FROM {LEGACY_RAW_TABLE}
                    WHERE seller_id=? AND week_start=? AND week_end=?""",
                (seller_id, week_start, week_end),
            )
            rows = raw.execute(
                """SELECT seller_id,report_id,rrd_id,report_type,week_start,
                          week_end,nm_id,vendor_code,barcode,doc_type_name,
                          seller_oper_name,row_hash,raw_json,first_seen_at,
                          updated_at
                   FROM finance_raw_current_rows
                   WHERE seller_id=? AND week_start=? AND week_end=?
                   ORDER BY report_id,rrd_id""",
                (seller_id, week_start, week_end),
            ).fetchall()
            for row in rows:
                destination.execute(
                    f"INSERT INTO {LEGACY_RAW_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(row),
                )
            destination.commit()
            refreshed_rows += len(rows)
            scopes.append(
                {
                    "seller_id": seller_id,
                    "week_start": week_start,
                    "week_end": week_end,
                    "row_count": len(rows),
                }
            )
        latest = int(
            raw.execute(
                "SELECT COALESCE(MAX(sequence_no),0) "
                "FROM finance_raw_outbox"
            ).fetchone()[0]
        )
        return {
            "captured_sequence": captured_sequence,
            "latest_sequence": latest,
            "replayed_event_count": max(0, latest - captured_sequence),
            "affected_scope_count": len(scopes),
            "refreshed_row_count": refreshed_rows,
            "scopes": scopes,
        }

    def apply(
        self,
        *,
        reviewed_plan: Mapping[str, Any],
        expected_fingerprint: str,
        approval_reference: str,
        candidate_evidence_path: Path,
    ) -> dict[str, Any]:
        exact = str(expected_fingerprint or "")
        if (
            str(reviewed_plan.get("contract_version") or "")
            != ROLLBACK_PLAN_CONTRACT
            or str(reviewed_plan.get("fingerprint") or "") != exact
            or self._fingerprint(reviewed_plan) != exact
            or not str(approval_reference or "").strip()
        ):
            raise FinanceStorageMigrationError(
                "reviewed Finance rollback plan is invalid"
            )
        evidence = _load_private_json(
            Path(candidate_evidence_path).expanduser().resolve(),
            label="rollback candidate evidence",
        )
        if (
            str(evidence.get("contract_version") or "")
            != ROLLBACK_CANDIDATE_CONTRACT
            or str(evidence.get("status") or "") != "candidate_ready"
            or str(evidence.get("plan_fingerprint") or "") != exact
        ):
            raise FinanceStorageMigrationError(
                "rollback candidate evidence is invalid"
            )
        barrier = barrier_status(self.runtime_dir)
        window_id = "rollback-" + exact.removeprefix("sha256:")[:20]
        if (
            barrier.get("active") is not True
            or str(barrier.get("phase") or "") != "held"
            or barrier.get("hold_confirmed") is not True
            or str(barrier.get("window_kind") or "") != "rollback_drill"
            or str(barrier.get("window_id") or "") != window_id
        ):
            raise FinanceStorageMigrationError(
                "exact rollback-drill HTTP write barrier is required"
            )
        maintenance = _load_private_json(
            self.runtime_dir / ".business-data-maintenance.json",
            label="business-data maintenance state",
        )
        if (
            str(maintenance.get("phase") or "") != "held"
            or not bool(
                (maintenance.get("hold_readback") or {}).get("quiet")
            )
        ):
            raise FinanceStorageMigrationError(
                "exact quiet writer/timer hold is required for rollback"
            )
        active = self.registry.load(require_files=True)
        if active.manifest_sha256 != str(
            evidence.get("active_manifest_sha256") or ""
        ):
            raise FinanceStorageMigrationError(
                "active split generation changed before rollback"
            )
        raw_path = self.registry.resolve("finance_raw", manifest=active)
        operational_path = self.registry.resolve(
            "operational",
            manifest=active,
        )
        candidate_path = Path(
            str(evidence.get("candidate_path") or "")
        ).resolve()
        candidate_path.relative_to(self.runtime_dir)
        raw = sqlite3.connect(
            f"file:{raw_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        operational = sqlite3.connect(
            f"file:{operational_path}?mode=ro",
            uri=True,
            timeout=60,
            isolation_level=None,
        )
        candidate = sqlite3.connect(
            candidate_path,
            timeout=60,
            isolation_level=None,
        )
        for conn in (raw, operational, candidate):
            conn.row_factory = sqlite3.Row
        try:
            raw.execute("PRAGMA query_only=ON")
            operational_recopy = self._fresh_operational_recopy(
                operational,
                candidate,
            )
            raw_replay = self._refresh_post_prepare_raw(
                raw,
                candidate,
                captured_sequence=int(
                    evidence["captured_outbox_sequence"]
                ),
            )
            expected_raw = FinanceStorageShadowVerifier._rows_digest(
                raw,
                table="finance_raw_current_rows",
            )
            actual_raw = FinanceStorageShadowVerifier._rows_digest(
                candidate,
                table=LEGACY_RAW_TABLE,
            )
            if expected_raw != actual_raw:
                raise FinanceStorageMigrationError(
                    "rollback raw logical readback mismatch"
                )
            quick = [
                str(row[0])
                for row in candidate.execute(
                    "PRAGMA quick_check(1000)"
                ).fetchall()
            ]
            if quick != ["ok"]:
                raise FinanceStorageMigrationError(
                    "rollback candidate final quick_check failed"
                )
            candidate.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            raw.close()
            operational.close()
            candidate.close()
        target = reviewed_plan["target"]
        rollback_manifest = build_manifest(
            state="monolith",
            canonical_source="monolith",
            generation_epoch=str(target["generation_epoch"]),
            raw_generation_id=str(target["generation_id"]),
            raw_relative_path=str(target["relative_path"]),
            raw_watermark=str(raw_replay["latest_sequence"]),
            operational_generation_id=str(target["generation_id"]),
            operational_relative_path=str(target["relative_path"]),
            operational_watermark=operational_recopy[
                "non_target_digest"
            ],
            rollback_generation_id=active.generation_epoch,
            source_fingerprint=_digest(
                {
                    "split_manifest": active.manifest_sha256,
                    "rollback_candidate": evidence[
                        "candidate_fingerprint"
                    ],
                }
            ),
            created_at=_utc_now(),
        )
        atomic_write_manifest(
            self.registry.manifest_path,
            rollback_manifest,
        )
        readback = self.registry.load(require_files=True)
        if readback.manifest_sha256 != rollback_manifest.manifest_sha256:
            raise FinanceStorageMigrationError(
                "rollback manifest readback mismatch"
            )
        result: dict[str, Any] = {
            "contract_version": ROLLBACK_RESULT_CONTRACT,
            "status": "rollback_complete",
            "plan_fingerprint": exact,
            "approval_reference": str(approval_reference).strip(),
            "manifest": manifest_payload(readback),
            "raw_replay": raw_replay,
            "raw_readback": {
                "row_count": actual_raw.row_count,
                "logical_digest": actual_raw.digest,
            },
            "operational_recopy": operational_recopy,
            "global_manifest_switched": True,
            "canonical_source": "monolith",
            "restart_required": ["wb-core-registry-http.service"],
            "original_monolith_retained": (
                self.runtime_dir / MONOLITH_FILENAME
            ).is_file(),
            "split_generation_retained": (
                raw_path.is_file() and operational_path.is_file()
            ),
            "retirement_authorized": False,
        }
        result["evidence_fingerprint"] = _digest(result)
        result_path = candidate_path.parent / "rollback_evidence.json"
        _atomic_write_json(result_path, result)
        return {**result, "evidence_path": str(result_path)}
