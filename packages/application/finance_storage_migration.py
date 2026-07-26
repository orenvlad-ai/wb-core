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
    OPERATIONAL_SCHEMA_TABLES,
    RAW_SCHEMA_TABLES,
    bind_generation_identity,
    ensure_operational_schema,
    ensure_raw_schema,
)
from packages.application.storage_registry import (
    MANIFEST_FILENAME,
    MONOLITH_FILENAME,
    StoreRegistry,
    atomic_write_manifest,
    build_manifest,
    explain_query_plan,
    manifest_payload,
)
from packages.application.warehouse_functional_lock import (
    WarehouseFunctionalBusyError,
    warehouse_functional_write_lock,
)


PLAN_CONTRACT = "wb_core_finance_storage_split_plan_v1"
MIGRATION_CONTRACT = "wb_core_finance_storage_split_candidate_v1"
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
    "wb-core-finance-weekly-sync.service",
    "wb-core-finance-weekly-sync.timer",
    "wb-core-warehouse-functional-sync.service",
    "wb-core-warehouse-functional-sync.timer",
    "wb-core-sheet-vitrina-refresh.service",
    "wb-core-sheet-vitrina-refresh.timer",
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
            flags = 0
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
                    "access_mode": {0: "read_only", 1: "write_only", 2: "read_write"}.get(
                        flags & os.O_ACCMODE, "unknown"
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


class FinanceStorageMigrationPlanner:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        chunk_size: int = 10_000,
        deployed_sha: str = "",
        repo_root: Path | None = None,
        require_exact_allocations: bool = True,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.registry = StoreRegistry(self.runtime_dir)
        self.chunk_size = int(chunk_size)
        self.deployed_sha = str(deployed_sha or "").strip()
        self.repo_root = Path(repo_root).resolve() if repo_root else None
        self.require_exact_allocations = bool(require_exact_allocations)
        if self.chunk_size <= 0 or self.chunk_size > 500_000:
            raise FinanceStorageMigrationError("chunk_size must be within 1..500000")

    def build_plan(self) -> dict[str, Any]:
        plan_started = time.monotonic()
        manifest = self.registry.load()
        if manifest.state != "monolith" or manifest.canonical_source != "monolith":
            raise FinanceStorageMigrationError("dry-run requires the canonical monolith generation")
        source = self.registry.resolve("operational", manifest=manifest)
        before_stat = source.stat()
        with self.registry.session(
            "operational",
            mode="ro",
            operation="finance_storage_split_dry_run",
            timeout_ms=60_000,
            isolation_level=None,
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
                "logical_store": "monolith",
                "identity": source_identity,
                "fingerprint": source_fingerprint,
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
                    not source_file_drift_observed
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

    def _maintenance_hold_evidence(self) -> dict[str, Any]:
        state_path = self.planner.runtime_dir / ".business-data-maintenance.json"
        if not state_path.is_file():
            raise FinanceStorageMigrationError(
                "canonical business-data maintenance hold is required before candidate apply"
            )
        if state_path.stat().st_mode & 0o077:
            raise FinanceStorageMigrationError(
                "business-data maintenance state must be private mode 0600"
            )
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FinanceStorageMigrationError(
                "business-data maintenance state is unreadable"
            ) from exc
        if (
            not isinstance(state, dict)
            or str(state.get("schema_version") or "")
            != "business_data_maintenance_v1"
            or str(state.get("phase") or "") != "held"
            or not bool((state.get("hold_readback") or {}).get("quiet"))
        ):
            raise FinanceStorageMigrationError(
                "business-data maintenance state does not prove an active quiet hold"
            )
        return {
            "schema_version": state["schema_version"],
            "phase": state["phase"],
            "held_at": str(state.get("held_at") or ""),
            "quiet": True,
            "state_path": str(state_path),
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
        source_path = self.planner.registry.resolve("operational")
        maintenance_evidence = self._maintenance_hold_evidence()
        try:
            warehouse_lock = warehouse_functional_write_lock(
                self.planner.runtime_dir,
                blocking=False,
            )
            warehouse_lock.__enter__()
        except WarehouseFunctionalBusyError as exc:
            raise FinanceStorageMigrationError(
                "warehouse functional writer lock is busy"
            ) from exc
        try:
            return self._apply_under_locks(
                current_plan=current_plan,
                generation=generation,
                candidate=candidate,
                candidate_root=candidate_root,
                raw_path=raw_path,
                operational_path=operational_path,
                source_path=source_path,
                maintenance_evidence=maintenance_evidence,
            )
        finally:
            warehouse_lock.__exit__(None, None, None)

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
        maintenance_evidence: dict[str, Any],
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
                       batch_id,source_identity,source_sha256,report_period,row_count,
                       rows_digest,status,created_at,committed_at
                       ) VALUES(?,?,?,?,?,?,'loading',?,NULL)""",
                    (
                        batch_id,
                        plan["source"]["fingerprint"],
                        plan["source"]["fingerprint"],
                        (
                            str(plan["raw"]["watermarks"].get("min_week_start") or "")
                            + "/"
                            + str(plan["raw"]["watermarks"].get("max_week_end") or "")
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
                    "business_data_maintenance": maintenance_evidence,
                    "warehouse_functional_lock_held": True,
                    "candidate_manifest_path": str(candidate_manifest_path),
                    "candidate_manifest": manifest_payload(candidate_manifest),
                    "raw_row_count": raw_readback.row_count,
                    "raw_destination_size_bytes": raw_path.stat().st_size,
                    "operational_destination_size_bytes": operational_path.stat().st_size,
                    "old_monolith_unchanged": (
                        source_path.stat().st_size == plan["source"]["identity"]["size_bytes"]
                        and source_path.stat().st_ino == plan["source"]["identity"]["inode"]
                    ),
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
