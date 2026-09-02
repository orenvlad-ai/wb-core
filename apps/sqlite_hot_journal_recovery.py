#!/usr/bin/env python3
"""Guarded recovery of one exact split operational SQLite hot journal.

The dry-run is read-only.  Apply is admitted only from a reviewed external
plan and lets SQLite itself play back the rollback journal; it never issues
business SQL.
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
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.business_data_maintenance as maintenance  # noqa: E402
from apps.recovery_file_utils import file_sha256  # noqa: E402
from packages.application.business_data_write_barrier import (  # noqa: E402
    barrier_status,
)
from packages.application.storage_registry import (  # noqa: E402
    MANIFEST_FILENAME,
    StoreRegistry,
    manifest_payload,
)


CONTRACT_NAME = "wbc0027_s047_split_hot_journal_recovery_v1"
RESULT_CONTRACT_NAME = "wbc0027_s047_split_hot_journal_recovery_result_v1"
MARKER_FILENAME = ".sqlite-hot-journal-recovery.json"
DEFAULT_BACKUP_ROOT = Path("/opt/wb-core-runtime/state/backups")
DEFAULT_RESERVE_BYTES = 42_198_454_272
DEFAULT_EVIDENCE_ENVELOPE_BYTES = 1_048_576
EXPECTED_JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
EXPECTED_PAGE_SIZE = 4096
EXPECTED_PARTIAL_EPOCH_SCHEMA = (
    "business_data_prepared_abort_partial_restore_recovery_v1"
)
EXPECTED_SOURCE_EPOCH_SHA = "a3761ea1ab96562f25a0f7b4542de4aa050cc941"
EXPECTED_WINDOW_ID = "wbc0027-s047-live-last-good-freeze-v2-896b02c0"
EXPECTED_GENERATION_ID = "c54072027f14f90b374b"
EXPECTED_DATABASE_SHA256 = (
    "63ef76ecd53c7fbe3ac46240720dde0f2ef4f8192709041a5753ddfe972e461b"
)
EXPECTED_DATABASE_SIZE = 7_434_108_928
EXPECTED_JOURNAL_SHA256 = (
    "0a9f0217242120c7bfac5444505b0dba3a8e2a5d72e9d66410dc88bb90e40d46"
)
EXPECTED_JOURNAL_SIZE = 7_949_552
EXPECTED_JOURNAL_RECORDS = 169
EXPECTED_RECOVERED_DATABASE_SHA256 = (
    "92d2f05c503afed742f58f0b318eff7b78ce32e1be2979275a205e31ac26f70f"
)
EXPECTED_RAW_SHA256 = (
    "187d1925f3989ec691809416ff49c6283b6c845d820774ee402dcbf7ecdf3fb9"
)
EXPECTED_MANIFEST_FILE_SHA256 = (
    "39c300c2e1be56eda3965163cec6cd5f333cfbce82aa65df98c04a97d1b27920"
)
RECOVERY_PAUSE_OWNED_TIMERS = (
    "wb-core-finance-backup-rotation.timer",
    "wb-core-fbs-warehouse-registry.timer",
    "wb-core-sheet-vitrina-canary-restore.timer",
    "wb-core-sheet-vitrina-health-candidate.timer",
    "wb-core-sheet-vitrina-health-confirmation.timer",
    "wb-core-fbs-shadow-collector.timer",
)
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
OPERATION_PATTERN = re.compile(r"[0-9a-f]{64}")


class HotJournalRecoveryError(RuntimeError):
    """The exact hot-journal recovery boundary is not admissible."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    stable = json.loads(_canonical_json(plan))
    stable.pop("fingerprint", None)
    stable.pop("created_at", None)
    return _fingerprint(stable)


def _file_identity(path: Path, *, sha256: bool = True) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HotJournalRecoveryError(f"unsafe or missing file: {path}")
    stat = path.stat()
    result: dict[str, Any] = {
        "path": str(path),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "mode": f"0o{stat.st_mode & 0o777:o}",
        "uid": int(stat.st_uid),
        "gid": int(stat.st_gid),
        "size_bytes": int(stat.st_size),
        "allocated_bytes": int(stat.st_blocks) * 512,
        "mtime_ns": int(stat.st_mtime_ns),
    }
    if sha256:
        result["sha256"] = file_sha256(path)
    return result


