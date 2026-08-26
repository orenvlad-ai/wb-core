"""Post-cutover Finance backup rotation with one verified restore set.

The runner deliberately shares the canonical Finance snapshot-retention root,
lock, plan/result contracts and audit.  A replacement is copied directly to
the dedicated backup mount, verified, selected atomically and only then are
superseded artifacts released.  The original monolith and every split
generation are outside this module's deletion allowlist.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat as stat_module
from typing import Any, Mapping

from packages.application.business_data_write_barrier import barrier_status
from packages.application.finance_storage_migration import (
    SHADOW_STATE_CONTRACT,
    SHADOW_STATE_FILENAME,
    logical_table_digest,
)
from packages.application.finance_storage_snapshot_retention import (
    ARCHIVE_CONTRACT,
    ARCHIVE_MANIFEST_FILENAME,
    ARCHIVE_RELATIVE_ROOT,
    AUDIT_FILENAME,
    FinanceStorageSnapshotRetention,
    FinanceStorageSnapshotRetentionError,
    LOCK_FILENAME,
    PLAN_CONTRACT,
    RESULT_CONTRACT,
    SNAPSHOT_DIRECTORY,
    TRANSACTION_CONTRACT as LEGACY_TRANSACTION_CONTRACT,
    TRANSACTION_FILENAME as LEGACY_TRANSACTION_FILENAME,
    _append_audit,
    _atomic_write_json,
    _canonical_json,
    _file_identity,
    _fingerprint,
    _fsync_directory,
    _load_json,
    _openers_below,
    _sha256_file,
)
from packages.application.finance_raw_storage import CONSUMER_ID, storage_health
from packages.application.root_storage_policy import (
    admit_root_write,
    predict_sqlite_backup_bytes,
)
from packages.application.storage_registry import (
    StoreRegistry,
    build_manifest,
    manifest_payload,
)


BACKUP_SET_CONTRACT = "wb_core_finance_storage_backup_set_v1"
CURRENT_CONTRACT = "wb_core_finance_storage_backup_current_v1"
POLICY_CONTRACT = "wb_core_finance_storage_backup_policy_v1"
TRANSACTION_CONTRACT = "wb_core_finance_storage_backup_transaction_v1"
STRATEGY = "post_cutover_atomic_replace_v1"
RETAINED_DIRECTORY = "retained"
TRANSACTIONS_DIRECTORY = "transactions"
CURRENT_FILENAME = "current.json"
POLICY_FILENAME = "retention_policy.json"
BACKUP_MANIFEST_FILENAME = "backup_manifest.json"
SOURCE_MANIFEST_FILENAME = "storage_generation_manifest.json"
RAW_BACKUP_FILENAME = "finance_raw.sqlite3"
OPERATIONAL_BACKUP_FILENAME = "operational.sqlite3"
DEFAULT_RETAINED_COUNT = 1
DEFAULT_TEMPORARY_COUNT = 2
DEFAULT_MAX_SET_BYTES = 32 * 1024**3
DEFAULT_COPY_OVERHEAD_BYTES = 64 * 1024**2
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MIN_REPLACEMENT_INTERVAL_SECONDS = 6 * 24 * 60 * 60
DEFAULT_HARD_RESERVE_BYTES = 8 * 1024**3
DEFAULT_DEGRADED_AVAILABLE_BYTES = 30 * 1024**3
DEFAULT_ROOT_TARGET_BYTES = 40_000_000_000
DEFAULT_BACKUP_TARGET_BYTES = 60_000_000_000
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_BACKUP_ID_RE = re.compile(r"finance-backup-[0-9a-f]{20}")
_LEGACY_ID_RE = re.compile(r"finance-split-[0-9a-f]{20}")
_BACKUP_FILES = {
    RAW_BACKUP_FILENAME,
    OPERATIONAL_BACKUP_FILENAME,
    SOURCE_MANIFEST_FILENAME,
    BACKUP_MANIFEST_FILENAME,
}
_ROOT_ALLOWED_FILES = {
    "monolith.sqlite3",
    "monolith.sqlite3-wal",
    "monolith.sqlite3-shm",
    "snapshot_manifest.json",
    "snapshot_capture_intent.json",
}
_ARCHIVE_ALLOWED_FILES = _ROOT_ALLOWED_FILES | {
    ARCHIVE_MANIFEST_FILENAME,
    LEGACY_TRANSACTION_FILENAME,
}


class FinanceStorageBackupRotationError(FinanceStorageSnapshotRetentionError):
    """The post-cutover backup boundary is unsafe or ambiguous."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    exact = str(value or "").strip()
    if exact.endswith("Z"):
        exact = exact[:-1] + "+00:00"
    parsed = datetime.fromisoformat(exact)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _filesystem(path: Path) -> dict[str, int]:
    stats = os.statvfs(path)
    return {
        "device": int(path.stat().st_dev),
        "available_bytes": int(stats.f_bavail) * int(stats.f_frsize),
        "capacity_bytes": int(stats.f_blocks) * int(stats.f_frsize),
        "free_inodes": int(stats.f_favail),
        "total_inodes": int(stats.f_files),
    }


def _directory_identity(path: Path) -> dict[str, int | str]:
    if path.is_symlink() or not path.is_dir():
        raise FinanceStorageBackupRotationError(f"directory is unsafe: {path}")
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "uid": int(stat.st_uid),
        "gid": int(stat.st_gid),
        "mode": int(stat.st_mode & 0o777),
    }


def _protected_path_identity(path: Path) -> dict[str, Any]:
    """Capture a bounded non-target identity without following symlinks."""

    def entry_identity(item: Path) -> dict[str, Any]:
        item_stat = item.lstat()
        if stat_module.S_ISLNK(item_stat.st_mode):
            kind = "symlink"
        elif stat_module.S_ISDIR(item_stat.st_mode):
            kind = "directory"
        elif stat_module.S_ISREG(item_stat.st_mode):
            kind = "file"
        else:
            kind = "other"
        identity: dict[str, Any] = {
            "name": item.name,
            "kind": kind,
            "device": int(item_stat.st_dev),
            "inode": int(item_stat.st_ino),
            "size_bytes": int(item_stat.st_size),
            "mtime_ns": int(item_stat.st_mtime_ns),
            "mode": int(item_stat.st_mode & 0o777),
            "uid": int(item_stat.st_uid),
            "gid": int(item_stat.st_gid),
        }
        if kind == "symlink":
            identity["link_target"] = os.readlink(item)
        return identity

    root = entry_identity(path)
    children: list[dict[str, Any]] = []
    if root["kind"] == "directory":
        children = [
            entry_identity(child)
            for child in sorted(path.iterdir(), key=lambda child: child.name)
        ]
    payload = {"root": root, "children": children}
    payload["fingerprint"] = _fingerprint(payload)
    return payload


def _protected_record(*, family: str, path: Path, reason: str) -> dict[str, Any]:
    return {
        "family": family,
        "path": str(path),
        "reason": reason,
        "protected_identity": _protected_path_identity(path),
    }


def _sqlite_source_identity(path: Path) -> dict[str, Any]:
    identity = _file_identity(path, include_sha256=False)
    sidecars: list[dict[str, Any]] = []
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink():
            raise FinanceStorageBackupRotationError(
                f"canonical SQLite sidecar is a symlink: {sidecar}"
            )
        if sidecar.is_file():
            sidecars.append(_file_identity(sidecar, include_sha256=False))
    identity["sidecars"] = sidecars
    return identity


def _mount_observation(path: Path) -> dict[str, Any]:
    exact = path.resolve()
    best: dict[str, Any] | None = None
    mountinfo = Path("/proc/self/mountinfo")
    if mountinfo.is_file():
        for line in mountinfo.read_text(encoding="utf-8").splitlines():
            left, separator, right = line.partition(" - ")
            if not separator:
                continue
            fields = left.split()
            after = right.split()
            if len(fields) < 6 or len(after) < 2:
                continue
            mount_path = Path(fields[4].replace("\\040", " ")).resolve()
            try:
                exact.relative_to(mount_path)
            except ValueError:
                continue
            candidate = {
                "mount_path": str(mount_path),
                "major_minor": fields[2],
                "mount_options": sorted(fields[5].split(",")),
                "filesystem": after[0],
                "source": after[1],
                "super_options": sorted(after[2].split(",")) if len(after) > 2 else [],
            }
            if best is None or len(mount_path.parts) > len(
                Path(best["mount_path"]).parts
            ):
                best = candidate
    if best is None:
        best = {
            "mount_path": str(exact),
            "major_minor": "",
            "mount_options": [],
            "filesystem": "",
            "source": "",
            "super_options": [],
        }
    best["device"] = int(exact.stat().st_dev)
    best["is_exact_mountpoint"] = Path(best["mount_path"]) == exact
    source = Path(str(best.get("source") or ""))
    source_device = None
    try:
        if source.is_block_device():
            source_device = int(source.stat().st_rdev)
    except OSError:
        source_device = None
    for kind, directory in (
        ("filesystem_uuid", Path("/dev/disk/by-uuid")),
        ("filesystem_label", Path("/dev/disk/by-label")),
    ):
        values: list[str] = []
        if source_device is not None and directory.is_dir():
            for candidate in directory.iterdir():
                try:
                    if (
                        int(candidate.resolve(strict=True).stat().st_rdev)
                        == source_device
                    ):
                        values.append(candidate.name)
                except (FileNotFoundError, OSError):
                    continue
        best[kind] = sorted(values)[0] if values else ""
    return best


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]