def _same_file_identity(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    allow_mtime_change: bool = False,
    allow_content_change: bool = False,
) -> bool:
    keys = ["path", "device", "inode", "mode", "uid", "gid", "size_bytes"]
    if not allow_mtime_change:
        keys.append("mtime_ns")
    if "sha256" in expected and not allow_content_change:
        keys.append("sha256")
    return all(expected.get(key) == actual.get(key) for key in keys)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HotJournalRecoveryError(f"{label} is unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HotJournalRecoveryError(f"{label} is invalid") from exc
    if not isinstance(payload, dict):
        raise HotJournalRecoveryError(f"{label} must be an object")
    return payload


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        data = (_canonical_json(payload) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _deployed_sha(path: Path, expected: str) -> str:
    value = path.read_text(encoding="utf-8").strip().lower()
    if SHA_PATTERN.fullmatch(expected) is None or value != expected:
        raise HotJournalRecoveryError("deployed SHA identity drifted")
    return value


def _filesystem(path: Path) -> dict[str, int]:
    stats = os.statvfs(path)
    return {
        "device": int(path.stat().st_dev),
        "capacity_bytes": int(stats.f_blocks) * int(stats.f_frsize),
        "available_bytes": int(stats.f_bavail) * int(stats.f_frsize),
        "free_inodes": int(stats.f_favail),
    }


def _be32(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


def _journal_overlay(
    database: Path,
    journal: Path,
) -> dict[str, Any]:
    database_size = database.stat().st_size
    with database.open("rb") as source:
        database_header = source.read(100)
    if (
        database_header[:16] != b"SQLite format 3\0"
        or _be32(b"\0\0" + database_header[16:18]) != EXPECTED_PAGE_SIZE
        or database_header[18:20] != b"\x01\x01"
    ):
        raise HotJournalRecoveryError(
            "operational SQLite header is outside the exact rollback mode"
        )
    if database_size % EXPECTED_PAGE_SIZE:
        raise HotJournalRecoveryError("operational SQLite size is not page aligned")
    database_pages = database_size // EXPECTED_PAGE_SIZE
    with journal.open("rb") as source:
        header = source.read(512)
        if len(header) != 512 or header[:8] != EXPECTED_JOURNAL_MAGIC:
            raise HotJournalRecoveryError("rollback journal header is not hot")
        record_count = _be32(header[8:12])
        nonce = _be32(header[12:16])
        initial_pages = _be32(header[16:20])
        sector_size = _be32(header[20:24])
        page_size = _be32(header[24:28])
        if (
            record_count <= 0
            or initial_pages != database_pages
            or sector_size != 512
            or page_size != EXPECTED_PAGE_SIZE
        ):
            raise HotJournalRecoveryError(
                "rollback journal header does not match the selected database"
            )
        pages: dict[int, bytes] = {}
        page_numbers: list[int] = []
        for index in range(record_count):
            page_number_raw = source.read(4)
            page = source.read(EXPECTED_PAGE_SIZE)
            checksum_raw = source.read(4)
            if (
                len(page_number_raw) != 4
                or len(page) != EXPECTED_PAGE_SIZE
                or len(checksum_raw) != 4
            ):
                raise HotJournalRecoveryError(
                    "rollback journal record region is truncated"
                )
            page_number = _be32(page_number_raw)
            checksum = nonce
            offset = EXPECTED_PAGE_SIZE - 200
            while offset > 0:
                checksum = (checksum + page[offset]) & 0xFFFFFFFF
                offset -= 200
            if _be32(checksum_raw) != checksum:
                raise HotJournalRecoveryError(
                    f"rollback journal checksum mismatch at record {index}"
                )
            if page_number < 1 or page_number > database_pages:
                raise HotJournalRecoveryError(
                    f"rollback journal page is outside database at record {index}"
                )
            if page_number in pages:
                raise HotJournalRecoveryError(
                    "rollback journal contains a duplicate page record"
                )
            pages[page_number] = page
            page_numbers.append(page_number)

    expected_used = 512 + record_count * (4 + EXPECTED_PAGE_SIZE + 4)
    if journal.stat().st_size < expected_used:
        raise HotJournalRecoveryError("rollback journal file is shorter than its header")
    recovered = hashlib.sha256()
    changed = 0
    same = 0
    with database.open("rb") as source:
        for page_number in range(1, database_pages + 1):
            current = source.read(EXPECTED_PAGE_SIZE)
            replacement = pages.get(page_number)
            if replacement is None:
                recovered.update(current)
            else:
                recovered.update(replacement)
                if replacement == current:
                    same += 1
                else:
                    changed += 1
    return {
        "magic_hex": header[:8].hex(),
        "record_count": record_count,
        "nonce_hex": header[12:16].hex(),
        "initial_database_pages": initial_pages,
        "sector_size": sector_size,
        "page_size": page_size,
        "record_region_bytes": expected_used,
        "trailing_bytes": journal.stat().st_size - expected_used,
        "unique_page_count": len(pages),
        "page_number_min": min(page_numbers),
        "page_number_max": max(page_numbers),
        "page_number_list_sha256": hashlib.sha256(
            _canonical_json(page_numbers).encode("utf-8")
        ).hexdigest(),
        "pages_different_from_main": changed,
        "pages_equal_to_main": same,
        "expected_recovered_database_size": database_size,
        "expected_recovered_database_sha256": recovered.hexdigest(),
    }


def _immutable_metadata(path: Path) -> dict[str, Any]:
    with closing(
        sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro&immutable=1",
            uri=True,
            timeout=5,
        )
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        return {
            "query_only": int(
                connection.execute("PRAGMA query_only").fetchone()[0]
            ),
            "journal_mode": str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ),
            "page_size": int(
                connection.execute("PRAGMA page_size").fetchone()[0]
            ),
            "page_count": int(
                connection.execute("PRAGMA page_count").fetchone()[0]
            ),
            "freelist_count": int(
                connection.execute("PRAGMA freelist_count").fetchone()[0]
            ),
            "schema_version": int(
                connection.execute("PRAGMA schema_version").fetchone()[0]
            ),
            "user_version": int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            ),
        }


def _systemd_jobs() -> list[str]:
    completed = subprocess.run(
        ["systemctl", "list-jobs", "--no-legend", "--no-pager"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise HotJournalRecoveryError("systemd job inventory failed")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _openers(paths: set[Path], proc_root: Path = Path("/proc")) -> list[dict[str, Any]]:
    exact = {path.resolve(): path for path in paths}
    rows: list[dict[str, Any]] = []
    for process in sorted(proc_root.glob("[0-9]*"), key=lambda item: item.name):
        fd_dir = process / "fd"
        try:
            entries = list(fd_dir.iterdir())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for descriptor in entries:
            try:
                target = Path(os.readlink(descriptor)).resolve()
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            if target not in exact:
                continue
            try:
                flags_line = next(
                    line
                    for line in (process / "fdinfo" / descriptor.name)
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line.startswith("flags:")
                )
                flags = int(flags_line.split()[1], 8)
                command = (
                    (process / "cmdline")
                    .read_bytes()
                    .replace(b"\0", b" ")
                    .decode("utf-8", errors="replace")
                    .strip()
                )
                cgroup = (process / "cgroup").read_text(encoding="utf-8")
            except (FileNotFoundError, PermissionError, StopIteration):
                raise HotJournalRecoveryError(
                    "database opener identity disappeared during inventory"
                )
            access = flags & os.O_ACCMODE
            rows.append(
                {
                    "pid": int(process.name),
                    "fd": int(descriptor.name),
                    "path": str(target),
                    "access": (
                        "read_only"
                        if access == os.O_RDONLY
                        else "write_only"
                        if access == os.O_WRONLY
                        else "read_write"
                    ),
                    "command": command[:500],
                    "cgroup": cgroup.strip()[:500],
                }
            )
    return sorted(rows, key=lambda item: (item["path"], item["pid"], item["fd"]))


def _kernel_locks(paths: set[Path], locks_path: Path = Path("/proc/locks")) -> list[str]:
    identities = set()
    for path in paths:
        stat = path.stat()
        identities.add(
            (os.major(stat.st_dev), os.minor(stat.st_dev), int(stat.st_ino))
        )
    try:
        rows = locks_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HotJournalRecoveryError("kernel lock inventory is unavailable") from exc
    matched: list[str] = []
    for row in rows:
        fields = row.split()
        if len(fields) < 6:
            continue
        device = fields[5].split(":")
        if len(device) != 3:
            continue
        try:
            identity = (int(device[0], 16), int(device[1], 16), int(device[2]))
        except ValueError:
            continue
        if identity in identities:
            matched.append(row[:1000])
    return matched


def _zstd_binary() -> dict[str, Any]:
    completed = subprocess.run(
        ["zstd", "--version"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise HotJournalRecoveryError("zstd is unavailable")
    path = Path("/usr/bin/zstd")
    if not path.is_file():
        raise HotJournalRecoveryError("canonical zstd binary is unavailable")
    return {
        "path": str(path),
        "version": completed.stdout.strip(),
        "sha256": file_sha256(path),
        "arguments": ["-T1", "-1"],
    }


def _zstd_measure(source: Path) -> dict[str, Any]:
    process = subprocess.Popen(
        ["zstd", "-q", "-T1", "-1", "-c", str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        size += len(chunk)
        digest.update(chunk)
    _stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise HotJournalRecoveryError(
            "zstd read-only measurement failed: "
            + stderr.decode("utf-8", errors="replace").strip()
        )
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def _timer_pair(value: Mapping[str, Any]) -> tuple[str, str]:
    return str(value.get("is_enabled") or ""), str(value.get("is_active") or "")


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
    allow_recovery_job: bool = False,
    recovered_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve()
    backup_root = backup_root.resolve()
    if runtime_dir != Path("/opt/wb-core-runtime/state"):
        raise HotJournalRecoveryError("recovery requires canonical runtime dir")
    if backup_root != DEFAULT_BACKUP_ROOT or backup_root.is_symlink():
        raise HotJournalRecoveryError("recovery requires canonical backup mount")
    if OPERATION_PATTERN.fullmatch(operation_id) is None:
        raise HotJournalRecoveryError("operation id must be exact 64-hex")
    if window_id != EXPECTED_WINDOW_ID or DIGEST_PATTERN.fullmatch(
        plan_fingerprint
    ) is None:
        raise HotJournalRecoveryError("barrier identity is invalid")
    if reserve_bytes != DEFAULT_RESERVE_BYTES:
        raise HotJournalRecoveryError("Finance reserve must remain exact")
    if evidence_envelope_bytes != DEFAULT_EVIDENCE_ENVELOPE_BYTES:
        raise HotJournalRecoveryError("evidence envelope must remain exact")
    current_sha = _deployed_sha(deployed_sha_file, deployed_sha)

    barrier = barrier_status(runtime_dir)
    if (
        barrier.get("active") is not True
        or str(barrier.get("phase") or "") != "acquiring"
        or barrier.get("hold_confirmed") is not False
        or str(barrier.get("window_id") or "") != window_id
        or str(barrier.get("plan_fingerprint") or "") != plan_fingerprint
    ):
        raise HotJournalRecoveryError("exact acquiring barrier drifted")
    state = _read_json(
        runtime_dir / maintenance.STATE_FILENAME,
        label="maintenance state",
    )
    epoch = dict(
        state.get("prepared_abort_partial_restore_recovery_epoch") or {}
    )
    expected_disabled = sorted(
        str(item) for item in epoch.get("timer_units_to_disable") or []
    )
    if (
        str(state.get("phase") or "") != "abort_quiescing"
        or state.get("exact_prior_state_restored") is True
        or str(epoch.get("schema_version") or "")
        != EXPECTED_PARTIAL_EPOCH_SCHEMA
        or int(epoch.get("epoch") or 0) != 2
        or not SHA_PATTERN.fullmatch(str(epoch.get("deployed_sha") or ""))
        or str(epoch.get("deployed_sha") or "") != EXPECTED_SOURCE_EPOCH_SHA
        or str(epoch.get("pending_disable_unit") or "")
        or sorted(epoch.get("disabled_timer_units") or []) != expected_disabled
        or expected_disabled != sorted(RECOVERY_PAUSE_OWNED_TIMERS)
        or str(epoch.get("window_id") or "") != window_id
        or str(epoch.get("plan_fingerprint") or "") != plan_fingerprint
        or str(epoch.get("barrier_state_fingerprint") or "")
        != str(barrier.get("state_fingerprint") or "")
    ):
        raise HotJournalRecoveryError("partial abort recovery epoch drifted")
    if current_sha == str(epoch.get("deployed_sha") or ""):
        raise HotJournalRecoveryError(
            "hot-journal recovery requires the reviewed correction runtime"
        )

    systemd = maintenance.SystemdClient()
    timer_states = {
        unit: systemd.unit_state(unit)
        for unit in maintenance.ALL_BUSINESS_TIMER_UNITS
    }
    service_states = {
        unit: systemd.unit_state(unit)
        for unit in maintenance.ALL_BUSINESS_SERVICE_UNITS
    }
    if any(
        _timer_pair(value) != ("disabled", "inactive")
        for value in timer_states.values()
    ):
        raise HotJournalRecoveryError("a business timer is not paused")
    if any(
        str(value.get("is_active") or "")
        not in maintenance.QUIESCENT_SERVICE_STATES
        or int((value.get("properties") or {}).get("MainPID") or 0) != 0
        for value in service_states.values()
    ):
        raise HotJournalRecoveryError("a business writer service is not terminal")
    if maintenance._writer_processes():
        raise HotJournalRecoveryError("a business writer process is present")
    systemd_jobs = _systemd_jobs()
    allowed_job_marker = (
        f"wb-core-storage-recovery-sanitation@{operation_id}.service"
    )
    unexpected_jobs = [
        row
        for row in systemd_jobs
        if not (allow_recovery_job and allowed_job_marker in row)
    ]
    if unexpected_jobs or (systemd_jobs and not allow_recovery_job):
        raise HotJournalRecoveryError("a systemd job is active")
    locks = maintenance._lock_summary(runtime_dir)
    if any(
        bool(value.get("held"))
        for key, value in locks.items()
        if key != "seller_portal"
    ) or bool((locks.get("seller_portal") or {}).get("busy")):
        raise HotJournalRecoveryError("a business writer lock is held")
    maintenance._require_pause_owned_active_service_inventory(systemd)
    counters = maintenance._prepared_abort_breakglass_counters(runtime_dir)

    registry = StoreRegistry(runtime_dir)
    manifest = registry.load(require_files=True)
    if manifest.state != "cutover" or manifest.canonical_source != "split":
        raise HotJournalRecoveryError("canonical storage is not the exact split cutover")
    database = registry.resolve("operational", manifest=manifest)
    raw = registry.resolve("finance_raw", manifest=manifest)
    journal = Path(str(database) + "-journal")
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    if wal.exists() or shm.exists():
        raise HotJournalRecoveryError("operational WAL/SHM sidecar is present")
    database_identity = _file_identity(database)
    recovered_resume = recovered_plan is not None and not journal.exists()
    journal_identity = (
        dict(recovered_plan["journal"])
        if recovered_resume
        else _file_identity(journal)
    )
    expected_database_path = (
        runtime_dir
        / "generations"
        / EXPECTED_GENERATION_ID
        / "operational.sqlite3"
    )
    if (
        database != expected_database_path
        or database_identity["size_bytes"] != EXPECTED_DATABASE_SIZE
        or database_identity["sha256"]
        != (
            EXPECTED_RECOVERED_DATABASE_SHA256
            if recovered_resume
            else EXPECTED_DATABASE_SHA256
        )
        or journal_identity["size_bytes"] != EXPECTED_JOURNAL_SIZE
        or journal_identity["sha256"] != EXPECTED_JOURNAL_SHA256
    ):
        raise HotJournalRecoveryError("exact incident DB/journal identity drifted")
    if database_identity["device"] != journal_identity["device"]:
        raise HotJournalRecoveryError("database and journal devices differ")
    lock_paths = {database} if recovered_resume else {database, journal}
    openers = _openers(lock_paths)
    kernel_locks = _kernel_locks(lock_paths)
    if kernel_locks:
        raise HotJournalRecoveryError("operational database has a kernel lock")
    for opener in openers:
        if opener["path"] == str(journal) or opener["access"] != "read_only":
            raise HotJournalRecoveryError("operational database has a writer opener")
        if (
            "apps/registry_upload_http_entrypoint_live.py" not in opener["command"]
            or "wb-core-registry-http.service" not in opener["cgroup"]
        ):
            raise HotJournalRecoveryError("operational database opener is unknown")
    overlay = (
        dict(recovered_plan["journal_overlay"])
        if recovered_resume
        else _journal_overlay(database, journal)
    )
    if (
        overlay["record_count"] != EXPECTED_JOURNAL_RECORDS
        or overlay["expected_recovered_database_sha256"]
        != EXPECTED_RECOVERED_DATABASE_SHA256
    ):
        raise HotJournalRecoveryError("exact incident journal overlay drifted")
    metadata = _immutable_metadata(database)
    if (
        metadata["query_only"] != 1
        or metadata["journal_mode"] != "delete"
        or metadata["page_size"] != EXPECTED_PAGE_SIZE
        or metadata["page_count"] != overlay["initial_database_pages"]
    ):
        raise HotJournalRecoveryError("immutable SQLite metadata drifted")

    zstd = _zstd_binary()
    if recovered_resume:
        compressed_database = dict(
            recovered_plan["compressed_measurement"]["database"]
        )
        compressed_journal = dict(
            recovered_plan["compressed_measurement"]["journal"]
        )
    else:
        compressed_database = _zstd_measure(database)
        compressed_journal = _zstd_measure(journal)
    capacity = _filesystem(backup_root)
    database_capacity = _filesystem(database.parent)
    if capacity["device"] == database_capacity["device"]:
        raise HotJournalRecoveryError("backup and operational devices are not distinct")
    allocation = (
        int(compressed_database["size_bytes"])
        + int(compressed_journal["size_bytes"])
        + evidence_envelope_bytes
    )
    if int(capacity["available_bytes"]) < reserve_bytes + allocation:
        raise HotJournalRecoveryError("backup capacity would breach Finance reserve")

    manifest_path = runtime_dir / MANIFEST_FILENAME
    manifest_file = _file_identity(manifest_path)
    raw_file = _file_identity(raw)
    if (
        manifest_file["sha256"] != EXPECTED_MANIFEST_FILE_SHA256
        or raw_file["sha256"] != EXPECTED_RAW_SHA256
    ):
        raise HotJournalRecoveryError("exact incident non-target identity drifted")
    backup_directory = (
        backup_root
        / "private-evidence"
        / "production-goals"
        / f"wbc0027-s047-hot-journal-recovery-{current_sha[:8]}"
        / operation_id
    )
    if backup_directory.exists() and not allow_existing_operation:
        raise HotJournalRecoveryError("backup operation directory already exists")
    return {
        "contract_name": CONTRACT_NAME,
        "read_only": True,
        "operation_id": operation_id,
        "deployed_sha": current_sha,
        "source_epoch_deployed_sha": str(epoch["deployed_sha"]),
        "runtime_dir": str(runtime_dir),
        "barrier": {
            key: barrier.get(key)
            for key in (
                "active",
                "phase",
                "hold_confirmed",
                "window_id",
                "plan_fingerprint",
                "state_fingerprint",
            )
        },
        "maintenance": {
            "phase": state.get("phase"),
            "partial_epoch_fingerprint": _fingerprint(epoch),
            "disabled_timer_units": expected_disabled,
            "timer_states": timer_states,
            "service_states": service_states,
            "writer_locks": locks,
            "business_operation_counters": counters,
        },
        "storage_generation_manifest": manifest_payload(manifest),
        "storage_generation_manifest_file": manifest_file,
        "database": database_identity,
        "journal": journal_identity,
        "journal_overlay": overlay,
        "immutable_database_metadata": metadata,
        "openers": openers,
        "kernel_locks": kernel_locks,
        "systemd_jobs": [],
        "raw_database": raw_file,
        "zstd": zstd,
        "compressed_measurement": {
            "database": compressed_database,
            "journal": compressed_journal,
        },
        "backup": {
            "directory": str(backup_directory),
            "reserve_bytes": reserve_bytes,
            "evidence_envelope_bytes": evidence_envelope_bytes,
            "allocation_bytes": allocation,
            "capacity_before": capacity,
            "projected_available_bytes": int(capacity["available_bytes"])
            - allocation,
            "projected_reserve_headroom_bytes": int(
                capacity["available_bytes"]
            )
            - allocation
            - reserve_bytes,
        },
        "expected_effect": {
            "logical_business_delta": 0,
            "physical_pages_restored": overlay["pages_different_from_main"],
            "journal_removed": True,
            "database_sha256": overlay[
                "expected_recovered_database_sha256"
            ],
            "database_size_bytes": overlay[
                "expected_recovered_database_size"
            ],
        },
    }


def build_plan(**kwargs: Any) -> dict[str, Any]:
    material = _preflight(**kwargs)
    result = {**material, "created_at": _now()}
    result["fingerprint"] = _plan_fingerprint(result)
    return result


def _stream_zstd(source: Path, destination: Path) -> dict[str, Any]:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    process = subprocess.Popen(
        ["zstd", "-q", "-T1", "-1", "-c", str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.close(descriptor)
        descriptor = -1
        _stdout, stderr = process.communicate()
        if process.returncode != 0:
            raise HotJournalRecoveryError(
                "zstd backup failed: "
                + stderr.decode("utf-8", errors="replace").strip()
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def _verify_zstd(path: Path, *, expected_source: Mapping[str, Any]) -> dict[str, Any]:
    tested = subprocess.run(
        ["zstd", "--test", "--quiet", str(path)],
        text=True,
        capture_output=True,
        timeout=7200,
        check=False,
    )
    if tested.returncode != 0:
        raise HotJournalRecoveryError("compressed capsule frame failed verification")
    process = subprocess.Popen(
        ["zstd", "--decompress", "--stdout", "--quiet", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    _stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise HotJournalRecoveryError(
            "compressed capsule decompression failed: "
            + stderr.decode("utf-8", errors="replace").strip()
        )
    result = {"size_bytes": size, "sha256": digest.hexdigest()}
    if (
        result["size_bytes"] != int(expected_source["size_bytes"])
        or result["sha256"] != str(expected_source["sha256"])
    ):
        raise HotJournalRecoveryError("compressed capsule source bytes drifted")
    return result


def _post_sqlite_readback(database: Path) -> dict[str, Any]:
    with closing(
        sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro",
            uri=True,
            timeout=120,
        )
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        foreign_keys = [
            list(row) for row in connection.execute("PRAGMA foreign_key_check")
        ]
        if integrity != ["ok"] or foreign_keys:
            raise HotJournalRecoveryError("post-recovery SQLite verification failed")
        return {
            "query_only": int(
                connection.execute("PRAGMA query_only").fetchone()[0]
            ),
            "integrity_check": "ok",
            "foreign_key_violation_count": 0,
            "schema_version": int(
                connection.execute("PRAGMA schema_version").fetchone()[0]
            ),
            "user_version": int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            ),
        }


def _publish_result_marker(
    *,
    runtime_dir: Path,
    backup_dir: Path,
    result: Mapping[str, Any],
    state_path: Path,
    state: dict[str, Any],
) -> None:
    marker = {
        "contract_name": RESULT_CONTRACT_NAME,
        "operation_id": result["operation_id"],
        "source_epoch_deployed_sha": result["source_epoch_deployed_sha"],
        "deployed_sha": result["deployed_sha"],
        "barrier": result["barrier"],
        "maintenance_partial_epoch_fingerprint": result[
            "maintenance_partial_epoch_fingerprint"
        ],
        "database_after": result["database_after"],
        "journal_absent": True,
        "sqlite_readback": result["sqlite_readback"],
        "business_operation_counters": result["business_operation_counters"],
        "result_path": str(backup_dir / "result.json"),
        "result_fingerprint": result["result_fingerprint"],
        "completed_at": result["completed_at"],
    }
    marker["marker_fingerprint"] = _fingerprint(marker)
    marker_path = runtime_dir / MARKER_FILENAME
    if marker_path.exists():
        if _read_json(marker_path, label="recovery result marker") != marker:
            raise HotJournalRecoveryError("recovery result marker drifted")
    else:
        _atomic_json(marker_path, marker)
    state["phase"] = "completed"
    state["completed_at"] = result["completed_at"]
    state["result_fingerprint"] = result["result_fingerprint"]
    _atomic_json(state_path, state)


def apply_plan(
    *,
    plan_path: Path,
    plan_sha256: str,
    fingerprint: str,
    deployed_sha_file: Path,
) -> dict[str, Any]:
    if DIGEST_PATTERN.fullmatch(plan_sha256) is None:
        raise HotJournalRecoveryError("reviewed plan SHA is invalid")
    if DIGEST_PATTERN.fullmatch(fingerprint) is None:
        raise HotJournalRecoveryError("reviewed plan fingerprint is invalid")
    plan_path = plan_path.resolve()
    plan = _read_json(plan_path, label="reviewed recovery plan")
    if "sha256:" + file_sha256(plan_path) != plan_sha256:
        raise HotJournalRecoveryError("reviewed plan bytes drifted")
    if (
        plan.get("contract_name") != CONTRACT_NAME
        or str(plan.get("fingerprint") or "") != fingerprint
        or _plan_fingerprint(plan) != fingerprint
        or plan.get("read_only") is not True
    ):
        raise HotJournalRecoveryError("reviewed recovery plan identity drifted")

    preflight_arguments = {
        "runtime_dir": Path(str(plan["runtime_dir"])),
        "backup_root": DEFAULT_BACKUP_ROOT,
        "deployed_sha": str(plan["deployed_sha"]),
        "deployed_sha_file": deployed_sha_file,
        "operation_id": str(plan["operation_id"]),
        "window_id": str(plan["barrier"]["window_id"]),
        "plan_fingerprint": str(plan["barrier"]["plan_fingerprint"]),
        "reserve_bytes": int(plan["backup"]["reserve_bytes"]),
        "evidence_envelope_bytes": int(
            plan["backup"]["evidence_envelope_bytes"]
        ),
        "allow_existing_operation": True,
        "allow_recovery_job": True,
    }
    operation_exists = Path(str(plan["backup"]["directory"])).exists()
    journal_absent = not Path(str(plan["journal"]["path"])).exists()
    if operation_exists and journal_absent:
        _preflight(**preflight_arguments, recovered_plan=plan)
        fresh = dict(plan)
    else:
        fresh = build_plan(**preflight_arguments)
        if operation_exists:
            fresh["backup"] = dict(plan["backup"])
    if _plan_fingerprint(fresh) != fingerprint:
        raise HotJournalRecoveryError("reviewed recovery plan is stale")

    runtime_dir = Path(str(plan["runtime_dir"]))
    database = Path(str(plan["database"]["path"]))
    journal = Path(str(plan["journal"]["path"]))
    backup_dir = Path(str(plan["backup"]["directory"]))
    state_path = backup_dir / "recovery-state.json"
    if backup_dir.exists():
        state = _read_json(state_path, label="recovery operation state")
        if any(
            state.get(key) != value
            for key, value in {
                "contract_name": RESULT_CONTRACT_NAME,
                "operation_id": plan["operation_id"],
                "deployed_sha": plan["deployed_sha"],
                "plan_sha256": plan_sha256,
                "plan_fingerprint": fingerprint,
            }.items()
        ):
            raise HotJournalRecoveryError("recovery operation state drifted")
        if str(state.get("phase") or "") not in {
            "backup_started",
            "database_backup_verified",
            "journal_backup_verified",
            "recovery_intent",
            "sqlite_recovery_returned",
            "completed",
        }:
            raise HotJournalRecoveryError("recovery operation phase drifted")
    else:
        backup_dir.mkdir(parents=True, mode=0o700)
        os.chmod(backup_dir, 0o700)
        _fsync_directory(backup_dir.parent)
        state = {
            "contract_name": RESULT_CONTRACT_NAME,
            "operation_id": plan["operation_id"],
            "deployed_sha": plan["deployed_sha"],
            "plan_sha256": plan_sha256,
            "plan_fingerprint": fingerprint,
            "phase": "backup_started",
            "started_at": _now(),
        }
        _atomic_json(state_path, state)

    existing_result_path = backup_dir / "result.json"
    if existing_result_path.exists():
        existing_result = _read_json(
            existing_result_path, label="recovery operation result"
        )
        result_material = dict(existing_result)
        result_fingerprint = str(
            result_material.pop("result_fingerprint", "")
        )
        if (
            existing_result.get("contract_name") != RESULT_CONTRACT_NAME
            or existing_result.get("operation_id") != plan["operation_id"]
            or result_fingerprint != _fingerprint(result_material)
        ):
            raise HotJournalRecoveryError("recovery operation result drifted")
        _publish_result_marker(
            runtime_dir=runtime_dir,
            backup_dir=backup_dir,
            result=existing_result,
            state_path=state_path,
            state=state,
        )
        return existing_result
    if str(state.get("phase") or "") == "completed":
        raise HotJournalRecoveryError("completed recovery result is unavailable")

    capsule: dict[str, Any] = {}
    for label, source_key in (("database", "database"), ("journal", "journal")):
        source = Path(str(plan[source_key]["path"]))
        partial = backup_dir / f"{label}.zst.partial"
        final = backup_dir / f"{label}.zst"
        expected_compressed = plan["compressed_measurement"][label]
        allowed_incomplete_phase = (
            "backup_started" if label == "database" else "database_backup_verified"
        )
        created_capsule = False
        if final.exists():
            final_identity = _file_identity(final)
            compressed = {
                "size_bytes": final_identity["size_bytes"],
                "sha256": final_identity["sha256"],
            }
            if compressed != expected_compressed:
                raise HotJournalRecoveryError(
                    f"{label} completed capsule bytes drifted"
                )
            verified = _verify_zstd(final, expected_source=plan[source_key])
        else:
            if str(state.get("phase") or "") != allowed_incomplete_phase:
                raise HotJournalRecoveryError(
                    f"{label} capsule is missing after a later durable phase"
                )
            if partial.exists():
                partial.unlink()
                _fsync_directory(backup_dir)
            compressed = _stream_zstd(source, partial)
            if compressed != expected_compressed:
                raise HotJournalRecoveryError(
                    f"{label} compressed bytes differ from reviewed measurement"
                )
            verified = _verify_zstd(partial, expected_source=plan[source_key])
            os.replace(partial, final)
            os.chmod(final, 0o600)
            _fsync_directory(backup_dir)
            created_capsule = True
        capsule[label] = {
            "path": str(final),
            "compressed": compressed,
            "decompressed": verified,
        }
        state["capsule"] = capsule
        if (
            created_capsule
            or str(state.get("phase") or "") == allowed_incomplete_phase
        ):
            state["phase"] = f"{label}_backup_verified"
            _atomic_json(state_path, state)

    journal_present = journal.exists()
    database_before_recovery = _file_identity(database)
    if journal_present:
        if not _same_file_identity(plan["database"], database_before_recovery):
            raise HotJournalRecoveryError("database drifted during capsule creation")
        if not _same_file_identity(plan["journal"], _file_identity(journal)):
            raise HotJournalRecoveryError("journal drifted during capsule creation")
        if _journal_overlay(database, journal) != plan["journal_overlay"]:
            raise HotJournalRecoveryError(
                "journal overlay drifted during capsule creation"
            )
    elif (
        str(state.get("phase") or "")
        not in {"recovery_intent", "sqlite_recovery_returned"}
        or database_before_recovery["sha256"]
        != plan["expected_effect"]["database_sha256"]
    ):
        raise HotJournalRecoveryError(
            "journal disappeared outside the durable recovery intent"
        )
    manifest = {
        "contract_name": CONTRACT_NAME,
        "operation_id": plan["operation_id"],
        "plan_sha256": plan_sha256,
        "plan_fingerprint": fingerprint,
        "source_database": plan["database"],
        "source_journal": plan["journal"],
        "journal_overlay": plan["journal_overlay"],
        "capsule": capsule,
        "verified_at": _now(),
    }
    manifest["manifest_fingerprint"] = _fingerprint(manifest)
    _atomic_json(backup_dir / "capsule-manifest.json", manifest)
    if str(state.get("phase") or "") not in {
        "sqlite_recovery_returned",
        "completed",
    }:
        state["phase"] = "recovery_intent"
    state["capsule_manifest_fingerprint"] = manifest["manifest_fingerprint"]
    _atomic_json(state_path, state)

    if journal_present:
        # The first pager read owns the rollback. No DML or schema statement is
        # issued by this recovery contour.
        with closing(
            sqlite3.connect(
                f"file:{database.as_posix()}?mode=rw",
                uri=True,
                timeout=120,
                isolation_level=None,
            )
        ) as connection:
            connection.execute("PRAGMA busy_timeout=120000")
            connection.execute("PRAGMA schema_version").fetchone()

        state["phase"] = "sqlite_recovery_returned"
        _atomic_json(state_path, state)
    if journal.exists():
        raise HotJournalRecoveryError("SQLite returned with rollback journal present")
    database_after = _file_identity(database)
    if not _same_file_identity(
        plan["database"],
        database_after,
        allow_mtime_change=True,
        allow_content_change=True,
    ) or database_after["sha256"] != plan["expected_effect"]["database_sha256"]:
        raise HotJournalRecoveryError("recovered database bytes differ from overlay")
    readback = _post_sqlite_readback(database)
    raw_after = _file_identity(Path(str(plan["raw_database"]["path"])))
    manifest_after = _file_identity(
        Path(str(plan["storage_generation_manifest_file"]["path"]))
    )
    if not _same_file_identity(plan["raw_database"], raw_after):
        raise HotJournalRecoveryError("Finance raw non-target drifted")
    if not _same_file_identity(
        plan["storage_generation_manifest_file"], manifest_after
    ):
        raise HotJournalRecoveryError("storage generation manifest drifted")
    counters = maintenance._prepared_abort_breakglass_counters(runtime_dir)
    capacity_after = _filesystem(DEFAULT_BACKUP_ROOT)
    if int(capacity_after["available_bytes"]) < (
        int(plan["backup"]["reserve_bytes"])
        + int(plan["backup"]["evidence_envelope_bytes"])
    ):
        raise HotJournalRecoveryError("post-recovery Finance reserve is insufficient")
    result = {
        "contract_name": RESULT_CONTRACT_NAME,
        "status": "recovered",
        "operation_id": plan["operation_id"],
        "deployed_sha": plan["deployed_sha"],
        "source_epoch_deployed_sha": plan["source_epoch_deployed_sha"],
        "plan_sha256": plan_sha256,
        "plan_fingerprint": fingerprint,
        "barrier": plan["barrier"],
        "maintenance_partial_epoch_fingerprint": plan["maintenance"][
            "partial_epoch_fingerprint"
        ],
        "database_before": plan["database"],
        "journal_before": plan["journal"],
        "database_after": database_after,
        "journal_absent": True,
        "journal_overlay": plan["journal_overlay"],
        "sqlite_readback": readback,
        "raw_database": raw_after,
        "storage_generation_manifest_file": manifest_after,
        "business_operation_counters": counters,
        "logical_business_delta": 0,
        "capsule": manifest,
        "capacity_after": capacity_after,
        "completed_at": _now(),
    }
    result["result_fingerprint"] = _fingerprint(result)
    _atomic_json(backup_dir / "result.json", result)
    _publish_result_marker(
        runtime_dir=runtime_dir,
        backup_dir=backup_dir,
        result=result,
        state_path=state_path,
        state=state,
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument(
        "--deployed-sha-file", type=Path, default=ROOT / ".wb-core-runtime-sha"
    )
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--window-id", required=True)
    parser.add_argument("--plan-fingerprint", required=True)
    parser.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    parser.add_argument(
        "--evidence-envelope-bytes",
        type=int,
        default=DEFAULT_EVIDENCE_ENVELOPE_BYTES,
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_plan(
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
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        output = args.output.resolve()
        if output.exists():
            raise HotJournalRecoveryError("dry-run output already exists")
        _atomic_json(output, payload)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