def _logical_inventory(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return {
        table: asdict(logical_table_digest(conn, table)) for table in _table_names(conn)
    }


def _sqlite_readback(
    path: Path,
    *,
    include_logical: bool,
    immutable: bool = False,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FinanceStorageBackupRotationError(f"SQLite backup is unsafe: {path}")
    if immutable:
        wal_path = Path(str(path) + "-wal")
        if wal_path.is_symlink() or (
            wal_path.exists()
            and (not wal_path.is_file() or wal_path.stat().st_size != 0)
        ):
            raise FinanceStorageBackupRotationError(
                f"immutable SQLite readback requires an absent or empty WAL: {path}"
            )
    connection: sqlite3.Connection | None = None
    try:
        immutable_query = "&immutable=1" if immutable else ""
        connection = sqlite3.connect(
            f"file:{path}?mode=ro{immutable_query}", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        foreign_keys = [
            list(row) for row in connection.execute("PRAGMA foreign_key_check")
        ]
        if query_only != 1 or integrity != ["ok"] or foreign_keys:
            raise FinanceStorageBackupRotationError(
                f"SQLite integrity readback failed: {path}"
            )
        logical = _logical_inventory(connection) if include_logical else {}
        return {
            "query_only": True,
            "integrity_check": "ok",
            "foreign_key_violation_count": 0,
            "logical_tables": logical,
        }
    except sqlite3.Error as exc:
        raise FinanceStorageBackupRotationError(
            f"SQLite integrity readback failed: {path}: {type(exc).__name__}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _copy_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    if source.is_symlink() or not source.is_file() or destination.exists():
        raise FinanceStorageBackupRotationError("SQLite copy boundary is unsafe")
    captured_at = _utc_now()
    source_identity = _sqlite_source_identity(source)
    admission = admit_root_write(
        owner="finance_post_cutover_backup_rotation",
        destination=destination,
        predicted_output_bytes=predict_sqlite_backup_bytes(source),
    )
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    source_connection.row_factory = sqlite3.Row
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.execute("PRAGMA query_only=ON")
        source_connection.execute("BEGIN")
        source_connection.backup(destination_connection, pages=8192, sleep=0.05)
        source_logical = _logical_inventory(source_connection)
        source_connection.rollback()
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(destination, 0o600)
    descriptor = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    readback = _sqlite_readback(destination, include_logical=True)
    if readback["logical_tables"] != source_logical:
        raise FinanceStorageBackupRotationError(
            f"SQLite logical digest drifted during copy: {source}"
        )
    identity = _file_identity(destination, include_sha256=True)
    return {
        "source_path": str(source),
        "source_file": source_identity,
        "captured_at": captured_at,
        "file": identity,
        "query_only_source": True,
        "integrity_check": "ok",
        "foreign_key_violation_count": 0,
        "logical_tables": source_logical,
        "root_storage_admission": admission,
    }


def _restore_watermarks(raw_path: Path, operational_path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(f"file:{raw_path}?mode=ro", uri=True)) as raw:
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA query_only=ON")
        latest_outbox = int(
            raw.execute(
                "SELECT COALESCE(MAX(sequence_no),0) FROM finance_raw_outbox"
            ).fetchone()[0]
        )
        raw_cursor_row = raw.execute(
            "SELECT last_sequence_no FROM finance_raw_consumer_cursors "
            "WHERE consumer_id=?",
            (CONSUMER_ID,),
        ).fetchone()
        raw_cursor = int(raw_cursor_row[0]) if raw_cursor_row else 0
        bridge_cursor = int(
            raw.execute(
                "SELECT COALESCE(MAX(last_sequence_no),0) "
                "FROM finance_raw_bridge_cursors"
            ).fetchone()[0]
        )
        raw_rows = int(
            raw.execute("SELECT COUNT(*) FROM finance_raw_rows").fetchone()[0]
        )
        raw_batches = int(
            raw.execute(
                "SELECT COUNT(*) FROM finance_raw_ingest_batches "
                "WHERE status='committed'"
            ).fetchone()[0]
        )
        pending_outbox = int(
            raw.execute(
                "SELECT COUNT(*) FROM finance_raw_outbox WHERE published_at IS NULL"
            ).fetchone()[0]
        )
    with closing(
        sqlite3.connect(f"file:{operational_path}?mode=ro", uri=True)
    ) as operational:
        operational.row_factory = sqlite3.Row
        operational.execute("PRAGMA query_only=ON")
        operational_cursor_row = operational.execute(
            "SELECT last_sequence_no FROM finance_operational_consumer_cursors "
            "WHERE consumer_id=?",
            (CONSUMER_ID,),
        ).fetchone()
        operational_cursor = (
            int(operational_cursor_row[0]) if operational_cursor_row else 0
        )
        mismatch_count = int(
            operational.execute(
                "SELECT COUNT(*) FROM finance_storage_shadow_comparisons "
                "WHERE status='mismatch'"
            ).fetchone()[0]
        )
        actionable_dead_letters = int(
            operational.execute(
                "SELECT COUNT(*) FROM finance_operational_dead_letters "
                "WHERE status='action_required'"
            ).fetchone()[0]
        )
    watermarks = {
        "latest_outbox_sequence": latest_outbox,
        "raw_ack_cursor": raw_cursor,
        "live_tail_cursor": bridge_cursor,
        "live_tail_applicable": False,
        "operational_cursor": operational_cursor,
        "raw_rows": raw_rows,
        "raw_batches": raw_batches,
        "pending_outbox": pending_outbox,
        "consumer_lag_events": max(0, latest_outbox - operational_cursor),
        "live_tail_lag_events": 0,
        "cursor_mismatch": raw_cursor != operational_cursor,
        "shadow_mismatch_count": mismatch_count,
        "actionable_dead_letters": actionable_dead_letters,
    }
    if (
        pending_outbox != 0
        or latest_outbox != raw_cursor
        or raw_cursor != operational_cursor
        or mismatch_count != 0
        or actionable_dead_letters != 0
    ):
        raise FinanceStorageBackupRotationError(
            "isolated restore watermarks are not a coherent zero-lag boundary"
        )
    return watermarks


def _candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    return _fingerprint(
        {
            key: value
            for key, value in candidate.items()
            if key not in {"candidate_fingerprint", "integrity_readback"}
        }
    )


def _audit_contains(
    path: Path, *, plan_fingerprint: str, result_fingerprint: str
) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FinanceStorageBackupRotationError(
                "Finance backup audit contains invalid JSON"
            ) from exc
        if (
            isinstance(payload, Mapping)
            and str(payload.get("event") or "") == "finance_backup_rotation_completed"
            and str(payload.get("plan_fingerprint") or "") == plan_fingerprint
            and str(payload.get("result_fingerprint") or "") == result_fingerprint
        ):
            return True
    return False


def _audit_contains_supersession(
    path: Path, *, source_plan_fingerprint: str, target_plan_fingerprint: str
) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FinanceStorageBackupRotationError(
                "Finance backup audit contains invalid JSON"
            ) from exc
        if (
            isinstance(payload, Mapping)
            and payload.get("event")
            == "finance_backup_pre_mutation_transaction_superseded"
            and payload.get("source_plan_fingerprint")
            == source_plan_fingerprint
            and payload.get("target_plan_fingerprint")
            == target_plan_fingerprint
        ):
            return True
    return False


class FinanceStorageBackupRotation:
    """Build and execute an exact post-cutover replacement transaction."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        deployed_sha: str,
        backup_root: Path | None = None,
        require_distinct_device: bool = True,
        require_backup_mountpoint: bool = True,
        root_target_bytes: int = DEFAULT_ROOT_TARGET_BYTES,
        backup_target_bytes: int = DEFAULT_BACKUP_TARGET_BYTES,
        hard_reserve_bytes: int = DEFAULT_HARD_RESERVE_BYTES,
        degraded_available_bytes: int = DEFAULT_DEGRADED_AVAILABLE_BYTES,
        max_set_bytes: int = DEFAULT_MAX_SET_BYTES,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
        minimum_replacement_interval_seconds: int = DEFAULT_MIN_REPLACEMENT_INTERVAL_SECONDS,
        now_factory: Any = _utc_now,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.deployed_sha = str(deployed_sha or "").strip().lower()
        self.snapshot_root = (self.runtime_dir / SNAPSHOT_DIRECTORY).resolve()
        self.backup_root = (
            Path(backup_root).expanduser().resolve()
            if backup_root is not None
            else (self.runtime_dir / ARCHIVE_RELATIVE_ROOT).resolve()
        )
        self.backup_mount = self.backup_root.parent
        self.require_distinct_device = bool(require_distinct_device)
        self.require_backup_mountpoint = bool(require_backup_mountpoint)
        self.root_target_bytes = int(root_target_bytes)
        self.backup_target_bytes = int(backup_target_bytes)
        self.hard_reserve_bytes = int(hard_reserve_bytes)
        self.degraded_available_bytes = int(degraded_available_bytes)
        self.max_set_bytes = int(max_set_bytes)
        self.max_age_seconds = int(max_age_seconds)
        self.minimum_replacement_interval_seconds = int(
            minimum_replacement_interval_seconds
        )
        self.now_factory = now_factory
        if _SHA_RE.fullmatch(self.deployed_sha) is None:
            raise FinanceStorageBackupRotationError("exact deployed SHA is required")
        if any(
            value < 0
            for value in (
                self.root_target_bytes,
                self.backup_target_bytes,
                self.hard_reserve_bytes,
                self.degraded_available_bytes,
                self.max_set_bytes,
                self.max_age_seconds,
                self.minimum_replacement_interval_seconds,
            )
        ):
            raise FinanceStorageBackupRotationError(
                "backup policy limits cannot be negative"
            )
        try:
            self.snapshot_root.relative_to(self.runtime_dir)
            self.backup_root.relative_to(self.runtime_dir)
        except ValueError as exc:
            raise FinanceStorageBackupRotationError(
                "Finance backup roots escape the canonical runtime"
            ) from exc
        self.retained_root = self.backup_root / RETAINED_DIRECTORY
        self.transactions_root = self.backup_root / TRANSACTIONS_DIRECTORY
        self.current_path = self.backup_root / CURRENT_FILENAME
        self.policy_path = self.backup_root / POLICY_FILENAME
        self.audit_path = self.backup_root / AUDIT_FILENAME

    def _guard(self) -> dict[str, Any]:
        registry = StoreRegistry(self.runtime_dir)
        manifest = registry.load(require_files=True)
        if manifest.state != "cutover" or manifest.canonical_source != "split":
            raise FinanceStorageBackupRotationError(
                "post-cutover backup requires the selected canonical split"
            )
        raw = registry.resolve("finance_raw", manifest=manifest)
        operational = registry.resolve("operational", manifest=manifest)
        barrier = barrier_status(self.runtime_dir)
        if barrier.get("active") is True:
            raise FinanceStorageBackupRotationError(
                "Finance backup is blocked by the active business write barrier"
            )
        generation_root = (self.runtime_dir / "generations").resolve()
        for path in (raw, operational):
            try:
                path.relative_to(generation_root)
            except ValueError as exc:
                raise FinanceStorageBackupRotationError(
                    "canonical split path escapes the protected generations root"
                ) from exc
        health = storage_health(registry)
        shadow_path = self.runtime_dir / SHADOW_STATE_FILENAME
        shadow_state: dict[str, Any] = {
            "path": str(shadow_path),
            "present": False,
            "enabled": False,
        }
        if shadow_path.exists():
            if shadow_path.is_symlink() or not shadow_path.is_file():
                raise FinanceStorageBackupRotationError(
                    "post-cutover backup shadow state is unsafe"
                )
            shadow_payload = _load_json(
                shadow_path, label="Finance shadow ingest state"
            )
            if (
                str(shadow_payload.get("contract_version") or "")
                != SHADOW_STATE_CONTRACT
                or shadow_payload.get("enabled") is not False
            ):
                raise FinanceStorageBackupRotationError(
                    "post-cutover backup requires inactive shadow ingest"
                )
            shadow_state = {
                "path": str(shadow_path),
                "present": True,
                "enabled": False,
                "contract_version": SHADOW_STATE_CONTRACT,
                "file": _file_identity(shadow_path, include_sha256=True),
            }
        if (
            health.get("raw_schema_ready") is not True
            or health.get("operational_schema_ready") is not True
            or str(health.get("cursor_contract") or "") != "split_outbox_v1"
            or bool(health.get("raw_health_error"))
            or bool(health.get("operational_health_error"))
            or int(health.get("consumer_lag_events") or 0) != 0
            or int(health.get("live_tail_lag_events") or 0) != 0
            or health.get("live_tail_applicable") is not False
            or health.get("cursor_mismatch") is not False
            or int(health.get("shadow_mismatch_count") or 0) != 0
            or int(health.get("actionable_dead_letters") or 0) != 0
            or int((health.get("raw_counts") or {}).get("pending_outbox") or 0) != 0
        ):
            raise FinanceStorageBackupRotationError(
                "canonical split is not at a coherent zero-lag backup boundary"
            )
        return {
            "state": manifest.state,
            "canonical_source": manifest.canonical_source,
            "generation_epoch": manifest.generation_epoch,
            "manifest_sha256": manifest.manifest_sha256,
            "manifest": manifest_payload(manifest),
            "barrier": {
                "active": False,
                "phase": str(barrier.get("phase") or ""),
                "window_id": str(barrier.get("window_id") or ""),
            },
            "source_fingerprint": manifest.source_fingerprint,
            "shadow_state": shadow_state,
            "raw": {
                "path": str(raw),
                **_sqlite_source_identity(raw),
                "schema_revision": manifest.raw.schema_revision,
                "watermark": manifest.raw.watermark,
                "openers": list((health.get("raw") or {}).get("openers") or []),
            },
            "operational": {
                "path": str(operational),
                **_sqlite_source_identity(operational),
                "schema_revision": manifest.operational.schema_revision,
                "watermark": manifest.operational.watermark,
                "openers": list((health.get("operational") or {}).get("openers") or []),
            },
            "watermarks": {
                "latest_outbox_sequence": int(
                    health.get("latest_outbox_sequence") or 0
                ),
                "raw_ack_cursor": int(health.get("raw_ack_cursor") or 0),
                "live_tail_cursor": int(health.get("live_tail_cursor") or 0),
                "live_tail_applicable": False,
                "operational_cursor": int(health.get("operational_cursor") or 0),
                "raw_rows": int((health.get("raw_counts") or {}).get("rows") or 0),
                "raw_batches": int(
                    (health.get("raw_counts") or {}).get("batches") or 0
                ),
                "pending_outbox": 0,
                "consumer_lag_events": 0,
                "live_tail_lag_events": 0,
                "cursor_mismatch": False,
                "shadow_mismatch_count": 0,
                "actionable_dead_letters": 0,
            },
            "protected_generation_root": str(generation_root),
            "protected_original_monolith": str(
                self.runtime_dir / "registry_upload_runtime.sqlite3"
            ),
        }

    def _device_guard(self) -> dict[str, Any]:
        if (
            not self.backup_mount.is_dir()
            or self.backup_mount.is_symlink()
            or self.backup_root.is_symlink()
            or (self.backup_root.exists() and not self.backup_root.is_dir())
        ):
            raise FinanceStorageBackupRotationError(
                "dedicated Finance backup mount is missing or unsafe"
            )
        runtime_device = int(self.runtime_dir.stat().st_dev)
        backup_device = int(self.backup_mount.stat().st_dev)
        mount = _mount_observation(self.backup_mount)
        if self.require_distinct_device and runtime_device == backup_device:
            raise FinanceStorageBackupRotationError(
                "Finance backup must use a distinct device"
            )
        if self.require_backup_mountpoint and (
            mount["is_exact_mountpoint"] is not True
            or not os.path.ismount(self.backup_mount)
        ):
            raise FinanceStorageBackupRotationError(
                "Finance backup mount is absent; root fallback is forbidden"
            )
        return {
            "runtime_device": runtime_device,
            "backup_device": backup_device,
            "distinct_device": runtime_device != backup_device,
            "mount": mount,
        }

    def _protected_non_target_identity(self) -> dict[str, Any]:
        monolith = self.runtime_dir / "registry_upload_runtime.sqlite3"
        monolith_identity = (
            _file_identity(monolith, include_sha256=True)
            if monolith.is_file() and not monolith.is_symlink()
            else {"path": str(monolith), "exists": False}
        )
        generations = self.runtime_dir / "generations"
        entries: list[dict[str, Any]] = []
        if generations.is_dir() and not generations.is_symlink():
            for path in sorted(
                generations.rglob("*"),
                key=lambda item: str(item.relative_to(generations)),
            ):
                if path.is_symlink():
                    raise FinanceStorageBackupRotationError(
                        "protected generation inventory contains a symlink"
                    )
                if path.is_dir() or (
                    path.is_file()
                    and (path.name.endswith(".sqlite3") or path.name.endswith(".json"))
                ):
                    stat = path.stat()
                    entries.append(
                        {
                            "relative_path": str(path.relative_to(generations)),
                            "kind": "directory" if path.is_dir() else "file",
                            "device": int(stat.st_dev),
                            "inode": int(stat.st_ino),
                        }
                    )
        structural_digest = _fingerprint(entries)
        return {
            "original_monolith": monolith_identity,
            "generation_root": str(generations.resolve()),
            "generation_entries": entries,
            "generation_structural_digest": structural_digest,
        }

    def _assert_inventory_cas(self, plan: Mapping[str, Any]) -> None:
        inventory = dict(plan.get("inventory") or {})
        protected = [
            dict(item)
            for item in inventory.get("protected") or []
            if isinstance(item, Mapping)
        ]
        planned_root_names = {
            Path(str(item.get("path") or "")).name
            for item in inventory.get("root_legacy") or []
        }
        planned_root_names.update(
            Path(str(item.get("path") or "")).name
            for item in protected
            if Path(str(item.get("path") or "")).parent == self.snapshot_root
        )
        current_root_names = (
            {item.name for item in self.snapshot_root.iterdir()}
            if self.snapshot_root.is_dir()
            else set()
        )
        if not current_root_names.issubset(planned_root_names):
            raise FinanceStorageBackupRotationError(
                "canonical root snapshot inventory CAS drifted"
            )
        planned_backup_legacy = {
            Path(str(item.get("path") or "")).name
            for item in inventory.get("backup_legacy") or []
        }
        planned_backup_foreign = {
            Path(str(item.get("path") or "")).name
            for item in protected
            if Path(str(item.get("path") or "")).parent == self.backup_root
        }
        allowed_controls = {
            RETAINED_DIRECTORY,
            TRANSACTIONS_DIRECTORY,
            CURRENT_FILENAME,
            POLICY_FILENAME,
            AUDIT_FILENAME,
        }
        current_backup_names = (
            {item.name for item in self.backup_root.iterdir()}
            if self.backup_root.is_dir()
            else set()
        )
        if not current_backup_names.issubset(
            planned_backup_legacy | planned_backup_foreign | allowed_controls
        ):
            raise FinanceStorageBackupRotationError(
                "canonical backup snapshot inventory CAS drifted"
            )
        planned_retained = {
            str(item.get("artifact_id") or "")
            for item in inventory.get("retained") or []
        }
        backup_id = str(plan.get("backup_id") or "")
        allowed_retained = planned_retained | {
            backup_id,
            f".{backup_id}.partial",
        }
        current_retained = (
            {item.name for item in self.retained_root.iterdir()}
            if self.retained_root.is_dir()
            else set()
        )
        if not current_retained.issubset(allowed_retained):
            raise FinanceStorageBackupRotationError(
                "retained backup inventory CAS drifted"
            )
        if self.transactions_root.is_dir():
            unknown_transactions = [
                item.name
                for item in self.transactions_root.iterdir()
                if (
                    item.is_symlink()
                    or not item.is_file()
                    or re.fullmatch(r"[0-9a-f]{64}\.json", item.name) is None
                )
            ]
            if unknown_transactions:
                raise FinanceStorageBackupRotationError(
                    "Finance backup transaction inventory has foreign entries"
                )
        for item in protected:
            path = Path(str(item.get("path") or ""))
            expected = item.get("protected_identity")
            if (
                not isinstance(expected, Mapping)
                or (not path.exists() and not path.is_symlink())
                or _protected_path_identity(path) != dict(expected)
            ):
                raise FinanceStorageBackupRotationError(
                    "protected Finance snapshot inventory CAS drifted"
                )

    @staticmethod
    def _files(path: Path, *, allowed: set[str]) -> list[dict[str, Any]]:
        if path.is_symlink() or not path.is_dir():
            raise FinanceStorageBackupRotationError(
                f"backup artifact is unsafe: {path}"
            )
        names = sorted(item.name for item in path.iterdir())
        unknown = sorted(set(names) - allowed)
        if unknown:
            raise FinanceStorageBackupRotationError(
                f"backup artifact has unknown files: {path}: {unknown}"
            )
        files: list[dict[str, Any]] = []
        for name in names:
            item_path = path / name
            identity = _file_identity(item_path, include_sha256=True)
            stat = item_path.stat()
            identity.update({"uid": int(stat.st_uid), "gid": int(stat.st_gid)})
            files.append(identity)
        return files

    def _root_candidate(self, path: Path) -> dict[str, Any]:
        legacy = FinanceStorageSnapshotRetention(
            self.runtime_dir,
            deployed_sha=self.deployed_sha,
            backup_root=self.backup_root,
            backup_reserve_bytes=0,
            minimum_root_free_bytes=0,
            require_distinct_device=False,
        )
        candidate = legacy._snapshot_candidate(path, include_sha256=True)
        integrity = None
        if candidate["snapshot_status"] != "integrity_verified":
            integrity = _sqlite_readback(
                path / "monolith.sqlite3",
                include_logical=False,
                immutable=True,
            )
        files: list[dict[str, Any]] = []
        for item in candidate["files"]:
            item_path = path / str(item["name"])
            stat = item_path.stat()
            files.append({**item, "uid": int(stat.st_uid), "gid": int(stat.st_gid)})
        result = {
            "family": "root_legacy_snapshot",
            "artifact_id": candidate["snapshot_id"],
            "path": candidate["source_path"],
            "directory_identity": _directory_identity(path),
            "files": files,
            "total_bytes": candidate["total_bytes"],
            "allocated_bytes": candidate["allocated_bytes"],
            "captured_at": "",
            "snapshot_status": candidate["snapshot_status"],
            "snapshot_deployed_sha": candidate["snapshot_deployed_sha"],
            "integrity_proven": bool(
                candidate["snapshot_status"] == "integrity_verified" or integrity
            ),
            "integrity_readback": integrity,
            "deletion_allowed": (
                str(candidate["snapshot_deployed_sha"]) != self.deployed_sha
            ),
        }
        manifest = _load_json(
            path / "snapshot_manifest.json", label="root snapshot manifest"
        )
        captured_at = str(
            manifest.get("integrity_verified_at")
            or manifest.get("captured_at")
            or manifest.get("created_at")
            or ""
        )
        try:
            _parse_time(captured_at)
        except ValueError as exc:
            raise FinanceStorageBackupRotationError(
                "root snapshot capture time is invalid"
            ) from exc
        result["captured_at"] = captured_at
        result["candidate_fingerprint"] = _candidate_fingerprint(result)
        return result

    def _archive_candidate(self, path: Path) -> dict[str, Any]:
        if _LEGACY_ID_RE.fullmatch(path.name) is None:
            raise FinanceStorageBackupRotationError("legacy archive id is invalid")
        files = self._files(path, allowed=_ARCHIVE_ALLOWED_FILES)
        names = {item["name"] for item in files}
        if not {
            "monolith.sqlite3",
            "snapshot_manifest.json",
            ARCHIVE_MANIFEST_FILENAME,
            LEGACY_TRANSACTION_FILENAME,
        }.issubset(names):
            raise FinanceStorageBackupRotationError("legacy archive is incomplete")
        archive_manifest = _load_json(
            path / ARCHIVE_MANIFEST_FILENAME, label="legacy archive manifest"
        )
        stable_archive = {
            key: value
            for key, value in archive_manifest.items()
            if key != "fingerprint"
        }
        transaction = _load_json(
            path / LEGACY_TRANSACTION_FILENAME, label="legacy archive transaction"
        )
        snapshot_manifest = _load_json(
            path / "snapshot_manifest.json", label="legacy snapshot manifest"
        )
        stable_snapshot = {
            key: value
            for key, value in snapshot_manifest.items()
            if key != "evidence_fingerprint"
        }
        declared_files = {
            str(item.get("name") or ""): item
            for item in archive_manifest.get("files") or []
            if isinstance(item, Mapping)
        }
        actual_files = {item["name"]: item for item in files}
        for name, expected in declared_files.items():
            actual = actual_files.get(name)
            if (
                actual is None
                or int(actual["size_bytes"])
                != int(
                    expected["size_bytes"]
                    if "size_bytes" in expected
                    else -1
                )
                or str(actual["sha256"]) != str(expected.get("sha256") or "")
            ):
                raise FinanceStorageBackupRotationError(
                    f"legacy archive byte manifest drifted: {path}"
                )
        if (
            str(archive_manifest.get("contract_version") or "") != ARCHIVE_CONTRACT
            or str(archive_manifest.get("snapshot_id") or "") != path.name
            or str(archive_manifest.get("status") or "") != "archive_verified"
            or archive_manifest.get("source_release_completed") is not True
            or str(archive_manifest.get("fingerprint") or "")
            != _fingerprint(stable_archive)
            or str(transaction.get("phase") or "") != "source_released"
            or str(transaction.get("contract_version") or "")
            != LEGACY_TRANSACTION_CONTRACT
            or str(transaction.get("archive_manifest_fingerprint") or "")
            != str(archive_manifest.get("fingerprint") or "")
            or str(snapshot_manifest.get("snapshot_id") or "") != path.name
            or str(snapshot_manifest.get("status") or "")
            not in {"captured_unverified", "integrity_verified"}
            or _SHA_RE.fullmatch(str(snapshot_manifest.get("deployed_sha") or ""))
            is None
            or str(snapshot_manifest.get("evidence_fingerprint") or "")
            != _fingerprint(stable_snapshot)
        ):
            raise FinanceStorageBackupRotationError(
                "legacy archive evidence is invalid"
            )
        status = str(snapshot_manifest.get("status") or "")
        integrity = None
        if status != "integrity_verified":
            integrity = _sqlite_readback(
                path / "monolith.sqlite3",
                include_logical=False,
                immutable=True,
            )
        captured_at = str(
            archive_manifest.get("verified_at")
            or snapshot_manifest.get("integrity_verified_at")
            or snapshot_manifest.get("captured_at")
            or ""
        )
        try:
            _parse_time(captured_at)
        except ValueError as exc:
            raise FinanceStorageBackupRotationError(
                "legacy archive capture time is invalid"
            ) from exc
        result = {
            "family": "backup_legacy_snapshot",
            "artifact_id": path.name,
            "path": str(path),
            "directory_identity": _directory_identity(path),
            "files": files,
            "total_bytes": sum(int(item["size_bytes"]) for item in files),
            "allocated_bytes": sum(int(item["allocated_bytes"]) for item in files),
            "captured_at": captured_at,
            "snapshot_status": status,
            "snapshot_deployed_sha": str(snapshot_manifest.get("deployed_sha") or ""),
            "integrity_proven": bool(status == "integrity_verified" or integrity),
            "integrity_readback": integrity,
            "deletion_allowed": (
                str(snapshot_manifest.get("deployed_sha") or "") != self.deployed_sha
            ),
        }
        result["candidate_fingerprint"] = _candidate_fingerprint(result)
        return result

    def _retained_candidate(self, path: Path) -> dict[str, Any]:
        if _BACKUP_ID_RE.fullmatch(path.name) is None:
            raise FinanceStorageBackupRotationError("retained backup id is invalid")
        files = self._files(path, allowed=_BACKUP_FILES)
        directory_identity = _directory_identity(path)
        if (
            int(directory_identity["mode"]) != 0o700
            or any(int(item["mode"]) != 0o600 for item in files)
            or any(
                int(item["uid"]) != int(directory_identity["uid"])
                or int(item["gid"]) != int(directory_identity["gid"])
                for item in files
            )
        ):
            raise FinanceStorageBackupRotationError(
                "retained backup permissions/ownership are unsafe"
            )
        names = {item["name"] for item in files}
        if names != _BACKUP_FILES:
            raise FinanceStorageBackupRotationError("retained backup is incomplete")
        manifest = _load_json(
            path / BACKUP_MANIFEST_FILENAME, label="retained backup manifest"
        )
        stable = {key: value for key, value in manifest.items() if key != "fingerprint"}
        declared = {
            str(item.get("name") or ""): item
            for item in manifest.get("files") or []
            if isinstance(item, Mapping)
        }
        actual = {item["name"]: item for item in files}
        if set(declared) != {
            RAW_BACKUP_FILENAME,
            OPERATIONAL_BACKUP_FILENAME,
            SOURCE_MANIFEST_FILENAME,
        }:
            raise FinanceStorageBackupRotationError(
                "retained backup byte manifest is incomplete"
            )
        for name in (
            RAW_BACKUP_FILENAME,
            OPERATIONAL_BACKUP_FILENAME,
            SOURCE_MANIFEST_FILENAME,
        ):
            expected = declared.get(name)
            item = actual.get(name)
            if (
                expected is None
                or item is None
                or int(expected.get("size_bytes") or -1) != int(item["size_bytes"])
                or str(expected.get("sha256") or "") != str(item["sha256"])
            ):
                raise FinanceStorageBackupRotationError("retained backup bytes drifted")
        restore_drill = dict(manifest.get("restore_drill") or {})
        watermarks = dict(
            (manifest.get("source_identity") or {}).get("watermarks") or {}
        )
        source_manifest = dict(manifest.get("source_manifest") or {})
        stable_source_manifest = {
            key: value
            for key, value in source_manifest.items()
            if key != "manifest_sha256"
        }
        watermark_keys = {
            "latest_outbox_sequence",
            "raw_ack_cursor",
            "live_tail_cursor",
            "live_tail_applicable",
            "operational_cursor",
            "raw_rows",
            "raw_batches",
            "pending_outbox",
            "consumer_lag_events",
            "live_tail_lag_events",
            "cursor_mismatch",
            "shadow_mismatch_count",
            "actionable_dead_letters",
        }
        if (
            str(manifest.get("contract_version") or "") != BACKUP_SET_CONTRACT
            or str(manifest.get("backup_id") or "") != path.name
            or str(manifest.get("status") or "") != "verified"
            or str(manifest.get("fingerprint") or "") != _fingerprint(stable)
            or restore_drill.get("status") != "verified"
            or not str(manifest.get("captured_at") or "")
            or str(manifest.get("restore_manifest_file_sha256") or "")
            != str(actual[SOURCE_MANIFEST_FILENAME]["sha256"])
            or str(source_manifest.get("manifest_sha256") or "")
            != str(manifest.get("source_manifest_sha256") or "")
            or str(manifest.get("source_manifest_sha256") or "")
            != _fingerprint(stable_source_manifest)
            or not watermark_keys.issubset(watermarks)
            or any(
                restore_drill.get(key) != watermarks.get(key) for key in watermark_keys
            )
            or int(watermarks.get("pending_outbox") or 0) != 0
            or int(watermarks.get("consumer_lag_events") or 0) != 0
            or int(watermarks.get("live_tail_lag_events") or 0) != 0
            or watermarks.get("live_tail_applicable") is not False
            or watermarks.get("cursor_mismatch") is not False
            or int(watermarks.get("shadow_mismatch_count") or 0) != 0
            or int(watermarks.get("actionable_dead_letters") or 0) != 0
        ):
            raise FinanceStorageBackupRotationError(
                "retained backup manifest is invalid"
            )
        restore_selected = StoreRegistry(path).load(require_files=True)
        if (
            restore_selected.manifest_sha256
            != str(manifest.get("restore_manifest_sha256") or "")
            or restore_drill.get("manifest_sha256") != restore_selected.manifest_sha256
        ):
            raise FinanceStorageBackupRotationError(
                "retained backup restore manifest is invalid"
            )
        try:
            _parse_time(str(manifest["captured_at"]))
            _parse_time(str(manifest["verified_at"]))
        except ValueError as exc:
            raise FinanceStorageBackupRotationError(
                "retained backup capture/verification time is invalid"
            ) from exc
        result = {
            "family": "retained_split_backup",
            "artifact_id": path.name,
            "path": str(path),
            "directory_identity": directory_identity,
            "files": files,
            "total_bytes": sum(int(item["size_bytes"]) for item in files),
            "allocated_bytes": sum(int(item["allocated_bytes"]) for item in files),
            "captured_at": str(manifest["captured_at"]),
            "source_manifest_sha256": str(manifest.get("source_manifest_sha256") or ""),
            "source_identity": dict(manifest.get("source_identity") or {}),
            "backup_manifest_fingerprint": str(manifest.get("fingerprint") or ""),
            "integrity_proven": True,
            "deletion_allowed": True,
        }
        result["candidate_fingerprint"] = _candidate_fingerprint(result)
        return result

    def _current(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        current = _load_json(self.current_path, label="Finance current backup selector")
        stable = {key: value for key, value in current.items() if key != "fingerprint"}
        backup_id = str(current.get("backup_id") or "")
        if (
            str(current.get("contract_version") or "") != CURRENT_CONTRACT
            or _BACKUP_ID_RE.fullmatch(backup_id) is None
            or str(current.get("fingerprint") or "") != _fingerprint(stable)
        ):
            raise FinanceStorageBackupRotationError(
                "Finance current selector is invalid"
            )
        try:
            _parse_time(str(current.get("selected_at") or ""))
        except ValueError as exc:
            raise FinanceStorageBackupRotationError(
                "Finance current selector time is invalid"
            ) from exc
        candidate = self._retained_candidate(self.retained_root / backup_id)
        if candidate["backup_manifest_fingerprint"] != str(
            current.get("backup_manifest_fingerprint") or ""
        ):
            raise FinanceStorageBackupRotationError("Finance current selector drifted")
        return {"selector": current, "candidate": candidate}

    def _root_inventory(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        known: list[dict[str, Any]] = []
        protected: list[dict[str, Any]] = []
        if not self.snapshot_root.exists():
            return known, protected
        if self.snapshot_root.is_symlink() or not self.snapshot_root.is_dir():
            raise FinanceStorageBackupRotationError("canonical snapshot root is unsafe")
        for path in sorted(self.snapshot_root.iterdir(), key=lambda item: item.name):
            if _LEGACY_ID_RE.fullmatch(path.name):
                try:
                    candidate = self._root_candidate(path)
                    if candidate["deletion_allowed"] is True:
                        known.append(candidate)
                    else:
                        protected.append(
                            _protected_record(
                                family="root_current_deployed_snapshot",
                                path=path,
                                reason="snapshot belongs to the active deployed SHA",
                            )
                        )
                except FinanceStorageSnapshotRetentionError as exc:
                    protected.append(
                        _protected_record(
                            family="root_unknown_or_corrupt",
                            path=path,
                            reason=str(exc),
                        )
                    )
            else:
                protected.append(
                    _protected_record(
                        family="root_foreign",
                        path=path,
                        reason="outside exact allowlist",
                    )
                )
        return known, protected

    def _backup_inventory(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        legacy: list[dict[str, Any]] = []
        retained: list[dict[str, Any]] = []
        protected: list[dict[str, Any]] = []
        if not self.backup_root.exists():
            return legacy, retained, protected
        allowed_control = {
            RETAINED_DIRECTORY,
            TRANSACTIONS_DIRECTORY,
            CURRENT_FILENAME,
            POLICY_FILENAME,
            AUDIT_FILENAME,
        }
        for path in sorted(self.backup_root.iterdir(), key=lambda item: item.name):
            if _LEGACY_ID_RE.fullmatch(path.name):
                try:
                    candidate = self._archive_candidate(path)
                    if candidate["deletion_allowed"] is True:
                        legacy.append(candidate)
                    else:
                        protected.append(
                            _protected_record(
                                family="backup_current_deployed_snapshot",
                                path=path,
                                reason="archive belongs to the active deployed SHA",
                            )
                        )
                except FinanceStorageSnapshotRetentionError as exc:
                    protected.append(
                        _protected_record(
                            family="backup_unknown_or_corrupt",
                            path=path,
                            reason=str(exc),
                        )
                    )
            elif path.name not in allowed_control:
                protected.append(
                    _protected_record(
                        family="backup_foreign",
                        path=path,
                        reason="outside exact allowlist",
                    )
                )
        if self.retained_root.exists():
            if self.retained_root.is_symlink() or not self.retained_root.is_dir():
                raise FinanceStorageBackupRotationError(
                    "retained backup root is unsafe"
                )
            for path in sorted(
                self.retained_root.iterdir(), key=lambda item: item.name
            ):
                if path.name.startswith(".") and path.name.endswith(".partial"):
                    protected.append(
                        _protected_record(
                            family="retained_partial",
                            path=path,
                            reason="requires exact transaction resume",
                        )
                    )
                    continue
                try:
                    retained.append(self._retained_candidate(path))
                except FinanceStorageSnapshotRetentionError as exc:
                    protected.append(
                        _protected_record(
                            family="retained_unknown_or_corrupt",
                            path=path,
                            reason=str(exc),
                        )
                    )
        return legacy, retained, protected

    def _pre_mutation_transaction_plan(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        terminalizations: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        if not self.transactions_root.exists():
            return terminalizations, blockers
        if self.transactions_root.is_symlink() or not self.transactions_root.is_dir():
            return terminalizations, [
                {"code": "finance_backup_transaction_inventory_unsafe"}
            ]
        for path in sorted(self.transactions_root.iterdir(), key=lambda item: item.name):
            if (
                path.is_symlink()
                or not path.is_file()
                or re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is None
            ):
                blockers.append(
                    {
                        "code": "finance_backup_transaction_inventory_unsafe",
                        "path": str(path),
                    }
                )
                continue
            try:
                transaction = _load_json(
                    path, label="Finance backup transaction inventory"
                )
            except FinanceStorageSnapshotRetentionError as exc:
                blockers.append(
                    {
                        "code": "finance_backup_transaction_inventory_unsafe",
                        "path": str(path),
                        "reason": str(exc),
                    }
                )
                continue
            if transaction.get("phase") == "completed":
                continue
            reviewed_plan = transaction.get("reviewed_plan")
            reviewed_stable = (
                {
                    key: value
                    for key, value in reviewed_plan.items()
                    if key not in {"fingerprint", "deploy_lease"}
                }
                if isinstance(reviewed_plan, Mapping)
                else {}
            )
            replacement = (
                dict(reviewed_plan.get("replacement") or {})
                if isinstance(reviewed_plan, Mapping)
                else {}
            )
            source_fingerprint = str(transaction.get("plan_fingerprint") or "")
            source_deployed_sha = str(transaction.get("deployed_sha") or "")
            destination_paths = [
                Path(str(replacement.get(key) or ""))
                for key in ("destination_partial", "destination_final")
                if str(replacement.get(key) or "")
            ]
            safely_superseded_before_mutation = (
                transaction.get("contract_version") == TRANSACTION_CONTRACT
                and transaction.get("strategy") == STRATEGY
                and transaction.get("phase") == "started"
                and _FINGERPRINT_RE.fullmatch(source_fingerprint) is not None
                and path.name
                == f"{source_fingerprint.removeprefix('sha256:')}.json"
                and _SHA_RE.fullmatch(source_deployed_sha) is not None
                and str(transaction.get("transaction_path") or "")
                == str(path.resolve())
                and isinstance(reviewed_plan, Mapping)
                and reviewed_plan.get("fingerprint") == source_fingerprint
                and reviewed_plan.get("deployed_sha") == source_deployed_sha
                and reviewed_plan.get("contract_version") == PLAN_CONTRACT
                and reviewed_plan.get("mode") == "snapshot_retention_dry_run"
                and reviewed_plan.get("strategy") == STRATEGY
                and reviewed_plan.get("runtime_dir") == str(self.runtime_dir)
                and reviewed_plan.get("snapshot_root") == str(self.snapshot_root)
                and reviewed_plan.get("archive_root") == str(self.backup_root)
                and reviewed_plan.get("apply_allowed_by_machine_preflight") is True
                and not list(reviewed_plan.get("blockers") or [])
                and _fingerprint(reviewed_stable) == source_fingerprint
                and transaction.get("backup_id") == reviewed_plan.get("backup_id")
                and _BACKUP_ID_RE.fullmatch(
                    str(transaction.get("backup_id") or "")
                )
                is not None
                and bool(str(transaction.get("approval_reference") or "").strip())
                and not list(transaction.get("completed_deletions") or [])
                and not dict(transaction.get("deletion_receipts") or {})
                and not str(transaction.get("pending_deletion") or "")
                and not dict(transaction.get("copy_proofs") or {})
                and not isinstance(transaction.get("result"), Mapping)
                and transaction.get("audit_recorded") is not True
                and not any(path.exists() or path.is_symlink() for path in destination_paths)
            )
            if not safely_superseded_before_mutation:
                blockers.append(
                    {
                        "code": "non_terminal_transaction_requires_exact_resume",
                        "path": str(path.resolve()),
                        "plan_fingerprint": source_fingerprint,
                        "deployed_sha": source_deployed_sha,
                        "phase": str(transaction.get("phase") or ""),
                    }
                )
                continue
            identity = _file_identity(path, include_sha256=True)
            item_stat = path.stat()
            identity.update(
                {"uid": int(item_stat.st_uid), "gid": int(item_stat.st_gid)}
            )
            terminalizations.append(
                {
                    "path": str(path.resolve()),
                    "transaction_identity": identity,
                    "source_plan_fingerprint": source_fingerprint,
                    "source_deployed_sha": source_deployed_sha,
                    "phase": "started",
                    "completed_deletions": [],
                    "copy_proofs": {},
                    "terminalization_allowed": True,
                }
            )
        return terminalizations, blockers

    @staticmethod
    def _source_changed(guard: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
        source = dict(current.get("source_identity") or {})
        if str(current.get("source_manifest_sha256") or "") != str(
            guard.get("manifest_sha256") or ""
        ):
            return True
        for name in ("raw", "operational"):
            expected = dict(source.get(name) or {})
            actual = dict(guard.get(name) or {})
            for key in ("device", "inode", "size_bytes", "mtime_ns"):
                if int(expected.get(key) or -1) != int(actual.get(key) or -2):
                    return True
            if list(expected.get("sidecars") or []) != list(
                actual.get("sidecars") or []
            ):
                return True
        return source.get("watermarks") != guard.get("watermarks")

    def build_plan(
        self,
        *,
        cleanup_legacy: bool = True,
        scheduled: bool = False,
        force_replacement: bool = False,
    ) -> dict[str, Any]:
        created_at = str(self.now_factory())
        guard = self._guard()
        device = self._device_guard()
        root_legacy, root_protected = self._root_inventory()
        backup_legacy, retained, backup_protected = self._backup_inventory()
        transaction_terminalizations, transaction_blockers = (
            self._pre_mutation_transaction_plan()
        )
        current = self._current()
        current_candidate = dict(current["candidate"]) if current else None
        retained_by_id = {item["artifact_id"]: item for item in retained}
        if current_candidate and current_candidate["artifact_id"] not in retained_by_id:
            raise FinanceStorageBackupRotationError(
                "selected current backup is not retained"
            )
        orphan_retained = [
            item
            for item in retained
            if current_candidate is None
            or item["artifact_id"] != current_candidate["artifact_id"]
        ]
        age_seconds = None
        source_changed = True
        if current_candidate:
            age_seconds = max(
                0,
                int(
                    (
                        _parse_time(created_at)
                        - _parse_time(str(current_candidate["captured_at"]))
                    ).total_seconds()
                ),
            )
            source_changed = self._source_changed(guard, current_candidate)
        replacement_due = bool(
            force_replacement
            or current_candidate is None
            or (
                source_changed
                and int(age_seconds or 0) >= self.minimum_replacement_interval_seconds
            )
            or int(age_seconds or 0) >= self.max_age_seconds
        )
        source_bytes = int(guard["raw"]["size_bytes"]) + int(
            guard["operational"]["size_bytes"]
        )
        source_allocated = int(guard["raw"]["allocated_bytes"]) + int(
            guard["operational"]["allocated_bytes"]
        )
        copy_required_bytes = source_bytes + DEFAULT_COPY_OVERHEAD_BYTES
        if replacement_due and source_bytes > self.max_set_bytes:
            raise FinanceStorageBackupRotationError(
                "canonical split exceeds the hard per-set byte cap"
            )
        root_capacity = _filesystem(self.runtime_dir)
        backup_capacity = _filesystem(self.backup_mount)
        backup_fallbacks = sorted(
            [item for item in backup_legacy if item["integrity_proven"]],
            key=lambda item: (str(item.get("captured_at") or ""), item["artifact_id"]),
        )
        pre_delete: list[dict[str, Any]] = (
            list(orphan_retained) if current_candidate is not None else []
        )
        if replacement_due:
            needed = copy_required_bytes + self.hard_reserve_bytes
            projected = int(backup_capacity["available_bytes"]) + sum(
                int(item["allocated_bytes"]) for item in pre_delete
            )
            keep_fallback_id = (
                backup_fallbacks[-1]["artifact_id"] if backup_fallbacks else ""
            )
            for candidate in backup_fallbacks:
                if projected >= needed:
                    break
                if candidate["artifact_id"] == keep_fallback_id:
                    continue
                pre_delete.append(candidate)
                projected += int(candidate["allocated_bytes"])
        pre_delete_ids = {item["artifact_id"] for item in pre_delete}
        post_delete: list[dict[str, Any]] = []
        if cleanup_legacy:
            post_delete.extend(root_legacy)
            post_delete.extend(
                item
                for item in backup_legacy
                if item["artifact_id"] not in pre_delete_ids
            )
        if replacement_due and current_candidate:
            post_delete.append(current_candidate)
        deduped_post: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for item in post_delete:
            if item["path"] not in seen_paths:
                seen_paths.add(item["path"])
                deduped_post.append(item)
        post_delete = deduped_post
        backup_released = sum(
            int(item["allocated_bytes"])
            for item in pre_delete + post_delete
            if Path(str(item["path"])).resolve().is_relative_to(self.backup_root)
        )
        root_released = sum(
            int(item["allocated_bytes"])
            for item in post_delete
            if Path(str(item["path"])).resolve().is_relative_to(self.snapshot_root)
        )
        temporary_backup_available = (
            int(backup_capacity["available_bytes"])
            + sum(int(item["allocated_bytes"]) for item in pre_delete)
            - (copy_required_bytes if replacement_due else 0)
        )
        final_backup_available = (
            int(backup_capacity["available_bytes"])
            + backup_released
            - (source_bytes if replacement_due else 0)
        )
        final_root_available = int(root_capacity["available_bytes"]) + root_released
        final_set_bytes = (
            source_bytes
            if replacement_due
            else int((current_candidate or {}).get("total_bytes") or 0)
        )
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        blockers.extend(transaction_blockers)
        if retained and current_candidate is None:
            blockers.append(
                {
                    "code": "retained_set_without_current_selector",
                    "retained_ids": [item["artifact_id"] for item in retained],
                }
            )
        partials = [
            item
            for item in root_protected + backup_protected
            if item.get("family") == "retained_partial"
        ]
        if partials:
            blockers.append(
                {
                    "code": "non_terminal_replacement_requires_exact_resume",
                    "partials": partials,
                }
            )
        if final_set_bytes > self.max_set_bytes:
            blockers.append(
                {
                    "code": "retained_set_bytes_exceed_cap",
                    "retained_bytes": final_set_bytes,
                    "cap_bytes": self.max_set_bytes,
                }
            )
        if replacement_due and temporary_backup_available < self.hard_reserve_bytes:
            blockers.append(
                {
                    "code": "replacement_capacity_shortfall",
                    "projected_available_bytes": temporary_backup_available,
                    "required_hard_reserve_bytes": self.hard_reserve_bytes,
                }
            )
        elif (
            replacement_due
            and temporary_backup_available < self.degraded_available_bytes
        ):
            warnings.append(
                {
                    "code": "replacement_enters_degraded_watermark",
                    "projected_available_bytes": temporary_backup_available,
                    "degraded_available_bytes": self.degraded_available_bytes,
                }
            )
        if cleanup_legacy and final_root_available < self.root_target_bytes:
            blockers.append(
                {
                    "code": "root_target_unreachable",
                    "projected_available_bytes": final_root_available,
                    "target_bytes": self.root_target_bytes,
                }
            )
        if cleanup_legacy and final_backup_available < self.backup_target_bytes:
            blockers.append(
                {
                    "code": "backup_target_unreachable",
                    "projected_available_bytes": final_backup_available,
                    "target_bytes": self.backup_target_bytes,
                }
            )
        if final_backup_available < (
            final_set_bytes + DEFAULT_COPY_OVERHEAD_BYTES + self.hard_reserve_bytes
        ):
            blockers.append(
                {
                    "code": "next_replacement_capacity_shortfall",
                    "projected_available_bytes": final_backup_available,
                    "next_set_bytes": final_set_bytes,
                    "copy_overhead_bytes": DEFAULT_COPY_OVERHEAD_BYTES,
                    "hard_reserve_bytes": self.hard_reserve_bytes,
                }
            )
        selected = pre_delete + post_delete
        openers = _openers_below(Path(str(item["path"])) for item in selected)
        if openers:
            blockers.append({"code": "deletion_candidate_openers", "openers": openers})
        identity_seed = {
            "deployed_sha": self.deployed_sha,
            "created_at": created_at,
            "manifest_sha256": guard["manifest_sha256"],
            "raw": guard["raw"],
            "operational": guard["operational"],
        }
        backup_id = (
            "finance-backup-"
            + hashlib.sha256(
                _canonical_json(identity_seed).encode("utf-8")
            ).hexdigest()[:20]
        )
        plan: dict[str, Any] = {
            "contract_version": PLAN_CONTRACT,
            "mode": "snapshot_retention_dry_run",
            "strategy": STRATEGY,
            "created_at": created_at,
            "deployed_sha": self.deployed_sha,
            "runtime_dir": str(self.runtime_dir),
            "snapshot_root": str(self.snapshot_root),
            "archive_root": str(self.backup_root),
            "backup_id": backup_id,
            "scheduled": bool(scheduled),
            "cleanup_legacy": bool(cleanup_legacy),
            "replacement": {
                "due": replacement_due,
                "reason": {
                    "no_current": current_candidate is None,
                    "source_changed": source_changed,
                    "age_seconds": age_seconds,
                    "max_age_seconds": self.max_age_seconds,
                    "minimum_interval_seconds": self.minimum_replacement_interval_seconds,
                    "forced": bool(force_replacement),
                },
                "copy_order": ["operational", "finance_raw"],
                "source_bytes": source_bytes,
                "source_allocated_bytes": source_allocated,
                "copy_required_bytes": copy_required_bytes,
                "destination_partial": str(
                    self.retained_root / f".{backup_id}.partial"
                ),
                "destination_final": str(self.retained_root / backup_id),
            },
            "canonical_guard": guard,
            "device_boundary": device,
            "current_before": current,
            "inventory": {
                "root_legacy": root_legacy,
                "backup_legacy": backup_legacy,
                "retained": retained,
                "protected": root_protected + backup_protected,
            },
            "pre_publish_deletions": pre_delete,
            "post_publish_deletions": post_delete,
            "pre_mutation_transaction_terminalizations": (
                transaction_terminalizations
            ),
            "openers": openers,
            "capacity": {
                "root_before": root_capacity,
                "backup_before": backup_capacity,
                "root_released_bytes": root_released,
                "backup_released_bytes": backup_released,
                "temporary_backup_available_bytes": temporary_backup_available,
                "final_root_available_bytes": final_root_available,
                "final_backup_available_bytes": final_backup_available,
                "next_replacement_required_bytes": (
                    final_set_bytes
                    + DEFAULT_COPY_OVERHEAD_BYTES
                    + self.hard_reserve_bytes
                ),
                "projected_30_day_growth_bytes": 0,
                "projected_90_day_growth_bytes": 0,
                "projected_30_day_available_bytes": final_backup_available,
                "projected_90_day_available_bytes": final_backup_available,
            },
            "policy": {
                "retained_count_cap": DEFAULT_RETAINED_COUNT,
                "temporary_count_cap": DEFAULT_TEMPORARY_COUNT,
                "retained_bytes_cap": self.max_set_bytes,
                "temporary_bytes_cap": self.max_set_bytes * 2,
                "age_cap_seconds": self.max_age_seconds,
                "minimum_interval_seconds": self.minimum_replacement_interval_seconds,
                "hard_reserve_bytes": self.hard_reserve_bytes,
                "degraded_available_bytes": self.degraded_available_bytes,
                "root_target_bytes": self.root_target_bytes,
                "backup_target_bytes": self.backup_target_bytes,
                "rpo_seconds": self.max_age_seconds,
                "rto_seconds": 4 * 60 * 60,
                "cadence": "daily due-check; full only after source change and six-day minimum, or seven-day hard age",
            },
            "protected_non_targets": {
                **self._protected_non_target_identity(),
                "mutation_allowed": False,
            },
            "query_only_contract": {
                "business_data_mutation_count": 0,
                "snapshot_byte_mutation_count": 0,
                "archive_byte_mutation_count": 0,
                "backup_byte_mutation_count": 0,
            },
            "blockers": blockers,
            "warnings": warnings,
            "apply_allowed_by_machine_preflight": not blockers,
            "replacement_policy": {
                "last_verified_set_preserved_until_new_selection": True,
                "sqlite_integrity_required": True,
                "foreign_key_check_required": True,
                "logical_digest_required": True,
                "isolated_restore_drill_required": True,
                "atomic_current_selector_required": True,
                "root_staging_retained": False,
                "unknown_artifacts_deleted": False,
            },
        }
        plan["fingerprint"] = _fingerprint(plan)
        return plan

    def _validate_plan(
        self, reviewed_plan: Mapping[str, Any], *, expected_fingerprint: str
    ) -> None:
        if (
            str(reviewed_plan.get("contract_version") or "") != PLAN_CONTRACT
            or str(reviewed_plan.get("mode") or "") != "snapshot_retention_dry_run"
            or str(reviewed_plan.get("strategy") or "") != STRATEGY
            or str(reviewed_plan.get("deployed_sha") or "") != self.deployed_sha
            or str(reviewed_plan.get("runtime_dir") or "") != str(self.runtime_dir)
            or str(reviewed_plan.get("snapshot_root") or "") != str(self.snapshot_root)
            or str(reviewed_plan.get("archive_root") or "") != str(self.backup_root)
            or reviewed_plan.get("apply_allowed_by_machine_preflight") is not True
            or list(reviewed_plan.get("blockers") or [])
            or _FINGERPRINT_RE.fullmatch(expected_fingerprint) is None
            or str(reviewed_plan.get("fingerprint") or "") != expected_fingerprint
        ):
            raise FinanceStorageBackupRotationError(
                "reviewed Finance backup plan is invalid"
            )
        stable = {
            key: value
            for key, value in reviewed_plan.items()
            if key not in {"fingerprint", "deploy_lease"}
        }
        if _fingerprint(stable) != expected_fingerprint:
            raise FinanceStorageBackupRotationError(
                "reviewed Finance backup plan fingerprint is stale"
            )

    def _assert_candidate(self, candidate: Mapping[str, Any]) -> Path:
        raw_path = Path(str(candidate.get("path") or ""))
        if raw_path.is_symlink():
            raise FinanceStorageBackupRotationError(
                "deletion candidate path became a symlink"
            )
        path = raw_path.resolve()
        family = str(candidate.get("family") or "")
        exact_boundary = {
            "root_legacy_snapshot": (self.snapshot_root, _LEGACY_ID_RE),
            "backup_legacy_snapshot": (self.backup_root, _LEGACY_ID_RE),
            "retained_split_backup": (self.retained_root, _BACKUP_ID_RE),
        }.get(family)
        if (
            exact_boundary is None
            or path.parent != exact_boundary[0]
            or exact_boundary[1].fullmatch(path.name) is None
            or str(candidate.get("artifact_id") or "") != path.name
            or candidate.get("deletion_allowed") is not True
        ):
            raise FinanceStorageBackupRotationError(
                "deletion candidate escapes the exact Finance allowlist"
            )
        if _directory_identity(path) != dict(candidate.get("directory_identity") or {}):
            raise FinanceStorageBackupRotationError(
                "deletion candidate directory identity drifted"
            )
        files = {
            str(item.get("name") or ""): dict(item)
            for item in candidate.get("files") or []
        }
        if not path.is_dir() or path.is_symlink() or not files:
            raise FinanceStorageBackupRotationError(
                "deletion candidate is absent or unsafe"
            )
        current_names = {item.name for item in path.iterdir()}
        if current_names != set(files):
            raise FinanceStorageBackupRotationError(
                "deletion candidate inventory drifted"
            )
        for name, expected in files.items():
            item_path = path / name
            current = _file_identity(item_path, include_sha256=True)
            stat = item_path.stat()
            current.update({"uid": int(stat.st_uid), "gid": int(stat.st_gid)})
            if any(
                current.get(key) != expected.get(key)
                for key in (
                    "name",
                    "device",
                    "inode",
                    "size_bytes",
                    "allocated_bytes",
                    "mtime_ns",
                    "mode",
                    "uid",
                    "gid",
                    "sha256",
                )
            ):
                raise FinanceStorageBackupRotationError(
                    f"deletion candidate drifted: {path / name}"
                )
        return path

    def _delete_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        transaction: dict[str, Any],
        fault_at: str = "",
    ) -> dict[str, Any]:
        raw_path = Path(str(candidate.get("path") or ""))
        if raw_path.is_symlink():
            raise FinanceStorageBackupRotationError(
                "deletion candidate path became a symlink"
            )
        path = raw_path.resolve()
        fingerprint = str(candidate.get("candidate_fingerprint") or "")
        completed = set(transaction.get("completed_deletions") or [])
        receipt = {
            "artifact_id": candidate.get("artifact_id"),
            "family": candidate.get("family"),
            "path": str(path),
            "released_allocated_bytes": int(candidate.get("allocated_bytes") or 0),
        }
        receipts = {
            str(key): dict(value)
            for key, value in dict(transaction.get("deletion_receipts") or {}).items()
            if isinstance(value, Mapping)
        }
        if fingerprint in completed:
            if path.exists():
                raise FinanceStorageBackupRotationError(
                    "completed deletion candidate reappeared"
                )
            if receipts.get(fingerprint) != receipt:
                raise FinanceStorageBackupRotationError(
                    "completed deletion candidate lacks its exact receipt"
                )
            return receipt
        pending = str(transaction.get("pending_deletion") or "")
        if pending and pending != fingerprint:
            raise FinanceStorageBackupRotationError(
                "another Finance backup deletion has a pending intent"
            )
        if not path.exists():
            if pending != fingerprint:
                raise FinanceStorageBackupRotationError(
                    "deletion candidate disappeared without durable intent"
                )
        else:
            if pending == fingerprint:
                exact_boundary = {
                    "root_legacy_snapshot": (self.snapshot_root, _LEGACY_ID_RE),
                    "backup_legacy_snapshot": (self.backup_root, _LEGACY_ID_RE),
                    "retained_split_backup": (self.retained_root, _BACKUP_ID_RE),
                }.get(str(candidate.get("family") or ""))
                if (
                    exact_boundary is None
                    or path.parent != exact_boundary[0]
                    or exact_boundary[1].fullmatch(path.name) is None
                    or _directory_identity(path)
                    != dict(candidate.get("directory_identity") or {})
                ):
                    raise FinanceStorageBackupRotationError(
                        "pending deletion directory identity drifted"
                    )
                planned_files = {
                    str(item.get("name") or ""): dict(item)
                    for item in candidate.get("files") or []
                }
                current_names = {item.name for item in path.iterdir()}
                if not current_names.issubset(planned_files):
                    raise FinanceStorageBackupRotationError(
                        "pending deletion gained an unknown file"
                    )
                for name in current_names:
                    current_path = path / name
                    current = _file_identity(current_path, include_sha256=True)
                    current_stat = current_path.stat()
                    current.update(
                        {
                            "uid": int(current_stat.st_uid),
                            "gid": int(current_stat.st_gid),
                        }
                    )
                    expected = planned_files[name]
                    if any(
                        current.get(key) != expected.get(key)
                        for key in (
                            "name",
                            "device",
                            "inode",
                            "size_bytes",
                            "allocated_bytes",
                            "mtime_ns",
                            "mode",
                            "uid",
                            "gid",
                            "sha256",
                        )
                    ):
                        raise FinanceStorageBackupRotationError(
                            "pending deletion remaining bytes drifted"
                        )
                exact = path
            else:
                exact = self._assert_candidate(candidate)
            if _openers_below([exact]):
                raise FinanceStorageBackupRotationError(
                    "deletion candidate gained an opener"
                )
            if pending != fingerprint:
                transaction["pending_deletion"] = fingerprint
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(Path(transaction["transaction_path"]), transaction)
            names = sorted(
                (item.name for item in exact.iterdir()),
                key=lambda name: (
                    name in {"snapshot_manifest.json", BACKUP_MANIFEST_FILENAME},
                    name,
                ),
            )
            for index, name in enumerate(names):
                (exact / name).unlink()
                _fsync_directory(exact)
                if fault_at == "during_candidate_delete" and index == 0:
                    raise RuntimeError("injected fault during candidate deletion")
            exact.rmdir()
            _fsync_directory(exact.parent)
        completed.add(fingerprint)
        transaction["completed_deletions"] = sorted(completed)
        receipts[fingerprint] = receipt
        transaction["deletion_receipts"] = receipts
        transaction["pending_deletion"] = ""
        transaction["updated_at"] = _utc_now()
        _atomic_write_json(Path(transaction["transaction_path"]), transaction)
        return receipt

    def _assert_plan_cas(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        guard = self._guard()
        expected = dict(plan.get("canonical_guard") or {})
        if guard["manifest_sha256"] != expected.get("manifest_sha256") or guard[
            "generation_epoch"
        ] != expected.get("generation_epoch"):
            raise FinanceStorageBackupRotationError("canonical manifest CAS drifted")
        if guard.get("shadow_state") != expected.get("shadow_state"):
            raise FinanceStorageBackupRotationError(
                "Finance shadow state CAS drifted"
            )
        for name in ("raw", "operational"):
            for key in ("path", "device", "inode"):
                if guard[name].get(key) != dict(expected.get(name) or {}).get(key):
                    raise FinanceStorageBackupRotationError(
                        f"canonical {name} identity CAS drifted"
                    )
        device = self._device_guard()
        expected_device = dict(plan.get("device_boundary") or {})
        if (
            device["runtime_device"] != expected_device.get("runtime_device")
            or device["backup_device"] != expected_device.get("backup_device")
            or device["mount"] != expected_device.get("mount")
        ):
            raise FinanceStorageBackupRotationError("backup mount/device CAS drifted")
        return guard

    def _write_restore_manifest(
        self,
        path: Path,
        manifest: Any,
        *,
        transaction: dict[str, Any],
    ) -> None:
        pending = path.with_name(path.name + ".pending")
        expected_payload = manifest_payload(manifest)
        if path.exists():
            if pending.exists():
                raise FinanceStorageBackupRotationError(
                    "restore manifest has both final and pending files"
                )
            selected = StoreRegistry(path.parent, manifest_path=path).load()
            if manifest_payload(selected) != expected_payload:
                raise FinanceStorageBackupRotationError(
                    "partial restore manifest drifted"
                )
            if transaction.get("manifest_phase") != "published":
                transaction["manifest_phase"] = "published"
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(Path(transaction["transaction_path"]), transaction)
            return
        if pending.exists():
            if pending.is_symlink() or not pending.is_file():
                raise FinanceStorageBackupRotationError(
                    "pending restore manifest is unsafe"
                )
            recovery = _file_identity(pending, include_sha256=True)
            pending_stat = pending.stat()
            parent_stat = path.parent.stat()
            if (
                int(recovery["mode"]) != 0o600
                or int(pending_stat.st_uid) != int(parent_stat.st_uid)
                or int(pending_stat.st_gid) != int(parent_stat.st_gid)
            ):
                raise FinanceStorageBackupRotationError(
                    "pending restore manifest permissions/ownership are unsafe"
                )
            recovered = list(transaction.get("manifest_recovery_evidence") or [])
            recovered.append(recovery)
            transaction["manifest_recovery_evidence"] = recovered
            transaction["manifest_phase"] = "recovering_pending"
            transaction["updated_at"] = _utc_now()
            _atomic_write_json(Path(transaction["transaction_path"]), transaction)
            try:
                selected = StoreRegistry(path.parent, manifest_path=pending).load()
            except ValueError:
                pending.unlink()
                _fsync_directory(path.parent)
            else:
                if manifest_payload(selected) != expected_payload:
                    raise FinanceStorageBackupRotationError(
                        "pending restore manifest belongs to another state"
                    )
                os.replace(pending, path)
                os.chmod(path, 0o600)
                _fsync_directory(path.parent)
                transaction["manifest_phase"] = "published"
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(Path(transaction["transaction_path"]), transaction)
                return
        transaction["manifest_phase"] = "writing"
        transaction["updated_at"] = _utc_now()
        _atomic_write_json(Path(transaction["transaction_path"]), transaction)
        descriptor = os.open(
            pending,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            data = (_canonical_json(expected_payload) + "\n").encode("utf-8")
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(pending, path)
            os.chmod(path, 0o600)
            _fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        transaction["manifest_phase"] = "published"
        transaction["updated_at"] = _utc_now()
        _atomic_write_json(Path(transaction["transaction_path"]), transaction)

    def _write_backup_manifest(
        self,
        path: Path,
        payload: Mapping[str, Any],
        *,
        transaction: dict[str, Any],
    ) -> None:
        expected = dict(payload)
        pending = path.with_name(path.name + ".pending")
        if path.exists():
            if pending.exists():
                raise FinanceStorageBackupRotationError(
                    "backup manifest has both final and pending files"
                )
            if _load_json(path, label="partial backup manifest") != expected:
                raise FinanceStorageBackupRotationError(
                    "partial backup manifest drifted"
                )
            if transaction.get("backup_manifest_phase") != "published":
                transaction["backup_manifest_phase"] = "published"
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(Path(transaction["transaction_path"]), transaction)
            return
        if pending.exists():
            if pending.is_symlink() or not pending.is_file():
                raise FinanceStorageBackupRotationError(
                    "pending backup manifest is unsafe"
                )
            recovery = _file_identity(pending, include_sha256=True)
            pending_stat = pending.stat()
            parent_stat = path.parent.stat()
            if (
                int(recovery["mode"]) != 0o600
                or int(pending_stat.st_uid) != int(parent_stat.st_uid)
                or int(pending_stat.st_gid) != int(parent_stat.st_gid)
            ):
                raise FinanceStorageBackupRotationError(
                    "pending backup manifest permissions/ownership are unsafe"
                )
            recovered = list(transaction.get("backup_manifest_recovery_evidence") or [])
            recovered.append(recovery)
            transaction["backup_manifest_recovery_evidence"] = recovered
            transaction["backup_manifest_phase"] = "recovering_pending"
            transaction["updated_at"] = _utc_now()
            _atomic_write_json(Path(transaction["transaction_path"]), transaction)
            try:
                pending_payload = _load_json(pending, label="pending backup manifest")
            except FinanceStorageSnapshotRetentionError:
                pending.unlink()
                _fsync_directory(path.parent)
            else:
                if pending_payload != expected:
                    raise FinanceStorageBackupRotationError(
                        "pending backup manifest belongs to another transaction"
                    )
                os.replace(pending, path)
                os.chmod(path, 0o600)
                _fsync_directory(path.parent)
                transaction["backup_manifest_phase"] = "published"
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(Path(transaction["transaction_path"]), transaction)
                return
        transaction["backup_manifest_phase"] = "writing"
        transaction["updated_at"] = _utc_now()
        _atomic_write_json(Path(transaction["transaction_path"]), transaction)
        descriptor = os.open(
            pending,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            data = (_canonical_json(expected) + "\n").encode("utf-8")
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(pending, path)
            os.chmod(path, 0o600)
            _fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        transaction["backup_manifest_phase"] = "published"
        transaction["updated_at"] = _utc_now()
        _atomic_write_json(Path(transaction["transaction_path"]), transaction)

    def _copy_replacement(
        self,
        plan: Mapping[str, Any],
        *,
        transaction: dict[str, Any],
        fault_at: str,
    ) -> dict[str, Any]:
        replacement = dict(plan.get("replacement") or {})
        backup_id = str(plan.get("backup_id") or "")
        partial = Path(str(replacement.get("destination_partial") or "")).resolve()
        final = Path(str(replacement.get("destination_final") or "")).resolve()
        if _BACKUP_ID_RE.fullmatch(backup_id) is None:
            raise FinanceStorageBackupRotationError("backup id is invalid")
        if final == self.retained_root / backup_id and final.is_dir():
            candidate = self._retained_candidate(final)
            if (
                str(transaction.get("backup_manifest_fingerprint") or "")
                != candidate["backup_manifest_fingerprint"]
            ):
                raise FinanceStorageBackupRotationError(
                    "published replacement is ambiguous"
                )
            return candidate
        if partial.parent != self.retained_root or final.parent != self.retained_root:
            raise FinanceStorageBackupRotationError(
                "replacement path escapes retained root"
            )
        self.retained_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.retained_root, 0o700)
        partial.mkdir(parents=False, exist_ok=True, mode=0o700)
        os.chmod(partial, 0o700)
        guard = self._assert_plan_cas(plan)
        proofs = dict(transaction.get("copy_proofs") or {})
        for logical, filename in (
            ("operational", OPERATIONAL_BACKUP_FILENAME),
            ("raw", RAW_BACKUP_FILENAME),
        ):
            destination = partial / filename
            proof = dict(proofs.get(logical) or {})
            if destination.exists():
                readback = _sqlite_readback(destination, include_logical=True)
                identity = _file_identity(destination, include_sha256=True)
                if proof:
                    if (
                        proof.get("file", {}).get("sha256") != identity["sha256"]
                        or proof.get("logical_tables") != readback["logical_tables"]
                        or not isinstance(proof.get("source_file"), Mapping)
                        or not str(proof.get("captured_at") or "")
                    ):
                        raise FinanceStorageBackupRotationError(
                            f"partial {logical} backup drifted"
                        )
                    _parse_time(str(proof["captured_at"]))
                else:
                    source_path = Path(str(guard[logical]["path"]))
                    source_readback = _sqlite_readback(
                        source_path, include_logical=True
                    )
                    if source_readback["logical_tables"] != readback["logical_tables"]:
                        raise FinanceStorageBackupRotationError(
                            f"unjournaled partial {logical} backup no longer "
                            "matches the canonical source"
                        )
                    proof = {
                        "source_path": str(source_path),
                        "source_file": _sqlite_source_identity(source_path),
                        "captured_at": _utc_now(),
                        "file": identity,
                        "query_only_source": True,
                        "integrity_check": "ok",
                        "foreign_key_violation_count": 0,
                        "logical_tables": readback["logical_tables"],
                    }
            else:
                proof = _copy_sqlite(Path(guard[logical]["path"]), destination)
            proofs[logical] = proof
            transaction["copy_proofs"] = proofs
            transaction["phase"] = f"{logical}_copied"
            transaction["updated_at"] = _utc_now()
            _atomic_write_json(Path(transaction["transaction_path"]), transaction)
            if fault_at == f"after_{logical}_copy":
                raise RuntimeError(f"injected fault after {logical} copy")
        current_guard = self._assert_plan_cas(plan)
        source_manifest_path = self.runtime_dir / SOURCE_MANIFEST_FILENAME
        if source_manifest_path.is_symlink() or not source_manifest_path.is_file():
            raise FinanceStorageBackupRotationError(
                "canonical source manifest is missing or unsafe"
            )
        source_manifest_bytes = source_manifest_path.read_bytes()
        source_manifest_file_sha = (
            "sha256:" + hashlib.sha256(source_manifest_bytes).hexdigest()
        )
        source_manifest_payload = json.loads(source_manifest_bytes.decode("utf-8"))
        if str(source_manifest_payload.get("manifest_sha256") or "") != str(
            current_guard["manifest_sha256"]
        ):
            raise FinanceStorageBackupRotationError("source manifest bytes drifted")
        canonical_manifest = dict(current_guard["manifest"])
        restore_manifest = build_manifest(
            state=str(canonical_manifest["state"]),
            canonical_source=str(canonical_manifest["canonical_source"]),
            generation_epoch=str(canonical_manifest["generation_epoch"]),
            raw_generation_id=str(canonical_manifest["raw"]["generation_id"]),
            raw_relative_path=RAW_BACKUP_FILENAME,
            raw_watermark=str(canonical_manifest["raw"]["watermark"]),
            operational_generation_id=str(
                canonical_manifest["operational"]["generation_id"]
            ),
            operational_relative_path=OPERATIONAL_BACKUP_FILENAME,
            operational_watermark=str(canonical_manifest["operational"]["watermark"]),
            rollback_generation_id=str(canonical_manifest["rollback_generation_id"]),
            source_fingerprint=str(canonical_manifest["source_fingerprint"]),
            created_at=str(canonical_manifest["created_at"]),
        )
        manifest_copy = partial / SOURCE_MANIFEST_FILENAME
        self._write_restore_manifest(
            manifest_copy,
            restore_manifest,
            transaction=transaction,
        )
        if fault_at == "after_restore_manifest":
            raise RuntimeError("injected fault after restore manifest")
        restore_registry = StoreRegistry(partial)
        restore_selected = restore_registry.load(require_files=True)
        if manifest_payload(restore_selected) != manifest_payload(restore_manifest):
            raise FinanceStorageBackupRotationError(
                "backup-local restore manifest drifted"
            )
        for logical_store in ("finance_raw", "operational"):
            with restore_registry.session(
                logical_store,
                mode="ro",
                operation="finance_backup_isolated_restore",
                manifest=restore_selected,
            ) as restore_connection:
                restore_connection.execute("SELECT 1").fetchone()
        raw_proof = proofs["raw"]
        operational_proof = proofs["operational"]
        raw_path = partial / RAW_BACKUP_FILENAME
        operational_path = partial / OPERATIONAL_BACKUP_FILENAME
        restore_raw = _sqlite_readback(raw_path, include_logical=True)
        restore_operational = _sqlite_readback(operational_path, include_logical=True)
        if (
            restore_raw["logical_tables"] != raw_proof["logical_tables"]
            or restore_operational["logical_tables"]
            != operational_proof["logical_tables"]
        ):
            raise FinanceStorageBackupRotationError(
                "isolated restore logical readback drifted"
            )
        restore_watermarks = _restore_watermarks(raw_path, operational_path)
        captured_at = min(
            str(raw_proof["captured_at"]),
            str(operational_proof["captured_at"]),
        )
        manifest_files = [
            _file_identity(partial / name, include_sha256=True)
            for name in (
                RAW_BACKUP_FILENAME,
                OPERATIONAL_BACKUP_FILENAME,
                SOURCE_MANIFEST_FILENAME,
            )
        ]
        backup_manifest: dict[str, Any] = {
            "contract_version": BACKUP_SET_CONTRACT,
            "status": "verified",
            "backup_id": backup_id,
            "created_by_deployed_sha": self.deployed_sha,
            "plan_fingerprint": str(plan["fingerprint"]),
            "source_manifest_sha256": current_guard["manifest_sha256"],
            "source_manifest_file_sha256": source_manifest_file_sha,
            "source_manifest": canonical_manifest,
            "restore_manifest_sha256": restore_selected.manifest_sha256,
            "restore_manifest_file_sha256": _sha256_file(manifest_copy),
            "source_identity": {
                "raw": {
                    **{
                        key: value
                        for key, value in current_guard["raw"].items()
                        if key != "openers"
                    },
                    **dict(raw_proof["source_file"]),
                    "path": str(raw_proof["source_path"]),
                    "captured_at": str(raw_proof["captured_at"]),
                },
                "operational": {
                    **{
                        key: value
                        for key, value in current_guard["operational"].items()
                        if key != "openers"
                    },
                    **dict(operational_proof["source_file"]),
                    "path": str(operational_proof["source_path"]),
                    "captured_at": str(operational_proof["captured_at"]),
                },
                "watermarks": restore_watermarks,
            },
            "files": [
                {
                    "name": item["name"],
                    "size_bytes": item["size_bytes"],
                    "allocated_bytes": item["allocated_bytes"],
                    "sha256": item["sha256"],
                }
                for item in manifest_files
            ],
            "sqlite_readback": {
                "raw": raw_proof,
                "operational": operational_proof,
            },
            "restore_drill": {
                "status": "verified",
                "isolated_target": str(final),
                "files": [RAW_BACKUP_FILENAME, OPERATIONAL_BACKUP_FILENAME],
                "manifest_sha256": restore_selected.manifest_sha256,
                "query_only": True,
                **restore_watermarks,
            },
            "captured_at": captured_at,
            "verified_at": _utc_now(),
        }
        prepared_manifest = transaction.get("backup_manifest_payload")
        if isinstance(prepared_manifest, Mapping):
            prepared_stable = {
                key: value
                for key, value in prepared_manifest.items()
                if key
                not in {
                    "fingerprint",
                    "verified_at",
                    "source_manifest_file_sha256",
                }
            }
            current_stable = {
                key: value
                for key, value in backup_manifest.items()
                if key not in {"verified_at", "source_manifest_file_sha256"}
            }
            if prepared_stable != current_stable or str(
                prepared_manifest.get("fingerprint") or ""
            ) != _fingerprint(
                {
                    key: value
                    for key, value in prepared_manifest.items()
                    if key != "fingerprint"
                }
            ):
                raise FinanceStorageBackupRotationError(
                    "prepared backup manifest drifted during resume"
                )
            backup_manifest = dict(prepared_manifest)
        else:
            backup_manifest["fingerprint"] = _fingerprint(backup_manifest)
            transaction["backup_manifest_payload"] = backup_manifest
            transaction["updated_at"] = _utc_now()
            _atomic_write_json(Path(transaction["transaction_path"]), transaction)
        self._write_backup_manifest(
            partial / BACKUP_MANIFEST_FILENAME,
            backup_manifest,
            transaction=transaction,
        )
        if fault_at == "after_backup_manifest":
            raise RuntimeError("injected fault after backup manifest")
        _fsync_directory(partial)
        transaction["phase"] = "replacement_verified"
        transaction["backup_manifest_fingerprint"] = backup_manifest["fingerprint"]
        transaction["updated_at"] = _utc_now()
        _atomic_write_json(Path(transaction["transaction_path"]), transaction)
        if fault_at == "after_replacement_verified":
            raise RuntimeError("injected fault after replacement verification")
        os.replace(partial, final)
        _fsync_directory(self.retained_root)
        return self._retained_candidate(final)

    def _select_current(
        self,
        candidate: Mapping[str, Any],
        *,
        plan_fingerprint: str,
    ) -> dict[str, Any]:
        selector: dict[str, Any] = {
            "contract_version": CURRENT_CONTRACT,
            "backup_id": candidate["artifact_id"],
            "backup_manifest_fingerprint": candidate["backup_manifest_fingerprint"],
            "plan_fingerprint": plan_fingerprint,
            "selected_at": _utc_now(),
        }
        selector["fingerprint"] = _fingerprint(selector)
        _atomic_write_json(self.current_path, selector)
        current = self._current()
        if (
            current is None
            or current["candidate"]["artifact_id"] != candidate["artifact_id"]
        ):
            raise FinanceStorageBackupRotationError(
                "atomic current selection readback failed"
            )
        return selector

    def _terminalize_superseded_pre_mutation_transaction(
        self,
        candidate: Mapping[str, Any],
        *,
        target_plan_fingerprint: str,
    ) -> dict[str, Any]:
        raw_path = Path(str(candidate.get("path") or ""))
        if raw_path.is_symlink():
            raise FinanceStorageBackupRotationError(
                "superseded Finance transaction path became a symlink"
            )
        path = raw_path.resolve()
        source_plan_fingerprint = str(
            candidate.get("source_plan_fingerprint") or ""
        )
        source_deployed_sha = str(candidate.get("source_deployed_sha") or "")
        receipt = {
            "path": str(path),
            "source_plan_fingerprint": source_plan_fingerprint,
            "source_deployed_sha": source_deployed_sha,
            "terminal_status": "superseded_before_mutation",
        }
        if (
            path.parent != self.transactions_root
            or re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is None
            or path.name
            != f"{source_plan_fingerprint.removeprefix('sha256:')}.json"
            or candidate.get("terminalization_allowed") is not True
            or _FINGERPRINT_RE.fullmatch(source_plan_fingerprint) is None
            or _SHA_RE.fullmatch(source_deployed_sha) is None
        ):
            raise FinanceStorageBackupRotationError(
                "superseded Finance transaction escapes the exact allowlist"
            )
        transaction = _load_json(
            path, label="superseded Finance backup transaction"
        )
        if transaction.get("phase") == "completed":
            if (
                transaction.get("terminal_status")
                != "superseded_before_mutation"
                or transaction.get("superseded_by_plan_fingerprint")
                != target_plan_fingerprint
                or transaction.get("superseded_by_deployed_sha")
                != self.deployed_sha
                or list(transaction.get("completed_deletions") or [])
                or dict(transaction.get("copy_proofs") or {})
            ):
                raise FinanceStorageBackupRotationError(
                    "completed superseded Finance transaction is ambiguous"
                )
        else:
            expected_identity = dict(candidate.get("transaction_identity") or {})
            current_identity = _file_identity(path, include_sha256=True)
            item_stat = path.stat()
            current_identity.update(
                {"uid": int(item_stat.st_uid), "gid": int(item_stat.st_gid)}
            )
            reviewed_plan = transaction.get("reviewed_plan")
            reviewed_stable = (
                {
                    key: value
                    for key, value in reviewed_plan.items()
                    if key not in {"fingerprint", "deploy_lease"}
                }
                if isinstance(reviewed_plan, Mapping)
                else {}
            )
            if (
                current_identity != expected_identity
                or transaction.get("contract_version") != TRANSACTION_CONTRACT
                or transaction.get("strategy") != STRATEGY
                or transaction.get("phase") != "started"
                or transaction.get("plan_fingerprint")
                != source_plan_fingerprint
                or transaction.get("deployed_sha") != source_deployed_sha
                or str(transaction.get("transaction_path") or "") != str(path)
                or not isinstance(reviewed_plan, Mapping)
                or reviewed_plan.get("fingerprint") != source_plan_fingerprint
                or reviewed_plan.get("deployed_sha") != source_deployed_sha
                or reviewed_plan.get("contract_version") != PLAN_CONTRACT
                or reviewed_plan.get("mode") != "snapshot_retention_dry_run"
                or reviewed_plan.get("strategy") != STRATEGY
                or reviewed_plan.get("runtime_dir") != str(self.runtime_dir)
                or reviewed_plan.get("snapshot_root") != str(self.snapshot_root)
                or reviewed_plan.get("archive_root") != str(self.backup_root)
                or reviewed_plan.get("apply_allowed_by_machine_preflight") is not True
                or list(reviewed_plan.get("blockers") or [])
                or _fingerprint(reviewed_stable) != source_plan_fingerprint
                or transaction.get("backup_id") != reviewed_plan.get("backup_id")
                or _BACKUP_ID_RE.fullmatch(
                    str(transaction.get("backup_id") or "")
                )
                is None
                or not str(transaction.get("approval_reference") or "").strip()
                or list(transaction.get("completed_deletions") or [])
                or dict(transaction.get("deletion_receipts") or {})
                or str(transaction.get("pending_deletion") or "")
                or dict(transaction.get("copy_proofs") or {})
                or isinstance(transaction.get("result"), Mapping)
                or transaction.get("audit_recorded") is True
            ):
                raise FinanceStorageBackupRotationError(
                    "superseded Finance transaction CAS drifted"
                )
            replacement = dict(reviewed_plan.get("replacement") or {})
            for key in ("destination_partial", "destination_final"):
                replacement_path = Path(str(replacement.get(key) or ""))
                if replacement_path.exists() or replacement_path.is_symlink():
                    raise FinanceStorageBackupRotationError(
                        "superseded Finance transaction owns replacement bytes"
                    )
            transaction["phase"] = "completed"
            transaction["terminal_status"] = "superseded_before_mutation"
            transaction["superseded_by_plan_fingerprint"] = (
                target_plan_fingerprint
            )
            transaction["superseded_by_deployed_sha"] = self.deployed_sha
            transaction["completed_at"] = _utc_now()
            transaction["updated_at"] = _utc_now()
            _atomic_write_json(path, transaction)
        if not _audit_contains_supersession(
            self.audit_path,
            source_plan_fingerprint=source_plan_fingerprint,
            target_plan_fingerprint=target_plan_fingerprint,
        ):
            _append_audit(
                self.audit_path,
                {
                    "event": (
                        "finance_backup_pre_mutation_transaction_superseded"
                    ),
                    "recorded_at": _utc_now(),
                    "source_plan_fingerprint": source_plan_fingerprint,
                    "source_deployed_sha": source_deployed_sha,
                    "target_plan_fingerprint": target_plan_fingerprint,
                    "target_deployed_sha": self.deployed_sha,
                    "completed_deletions": [],
                    "copy_proofs": {},
                },
            )
        if transaction.get("terminal_audit_recorded") is not True:
            transaction["terminal_audit_recorded"] = True
            transaction["updated_at"] = _utc_now()
            _atomic_write_json(path, transaction)
        return receipt

    def _write_policy(
        self,
        *,
        approval_reference: str,
        plan: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        policy: dict[str, Any] = {
            "contract_version": POLICY_CONTRACT,
            "enabled": True,
            "approved_at": _utc_now(),
            "approval_reference": approval_reference,
            "approved_plan_fingerprint": str(plan["fingerprint"]),
            "approved_deployed_sha": self.deployed_sha,
            "policy": dict(plan.get("policy") or {}),
            "last_success": {
                "at": _utc_now(),
                "backup_id": result.get("retained_backup_id"),
                "plan_fingerprint": str(plan["fingerprint"]),
            },
            "last_failure": None,
        }
        policy["fingerprint"] = _fingerprint(policy)
        _atomic_write_json(self.policy_path, policy)
        return policy

    def apply(
        self,
        *,
        reviewed_plan: dict[str, Any],
        expected_fingerprint: str,
        approval_reference: str,
        activate_policy: bool = True,
        fault_at: str = "",
    ) -> dict[str, Any]:
        approval = str(approval_reference or "").strip()
        if not approval:
            raise FinanceStorageBackupRotationError(
                "exact approval reference is required"
            )
        self._validate_plan(reviewed_plan, expected_fingerprint=expected_fingerprint)
        self.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.backup_root, 0o700)
        self.transactions_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        transaction_path = self.transactions_root / (
            expected_fingerprint.removeprefix("sha256:") + ".json"
        )
        lock_path = self.runtime_dir / LOCK_FILENAME
        lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FinanceStorageBackupRotationError(
                    "another Finance backup worker is active"
                ) from exc
            self._assert_plan_cas(reviewed_plan)
            expected_non_targets = dict(
                reviewed_plan.get("protected_non_targets") or {}
            )
            current_non_targets = self._protected_non_target_identity()
            if any(
                current_non_targets.get(key) != expected_non_targets.get(key)
                for key in (
                    "original_monolith",
                    "generation_root",
                    "generation_entries",
                    "generation_structural_digest",
                )
            ):
                raise FinanceStorageBackupRotationError(
                    "protected monolith/generation identity drifted before apply"
                )
            superseded_transaction_receipts = [
                self._terminalize_superseded_pre_mutation_transaction(
                    candidate,
                    target_plan_fingerprint=expected_fingerprint,
                )
                for candidate in reviewed_plan.get(
                    "pre_mutation_transaction_terminalizations"
                )
                or []
            ]
            transaction: dict[str, Any] = {
                "contract_version": TRANSACTION_CONTRACT,
                "strategy": STRATEGY,
                "plan_fingerprint": expected_fingerprint,
                "backup_id": reviewed_plan["backup_id"],
                "deployed_sha": self.deployed_sha,
                "approval_reference": approval,
                "transaction_path": str(transaction_path),
                "reviewed_plan": reviewed_plan,
                "phase": "started",
                "completed_deletions": [],
                "deletion_receipts": {},
                "pending_deletion": "",
                "copy_proofs": {},
                "superseded_pre_mutation_transactions": (
                    superseded_transaction_receipts
                ),
                "updated_at": _utc_now(),
            }
            transaction_preexisted = transaction_path.exists()
            if transaction_preexisted:
                existing = _load_json(
                    transaction_path, label="Finance backup transaction"
                )
                for key in (
                    "contract_version",
                    "strategy",
                    "plan_fingerprint",
                    "backup_id",
                    "deployed_sha",
                    "approval_reference",
                    "transaction_path",
                    "superseded_pre_mutation_transactions",
                ):
                    if existing.get(key) != transaction.get(key):
                        raise FinanceStorageBackupRotationError(
                            "existing Finance backup transaction is ambiguous"
                        )
                transaction = existing
            else:
                replacement = dict(reviewed_plan.get("replacement") or {})
                replacement_paths = (
                    Path(str(replacement.get("destination_partial") or "")),
                    Path(str(replacement.get("destination_final") or "")),
                )
                if any(
                    path.exists() or path.is_symlink() for path in replacement_paths
                ):
                    raise FinanceStorageBackupRotationError(
                        "unowned replacement path appeared before transaction"
                    )
                _atomic_write_json(transaction_path, transaction)
            if str(transaction.get("phase") or "") not in {
                "started",
                "pre_gc_complete",
                "operational_copied",
                "raw_copied",
                "replacement_verified",
                "current_selected",
                "post_gc_complete",
                "completed",
            }:
                raise FinanceStorageBackupRotationError(
                    "Finance backup transaction phase is invalid"
                )
            if transaction.get("phase") == "completed":
                terminal_result = transaction.get("result")
                if not isinstance(terminal_result, Mapping):
                    raise FinanceStorageBackupRotationError(
                        "completed Finance backup transaction lacks its result"
                    )
                stable_result = {
                    key: value
                    for key, value in terminal_result.items()
                    if key != "fingerprint"
                }
                current = self._current()
                if (
                    str(terminal_result.get("contract_version") or "")
                    != RESULT_CONTRACT
                    or str(terminal_result.get("status") or "") != "completed"
                    or str(terminal_result.get("plan_fingerprint") or "")
                    != expected_fingerprint
                    or str(terminal_result.get("fingerprint") or "")
                    != _fingerprint(stable_result)
                    or current is None
                    or str(current["selector"].get("plan_fingerprint") or "")
                    != expected_fingerprint
                    or str(current["candidate"].get("artifact_id") or "")
                    != str(terminal_result.get("retained_backup_id") or "")
                    or transaction.get("audit_recorded") is not True
                    or not _audit_contains(
                        self.audit_path,
                        plan_fingerprint=expected_fingerprint,
                        result_fingerprint=str(
                            terminal_result.get("fingerprint") or ""
                        ),
                    )
                ):
                    raise FinanceStorageBackupRotationError(
                        "completed Finance backup transaction is ambiguous"
                    )
                self._assert_inventory_cas(reviewed_plan)
                if any(
                    Path(str(candidate.get("path") or "")).exists()
                    for candidate in (
                        list(reviewed_plan.get("pre_publish_deletions") or [])
                        + list(reviewed_plan.get("post_publish_deletions") or [])
                    )
                ):
                    raise FinanceStorageBackupRotationError(
                        "completed deletion candidate reappeared"
                    )
                return dict(terminal_result)
            self._assert_inventory_cas(reviewed_plan)
            completed_deletions = set(transaction.get("completed_deletions") or [])
            pending_deletion = str(transaction.get("pending_deletion") or "")
            for candidate in list(
                reviewed_plan.get("pre_publish_deletions") or []
            ) + list(reviewed_plan.get("post_publish_deletions") or []):
                candidate_path = Path(str(candidate.get("path") or ""))
                candidate_fingerprint = str(
                    candidate.get("candidate_fingerprint") or ""
                )
                if candidate_path.exists():
                    if candidate_fingerprint != pending_deletion:
                        self._assert_candidate(candidate)
                elif (
                    candidate_fingerprint not in completed_deletions
                    and candidate_fingerprint != pending_deletion
                ):
                    raise FinanceStorageBackupRotationError(
                        "reviewed deletion candidate disappeared before apply"
                    )
            expected_current = reviewed_plan.get("current_before")
            actual_current = self._current()
            if expected_current is None and actual_current is not None:
                # An exact resume may already have published this plan's set.
                if (
                    str(actual_current["selector"].get("plan_fingerprint") or "")
                    != expected_fingerprint
                ):
                    raise FinanceStorageBackupRotationError(
                        "current selector appeared after the reviewed plan"
                    )
            elif expected_current is not None:
                expected_selector = dict(expected_current.get("selector") or {})
                if (
                    actual_current is None
                    or actual_current["selector"] != expected_selector
                ):
                    if (
                        actual_current is None
                        or str(actual_current["selector"].get("plan_fingerprint") or "")
                        != expected_fingerprint
                    ):
                        raise FinanceStorageBackupRotationError(
                            "current selector CAS drifted after the reviewed plan"
                        )
            removed: list[dict[str, Any]] = []
            for candidate in reviewed_plan.get("pre_publish_deletions") or []:
                removed.append(
                    self._delete_candidate(
                        candidate,
                        transaction=transaction,
                        fault_at=fault_at,
                    )
                )
            phase = str(transaction.get("phase") or "")
            if phase in {"started", "pre_gc_complete"}:
                transaction["phase"] = "pre_gc_complete"
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(transaction_path, transaction)
            elif phase not in {
                "operational_copied",
                "raw_copied",
                "replacement_verified",
                "current_selected",
                "post_gc_complete",
                "completed",
            }:
                raise FinanceStorageBackupRotationError(
                    "Finance backup transaction phase is invalid"
                )
            if fault_at == "after_pre_publish_gc":
                raise RuntimeError("injected fault after pre-publish GC")
            replacement_due = bool((reviewed_plan.get("replacement") or {}).get("due"))
            if replacement_due:
                replacement = self._copy_replacement(
                    reviewed_plan, transaction=transaction, fault_at=fault_at
                )
                selected = self._current()
                if (
                    selected is not None
                    and selected["candidate"]["artifact_id"]
                    == replacement["artifact_id"]
                    and str(selected["selector"].get("plan_fingerprint") or "")
                    == expected_fingerprint
                ):
                    selector = selected["selector"]
                else:
                    selector = self._select_current(
                        replacement, plan_fingerprint=expected_fingerprint
                    )
                transaction["phase"] = "current_selected"
                transaction["current_selector_fingerprint"] = selector["fingerprint"]
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(transaction_path, transaction)
            else:
                current = self._current()
                if current is None:
                    raise FinanceStorageBackupRotationError(
                        "replacement was skipped without a verified current set"
                    )
                replacement = current["candidate"]
            if fault_at == "after_current_selected":
                raise RuntimeError("injected fault after current selection")
            for candidate in reviewed_plan.get("post_publish_deletions") or []:
                if candidate.get("artifact_id") == replacement.get("artifact_id"):
                    raise FinanceStorageBackupRotationError(
                        "current replacement entered the deletion allowlist"
                    )
                removed.append(
                    self._delete_candidate(
                        candidate,
                        transaction=transaction,
                        fault_at=fault_at,
                    )
                )
            self._assert_inventory_cas(reviewed_plan)
            transaction["phase"] = "post_gc_complete"
            transaction["updated_at"] = _utc_now()
            _atomic_write_json(transaction_path, transaction)
            if fault_at == "after_post_publish_gc":
                raise RuntimeError("injected fault after post-publish GC")
            root_capacity = _filesystem(self.runtime_dir)
            backup_capacity = _filesystem(self.backup_mount)
            final_non_targets = self._protected_non_target_identity()
            if final_non_targets != current_non_targets:
                raise FinanceStorageBackupRotationError(
                    "protected monolith/generation identity drifted during apply"
                )
            result: dict[str, Any] = {
                "contract_version": RESULT_CONTRACT,
                "status": "completed",
                "strategy": STRATEGY,
                "deployed_sha": self.deployed_sha,
                "plan_fingerprint": expected_fingerprint,
                "approval_reference": approval,
                "completed_at": _utc_now(),
                "replacement_performed": replacement_due,
                "replacement_verified": True,
                "retained_backup_id": replacement["artifact_id"],
                "retained_backup_count": 1,
                "retained_backup_bytes": int(replacement["total_bytes"]),
                "backup_manifest_fingerprint": replacement[
                    "backup_manifest_fingerprint"
                ],
                "removed_artifacts": removed,
                "removed_artifact_count": len(removed),
                "archived_snapshot_count": len(removed),
                "superseded_pre_mutation_transactions": (
                    superseded_transaction_receipts
                ),
                "released_allocated_bytes": sum(
                    int(item.get("released_allocated_bytes") or 0) for item in removed
                ),
                "root_available_after_bytes": root_capacity["available_bytes"],
                "backup_available_after_bytes": backup_capacity["available_bytes"],
                "root_target_bytes": self.root_target_bytes,
                "backup_target_bytes": self.backup_target_bytes,
                "capacity_sufficient": (
                    root_capacity["available_bytes"] >= self.root_target_bytes
                    and backup_capacity["available_bytes"] >= self.backup_target_bytes
                ),
                "next_replacement_capacity": (
                    backup_capacity["available_bytes"]
                    >= int(replacement["allocated_bytes"])
                    + DEFAULT_COPY_OVERHEAD_BYTES
                    + self.hard_reserve_bytes
                ),
                "projected_30_day_growth_bytes": 0,
                "projected_90_day_growth_bytes": 0,
                "projected_30_day_available_bytes": backup_capacity["available_bytes"],
                "projected_90_day_available_bytes": backup_capacity["available_bytes"],
                "protected_original_monolith_touched": False,
                "protected_non_target_identity": final_non_targets,
                "split_generation_touched": False,
                "live_monolith_touched": False,
                "fail_closed": True,
            }
            result["fingerprint"] = _fingerprint(result)
            existing_result = transaction.get("result")
            if isinstance(existing_result, Mapping):
                stable_existing = {
                    key: value
                    for key, value in existing_result.items()
                    if key != "fingerprint"
                }
                if (
                    str(existing_result.get("contract_version") or "")
                    != RESULT_CONTRACT
                    or str(existing_result.get("status") or "") != "completed"
                    or str(existing_result.get("plan_fingerprint") or "")
                    != expected_fingerprint
                    or str(existing_result.get("retained_backup_id") or "")
                    != str(replacement["artifact_id"])
                    or str(existing_result.get("fingerprint") or "")
                    != _fingerprint(stable_existing)
                ):
                    raise FinanceStorageBackupRotationError(
                        "terminal Finance backup result is ambiguous"
                    )
                result = dict(existing_result)
            else:
                transaction["result"] = result
                transaction["result_fingerprint"] = result["fingerprint"]
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(transaction_path, transaction)
            if activate_policy:
                existing_policy = (
                    _load_json(self.policy_path, label="Finance backup policy")
                    if self.policy_path.is_file()
                    else None
                )
                if (
                    existing_policy is None
                    or str(existing_policy.get("approved_plan_fingerprint") or "")
                    != expected_fingerprint
                ):
                    self._write_policy(
                        approval_reference=approval,
                        plan=reviewed_plan,
                        result=result,
                    )
            if transaction.get("audit_recorded") is not True:
                if not _audit_contains(
                    self.audit_path,
                    plan_fingerprint=expected_fingerprint,
                    result_fingerprint=str(result["fingerprint"]),
                ):
                    _append_audit(
                        self.audit_path,
                        {
                            "event": "finance_backup_rotation_completed",
                            "recorded_at": _utc_now(),
                            "plan_fingerprint": expected_fingerprint,
                            "result_fingerprint": result["fingerprint"],
                            "retained_backup_id": result["retained_backup_id"],
                            "removed_artifacts": [
                                item.get("path") for item in result["removed_artifacts"]
                            ],
                            "deployed_sha": self.deployed_sha,
                        },
                    )
                transaction["audit_recorded"] = True
                transaction["phase"] = "completed"
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(transaction_path, transaction)
            elif transaction.get("phase") != "completed":
                transaction["phase"] = "completed"
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(transaction_path, transaction)
            return result
        finally:
            os.close(lock_descriptor)

    def readback(
        self, *, reviewed_plan: dict[str, Any], expected_fingerprint: str
    ) -> dict[str, Any]:
        self._validate_plan(reviewed_plan, expected_fingerprint=expected_fingerprint)
        guard = self._guard()
        self._device_guard()
        expected_non_targets = dict(reviewed_plan.get("protected_non_targets") or {})
        current_non_targets = self._protected_non_target_identity()
        if any(
            current_non_targets.get(key) != expected_non_targets.get(key)
            for key in (
                "original_monolith",
                "generation_root",
                "generation_entries",
                "generation_structural_digest",
            )
        ):
            raise FinanceStorageBackupRotationError(
                "protected monolith/generation readback drifted"
            )
        current = self._current()
        if current is None:
            raise FinanceStorageBackupRotationError("verified current backup is absent")
        candidate = current["candidate"]
        for item in list(reviewed_plan.get("pre_publish_deletions") or []) + list(
            reviewed_plan.get("post_publish_deletions") or []
        ):
            if Path(str(item.get("path") or "")).exists():
                raise FinanceStorageBackupRotationError(
                    "reviewed superseded artifact still exists"
                )
        raw_readback = _sqlite_readback(
            Path(candidate["path"]) / RAW_BACKUP_FILENAME, include_logical=True
        )
        operational_readback = _sqlite_readback(
            Path(candidate["path"]) / OPERATIONAL_BACKUP_FILENAME,
            include_logical=True,
        )
        manifest = _load_json(
            Path(candidate["path"]) / BACKUP_MANIFEST_FILENAME,
            label="current backup manifest",
        )
        restore_registry = StoreRegistry(Path(candidate["path"]))
        restore_selected = restore_registry.load(require_files=True)
        for logical_store in ("finance_raw", "operational"):
            with restore_registry.session(
                logical_store,
                mode="ro",
                operation="finance_backup_terminal_restore_readback",
                manifest=restore_selected,
            ) as restore_connection:
                restore_connection.execute("SELECT 1").fetchone()
        if (
            raw_readback["logical_tables"]
            != manifest["sqlite_readback"]["raw"]["logical_tables"]
            or operational_readback["logical_tables"]
            != manifest["sqlite_readback"]["operational"]["logical_tables"]
        ):
            raise FinanceStorageBackupRotationError("current restore readback drifted")
        root_capacity = _filesystem(self.runtime_dir)
        backup_capacity = _filesystem(self.backup_mount)
        retained, _protected = [], []
        _legacy, retained, protected = self._backup_inventory()
        current_ids = [item["artifact_id"] for item in retained]
        if current_ids != [candidate["artifact_id"]] or protected:
            raise FinanceStorageBackupRotationError(
                "post-rotation retained inventory is not exactly one verified set"
            )
        health = backup_rotation_health(self.runtime_dir)
        if (
            health.get("status") != "healthy"
            or health.get("retained_backup_id") != candidate["artifact_id"]
            or int(health.get("retained_count") or 0) != 1
            or health.get("next_replacement_capacity") is not True
        ):
            raise FinanceStorageBackupRotationError(
                "post-rotation policy/health readback is not healthy"
            )
        terminalized_transactions: list[dict[str, Any]] = []
        for item in reviewed_plan.get(
            "pre_mutation_transaction_terminalizations"
        ) or []:
            path = Path(str(item.get("path") or "")).resolve()
            transaction = _load_json(
                path, label="terminalized Finance backup transaction"
            )
            if (
                path.parent != self.transactions_root
                or transaction.get("phase") != "completed"
                or transaction.get("terminal_status")
                != "superseded_before_mutation"
                or transaction.get("superseded_by_plan_fingerprint")
                != expected_fingerprint
                or transaction.get("superseded_by_deployed_sha")
                != self.deployed_sha
                or transaction.get("terminal_audit_recorded") is not True
                or list(transaction.get("completed_deletions") or [])
                or dict(transaction.get("copy_proofs") or {})
                or not _audit_contains_supersession(
                    self.audit_path,
                    source_plan_fingerprint=str(
                        item.get("source_plan_fingerprint") or ""
                    ),
                    target_plan_fingerprint=expected_fingerprint,
                )
            ):
                raise FinanceStorageBackupRotationError(
                    "superseded pre-mutation transaction readback is incomplete"
                )
            terminalized_transactions.append(
                {
                    "path": str(path),
                    "source_plan_fingerprint": str(
                        item.get("source_plan_fingerprint") or ""
                    ),
                    "source_deployed_sha": str(
                        item.get("source_deployed_sha") or ""
                    ),
                    "terminal_status": "superseded_before_mutation",
                }
            )
        payload: dict[str, Any] = {
            "contract_version": RESULT_CONTRACT,
            "status": "readback_verified",
            "strategy": STRATEGY,
            "deployed_sha": self.deployed_sha,
            "plan_fingerprint": expected_fingerprint,
            "verified_at": _utc_now(),
            "retained_backup_id": candidate["artifact_id"],
            "retained_backup_count": 1,
            "retained_backup_bytes": candidate["total_bytes"],
            "replacement_verified": True,
            "restore_drill_verified": True,
            "superseded_pre_mutation_transactions": (
                terminalized_transactions
            ),
            "source_manifest_sha256": manifest["source_manifest_sha256"],
            "restore_manifest_sha256": restore_selected.manifest_sha256,
            "active_manifest_sha256": guard["manifest_sha256"],
            "root_available_bytes": root_capacity["available_bytes"],
            "backup_available_bytes": backup_capacity["available_bytes"],
            "root_target_bytes": self.root_target_bytes,
            "backup_target_bytes": self.backup_target_bytes,
            "capacity_sufficient": (
                root_capacity["available_bytes"] >= self.root_target_bytes
                and backup_capacity["available_bytes"] >= self.backup_target_bytes
            ),
            "next_replacement_capacity": (
                backup_capacity["available_bytes"]
                >= int(candidate["allocated_bytes"])
                + DEFAULT_COPY_OVERHEAD_BYTES
                + self.hard_reserve_bytes
            ),
            "root_long_lived_snapshot_count": len(
                [path for path in self.snapshot_root.iterdir()]
                if self.snapshot_root.is_dir()
                else []
            ),
            "backup_legacy_snapshot_count": len(_legacy),
            "projected_30_day_growth_bytes": 0,
            "projected_90_day_growth_bytes": 0,
            "projected_30_day_available_bytes": backup_capacity["available_bytes"],
            "projected_90_day_available_bytes": backup_capacity["available_bytes"],
            "backup_health": health,
            "protected_original_monolith_touched": False,
            "protected_non_target_identity": current_non_targets,
            "split_generation_touched": False,
            "live_monolith_touched": False,
            "fail_closed": True,
        }
        if (
            payload["capacity_sufficient"] is not True
            or payload["next_replacement_capacity"] is not True
            or payload["root_long_lived_snapshot_count"] != 0
            or payload["backup_legacy_snapshot_count"] != 0
        ):
            raise FinanceStorageBackupRotationError(
                "Finance backup readback lacks terminal capacity or retention proof"
            )
        payload["fingerprint"] = _fingerprint(payload)
        return payload


def backup_rotation_health(runtime_dir: Path) -> dict[str, Any]:
    """Return bounded operator health without mutating or trusting stale state."""

    runtime = Path(runtime_dir).expanduser().resolve()
    backup_root = (runtime / ARCHIVE_RELATIVE_ROOT).resolve()
    current_path = backup_root / CURRENT_FILENAME
    policy_path = backup_root / POLICY_FILENAME
    result: dict[str, Any] = {
        "contract_version": "wb_core_finance_storage_backup_health_v1",
        "status": "unconfigured",
        "retained_backup_id": "",
        "retained_count": 0,
        "retained_bytes": 0,
        "age_seconds": None,
        "rpo_seconds": DEFAULT_MAX_AGE_SECONDS,
        "rto_seconds": 4 * 60 * 60,
        "next_replacement_capacity": False,
        "last_success": None,
        "last_failure": None,
        "projected_30_day_growth_bytes": 0,
        "projected_90_day_growth_bytes": 0,
        "projected_30_day_available_bytes": None,
        "projected_90_day_available_bytes": None,
        "blockers": [],
    }
    try:
        if not policy_path.is_file() or not current_path.is_file():
            result["blockers"] = ["backup policy/current selector is not activated"]
            return result
        policy = _load_json(policy_path, label="Finance backup policy")
        stable_policy = {
            key: value for key, value in policy.items() if key != "fingerprint"
        }
        if (
            policy.get("contract_version") != POLICY_CONTRACT
            or policy.get("enabled") is not True
            or policy.get("fingerprint") != _fingerprint(stable_policy)
            or not str(policy.get("approval_reference") or "").strip()
        ):
            raise FinanceStorageBackupRotationError("Finance backup policy is invalid")
        selector = _load_json(current_path, label="Finance current selector")
        stable_selector = {
            key: value for key, value in selector.items() if key != "fingerprint"
        }
        backup_id = str(selector.get("backup_id") or "")
        if (
            selector.get("contract_version") != CURRENT_CONTRACT
            or _BACKUP_ID_RE.fullmatch(backup_id) is None
            or selector.get("fingerprint") != _fingerprint(stable_selector)
        ):
            raise FinanceStorageBackupRotationError(
                "Finance current selector is invalid"
            )
        manifest = _load_json(
            backup_root / RETAINED_DIRECTORY / backup_id / BACKUP_MANIFEST_FILENAME,
            label="Finance current backup manifest",
        )
        stable_manifest = {
            key: value for key, value in manifest.items() if key != "fingerprint"
        }
        if (
            manifest.get("contract_version") != BACKUP_SET_CONTRACT
            or manifest.get("status") != "verified"
            or manifest.get("fingerprint") != _fingerprint(stable_manifest)
            or selector.get("backup_manifest_fingerprint")
            != manifest.get("fingerprint")
        ):
            raise FinanceStorageBackupRotationError("Finance current backup is invalid")
        captured_at = str(manifest.get("captured_at") or "")
        if not captured_at:
            raise FinanceStorageBackupRotationError(
                "Finance current backup lacks its data capture time"
            )
        retained_root = backup_root / RETAINED_DIRECTORY
        selected_root = retained_root / backup_id
        if retained_root.is_symlink() or not retained_root.is_dir():
            raise FinanceStorageBackupRotationError(
                "Finance retained backup root is unsafe"
            )
        retained_entries = sorted(retained_root.iterdir(), key=lambda item: item.name)
        retained_ids = [
            item.name
            for item in retained_entries
            if item.is_dir()
            and not item.is_symlink()
            and _BACKUP_ID_RE.fullmatch(item.name)
        ]
        inventory_blockers: list[str] = []
        if retained_ids != [backup_id]:
            inventory_blockers.append(
                "retained inventory is not exactly the selected current set"
            )
        if len(retained_entries) != len(retained_ids):
            inventory_blockers.append(
                "retained inventory contains partial, foreign or unsafe entries"
            )
        selected_directory_identity = _directory_identity(selected_root)
        actual_files: dict[str, dict[str, Any]] = {}
        for item in selected_root.iterdir():
            if item.is_file() and not item.is_symlink():
                # The selector and signed manifest preserve the terminal byte
                # hashes. Operator health is intentionally stat-bounded so an
                # ordinary UI request never re-hashes an 18+ GiB restore set.
                identity = _file_identity(item, include_sha256=False)
                stat = item.stat()
                identity.update({"uid": int(stat.st_uid), "gid": int(stat.st_gid)})
                actual_files[item.name] = identity
        if set(actual_files) != _BACKUP_FILES:
            raise FinanceStorageBackupRotationError(
                "Finance current backup file inventory drifted"
            )
        if (
            int(selected_directory_identity["mode"]) != 0o700
            or any(int(item["mode"]) != 0o600 for item in actual_files.values())
            or any(
                int(item["uid"]) != int(selected_directory_identity["uid"])
                or int(item["gid"]) != int(selected_directory_identity["gid"])
                for item in actual_files.values()
            )
        ):
            raise FinanceStorageBackupRotationError(
                "Finance current backup permissions/ownership are unsafe"
            )
        declared_files = {
            str(item.get("name") or ""): dict(item)
            for item in manifest.get("files") or []
            if isinstance(item, Mapping)
        }
        for name in (
            RAW_BACKUP_FILENAME,
            OPERATIONAL_BACKUP_FILENAME,
            SOURCE_MANIFEST_FILENAME,
        ):
            expected = declared_files.get(name)
            actual = actual_files[name]
            if expected is None or int(expected.get("size_bytes") or -1) != int(
                actual["size_bytes"]
            ):
                raise FinanceStorageBackupRotationError(
                    "Finance current backup file size drifted"
                )
        retained_bytes = sum(
            int(item["allocated_bytes"]) for item in actual_files.values()
        )
        age = max(
            0,
            int(
                (datetime.now(timezone.utc) - _parse_time(captured_at)).total_seconds()
            ),
        )
        capacity = _filesystem(backup_root.parent)
        policy_settings = dict(policy.get("policy") or {})
        hard_reserve = int(
            policy_settings["hard_reserve_bytes"]
            if policy_settings.get("hard_reserve_bytes") is not None
            else DEFAULT_HARD_RESERVE_BYTES
        )
        degraded_available = int(
            policy_settings["degraded_available_bytes"]
            if policy_settings.get("degraded_available_bytes") is not None
            else DEFAULT_DEGRADED_AVAILABLE_BYTES
        )
        rpo = int(
            policy_settings["rpo_seconds"]
            if policy_settings.get("rpo_seconds") is not None
            else DEFAULT_MAX_AGE_SECONDS
        )
        blockers: list[str] = list(inventory_blockers)
        if age > rpo:
            blockers.append("retained backup exceeded RPO age")
        retained_count_cap = int(
            policy_settings.get("retained_count_cap") or DEFAULT_RETAINED_COUNT
        )
        retained_bytes_cap = int(
            policy_settings.get("retained_bytes_cap") or DEFAULT_MAX_SET_BYTES
        )
        if len(retained_ids) > retained_count_cap:
            blockers.append("retained backup count exceeded its hard cap")
        if retained_bytes > retained_bytes_cap:
            blockers.append("retained backup bytes exceeded their hard cap")
        for root in (runtime / SNAPSHOT_DIRECTORY, backup_root):
            if root.is_dir() and any(
                _LEGACY_ID_RE.fullmatch(item.name) for item in root.iterdir()
            ):
                blockers.append("legacy Finance snapshots remain outside steady state")
                break
        transactions_root = backup_root / TRANSACTIONS_DIRECTORY
        if transactions_root.is_dir():
            for transaction_path in transactions_root.iterdir():
                if (
                    transaction_path.is_symlink()
                    or not transaction_path.is_file()
                    or re.fullmatch(r"[0-9a-f]{64}\.json", transaction_path.name)
                    is None
                ):
                    blockers.append("Finance backup transaction inventory is unsafe")
                    break
                transaction = _load_json(
                    transaction_path, label="Finance backup health transaction"
                )
                if (
                    transaction.get("contract_version") != TRANSACTION_CONTRACT
                    or transaction.get("phase") != "completed"
                ):
                    blockers.append("Finance backup replacement is non-terminal")
                    break
        next_replacement_required = (
            retained_bytes + DEFAULT_COPY_OVERHEAD_BYTES + hard_reserve
        )
        if capacity["available_bytes"] < next_replacement_required:
            blockers.append("insufficient capacity for atomic next replacement")
        if capacity["available_bytes"] < degraded_available:
            blockers.append("backup filesystem crossed degraded watermark")
        result.update(
            {
                "status": "healthy" if not blockers else "degraded",
                "retained_backup_id": backup_id,
                "retained_count": len(retained_ids),
                "retained_bytes": retained_bytes,
                "age_seconds": age,
                "captured_at": captured_at,
                "verified_at": str(manifest.get("verified_at") or ""),
                "byte_hash_status": "verified_at_selection_and_terminal_readback",
                "rpo_seconds": rpo,
                "rto_seconds": int(
                    policy_settings["rto_seconds"]
                    if policy_settings.get("rto_seconds") is not None
                    else 4 * 60 * 60
                ),
                "available_bytes": capacity["available_bytes"],
                "next_replacement_required_bytes": next_replacement_required,
                "degraded_available_bytes": degraded_available,
                "next_replacement_capacity": (
                    capacity["available_bytes"] >= next_replacement_required
                ),
                "last_success": policy.get("last_success"),
                "last_failure": policy.get("last_failure"),
                "projected_30_day_available_bytes": capacity["available_bytes"],
                "projected_90_day_available_bytes": capacity["available_bytes"],
                "blockers": blockers,
            }
        )
    except (
        FinanceStorageSnapshotRetentionError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        result["status"] = "blocked"
        result["blockers"] = [f"{type(exc).__name__}: {str(exc)[:300]}"]
    return result


def scheduled_rotation(
    runtime_dir: Path,
    *,
    deployed_sha: str,
    require_distinct_device: bool = True,
    require_backup_mountpoint: bool = True,
) -> dict[str, Any]:
    """Run the already-approved policy; absent policy is an inert no-op."""

    runtime = Path(runtime_dir).expanduser().resolve()
    policy_path = runtime / ARCHIVE_RELATIVE_ROOT / POLICY_FILENAME
    if not policy_path.is_file():
        return {
            "contract_version": RESULT_CONTRACT,
            "status": "policy_inert",
            "mutation_count": 0,
        }
    policy = _load_json(policy_path, label="Finance scheduled backup policy")
    stable = {key: value for key, value in policy.items() if key != "fingerprint"}
    if (
        policy.get("contract_version") != POLICY_CONTRACT
        or policy.get("enabled") is not True
        or policy.get("fingerprint") != _fingerprint(stable)
        or not str(policy.get("approval_reference") or "").strip()
    ):
        raise FinanceStorageBackupRotationError(
            "scheduled Finance backup policy is invalid"
        )

    def record_failure(exc: Exception, plan_fingerprint: str = "") -> None:
        failed = dict(policy)
        failed.pop("fingerprint", None)
        failed["last_failure"] = {
            "at": _utc_now(),
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "plan_fingerprint": plan_fingerprint,
        }
        failed["fingerprint"] = _fingerprint(failed)
        _atomic_write_json(policy_path, failed)

    def record_success(result: Mapping[str, Any], plan_fingerprint: str) -> None:
        succeeded = dict(policy)
        succeeded.pop("fingerprint", None)
        succeeded["last_success"] = {
            "at": _utc_now(),
            "backup_id": result.get("retained_backup_id"),
            "plan_fingerprint": plan_fingerprint,
        }
        succeeded["last_failure"] = None
        succeeded["fingerprint"] = _fingerprint(succeeded)
        _atomic_write_json(policy_path, succeeded)

    configured = dict(policy.get("policy") or {})

    def configured_int(key: str, default: int) -> int:
        value = configured.get(key)
        return int(value if value is not None else default)

    rotation = FinanceStorageBackupRotation(
        runtime,
        deployed_sha=deployed_sha,
        require_distinct_device=require_distinct_device,
        require_backup_mountpoint=require_backup_mountpoint,
        root_target_bytes=configured_int(
            "root_target_bytes", DEFAULT_ROOT_TARGET_BYTES
        ),
        backup_target_bytes=configured_int(
            "backup_target_bytes", DEFAULT_BACKUP_TARGET_BYTES
        ),
        hard_reserve_bytes=configured_int(
            "hard_reserve_bytes", DEFAULT_HARD_RESERVE_BYTES
        ),
        degraded_available_bytes=configured_int(
            "degraded_available_bytes", DEFAULT_DEGRADED_AVAILABLE_BYTES
        ),
        max_set_bytes=configured_int("retained_bytes_cap", DEFAULT_MAX_SET_BYTES),
        max_age_seconds=configured_int("age_cap_seconds", DEFAULT_MAX_AGE_SECONDS),
        minimum_replacement_interval_seconds=configured_int(
            "minimum_interval_seconds",
            DEFAULT_MIN_REPLACEMENT_INTERVAL_SECONDS,
        ),
    )
    transactions_root = runtime / ARCHIVE_RELATIVE_ROOT / TRANSACTIONS_DIRECTORY
    if transactions_root.is_dir():
        transaction_paths = sorted(transactions_root.iterdir())
        if any(
            path.is_symlink()
            or not path.is_file()
            or re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is None
            for path in transaction_paths
        ):
            raise FinanceStorageBackupRotationError(
                "scheduled Finance backup transaction inventory is unsafe"
            )
        pending_transactions: list[tuple[Path, dict[str, Any]]] = []
        for transaction_path in transaction_paths:
            transaction = _load_json(
                transaction_path, label="Finance scheduled backup transaction"
            )
            if transaction.get("contract_version") != TRANSACTION_CONTRACT:
                raise FinanceStorageBackupRotationError(
                    "scheduled Finance backup transaction contract is invalid"
                )
            if transaction.get("phase") == "completed":
                continue
            pending_transactions.append((transaction_path, transaction))
        if len(pending_transactions) > 1:
            raise FinanceStorageBackupRotationError(
                "multiple non-terminal Finance backup transactions are ambiguous"
            )
        for transaction_path, transaction in pending_transactions:
            pending_plan = transaction.get("reviewed_plan")
            if not isinstance(pending_plan, dict):
                raise FinanceStorageBackupRotationError(
                    "non-terminal scheduled transaction lacks its reviewed plan"
                )
            if str(pending_plan.get("deployed_sha") or "") != deployed_sha:
                raise FinanceStorageBackupRotationError(
                    "non-terminal scheduled transaction belongs to another deployed SHA"
                )
            try:
                result = rotation.apply(
                    reviewed_plan=pending_plan,
                    expected_fingerprint=str(transaction.get("plan_fingerprint") or ""),
                    approval_reference=str(policy["approval_reference"]),
                    activate_policy=False,
                )
                record_success(result, str(transaction.get("plan_fingerprint") or ""))
                return result
            except Exception as exc:
                record_failure(exc, str(transaction.get("plan_fingerprint") or ""))
                raise
    try:
        plan = rotation.build_plan(cleanup_legacy=False, scheduled=True)
    except Exception as exc:
        record_failure(exc)
        raise
    if not bool((plan.get("replacement") or {}).get("due")):
        return {
            "contract_version": RESULT_CONTRACT,
            "status": "not_due",
            "mutation_count": 0,
            "plan_fingerprint": plan["fingerprint"],
            "health": backup_rotation_health(runtime),
        }
    try:
        result = rotation.apply(
            reviewed_plan=plan,
            expected_fingerprint=str(plan["fingerprint"]),
            approval_reference=str(policy["approval_reference"]),
            activate_policy=False,
        )
        record_success(result, str(plan.get("fingerprint") or ""))
        return result
    except Exception as exc:
        record_failure(exc, str(plan.get("fingerprint") or ""))
        raise
