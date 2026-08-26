#!/usr/bin/env python3
"""Archive the exact WBC0008 block-006 SQLite copies to the backup mount.

This is intentionally not a general cleanup primitive.  The six source paths,
their provenance contracts and the single destination family are fixed below.
Dry-run materializes a private JIT manifest; apply consumes only those exact
bytes and readback never submits another unlink.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.root_storage_policy import (  # noqa: E402
    _collect_correction_journal_inventory,
    _effective_journald_config,
    _reconcile_correction_journal_inventory,
)
from apps.storage_recovery_sanitation import _verify_deployed_sha  # noqa: E402
from packages.application.finance_storage_backup_rotation import (  # noqa: E402
    backup_rotation_health,
)
from packages.application.finance_storage_snapshot_retention import (  # noqa: E402
    LOCK_FILENAME as FINANCE_STORAGE_LOCK_FILENAME,
)
from packages.application.root_storage_policy import (  # noqa: E402
    collect_root_storage_status,
    load_policy,
    read_root_storage_status_artifact,
)
from packages.application.storage_registry import StoreRegistry, manifest_payload  # noqa: E402


CONTRACT_NAME = "root_storage_warm_archive_wbc0008_006_v2"
PROFILE = "root-warm-archive-six"
EXPECTED_SOURCE_COUNT = 6
DESTINATION_FAMILY_NAME = "root-warm-archive-wbc0008-006"
DESTINATION_ROOT = Path("/opt/wb-core-runtime/state/backups")
GENERATION_ROOT = Path("/opt/wb-core-runtime/state/generations")
ROOT_MINIMUM_AFTER_BYTES = 25 * 1024**3
EMERGENCY_RESERVE_BYTES = 8 * 1024**3
CONTROL_ARTIFACT_RESERVE_BYTES = 64 * 1024**2
MANIFEST_RESERVE_BYTES_PER_SOURCE = 1024**2
CHUNK_SIZE = 8 * 1024**2
READINESS_REQUIRED_CONSECUTIVE_CLEAN = 3
READINESS_MAX_STABILIZATION_SECONDS = 60
READINESS_SAMPLE_INTERVAL_SECONDS = 2.0
JOB_ID_RE = re.compile(r"[0-9a-f]{64}")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
READINESS_ID_RE = re.compile(r"readiness-v1-[0-9a-f]{32}")
HOLD_TERMS = ("hold", "legal", "forensic", "incident", "preserve", "retain")
PROTECTED_PREFIXES = (
    "/opt/wb-core-runtime/state/incident_backups/",
    "/opt/wb-core-runtime/state/forensics/",
    "/opt/wb-core-runtime/state/generations/",
    "/opt/wb-core-runtime/state/backups/finance/",
)
PERSISTENT_SERVICE_NAMES = frozenset(
    {
        "wb-core-registry-http.service",
        "wb-ai-api.service",
        "wb-core-data-mcp.service",
    }
)
SERVICE_NAMES = (
    "wb-core-registry-http.service",
    "wb-ai-api.service",
    "wb-core-data-mcp.service",
    "wb-core-sheet-vitrina-refresh.service",
    "wb-core-sheet-vitrina-refresh.timer",
    "wb-core-sheet-vitrina-canary-restore.service",
    "wb-core-sheet-vitrina-canary-restore.timer",
    "wb-core-sheet-vitrina-closure-retry.service",
    "wb-core-sheet-vitrina-closure-retry.timer",
    "wb-core-feedbacks-auto-complaints-tick.service",
    "wb-core-feedbacks-auto-complaints-tick.timer",
    "wb-core-wb-finance-weekly.service",
    "wb-core-wb-finance-weekly.timer",
    "wb-core-finance-backup-rotation.service",
    "wb-core-root-storage-policy.timer",
    "wb-core-finance-backup-rotation.timer",
    "wb-core-warehouse-functional-sync.service",
    "wb-core-warehouse-functional-sync.timer",
    "wb-core-fbs-shadow-collector.service",
    "wb-core-fbs-shadow-collector.timer",
    "wb-core-fbs-warehouse-registry.service",
    "wb-core-fbs-warehouse-registry.timer",
    "wb-core-root-storage-policy.service",
    "wb-core-autoanswers-readonly-sync.service",
    "wb-core-autoanswers-readonly-sync.timer",
    "wb-core-autoanswers-worker.service",
    "wb-core-autoanswers-worker.timer",
)
OTHER_LIFECYCLE_LOCKS = (
    ".finance-storage-split.lock",
    ".finance-storage-stale-writer-recovery.lock",
    ".business-data-maintenance-restore.lock",
)


TARGET_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "key": "ff-pool-overhead-backfill",
        "source_path": "/opt/wb-core-runtime/backups/ff-pool-overhead-backfill/ff-pool-overhead-backfill-30c5c0e4dbb60a37.sqlite3",
        "expected_identity": {
            "device": 2049,
            "inode": 1109977,
            "apparent_size_bytes": 5662277632,
            "allocated_blocks_512": 11059208,
            "allocated_bytes": 5662314496,
            "mode": "0o600",
            "uid": 0,
            "gid": 0,
            "mtime_ns": 1787337300807056421,
            "ctime_ns": 1787337300807056421,
            "nlink": 1,
            "sha256": "sha256:0d31c2f8acdcea00705862a114a2de420550d510cc648b88ae97dc9d5835bca4",
        },
        "archive_name": "01-ff-pool-overhead-backfill.sqlite3.zst",
        "owner": "ff_pool_overhead_backfill",
        "family": "root_legacy_backup_families/ff-pool-overhead-backfill",
        "restore_role": "completed FF pool overhead backfill pre-change rollback copy",
        "hold_root": "/opt/wb-core-runtime/backups/ff-pool-overhead-backfill",
        "provenance": (
            {
                "path": "/opt/wb-core-runtime/backups/ff-pool-overhead-backfill/evidence/ff-pool-overhead-backfill-30c5c0e4dbb60a37.json",
                "status": "complete",
                "backup_path_field": "backup.path",
                "backup_sha_field": "backup.sha256",
            },
            {
                "path": "/opt/wb-core-runtime/backups/ff-pool-overhead-backfill/ff-pool-overhead-backfill-30c5c0e4dbb60a37.sqlite3.receipt.json",
                "path_field": "path",
                "sha_field": "sha256",
                "integrity_field": "integrity_check",
            },
        ),
    },
    {
        "key": "buyout-mature-backfill-pr945",
        "source_path": "/opt/wb-core-runtime/evidence/buyout-mature-backfill-pr945/backups/registry-20260809T155334Z-sha256:a7716.sqlite3",
        "expected_identity": {
            "device": 2049,
            "inode": 1095779,
            "apparent_size_bytes": 4059611136,
            "allocated_blocks_512": 7928928,
            "allocated_bytes": 4059611136,
            "mode": "0o600",
            "uid": 0,
            "gid": 0,
            "mtime_ns": 1786290837999322136,
            "ctime_ns": 1786290844048504356,
            "nlink": 1,
            "sha256": "sha256:ddc16824cf547e8818ae848cc3e7b0623b67cf66b13b09761331ece72a1a8736",
        },
        "archive_name": "02-buyout-mature-backfill-pr945.sqlite3.zst",
        "owner": "buyout_mature_backfill",
        "family": "task_evidence_full_copies/buyout-mature-backfill-pr945",
        "restore_role": "superseded PR 945 pre-change recovery copy",
        "hold_root": "/opt/wb-core-runtime/evidence/buyout-mature-backfill-pr945",
        "provenance": (
            {
                "path": "/opt/wb-core-runtime/evidence/buyout-mature-backfill-pr945/buyout-mature-backfill-plan-20260809T155202Z.json",
                "status": "ready",
                "recovery_contains": "verified coherent SQLite backup",
            },
        ),
    },
    {
        "key": "buyout-mature-backfill-pr946",
        "source_path": "/opt/wb-core-runtime/evidence/buyout-mature-backfill-pr946/backups/registry-20260809T161351Z-sha256:83e47.sqlite3",
        "expected_identity": {
            "device": 2049,
            "inode": 1096270,
            "apparent_size_bytes": 4059611136,
            "allocated_blocks_512": 7928928,
            "allocated_bytes": 4059611136,
            "mode": "0o600",
            "uid": 0,
            "gid": 0,
            "mtime_ns": 1786292048941967700,
            "ctime_ns": 1786292050918014984,
            "nlink": 1,
            "sha256": "sha256:00eb64038f34cceaa00d7f2e8741fde3f199bfdb7c97b2b2f3ef49ba7c012534",
        },
        "archive_name": "03-buyout-mature-backfill-pr946.sqlite3.zst",
        "owner": "buyout_mature_backfill",
        "family": "task_evidence_full_copies/buyout-mature-backfill-pr946",
        "restore_role": "completed PR 946 pre-change recovery copy superseded by PR 947",
        "hold_root": "/opt/wb-core-runtime/evidence/buyout-mature-backfill-pr946",
        "provenance": (
            {
                "path": "/opt/wb-core-runtime/evidence/buyout-mature-backfill-pr946/buyout-mature-backfill-reconciliation-20260809T161351Z.json",
                "status": "reconciled",
                "backup_path_field": "backup_path",
                "backup_sha_field": "backup_sha256",
            },
        ),
    },
    {
        "key": "buyout-mature-backfill-pr947",
        "source_path": "/opt/wb-core-runtime/evidence/buyout-mature-backfill-pr947/backups/registry-20260809T163909Z-sha256:44497.sqlite3",
        "expected_identity": {
            "device": 2049,
            "inode": 1095768,
            "apparent_size_bytes": 4068130816,
            "allocated_blocks_512": 7945568,
            "allocated_bytes": 4068130816,
            "mode": "0o600",
            "uid": 0,
            "gid": 0,
            "mtime_ns": 1786293564833249376,
            "ctime_ns": 1786293566707230582,
            "nlink": 1,
            "sha256": "sha256:5b1231729f1458636952b465dbfe9720046b0670cfcd5d706377a547f383d4b3",
        },
        "archive_name": "04-buyout-mature-backfill-pr947.sqlite3.zst",
        "owner": "buyout_mature_backfill",
        "family": "task_evidence_full_copies/buyout-mature-backfill-pr947",
        "restore_role": "completed PR 947 pre-change recovery copy",
        "hold_root": "/opt/wb-core-runtime/evidence/buyout-mature-backfill-pr947",
        "provenance": (
            {
                "path": "/opt/wb-core-runtime/evidence/buyout-mature-backfill-pr947/buyout-mature-backfill-reconciliation-20260809T163909Z.json",
                "status": "reconciled",
                "backup_path_field": "backup_path",
                "backup_sha_field": "backup_sha256",
            },
        ),
    },
    {
        "key": "proxy-v4-pr949",
        "source_path": "/opt/wb-core-runtime/evidence/proxy-v4-pr949/backups/proxy-v4-reconciliation-20260810T113126Z-5dd417c641fc.sqlite3",
        "expected_identity": {
            "device": 2049,
            "inode": 1098060,
            "apparent_size_bytes": 4161679360,
            "allocated_blocks_512": 8128280,
            "allocated_bytes": 4161679360,
            "mode": "0o600",
            "uid": 0,
            "gid": 0,
            "mtime_ns": 1786361561239034701,
            "ctime_ns": 1786361562498068588,
            "nlink": 1,
            "sha256": "sha256:6160c1d7b68fe245dce557e367f45729896e5042df4883d26b74eb79474ce698",
        },
        "archive_name": "05-proxy-v4-pr949.sqlite3.zst",
        "owner": "proxy_v4_reconciliation",
        "family": "task_evidence_full_copies/proxy-v4-pr949",
        "restore_role": "completed Proxy V4 reconciliation pre-change recovery copy",
        "hold_root": "/opt/wb-core-runtime/evidence/proxy-v4-pr949",
        "provenance": (
            {
                "path": "/opt/wb-core-runtime/evidence/proxy-v4-pr949/proxy-v4-reconciliation-20260810T113126Z.json",
                "status": "reconciled",
                "backup_path_field": "backup_path",
                "backup_sha_field": "backup_sha256",
            },
        ),
    },
    {
        "key": "proxy-v4-transit-pr995",
        "source_path": "/opt/wb-core-runtime/evidence/proxy-v4-transit-pr995/backups/proxy-v4-transit-20260821T050837Z-11e9118fa18b.sqlite3",
        "expected_identity": {
            "device": 2049,
            "inode": 1107992,
            "apparent_size_bytes": 5580365824,
            "allocated_blocks_512": 10899176,
            "allocated_bytes": 5580378112,
            "mode": "0o600",
            "uid": 0,
            "gid": 0,
            "mtime_ns": 1787288945666080666,
            "ctime_ns": 1787288945666080666,
            "nlink": 1,
            "sha256": "sha256:3c857c0fa293fd9ab01bb3b2cd8026a7da85160caeb14fcaf95565253c8f5a26",
        },
        "archive_name": "06-proxy-v4-transit-pr995.sqlite3.zst",
        "owner": "proxy_v4_transit_repair",
        "family": "task_evidence_full_copies/proxy-v4-transit-pr995",
        "restore_role": "completed Proxy V4 transit repair pre-change recovery copy",
        "hold_root": "/opt/wb-core-runtime/evidence/proxy-v4-transit-pr995",
        "provenance": (
            {
                "path": "/opt/wb-core-runtime/evidence/proxy-v4-transit-pr995/proxy-v4-transit-reconciliation-20260821T050837Z.json",
                "status": "reconciled",
                "backup_path_field": "backup_path",
                "backup_sha_field": "backup_sha256",
                "integrity_field": "backup_integrity_check",
            },
        ),
    },
)


class WarmArchiveError(RuntimeError):
    """Exact block-006 guard failed closed."""

    def __init__(self, message: str, *, evidence: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.evidence = dict(evidence or {})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default="/opt/wb-core-runtime/state")
    parser.add_argument("--root-backups", default="/opt/wb-core-runtime/backups")
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument(
        "--deployed-sha-file",
        default="/opt/wb-core-runtime/app/.wb-core-runtime-sha",
    )
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--operation-id", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)
    readiness = subparsers.add_parser("readiness")
    readiness.add_argument("--readiness-id", required=True)
    dry_run_parser = subparsers.add_parser("dry-run")
    dry_run_parser.add_argument("--projection-manifest", required=True)
    dry_run_parser.add_argument("--projection-manifest-sha256", required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--manifest", required=True)
    apply.add_argument("--manifest-sha256", required=True)
    apply.add_argument("--approval-reference", required=True)
    readback = subparsers.add_parser("readback")
    readback.add_argument("--manifest", required=True)
    readback.add_argument("--manifest-sha256", required=True)
    readback.add_argument("--job-id", default="")
    readback.add_argument("--wait-seconds", type=int, default=0)
    return parser


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_json_bytes(value)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _file_identity(path: Path, *, include_sha256: bool = True) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise WarmArchiveError(f"exact file is unavailable or unsafe: {path}")
    value = path.stat()
    result = {
        "path": str(path.resolve()),
        "device": int(value.st_dev),
        "device_major": int(os.major(value.st_dev)),
        "device_minor": int(os.minor(value.st_dev)),
        "inode": int(value.st_ino),
        "apparent_size_bytes": int(value.st_size),
        "allocated_blocks_512": int(value.st_blocks),
        "allocated_bytes": int(value.st_blocks * 512),
        "mode": oct(stat.S_IMODE(value.st_mode)),
        "uid": int(value.st_uid),
        "gid": int(value.st_gid),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
        "nlink": int(value.st_nlink),
    }
    if include_sha256:
        result["sha256"] = _sha256_file(path)
    return result


def _sidecar_observation(source: Path) -> list[dict[str, Any]]:
    result = []
    for suffix in ("-wal", "-shm", "-journal"):
        path = Path(str(source) + suffix)
        if path.is_symlink():
            result.append(
                {
                    "suffix": suffix,
                    "path": str(path),
                    "present": True,
                    "kind": "symlink",
                }
            )
            continue
        row = {"suffix": suffix, "path": str(path), "present": path.exists()}
        if path.exists():
            row["identity"] = _file_identity(path)
        result.append(row)
    return result


def _sidecars(source: Path) -> list[dict[str, Any]]:
    result = _sidecar_observation(source)
    wal = next(item for item in result if item["suffix"] == "-wal")
    rollback = next(item for item in result if item["suffix"] == "-journal")
    if any(item.get("kind") == "symlink" for item in result):
        raise WarmArchiveError(f"SQLite source sidecar is a symlink: {source}")
    if wal["present"] and int(wal["identity"]["apparent_size_bytes"]) != 0:
        raise WarmArchiveError("SQLite source WAL is non-empty")
    if rollback["present"]:
        raise WarmArchiveError("SQLite source rollback journal exists")
    if any(item["present"] for item in result):
        raise WarmArchiveError("exact block-006 sources must have no SQLite sidecars")
    return result


def _sqlite_probe(path: Path) -> dict[str, Any]:
    before = _sidecars(path)
    with path.open("rb") as handle:
        header = handle.read(100)
    if header[:16] != b"SQLite format 3\x00":
        raise WarmArchiveError("source SQLite header is invalid")
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
        connection.execute("PRAGMA query_only=ON")
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        schema_rows = [
            list(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,rootpage,coalesce(sql,'') "
                "FROM sqlite_master ORDER BY type,name,tbl_name,rootpage,sql"
            )
        ]
        pragmas = {
            name: connection.execute(f"PRAGMA {name}").fetchone()[0]
            for name in (
                "application_id",
                "user_version",
                "schema_version",
                "page_count",
                "page_size",
                "freelist_count",
                "journal_mode",
            )
        }
    if not query_only or quick != ["ok"] or integrity != ["ok"]:
        raise WarmArchiveError("SQLite source query-only integrity proof failed")
    if _sidecars(path) != before:
        raise WarmArchiveError("SQLite immutable query changed source sidecars")
    return {
        "open": {"mode": "ro", "immutable": True, "query_only": True},
        "header": {
            "magic": "SQLite format 3\\0",
            "header_sha256": _digest(header),
            "page_size": int.from_bytes(header[16:18], "big"),
            "write_version": int(header[18]),
            "read_version": int(header[19]),
            "schema_cookie": int.from_bytes(header[40:44], "big"),
            "schema_format": int.from_bytes(header[44:48], "big"),
            "text_encoding": int.from_bytes(header[56:60], "big"),
        },
        "quick_check": "ok",
        "integrity_check": "ok",
        "schema_identity_sha256": _digest(schema_rows),
        "schema_object_count": len(schema_rows),
        "table_count": sum(1 for row in schema_rows if row[0] == "table"),
        "pragmas": pragmas,
    }


def _nested(payload: Mapping[str, Any], dotted: str) -> Any:
    value: Any = payload
    for key in dotted.split("."):
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def _provenance(policy: Mapping[str, Any], source_identity: Mapping[str, Any]) -> dict[str, Any]:
    records = []
    for rule in policy["provenance"]:
        path = Path(str(rule["path"]))
        identity = _file_identity(path)
        if identity["mode"] != "0o600" or identity["uid"] != 0 or identity["gid"] != 0:
            raise WarmArchiveError("provenance file permissions/ownership are unsafe")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WarmArchiveError("provenance JSON is unreadable") from exc
        if not isinstance(payload, Mapping):
            raise WarmArchiveError("provenance JSON must be an object")
        expected_status = str(rule.get("status") or "")
        if expected_status and str(payload.get("status") or "") != expected_status:
            raise WarmArchiveError("provenance terminal status drifted")
        for field_name in ("backup_path_field", "path_field"):
            field = str(rule.get(field_name) or "")
            if field and str(_nested(payload, field) or "") != str(source_identity["path"]):
                raise WarmArchiveError("provenance source path mismatch")
        for field_name in ("backup_sha_field", "sha_field"):
            field = str(rule.get(field_name) or "")
            if field:
                observed = str(_nested(payload, field) or "")
                if not observed.startswith("sha256:"):
                    observed = "sha256:" + observed
                if observed != source_identity["sha256"]:
                    raise WarmArchiveError("provenance source SHA-256 mismatch")
        integrity_field = str(rule.get("integrity_field") or "")
        if integrity_field and str(_nested(payload, integrity_field) or "") != "ok":
            raise WarmArchiveError("provenance integrity proof is absent")
        recovery_contains = str(rule.get("recovery_contains") or "")
        if recovery_contains and recovery_contains not in str(payload.get("recovery") or ""):
            raise WarmArchiveError("provenance recovery role is absent")
        records.append(
            {
                "path": str(path),
                "identity": identity,
                "status": str(payload.get("status") or ""),
                "deployed_sha": str(payload.get("deployed_sha") or ""),
                "approval_reference_present": bool(payload.get("approval_reference")),
            }
        )
    return {"records": records, "digest": _digest(records)}


def _kernel_locks(identity: Mapping[str, Any], *, source: Path) -> list[dict[str, Any]]:
    token = (
        f"{int(identity['device_major']):02x}:"
        f"{int(identity['device_minor']):02x}:{int(identity['inode'])}"
    )
    locks = Path("/proc/locks")
    if not locks.is_file():
        raise WarmArchiveError(
            f"kernel lock inventory is unavailable for source: {source}",
            evidence={"source_path": str(source), "kernel_lock_inventory": "unavailable"},
        )
    result = []
    for line in locks.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if token not in fields:
            continue
        result.append(
            {
                "source_path": str(source),
                "lock_id": fields[0].rstrip(":") if fields else "",
                "lock_type": fields[1] if len(fields) > 1 else "unknown",
                "scope": fields[2] if len(fields) > 2 else "unknown",
                "access_mode": fields[3].lower() if len(fields) > 3 else "unknown",
                "pid": int(fields[4]) if len(fields) > 4 and fields[4].isdigit() else None,
                "device_inode": token,
                "range_start": fields[6] if len(fields) > 6 else "unknown",
                "range_end": fields[7] if len(fields) > 7 else "unknown",
            }
        )
    return result


def _process_fd_openers(source: Path) -> list[dict[str, Any]]:
    """Collect exact inode-bound FD evidence with fail-closed access modes."""

    if source.is_symlink() or not source.is_file():
        raise WarmArchiveError(
            f"source is unavailable during FD inventory: {source}",
            evidence={"source_path": str(source), "fd_inventory": "source_unavailable"},
        )
    source_stat = source.stat()
    proc = Path("/proc")
    if not proc.is_dir():
        raise WarmArchiveError(
            f"process inventory is unavailable for source: {source}",
            evidence={"source_path": str(source), "fd_inventory": "proc_unavailable"},
        )
    result = []
    for pid_dir in sorted(
        (item for item in proc.iterdir() if item.name.isdigit()),
        key=lambda item: int(item.name),
    ):
        fd_dir = pid_dir / "fd"
        try:
            fd_paths = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd_path in fd_paths:
            try:
                fd_stat = fd_path.stat()
            except OSError:
                continue
            if fd_stat.st_dev != source_stat.st_dev or fd_stat.st_ino != source_stat.st_ino:
                continue
            flags: int | None = None
            try:
                for line in (pid_dir / "fdinfo" / fd_path.name).read_text(
                    encoding="utf-8"
                ).splitlines():
                    if line.startswith("flags:"):
                        flags = int(line.split(":", 1)[1].strip(), 8)
                        break
            except (OSError, ValueError):
                pass
            try:
                comm = (pid_dir / "comm").read_text(encoding="utf-8").strip()[:120]
            except OSError:
                comm = ""
            try:
                fd_target = os.readlink(fd_path)
            except OSError:
                fd_target = "unavailable"
            try:
                real_target = str(fd_path.resolve(strict=True))
            except OSError:
                real_target = "unavailable"
            result.append(
                {
                    "source_path": str(source),
                    "pid": int(pid_dir.name),
                    "fd": int(fd_path.name),
                    "access_mode": (
                        {0: "read_only", 1: "write_only", 2: "read_write"}.get(
                            flags & os.O_ACCMODE, "unknown"
                        )
                        if flags is not None
                        else "unknown"
                    ),
                    "comm": comm,
                    "fd_target": fd_target[:500],
                    "real_fd_target": real_target[:500],
                    "target_device": int(fd_stat.st_dev),
                    "target_device_major": int(os.major(fd_stat.st_dev)),
                    "target_device_minor": int(os.minor(fd_stat.st_dev)),
                    "target_inode": int(fd_stat.st_ino),
                    "binds_source_device_inode": True,
                }
            )
    return result


def _related_processes(source: Path) -> list[dict[str, Any]]:
    terms = {source.name, source.parent.name, source.parent.parent.name}
    own = {os.getpid(), os.getppid()}
    result = []
    proc = Path("/proc")
    if not proc.is_dir():
        raise WarmArchiveError("process inventory is unavailable")
    for item in proc.iterdir():
        if not item.name.isdigit() or int(item.name) in own:
            continue
        try:
            command_bytes = (item / "cmdline").read_bytes()
            command = command_bytes.replace(b"\x00", b" ").decode(
                "utf-8", errors="replace"
            ).strip()
        except OSError:
            continue
        matches = sorted(term for term in terms if term and term in command)
        if matches:
            try:
                comm = (item / "comm").read_text(encoding="utf-8").strip()[:120]
            except OSError:
                comm = ""
            result.append(
                {
                    "pid": int(item.name),
                    "comm": comm,
                    "matches": matches,
                    "cmdline_sha256": _digest(command_bytes),
                    "classification": "observation_only_without_fd_or_lock_binding",
                }
            )
    return result


def _identity_fields_match(
    observed: Mapping[str, Any], expected: Mapping[str, Any], *, include_sha256: bool
) -> bool:
    fields = [key for key in expected if include_sha256 or key != "sha256"]
    return {key: observed.get(key) for key in fields} == {
        key: expected.get(key) for key in fields
    }


def _classify_activity_evidence(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if evidence.get("identity_matches_expected") is not True:
        blockers.append({"code": "source_identity_drift"})
    if evidence.get("material_stable_during_gate") is not True:
        blockers.append({"code": "source_material_drift_during_gate"})
    sidecars = [item for item in evidence.get("sidecars", []) if item.get("present")]
    if sidecars:
        blockers.append({"code": "sqlite_sidecar_present", "sidecars": sidecars})
    for opener in evidence.get("fd_openers", []):
        mode = str(opener.get("access_mode") or "unknown")
        if mode != "read_only":
            blockers.append(
                {
                    "code": "write_capable_or_unknown_fd_opener",
                    "pid": opener.get("pid"),
                    "fd": opener.get("fd"),
                    "comm": opener.get("comm"),
                    "access_mode": mode,
                }
            )
    if evidence.get("kernel_locks"):
        blockers.append(
            {"code": "kernel_lock_present", "locks": evidence["kernel_locks"]}
        )
    hold = evidence.get("hold_evidence") or {}
    if hold.get("marker_paths") or hold.get("hold_xattr_names"):
        blockers.append({"code": "hold_evidence_present", "hold_evidence": hold})
    if evidence.get("provenance_matches_expected") is not True:
        blockers.append({"code": "provenance_drift"})
    return blockers


def _source_activity_gate(
    *,
    policy: Mapping[str, Any],
    expected_identity: Mapping[str, Any],
    expected_provenance_digest: str,
    gate_name: str,
    include_sha256: bool,
    raise_on_block: bool = True,
) -> dict[str, Any]:
    """Observe exact file activity; string coincidences never become blockers."""

    source = Path(str(policy["source_path"]))
    identity_before = _file_identity(source, include_sha256=include_sha256)
    sidecars = _sidecar_observation(source)
    openers = _process_fd_openers(source)
    locks = _kernel_locks(identity_before, source=source)
    related = _related_processes(source)
    opener_pids = {int(item["pid"]) for item in openers}
    for item in related:
        item["actual_fd_binding_observed"] = int(item["pid"]) in opener_pids
        if item["actual_fd_binding_observed"]:
            item["classification"] = "related_process_with_exact_fd_binding"
    hold = _hold_evidence(policy, source, fail_on_hold=False)
    source_identity_for_provenance = dict(expected_identity)
    source_identity_for_provenance.update(identity_before)
    source_identity_for_provenance.setdefault("sha256", expected_identity.get("sha256"))
    try:
        provenance = _provenance(policy, source_identity_for_provenance)
        provenance_error = None
    except WarmArchiveError as exc:
        provenance = {"records": [], "digest": "", "error": str(exc)}
        provenance_error = str(exc)
    identity_after = _file_identity(source, include_sha256=False)
    expected_stat = {key: value for key, value in identity_before.items() if key != "sha256"}
    evidence = {
        "gate": gate_name,
        "observed_at": _now(),
        "source_path": str(source),
        "expected_identity": dict(expected_identity),
        "identity_before": identity_before,
        "identity_after": identity_after,
        "identity_matches_expected": _identity_fields_match(
            identity_before, expected_identity, include_sha256=include_sha256
        ),
        "sha256_verified": include_sha256,
        "sha256_matches_expected": (
            identity_before.get("sha256") == expected_identity.get("sha256")
            if include_sha256
            else None
        ),
        "material_stable_during_gate": identity_after == expected_stat,
        "sidecars": sidecars,
        "fd_openers": openers,
        "read_only_opener_count": sum(
            1 for item in openers if item.get("access_mode") == "read_only"
        ),
        "write_capable_or_unknown_opener_count": sum(
            1 for item in openers if item.get("access_mode") != "read_only"
        ),
        "kernel_locks": locks,
        "hold_evidence": hold,
        "provenance": provenance,
        "provenance_error": provenance_error,
        "provenance_matches_expected": (
            provenance_error is None
            and (
                not expected_provenance_digest
                or provenance.get("digest") == expected_provenance_digest
            )
        ),
        "related_process_observations": related,
    }
    blockers = _classify_activity_evidence(evidence)
    evidence["blockers"] = blockers
    evidence["classification"] = (
        "blocked"
        if blockers
        else "clean_with_read_only_openers"
        if openers
        else "clean"
    )
    if blockers and raise_on_block:
        raise WarmArchiveError(
            f"source activity gate blocked: {source}",
            evidence=evidence,
        )
    return evidence


def _hold_evidence(
    policy: Mapping[str, Any], source: Path, *, fail_on_hold: bool = True
) -> dict[str, Any]:
    if any(str(source).startswith(prefix) for prefix in PROTECTED_PREFIXES):
        raise WarmArchiveError(
            f"source entered an incident/forensic/Finance protected prefix: {source}",
            evidence={"source_path": str(source), "protected_prefix_match": True},
        )
    root = Path(str(policy["hold_root"]))
    try:
        source.resolve().relative_to(root.resolve())
    except (ValueError, OSError) as exc:
        raise WarmArchiveError(
            f"source does not belong to its exact proven family: {source}",
            evidence={"source_path": str(source), "searched_root": str(root)},
        ) from exc
    if root.is_symlink() or not root.is_dir() or source.is_symlink() or not source.is_file():
        raise WarmArchiveError(
            f"source does not belong to its exact proven family: {source}",
            evidence={"source_path": str(source), "searched_root": str(root)},
        )
    markers = sorted(
        str(candidate)
        for candidate in root.rglob("*")
        if any(term in candidate.name.lower() for term in HOLD_TERMS)
    )
    try:
        xattrs = sorted(os.listxattr(source, follow_symlinks=False))
    except OSError as exc:
        raise WarmArchiveError("source xattr inventory failed") from exc
    hold_xattrs = [name for name in xattrs if any(term in name.lower() for term in HOLD_TERMS)]
    result = {
        "classification": (
            "incident_forensic_legal_hold_evidence_present"
            if markers or hold_xattrs
            else "no_incident_forensic_legal_hold_evidence"
        ),
        "searched_root": str(root),
        "marker_paths": markers,
        "xattr_names": xattrs,
        "hold_xattr_names": hold_xattrs,
        "protected_prefix_match": False,
    }
    if fail_on_hold and (markers or hold_xattrs):
        raise WarmArchiveError(
            f"incident/forensic/legal hold evidence is present for source: {source}",
            evidence={"source_path": str(source), "hold_evidence": result},
        )
    return result


def _zstd() -> str:
    value = shutil.which("zstd")
    if not value:
        raise WarmArchiveError("zstd executable is required")
    return value


def _measure_compressed_size(source: Path) -> int:
    process = subprocess.Popen(
        [_zstd(), "-1", "-T1", "-q", "-c", "--", str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        raise WarmArchiveError("zstd measurement stream is unavailable")
    size = 0
    for chunk in iter(lambda: process.stdout.read(CHUNK_SIZE), b""):
        size += len(chunk)
    stderr = process.stderr.read() if process.stderr is not None else b""
    returncode = process.wait()
    if returncode != 0:
        raise WarmArchiveError(
            "zstd measurement failed: "
            + (stderr.decode("utf-8", errors="replace").strip() or str(returncode))
        )
    return size


def _projection_precheck(
    policy: Mapping[str, Any], expected_identity: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started = time.monotonic()
    samples = []
    consecutive_clean = 0
    while True:
        sample = _source_activity_gate(
            policy=policy,
            expected_identity=expected_identity,
            expected_provenance_digest="",
            gate_name=f"full_projection_lightweight_precheck:{len(samples) + 1}",
            include_sha256=False,
            raise_on_block=False,
        )
        samples.append(sample)
        non_activity_blockers = [
            item
            for item in sample["blockers"]
            if item["code"] != "write_capable_or_unknown_fd_opener"
        ]
        if non_activity_blockers:
            raise WarmArchiveError(
                f"source material gate blocked before full projection: {policy['source_path']}",
                evidence={**sample, "blockers": non_activity_blockers},
            )
        consecutive_clean = consecutive_clean + 1 if not sample["blockers"] else 0
        if consecutive_clean >= 2:
            full_sample = _source_activity_gate(
                policy=policy,
                expected_identity=expected_identity,
                expected_provenance_digest="",
                gate_name="full_projection_single_hash_precheck",
                include_sha256=True,
                raise_on_block=False,
            )
            samples.append(full_sample)
            full_non_activity_blockers = [
                item
                for item in full_sample["blockers"]
                if item["code"] != "write_capable_or_unknown_fd_opener"
            ]
            if full_non_activity_blockers:
                raise WarmArchiveError(
                    f"source material gate blocked during full projection: {policy['source_path']}",
                    evidence={**full_sample, "blockers": full_non_activity_blockers},
                )
            if not full_sample["blockers"]:
                return full_sample, samples
            consecutive_after_hash = 0
            while True:
                after_hash = _source_activity_gate(
                    policy=policy,
                    expected_identity={
                        **expected_identity,
                        **full_sample["identity_before"],
                    },
                    expected_provenance_digest=str(
                        full_sample["provenance"]["digest"]
                    ),
                    gate_name=(
                        "full_projection_post_hash_activity_stabilization:"
                        f"{len(samples) + 1}"
                    ),
                    include_sha256=False,
                    raise_on_block=False,
                )
                samples.append(after_hash)
                after_non_activity = [
                    item
                    for item in after_hash["blockers"]
                    if item["code"] != "write_capable_or_unknown_fd_opener"
                ]
                if after_non_activity:
                    raise WarmArchiveError(
                        f"source material drifted after full projection hash: {policy['source_path']}",
                        evidence={**after_hash, "blockers": after_non_activity},
                    )
                consecutive_after_hash = (
                    consecutive_after_hash + 1 if not after_hash["blockers"] else 0
                )
                if consecutive_after_hash >= 2:
                    return full_sample, samples
                if time.monotonic() - started >= READINESS_MAX_STABILIZATION_SECONDS:
                    raise WarmArchiveError(
                        f"persistent write-capable source activity after full projection hash: {policy['source_path']}",
                        evidence={
                            "source_path": policy["source_path"],
                            "classification": "persistent_write_capable_activity",
                            "samples": samples,
                            "callback": after_hash["blockers"],
                        },
                    )
                time.sleep(READINESS_SAMPLE_INTERVAL_SECONDS)
        if time.monotonic() - started >= READINESS_MAX_STABILIZATION_SECONDS:
            raise WarmArchiveError(
                f"persistent write-capable source activity before full projection: {policy['source_path']}",
                evidence={
                    "source_path": policy["source_path"],
                    "classification": "persistent_write_capable_activity",
                    "samples": samples,
                    "callback": samples[-1]["blockers"],
                },
            )
        time.sleep(READINESS_SAMPLE_INTERVAL_SECONDS)


def _target_probe(
    policy: Mapping[str, Any], *, measure_compression: bool
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = Path(str(policy["source_path"]))
    expected_identity = dict(policy["expected_identity"])
    first_activity, precheck_samples = _projection_precheck(policy, expected_identity)
    identity_before = first_activity["identity_before"]
    if identity_before["mode"] != "0o600" or identity_before["uid"] != 0 or identity_before["gid"] != 0:
        raise WarmArchiveError(
            f"source permissions/ownership are not private root:root: {source}",
            evidence=first_activity,
        )
    if identity_before["nlink"] != 1:
        raise WarmArchiveError(
            f"source has another hard link: {source}", evidence=first_activity
        )
    sidecars_before = first_activity["sidecars"]
    sqlite = _sqlite_probe(source)
    provenance = first_activity["provenance"]
    hold = first_activity["hold_evidence"]
    result = {
        "key": str(policy["key"]),
        "source_path": str(source),
        "archive_name": str(policy["archive_name"]),
        "owner": str(policy["owner"]),
        "family": str(policy["family"]),
        "restore_role": str(policy["restore_role"]),
        "identity": identity_before,
        "sidecars": sidecars_before,
        "sqlite": sqlite,
        "provenance": provenance,
        "hold_evidence": hold,
    }
    if measure_compression:
        result["projected_archive_size_bytes"] = _measure_compressed_size(source)
    final_activity = _source_activity_gate(
        policy=policy,
        expected_identity=identity_before,
        expected_provenance_digest=str(provenance["digest"]),
        gate_name="full_projection_postcheck",
        include_sha256=False,
        raise_on_block=False,
    )
    material_blockers = [
        item
        for item in final_activity["blockers"]
        if item["code"]
        in {
            "source_identity_drift",
            "source_material_drift_during_gate",
            "sqlite_sidecar_present",
            "kernel_lock_present",
            "hold_evidence_present",
            "provenance_drift",
        }
    ]
    if material_blockers:
        final_activity["blockers"] = material_blockers
        raise WarmArchiveError(
            f"source material drifted during full projection: {source}",
            evidence=final_activity,
        )
    return result, [*precheck_samples, final_activity]


def _lightweight_target_witness(
    target: Mapping[str, Any], *, gate_name: str, raise_on_block: bool = True
) -> dict[str, Any]:
    policy = next(
        item for item in TARGET_POLICIES if str(item["key"]) == str(target["key"])
    )
    return _source_activity_gate(
        policy=policy,
        expected_identity=target["identity"],
        expected_provenance_digest=str(target["provenance"]["digest"]),
        gate_name=gate_name,
        include_sha256=False,
        raise_on_block=raise_on_block,
    )


def _filesystem(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_dir():
        raise WarmArchiveError(f"filesystem path is unavailable: {path}")
    value = path.stat()
    fs = os.statvfs(path)
    mount = _mount_identity(path)
    return {
        "path": str(path.resolve()),
        "device": int(value.st_dev),
        "available_bytes": int(fs.f_bavail * fs.f_frsize),
        "free_bytes": int(fs.f_bfree * fs.f_frsize),
        "total_bytes": int(fs.f_blocks * fs.f_frsize),
        "inode_available": int(fs.f_favail),
        "inode_free": int(fs.f_ffree),
        "inode_total": int(fs.f_files),
        "mount": mount,
    }


def _mount_identity(path: Path) -> dict[str, Any]:
    matches = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        separator = fields.index("-")
        mount_point = Path(
            fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
        )
        try:
            path.resolve().relative_to(mount_point)
        except ValueError:
            continue
        matches.append(
            (
                len(mount_point.parts),
                {
                    "mount_id": int(fields[0]),
                    "mount_point": str(mount_point),
                    "filesystem_type": fields[separator + 1],
                    "source": fields[separator + 2],
                    "options": fields[5],
                },
            )
        )
    if not matches:
        raise WarmArchiveError(f"mount identity is unavailable: {path}")
    return max(matches, key=lambda item: item[0])[1]


def _filesystem_snapshot(runtime_dir: Path, root_backups: Path) -> dict[str, Any]:
    result = {
        "root": _filesystem(root_backups),
        "backup": _filesystem(DESTINATION_ROOT),
        "generation": _filesystem(GENERATION_ROOT),
    }
    expected = {"root": "/dev/sda1", "backup": "/dev/sdb1", "generation": "/dev/sdc1"}
    for name, source in expected.items():
        if result[name]["mount"]["source"] != source:
            raise WarmArchiveError(f"{name} filesystem source drifted")
    if len({int(item["device"]) for item in result.values()}) != 3:
        raise WarmArchiveError("root/backup/generation devices are not distinct")
    if runtime_dir.resolve() != Path("/opt/wb-core-runtime/state"):
        raise WarmArchiveError("runtime directory is not canonical")
    if root_backups.resolve() != Path("/opt/wb-core-runtime/backups"):
        raise WarmArchiveError("root backup directory is not canonical")
    return result


def _store_registry(runtime_dir: Path, targets: list[Mapping[str, Any]]) -> dict[str, Any]:
    registry = StoreRegistry(runtime_dir)
    manifest = registry.load(require_files=True)
    raw = registry.resolve("finance_raw", manifest=manifest)
    operational = registry.resolve("operational", manifest=manifest)
    active = {str(raw.resolve()), str(operational.resolve()), str((runtime_dir / "registry_upload_runtime.sqlite3").resolve())}
    if any(str(item["source_path"]) in active for item in targets):
        raise WarmArchiveError("a target is an active/canonical StoreRegistry database")
    manifest_path = runtime_dir / "storage_generation_manifest.json"
    return {
        "manifest": manifest_payload(manifest),
        "manifest_file_sha256": _sha256_file(manifest_path) if manifest_path.is_file() else None,
        "active_paths": sorted(active),
        "digest": _digest({"manifest": manifest_payload(manifest), "active_paths": sorted(active)}),
    }


def _root_policy_snapshot(
    targets: list[Mapping[str, Any]], *, require_targets: bool = True
) -> dict[str, Any]:
    policy = load_policy()
    status_payload = collect_root_storage_status(policy=policy)
    if status_payload.get("unregistered_large_root_files"):
        raise WarmArchiveError("root storage status has an unregistered producer")
    by_path = {str(item["path"]): item for item in status_payload["large_root_files"]}
    target_rows = []
    for item in targets:
        row = by_path.get(str(item["source_path"]))
        expected_owner = (
            "root_legacy_backup_families"
            if item["key"] == "ff-pool-overhead-backfill"
            else "task_evidence_full_copies"
        )
        if row is None:
            if require_targets:
                raise WarmArchiveError(
                    "target root-storage owner/classification is not exact"
                )
            continue
        if row.get("owner") != expected_owner or row.get("registered") is not True:
            raise WarmArchiveError("target root-storage owner/classification is not exact")
        target_rows.append(dict(row))
    target_paths = {str(item["source_path"]) for item in targets}
    protected = []
    for row in status_payload["large_root_files"]:
        if str(row["path"]) in target_paths:
            continue
        protected.append(
            {
                "path": str(row["path"]),
                "device": int(row["device"]),
                "inode": int(row["inode"]),
                "size_bytes": int(row["size_bytes"]),
                "mtime_ns": int(row["mtime_ns"]),
                "owner": str(row["owner"]),
                "classification": str(row["classification"]),
            }
        )
    return {
        "policy_sha256": status_payload["policy_sha256"],
        "target_rows": target_rows,
        "protected_path_identities": protected,
        "protected_path_identity_digest": _digest(protected),
        "status": status_payload["status"],
        "available_bytes": int(status_payload["filesystems"]["root"]["available_bytes"]),
    }


def _non_target_snapshot() -> dict[str, Any]:
    excluded = set()
    roots = set()
    for policy in TARGET_POLICIES:
        source = Path(str(policy["source_path"]))
        excluded.update({str(source), str(source) + "-wal", str(source) + "-shm", str(source) + "-journal"})
        roots.add(Path(str(policy["hold_root"])))
    rows = []
    for root in sorted(roots, key=str):
        for path in sorted(root.rglob("*"), key=str):
            if str(path) in excluded:
                continue
            value = path.lstat()
            row = {
                "path": str(path),
                "mode": oct(stat.S_IMODE(value.st_mode)),
                "uid": int(value.st_uid),
                "gid": int(value.st_gid),
                "device": int(value.st_dev),
                "inode": int(value.st_ino),
                "kind": "symlink" if stat.S_ISLNK(value.st_mode) else "file" if stat.S_ISREG(value.st_mode) else "directory" if stat.S_ISDIR(value.st_mode) else "other",
            }
            if row["kind"] == "file":
                row.update(
                    {
                        "size_bytes": int(value.st_size),
                        "allocated_bytes": int(value.st_blocks * 512),
                        "mtime_ns": int(value.st_mtime_ns),
                        "ctime_ns": int(value.st_ctime_ns),
                        "sha256": _sha256_file(path),
                    }
                )
            rows.append(row)
    global_rows = []
    destination = DESTINATION_ROOT / DESTINATION_FAMILY_NAME
    for root in (
        Path("/opt/wb-core-runtime/backups"),
        Path("/opt/wb-core-runtime/evidence"),
        DESTINATION_ROOT,
    ):
        if root.is_symlink() or not root.is_dir():
            raise WarmArchiveError(f"non-target inventory root is unavailable: {root}")
        for path in sorted(root.rglob("*"), key=str):
            value = path.lstat()
            if str(path) in excluded:
                continue
            try:
                path.resolve().relative_to(destination.resolve())
            except ValueError:
                pass
            else:
                continue
            if not stat.S_ISREG(value.st_mode) and not stat.S_ISLNK(value.st_mode):
                continue
            global_rows.append(
                {
                    "path": str(path),
                    "kind": "symlink" if stat.S_ISLNK(value.st_mode) else "file",
                    "device": int(value.st_dev),
                    "inode": int(value.st_ino),
                    "size_bytes": int(value.st_size),
                    "allocated_bytes": int(value.st_blocks * 512),
                    "mode": oct(stat.S_IMODE(value.st_mode)),
                    "uid": int(value.st_uid),
                    "gid": int(value.st_gid),
                    "mtime_ns": int(value.st_mtime_ns),
                    "ctime_ns": int(value.st_ctime_ns),
                }
            )
    material = {
        "exact_family_content_rows": rows,
        "global_backup_evidence_identity_rows": global_rows,
    }
    return {
        **material,
        "exact_family_content_digest": _digest(rows),
        "global_backup_evidence_identity_digest": _digest(global_rows),
        "digest": _digest(material),
    }


def _finance_snapshot(runtime_dir: Path) -> dict[str, Any]:
    health = backup_rotation_health(runtime_dir)
    if (
        health.get("status") != "healthy"
        or health.get("next_replacement_capacity") is not True
        or health.get("blockers")
        or int(health.get("next_replacement_required_bytes") or 0) <= 0
    ):
        raise WarmArchiveError("Finance backup health/capacity is not ready")
    return {
        "status": "healthy",
        "retained_backup_id": str(health["retained_backup_id"]),
        "retained_count": int(health["retained_count"]),
        "retained_bytes": int(health["retained_bytes"]),
        "next_replacement_required_bytes": int(health["next_replacement_required_bytes"]),
        "emergency_reserve_bytes": EMERGENCY_RESERVE_BYTES,
        "required_available_floor_bytes": int(health["next_replacement_required_bytes"]) + EMERGENCY_RESERVE_BYTES,
        "available_bytes": int(health["available_bytes"]),
        "last_success": health.get("last_success"),
        "last_failure": health.get("last_failure"),
    }


def _active_sanitation_jobs(runtime_dir: Path, *, own_job_id: str = "") -> list[dict[str, Any]]:
    jobs_root = runtime_dir / "storage-recovery-sanitation-jobs"
    if not jobs_root.exists():
        return []
    result = []
    for path in sorted(jobs_root.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or not JOB_ID_RE.fullmatch(path.name) or path.name == own_job_id:
            continue
        status_path = path / "status.json"
        if not status_path.is_file():
            raise WarmArchiveError("sanitation job inventory is ambiguous")
        status_payload = json.loads(status_path.read_text(encoding="utf-8"))
        if status_payload.get("terminal") is not True:
            result.append({"job_id": path.name, "status": status_payload.get("status")})
    return result


def _other_lifecycle_locks(runtime_dir: Path) -> list[dict[str, Any]]:
    result = []
    for name in OTHER_LIFECYCLE_LOCKS:
        path = runtime_dir / name
        if path.is_symlink():
            raise WarmArchiveError(f"lifecycle lock is a symlink: {path}")
        if not path.exists():
            result.append({"path": str(path), "present": False, "locked": False})
            continue
        if not path.is_file():
            raise WarmArchiveError(f"lifecycle lock is not a file: {path}")
        with path.open("r+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                locked = True
            else:
                locked = False
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        result.append({"path": str(path), "present": True, "locked": locked})
    return result


def _systemd_snapshot(names: tuple[str, ...] = SERVICE_NAMES) -> dict[str, Any]:
    result = {}
    for name in names:
        completed = subprocess.run(
            [
                "systemctl",
                "show",
                name,
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=Result",
                "--property=MainPID",
                "--property=ExecMainStatus",
                "--property=UnitFileState",
            ],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            raise WarmArchiveError(f"systemd readback failed: {name}")
        values = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        result[name] = values
    return result


def _services_healthy(snapshot: Mapping[str, Any]) -> bool:
    for name, values in snapshot.items():
        if name.endswith(".timer"):
            if (
                values.get("LoadState") != "loaded"
                or values.get("ActiveState") != "active"
                or values.get("UnitFileState") != "enabled"
            ):
                return False
        elif name in PERSISTENT_SERVICE_NAMES:
            if (
                values.get("LoadState") != "loaded"
                or values.get("ActiveState") != "active"
                or int(values.get("MainPID") or 0) <= 0
            ):
                return False
        elif (
            values.get("LoadState") != "loaded"
            or values.get("ActiveState") not in {"active", "inactive", "activating"}
            or values.get("Result") not in {"", "success"}
            or int(values.get("ExecMainStatus") or 0) != 0
        ):
            return False
    return True


def _journald_snapshot() -> dict[str, Any]:
    service = _systemd_snapshot(("systemd-journald.service",))["systemd-journald.service"]
    effective = _effective_journald_config(expected={})
    inventory = _collect_correction_journal_inventory(Path("/var/log/journal"))
    if service.get("ActiveState") != "active" or int(service.get("MainPID") or 0) <= 0:
        raise WarmArchiveError("journald service is not active")
    if not effective.get("matches_expected"):
        raise WarmArchiveError("journald effective retention configuration drifted")
    return {"service": service, "effective": effective, "inventory": inventory}


def _validate_reusable_material(material: Mapping[str, Any]) -> list[dict[str, Any]]:
    if (
        material.get("contract_name") != CONTRACT_NAME
        or material.get("profile") != PROFILE
        or int(material.get("source_count") or 0) != EXPECTED_SOURCE_COUNT
        or material.get("destination_root") != str(DESTINATION_ROOT)
        or material.get("destination_family")
        != str(DESTINATION_ROOT / DESTINATION_FAMILY_NAME)
        or material.get("compression") != "zstd-level-1-single-thread"
    ):
        raise WarmArchiveError("reusable compression projection contract is invalid")
    targets = material.get("targets")
    if not isinstance(targets, list) or len(targets) != EXPECTED_SOURCE_COUNT:
        raise WarmArchiveError("reusable compression projection target count is invalid")
    for policy, target in zip(TARGET_POLICIES, targets, strict=True):
        if (
            target.get("key") != policy["key"]
            or target.get("source_path") != policy["source_path"]
            or target.get("archive_name") != policy["archive_name"]
            or target.get("owner") != policy["owner"]
            or target.get("family") != policy["family"]
            or target.get("restore_role") != policy["restore_role"]
            or not isinstance(target.get("projected_archive_size_bytes"), int)
            or int(target["projected_archive_size_bytes"]) <= 0
            or {
                key: (target.get("identity") or {}).get(key)
                for key in policy["expected_identity"]
            }
            != dict(policy["expected_identity"])
        ):
            raise WarmArchiveError(
                f"reusable compression projection escaped exact source: {policy['source_path']}"
            )
    return copy.deepcopy(targets)


def _material_snapshot(
    *,
    runtime_dir: Path,
    root_backups: Path,
    own_job_id: str = "",
    lifecycle_locks_held: bool = False,
    reusable_material: Mapping[str, Any] | None = None,
    witness_name: str = "material_qualification",
) -> tuple[dict[str, Any], dict[str, Any]]:
    filesystems = _filesystem_snapshot(runtime_dir, root_backups)
    activity_gates: list[dict[str, Any]] = []
    if reusable_material is None:
        targets = []
        for policy in TARGET_POLICIES:
            target, target_gates = _target_probe(policy, measure_compression=True)
            targets.append(target)
            activity_gates.extend(target_gates)
    else:
        targets = _validate_reusable_material(reusable_material)
        activity_gates = [
            _lightweight_target_witness(
                target,
                gate_name=f"{witness_name}:{target['key']}",
            )
            for target in targets
        ]
    if len(targets) != EXPECTED_SOURCE_COUNT:
        raise WarmArchiveError("exact target count is not six")
    root_policy = _root_policy_snapshot(targets)
    finance = _finance_snapshot(runtime_dir)
    active_jobs = _active_sanitation_jobs(runtime_dir, own_job_id=own_job_id)
    if active_jobs:
        raise WarmArchiveError("another sanitation operation is non-terminal")
    lifecycle_locks = (
        [
            {
                "path": str(runtime_dir / name),
                "present": True,
                "locked": True,
                "held_by_batch": True,
            }
            for name in OTHER_LIFECYCLE_LOCKS
        ]
        if lifecycle_locks_held
        else _other_lifecycle_locks(runtime_dir)
    )
    if not lifecycle_locks_held and any(item["locked"] for item in lifecycle_locks):
        raise WarmArchiveError("another storage lifecycle operation is active")
    store_registry = _store_registry(runtime_dir, targets)
    non_target = _non_target_snapshot()
    destination_family = DESTINATION_ROOT / DESTINATION_FAMILY_NAME
    if destination_family.exists():
        if destination_family.is_symlink() or not destination_family.is_dir():
            raise WarmArchiveError("destination family is unsafe")
        foreign = [
            str(path)
            for path in destination_family.iterdir()
            if not any(path.name == item["archive_name"] or path.name == item["archive_name"] + ".manifest.json" for item in targets)
            and not path.name.startswith(".wbc0008-006-")
        ]
        if foreign:
            raise WarmArchiveError("destination family contains a foreign artifact")
    running = 0
    stages = []
    floor = int(finance["required_available_floor_bytes"])
    start_available = int(filesystems["backup"]["available_bytes"])
    for target in targets:
        archive_bytes = int(target["projected_archive_size_bytes"])
        restore_bytes = int(target["identity"]["apparent_size_bytes"])
        running += archive_bytes + MANIFEST_RESERVE_BYTES_PER_SOURCE
        stage_minimum = (
            start_available
            - running
            - restore_bytes
            - CONTROL_ARTIFACT_RESERVE_BYTES
        )
        stages.append(
            {
                "key": target["key"],
                "projected_archive_size_bytes": archive_bytes,
                "full_restore_temp_bytes": restore_bytes,
                "projected_available_at_peak_bytes": stage_minimum,
                "required_available_floor_bytes": floor,
                "sufficient": stage_minimum >= floor,
            }
        )
    if any(not item["sufficient"] for item in stages):
        raise WarmArchiveError("backup capacity cannot preserve Finance plus emergency reserve")
    expected_reclaimed = sum(int(item["identity"]["allocated_bytes"]) for item in targets)
    projected_root = int(filesystems["root"]["available_bytes"]) + expected_reclaimed
    if projected_root < ROOT_MINIMUM_AFTER_BYTES:
        raise WarmArchiveError("exact six do not project root above 25 GiB")
    services = _systemd_snapshot()
    if not _services_healthy(services):
        raise WarmArchiveError("required production service/timer health is not ready")
    material = {
        "contract_name": CONTRACT_NAME,
        "profile": PROFILE,
        "source_count": EXPECTED_SOURCE_COUNT,
        "destination_root": str(DESTINATION_ROOT),
        "destination_family": str(destination_family),
        "targets": targets,
        "filesystems": {
            name: {
                "path": row["path"],
                "device": row["device"],
                "mount": row["mount"],
            }
            for name, row in filesystems.items()
        },
        "finance": {
            key: value
            for key, value in finance.items()
            if key not in {"available_bytes", "last_success", "last_failure"}
        },
        "store_registry": store_registry,
        "root_policy": {
            "policy_sha256": root_policy["policy_sha256"],
            "target_rows": root_policy["target_rows"],
            "protected_path_identities": root_policy["protected_path_identities"],
            "protected_path_identity_digest": root_policy["protected_path_identity_digest"],
        },
        "non_target_digest": non_target["digest"],
        "expected_unlink_count": EXPECTED_SOURCE_COUNT,
        "expected_reclaimed_allocated_bytes": expected_reclaimed,
        "root_minimum_after_bytes": ROOT_MINIMUM_AFTER_BYTES,
        "control_artifact_reserve_bytes": CONTROL_ARTIFACT_RESERVE_BYTES,
        "compression": "zstd-level-1-single-thread",
    }
    observations = {
        "witness_name": witness_name,
        "reused_compression_projection": reusable_material is not None,
        "activity_gates": activity_gates,
        "filesystems_before": filesystems,
        "root_policy_status": root_policy["status"],
        "finance_available_bytes": finance["available_bytes"],
        "capacity_stages": stages,
        "projected_root_available_bytes": projected_root,
        "active_sanitation_jobs": active_jobs,
        "non_target": non_target,
        "journald": _journald_snapshot(),
        "services": services,
    }
    return material, observations


def _validate_evidence_scope(evidence_dir: Path, operation_id: str) -> Path:
    if not re.fullmatch(r"production-goal-v1-[0-9a-f]{32}", operation_id):
        raise WarmArchiveError("operation id is invalid")
    evidence_dir = evidence_dir.resolve()
    expected = Path("/opt/wb-core-runtime/state/private-evidence/production-goals") / operation_id
    if evidence_dir != expected or evidence_dir.is_symlink() or not evidence_dir.is_dir():
        raise WarmArchiveError("evidence directory escaped exact operation scope")
    if stat.S_IMODE(evidence_dir.stat().st_mode) != 0o700:
        raise WarmArchiveError("evidence directory is not private")
    return evidence_dir


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(_canonical_json_bytes(payload) + b"\n")
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
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_readiness_scope(evidence_dir: Path, readiness_id: str) -> Path:
    if READINESS_ID_RE.fullmatch(readiness_id) is None:
        raise WarmArchiveError("readiness id is invalid")
    evidence_dir = evidence_dir.resolve()
    expected = (
        Path("/opt/wb-core-runtime/state/private-evidence/root-warm-archive-readiness")
        / readiness_id
    )
    if evidence_dir != expected or evidence_dir.is_symlink() or not evidence_dir.is_dir():
        raise WarmArchiveError("readiness evidence directory escaped exact scope")
    if stat.S_IMODE(evidence_dir.stat().st_mode) != 0o700:
        raise WarmArchiveError("readiness evidence directory is not private")
    return evidence_dir


def _activity_sample(
    targets: list[Mapping[str, Any]], *, phase: str, sample_number: int
) -> dict[str, Any]:
    started = time.monotonic()
    observations = [
        _lightweight_target_witness(
            target,
            gate_name=f"readiness:{phase}:{sample_number}:{target['key']}",
            raise_on_block=False,
        )
        for target in targets
    ]
    blockers = [
        {
            "source_path": item["source_path"],
            "classification": item["classification"],
            "blockers": item["blockers"],
            "fd_openers": item["fd_openers"],
            "kernel_locks": item["kernel_locks"],
        }
        for item in observations
        if item["blockers"]
    ]
    return {
        "phase": phase,
        "sample_number": sample_number,
        "observed_at": _now(),
        "duration_seconds": round(time.monotonic() - started, 6),
        "clean": not blockers,
        "blockers": blockers,
        "sources": observations,
    }


def _literal_activity_sample(*, phase: str, sample_number: int) -> dict[str, Any]:
    targets = []
    for policy in TARGET_POLICIES:
        identity = dict(policy["expected_identity"])
        identity.update(
            {
                "path": str(Path(str(policy["source_path"])).resolve()),
                "device_major": int(os.major(int(identity["device"]))),
                "device_minor": int(os.minor(int(identity["device"]))),
            }
        )
        provenance = _provenance(policy, identity)
        targets.append(
            {
                "key": policy["key"],
                "source_path": policy["source_path"],
                "identity": identity,
                "provenance": provenance,
            }
        )
    return _activity_sample(targets, phase=phase, sample_number=sample_number)


def _stabilize_activity(
    *,
    phase: str,
    required_clean: int,
    targets: list[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + READINESS_MAX_STABILIZATION_SECONDS
    samples = []
    consecutive_clean = 0
    sample_number = 0
    while True:
        sample_number += 1
        sample = (
            _activity_sample(targets, phase=phase, sample_number=sample_number)
            if targets is not None
            else _literal_activity_sample(phase=phase, sample_number=sample_number)
        )
        samples.append(sample)
        consecutive_clean = consecutive_clean + 1 if sample["clean"] else 0
        if consecutive_clean >= required_clean:
            return {
                "status": "clean",
                "phase": phase,
                "required_consecutive_clean": required_clean,
                "consecutive_clean": consecutive_clean,
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "samples": samples,
            }
        if time.monotonic() >= deadline:
            return {
                "status": "blocked",
                "phase": phase,
                "required_consecutive_clean": required_clean,
                "consecutive_clean": consecutive_clean,
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "samples": samples,
                "callback": samples[-1]["blockers"],
            }
        time.sleep(READINESS_SAMPLE_INTERVAL_SECONDS)


def readiness(
    *,
    runtime_dir: Path,
    root_backups: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
    evidence_dir: Path,
    readiness_id: str,
) -> dict[str, Any]:
    """Build one full projection, then prove bounded lightweight stability."""

    _verify_deployed_sha(deployed_sha=deployed_sha, deployed_sha_file=deployed_sha_file)
    evidence_dir = _validate_readiness_scope(evidence_dir, readiness_id)
    receipt_path = evidence_dir / "root-warm-archive-readiness.json"
    if receipt_path.exists():
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("contract_name") != CONTRACT_NAME
            or payload.get("readiness_id") != readiness_id
            or payload.get("deployed_sha") != deployed_sha
        ):
            raise WarmArchiveError("existing readiness receipt binding is invalid")
        return {**payload, "idempotent": True}

    pre_stabilization = _stabilize_activity(
        phase="pre_projection", required_clean=2, targets=None
    )
    if pre_stabilization["status"] != "clean":
        result = {
            "contract_name": CONTRACT_NAME,
            "status": "blocked",
            "query_only": True,
            "database_written": False,
            "readiness_id": readiness_id,
            "deployed_sha": deployed_sha,
            "reason": "persistent_source_activity_before_projection",
            "pre_projection_stabilization": pre_stabilization,
            "callback": pre_stabilization.get("callback", []),
            "completed_at": _now(),
        }
        _atomic_write_json(receipt_path, result)
        return result

    try:
        material, full_observations = _material_snapshot(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            witness_name="readiness_full_projection",
        )
    except WarmArchiveError as exc:
        result = {
            "contract_name": CONTRACT_NAME,
            "status": "blocked",
            "query_only": True,
            "database_written": False,
            "readiness_id": readiness_id,
            "deployed_sha": deployed_sha,
            "reason": "full_projection_or_material_preflight_blocked",
            "pre_projection_stabilization": pre_stabilization,
            "callback": [
                {
                    "message": str(exc),
                    "source_path": exc.evidence.get("source_path"),
                    "classification": exc.evidence.get("classification"),
                    "blockers": exc.evidence.get("blockers"),
                    "fd_openers": exc.evidence.get("fd_openers"),
                    "kernel_locks": exc.evidence.get("kernel_locks"),
                    "evidence": exc.evidence,
                }
            ],
            "completed_at": _now(),
        }
        _atomic_write_json(receipt_path, result)
        return result
    projection = {
        "contract_name": CONTRACT_NAME,
        "status": "projection_ready",
        "query_only": True,
        "database_written": False,
        "readiness_id": readiness_id,
        "deployed_sha": deployed_sha,
        "created_at": _now(),
        "material": material,
        "material_qualification_digest": _digest(material),
        "observations": full_observations,
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    projection_path = evidence_dir / f"root-warm-archive-readiness-projection-{timestamp}.json"
    _atomic_write_json(projection_path, projection)
    projection_sha256 = _sha256_file(projection_path)

    stabilization = _stabilize_activity(
        phase="post_projection",
        required_clean=READINESS_REQUIRED_CONSECUTIVE_CLEAN,
        targets=material["targets"],
    )
    final_material = None
    final_observations = None
    final_error = None
    if stabilization["status"] == "clean":
        try:
            final_material, final_observations = _material_snapshot(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                reusable_material=material,
                witness_name="readiness_final_capacity_and_material_cas",
            )
        except WarmArchiveError as exc:
            final_error = {"message": str(exc), "evidence": exc.evidence}
    ready = bool(
        stabilization["status"] == "clean"
        and final_error is None
        and final_material is not None
        and _digest(final_material) == _digest(material)
    )
    result = {
        "contract_name": CONTRACT_NAME,
        "status": "ready" if ready else "blocked",
        "query_only": True,
        "database_written": False,
        "readiness_id": readiness_id,
        "deployed_sha": deployed_sha,
        "source_count": EXPECTED_SOURCE_COUNT,
        "required_consecutive_clean": READINESS_REQUIRED_CONSECUTIVE_CLEAN,
        "max_stabilization_seconds": READINESS_MAX_STABILIZATION_SECONDS,
        "pre_projection_stabilization": pre_stabilization,
        "post_projection_stabilization": stabilization,
        "projection_manifest_path": str(projection_path),
        "projection_manifest_sha256": projection_sha256,
        "material_qualification_digest": _digest(material),
        "expected_reclaimed_allocated_bytes": material[
            "expected_reclaimed_allocated_bytes"
        ],
        "required_backup_floor_bytes": material["finance"][
            "required_available_floor_bytes"
        ],
        "root_minimum_after_bytes": ROOT_MINIMUM_AFTER_BYTES,
        "capacity_guard_passed": bool(
            ready
            and final_observations
            and all(
                bool(item["sufficient"])
                for item in final_observations["capacity_stages"]
            )
        ),
        "minimum_projected_backup_available_bytes": (
            min(
                int(item["projected_available_at_peak_bytes"])
                for item in final_observations["capacity_stages"]
            )
            if final_observations
            else None
        ),
        "projected_root_available_bytes": (
            final_observations["projected_root_available_bytes"]
            if final_observations
            else None
        ),
        "final_material_digest": _digest(final_material) if final_material else None,
        "final_observations": final_observations,
        "final_error": final_error,
        "callback": (
            []
            if ready
            else stabilization.get("callback", [])
            or ([final_error] if final_error else [])
        ),
        "completed_at": _now(),
    }
    _atomic_write_json(receipt_path, result)
    return result


def _load_readiness_projection(
    *,
    projection_path: Path,
    projection_sha256: str,
    deployed_sha: str,
) -> dict[str, Any]:
    projection_path = projection_path.resolve()
    expected_root = Path(
        "/opt/wb-core-runtime/state/private-evidence/root-warm-archive-readiness"
    )
    try:
        relative = projection_path.relative_to(expected_root)
    except ValueError as exc:
        raise WarmArchiveError("compression projection escaped readiness evidence") from exc
    if (
        len(relative.parts) != 2
        or READINESS_ID_RE.fullmatch(relative.parts[0]) is None
        or re.fullmatch(
            r"root-warm-archive-readiness-projection-[0-9]{8}T[0-9]{6}Z\.json",
            relative.parts[1],
        )
        is None
        or not SHA256_RE.fullmatch(projection_sha256)
        or projection_path.is_symlink()
        or not projection_path.is_file()
        or stat.S_IMODE(projection_path.stat().st_mode) != 0o600
        or _sha256_file(projection_path) != projection_sha256
    ):
        raise WarmArchiveError("compression projection binding is invalid")
    payload = json.loads(projection_path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("contract_name") != CONTRACT_NAME
        or payload.get("status") != "projection_ready"
        or payload.get("query_only") is not True
        or payload.get("database_written") is not False
        or payload.get("deployed_sha") != deployed_sha
        or payload.get("readiness_id") != relative.parts[0]
        or payload.get("material_qualification_digest") != _digest(payload.get("material"))
    ):
        raise WarmArchiveError("compression projection payload is invalid")
    readiness_receipt_path = projection_path.parent / "root-warm-archive-readiness.json"
    if (
        readiness_receipt_path.is_symlink()
        or not readiness_receipt_path.is_file()
        or stat.S_IMODE(readiness_receipt_path.stat().st_mode) != 0o600
    ):
        raise WarmArchiveError("ready compression projection receipt is unavailable")
    readiness_receipt = json.loads(readiness_receipt_path.read_text(encoding="utf-8"))
    if (
        not isinstance(readiness_receipt, dict)
        or readiness_receipt.get("status") != "ready"
        or readiness_receipt.get("query_only") is not True
        or readiness_receipt.get("database_written") is not False
        or readiness_receipt.get("readiness_id") != payload.get("readiness_id")
        or readiness_receipt.get("deployed_sha") != deployed_sha
        or readiness_receipt.get("projection_manifest_path") != str(projection_path)
        or readiness_receipt.get("projection_manifest_sha256") != projection_sha256
        or readiness_receipt.get("material_qualification_digest")
        != payload.get("material_qualification_digest")
    ):
        raise WarmArchiveError("compression projection lacks an exact ready receipt")
    _validate_reusable_material(payload["material"])
    return payload


def dry_run(
    *,
    runtime_dir: Path,
    root_backups: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
    evidence_dir: Path,
    operation_id: str,
    projection_manifest: Path,
    projection_manifest_sha256: str,
) -> dict[str, Any]:
    _verify_deployed_sha(deployed_sha=deployed_sha, deployed_sha_file=deployed_sha_file)
    evidence_dir = _validate_evidence_scope(evidence_dir, operation_id)
    projection = _load_readiness_projection(
        projection_path=projection_manifest,
        projection_sha256=projection_manifest_sha256,
        deployed_sha=deployed_sha,
    )
    material, observations = _material_snapshot(
        runtime_dir=runtime_dir,
        root_backups=root_backups,
        reusable_material=projection["material"],
        witness_name="jit_lightweight_material_qualification",
    )
    material_digest = _digest(material)
    manifest = {
        "contract_name": CONTRACT_NAME,
        "status": "ready",
        "mode": "dry-run",
        "database_written": False,
        "deployed_sha": deployed_sha,
        "operation_id": operation_id,
        "created_at": _now(),
        "material": material,
        "material_qualification_digest": material_digest,
        "observations": observations,
        "readiness_projection": {
            "readiness_id": projection["readiness_id"],
            "path": str(projection_manifest),
            "sha256": projection_manifest_sha256,
            "material_qualification_digest": projection[
                "material_qualification_digest"
            ],
        },
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = evidence_dir / f"root-warm-archive-plan-{timestamp}.json"
    suffix = 0
    while manifest_path.exists():
        suffix += 1
        manifest_path = evidence_dir / f"root-warm-archive-plan-{timestamp}-{suffix}.json"
    _atomic_write_json(manifest_path, manifest)
    manifest_sha256 = _sha256_file(manifest_path)
    stages = observations["capacity_stages"]
    activity = observations["activity_gates"]
    read_only_openers = sum(
        int(item["read_only_opener_count"]) for item in activity
    )
    blocking_openers = sum(
        int(item["write_capable_or_unknown_opener_count"]) for item in activity
    )
    return {
        "contract_name": CONTRACT_NAME,
        "status": "ready",
        "mode": "dry-run",
        "database_written": False,
        "deployed_sha": deployed_sha,
        "operation_id": operation_id,
        "source_count": EXPECTED_SOURCE_COUNT,
        "expected_unlink_count": EXPECTED_SOURCE_COUNT,
        "expected_reclaimed_allocated_bytes": material["expected_reclaimed_allocated_bytes"],
        "destination_family": material["destination_family"],
        "root_minimum_after_bytes": ROOT_MINIMUM_AFTER_BYTES,
        "projected_root_available_bytes": observations["projected_root_available_bytes"],
        "finance_next_replacement_required_bytes": material["finance"]["next_replacement_required_bytes"],
        "emergency_reserve_bytes": EMERGENCY_RESERVE_BYTES,
        "required_backup_floor_bytes": material["finance"]["required_available_floor_bytes"],
        "minimum_projected_backup_available_bytes": min(int(item["projected_available_at_peak_bytes"]) for item in stages),
        "capacity_guard_passed": all(bool(item["sufficient"]) for item in stages),
        "openers_count": blocking_openers,
        "read_only_openers_count": read_only_openers,
        "write_capable_or_unknown_openers_count": blocking_openers,
        "locks_count": sum(len(item["kernel_locks"]) for item in activity),
        "holds_count": sum(
            1
            for item in activity
            if item["hold_evidence"]["marker_paths"]
            or item["hold_evidence"]["hold_xattr_names"]
        ),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "material_qualification_digest": material_digest,
        "non_target_digest": material["non_target_digest"],
        "readiness_id": projection["readiness_id"],
        "projection_manifest_path": str(projection_manifest),
        "projection_manifest_sha256": projection_manifest_sha256,
        "activity_evidence": observations["activity_gates"],
        "root_policy_sha256": material["root_policy"]["policy_sha256"],
    }


def _load_manifest(
    *, evidence_dir: Path, operation_id: str, manifest_path: Path, manifest_sha256: str
) -> dict[str, Any]:
    evidence_dir = _validate_evidence_scope(evidence_dir, operation_id)
    manifest_path = manifest_path.resolve()
    if (
        manifest_path.parent != evidence_dir
        or not re.fullmatch(r"root-warm-archive-plan-[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?\.json", manifest_path.name)
        or not SHA256_RE.fullmatch(manifest_sha256)
        or _sha256_file(manifest_path) != manifest_sha256
        or stat.S_IMODE(manifest_path.stat().st_mode) != 0o600
    ):
        raise WarmArchiveError("manifest binding escaped exact private evidence scope")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    projection = payload.get("readiness_projection") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("contract_name") != CONTRACT_NAME
        or payload.get("status") != "ready"
        or payload.get("operation_id") != operation_id
        or payload.get("material_qualification_digest") != _digest(payload.get("material"))
        or int((payload.get("material") or {}).get("source_count") or 0) != EXPECTED_SOURCE_COUNT
        or not isinstance(projection, Mapping)
        or READINESS_ID_RE.fullmatch(str(projection.get("readiness_id") or "")) is None
        or re.fullmatch(
            r"/opt/wb-core-runtime/state/private-evidence/root-warm-archive-readiness/"
            r"readiness-v1-[0-9a-f]{32}/"
            r"root-warm-archive-readiness-projection-[0-9]{8}T[0-9]{6}Z\.json",
            str(projection.get("path") or ""),
        )
        is None
        or not SHA256_RE.fullmatch(str(projection.get("sha256") or ""))
        or projection.get("material_qualification_digest")
        != payload.get("material_qualification_digest")
    ):
        raise WarmArchiveError("manifest contract/material binding is invalid")
    return payload


class _exclusive_finance_lock:
    def __init__(self, runtime_dir: Path):
        self.path = runtime_dir / FINANCE_STORAGE_LOCK_FILENAME
        self.handle: Any = None

    def __enter__(self):
        if self.path.is_symlink():
            raise WarmArchiveError("Finance storage lock is a symlink")
        self.handle = self.path.open("a+b")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()


class _exclusive_other_lifecycle_locks:
    def __init__(self, runtime_dir: Path):
        self.paths = [runtime_dir / name for name in OTHER_LIFECYCLE_LOCKS]
        self.handles: list[Any] = []

    def __enter__(self):
        try:
            for path in self.paths:
                if path.is_symlink():
                    raise WarmArchiveError(f"lifecycle lock is a symlink: {path}")
                handle = path.open("a+b")
                os.chmod(path, 0o600)
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    handle.close()
                    raise WarmArchiveError(
                        "another storage lifecycle operation is active"
                    ) from exc
                self.handles.append(handle)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self.handles

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        while self.handles:
            handle = self.handles.pop()
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            raise WarmArchiveError("Finance storage operation/reservation is active") from exc
        return self.handle

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        assert self.handle is not None
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def _compress(source: Path, temporary: Path) -> dict[str, Any]:
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    size = 0
    process = subprocess.Popen(
        [_zstd(), "-1", "-T1", "-q", "-c", "--", str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        os.close(descriptor)
        raise WarmArchiveError("zstd compression stream is unavailable")
    try:
        with os.fdopen(descriptor, "wb") as output:
            for chunk in iter(lambda: process.stdout.read(CHUNK_SIZE), b""):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        stderr = process.stderr.read() if process.stderr is not None else b""
        returncode = process.wait()
    except Exception:
        process.kill()
        process.wait()
        raise
    if returncode != 0:
        raise WarmArchiveError(
            "zstd compression failed: "
            + (stderr.decode("utf-8", errors="replace").strip() or str(returncode))
        )
    return {"archive_size_bytes": size, "archive_sha256": "sha256:" + digest.hexdigest()}


def _stream_decompressed_identity(archive: Path) -> dict[str, Any]:
    process = subprocess.Popen(
        [_zstd(), "-q", "-d", "-c", "--", str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        raise WarmArchiveError("zstd decompression stream is unavailable")
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: process.stdout.read(CHUNK_SIZE), b""):
        digest.update(chunk)
        size += len(chunk)
    stderr = process.stderr.read() if process.stderr is not None else b""
    returncode = process.wait()
    if returncode != 0:
        raise WarmArchiveError(
            "zstd decompression failed: "
            + (stderr.decode("utf-8", errors="replace").strip() or str(returncode))
        )
    return {"decompressed_size_bytes": size, "decompressed_sha256": "sha256:" + digest.hexdigest()}


def _full_restore_proof(
    *, archive: Path, expected_source: Mapping[str, Any], temporary: Path
) -> dict[str, Any]:
    if temporary.exists():
        raise WarmArchiveError("restore verification temp already exists")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    process = subprocess.Popen(
        [_zstd(), "-q", "-d", "-c", "--", str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        os.close(descriptor)
        raise WarmArchiveError("restore decompression stream is unavailable")
    try:
        with os.fdopen(descriptor, "wb") as output:
            for chunk in iter(lambda: process.stdout.read(CHUNK_SIZE), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        stderr = process.stderr.read() if process.stderr is not None else b""
        returncode = process.wait()
        if returncode != 0:
            raise WarmArchiveError(
                "full restore decompression failed: "
                + (stderr.decode("utf-8", errors="replace").strip() or str(returncode))
            )
        identity = _file_identity(temporary)
        if (
            identity["apparent_size_bytes"] != int(expected_source["apparent_size_bytes"])
            or identity["sha256"] != str(expected_source["sha256"])
        ):
            raise WarmArchiveError("full restore size/SHA differs from source")
        sqlite = _sqlite_probe(temporary)
        return {
            "temporary_mode": identity["mode"],
            "temporary_uid": identity["uid"],
            "temporary_gid": identity["gid"],
            "restored_size_bytes": identity["apparent_size_bytes"],
            "restored_sha256": identity["sha256"],
            "quick_check": sqlite["quick_check"],
            "integrity_check": sqlite["integrity_check"],
            "schema_identity_sha256": sqlite["schema_identity_sha256"],
            "verified_at": _now(),
        }
    finally:
        temporary.unlink(missing_ok=True)
        _fsync_directory(temporary.parent)


def _verify_archive_pair(
    *,
    archive: Path,
    manifest_path: Path,
    operation_id: str,
    expected_target: Mapping[str, Any],
    full_restore: bool,
    restore_temp: Path,
) -> dict[str, Any]:
    archive_identity = _file_identity(archive)
    manifest_identity = _file_identity(manifest_path)
    if (
        archive_identity["mode"] != "0o600"
        or manifest_identity["mode"] != "0o600"
        or archive_identity["uid"] != 0
        or archive_identity["gid"] != 0
        or manifest_identity["uid"] != 0
        or manifest_identity["gid"] != 0
    ):
        raise WarmArchiveError("published archive pair is not private root:root")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        payload.get("contract_name") != CONTRACT_NAME
        or payload.get("operation_id") != operation_id
        or payload.get("source") != expected_target["identity"]
        or payload.get("archive_path") != str(archive)
        or payload.get("archive_sha256") != archive_identity["sha256"]
        or int(payload.get("archive_size_bytes") or -1) != archive_identity["apparent_size_bytes"]
    ):
        raise WarmArchiveError("published archive/manifest provenance mismatch")
    tested = subprocess.run(
        [_zstd(), "-q", "-t", "--", str(archive)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if tested.returncode != 0:
        raise WarmArchiveError("published zstd frame test failed")
    decompressed = _stream_decompressed_identity(archive)
    if (
        decompressed["decompressed_size_bytes"] != int(expected_target["identity"]["apparent_size_bytes"])
        or decompressed["decompressed_sha256"] != expected_target["identity"]["sha256"]
    ):
        raise WarmArchiveError("published archive decompressed identity mismatch")
    result = {
        "archive_identity": archive_identity,
        "manifest_identity": manifest_identity,
        "zstd_test": "ok",
        **decompressed,
    }
    if full_restore:
        result["restore_proof"] = _full_restore_proof(
            archive=archive,
            expected_source=expected_target["identity"],
            temporary=restore_temp,
        )
    return result


def _capacity_guard(
    *, runtime_dir: Path, archive_bytes: int, restore_bytes: int
) -> dict[str, Any]:
    finance = _finance_snapshot(runtime_dir)
    capacity = _filesystem(DESTINATION_ROOT)
    required_floor = int(finance["required_available_floor_bytes"])
    required_before = (
        required_floor
        + int(archive_bytes)
        + int(restore_bytes)
        + CONTROL_ARTIFACT_RESERVE_BYTES
        + MANIFEST_RESERVE_BYTES_PER_SOURCE
    )
    if int(capacity["available_bytes"]) < required_before:
        raise WarmArchiveError("backup capacity guard failed before source archive")
    return {
        "finance": finance,
        "capacity": capacity,
        "archive_bytes": int(archive_bytes),
        "restore_bytes": int(restore_bytes),
        "required_before_bytes": required_before,
        "required_floor_bytes": required_floor,
    }


def _exact_source_cas(
    target: Mapping[str, Any], *, gate_name: str, full_hash: bool
) -> dict[str, Any]:
    policy = next(item for item in TARGET_POLICIES if item["key"] == target["key"])
    return _source_activity_gate(
        policy=policy,
        expected_identity=target["identity"],
        expected_provenance_digest=str(target["provenance"]["digest"]),
        gate_name=gate_name,
        include_sha256=full_hash,
    )


def _journal_path(evidence_dir: Path) -> Path:
    return evidence_dir / "root-warm-archive-apply.json"


def _read_journal(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise WarmArchiveError("durable apply journal is unavailable or unsafe")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("contract_name") != CONTRACT_NAME:
        raise WarmArchiveError("durable apply journal contract is invalid")
    return payload


def _ensure_destination_family() -> Path:
    destination = DESTINATION_ROOT / DESTINATION_FAMILY_NAME
    if destination.is_symlink():
        raise WarmArchiveError("destination family is a symlink")
    destination.mkdir(mode=0o700, exist_ok=True)
    os.chmod(destination, 0o700)
    if destination.resolve().parent != DESTINATION_ROOT.resolve():
        raise WarmArchiveError("destination family escaped the backup mount")
    _fsync_directory(DESTINATION_ROOT)
    return destination


def _reconcile_pending_unlink(
    *,
    target: Mapping[str, Any],
    item_state: Mapping[str, Any],
    archive: Path,
    manifest_path: Path,
    operation_id: str,
    restore_temp: Path,
) -> dict[str, Any] | None:
    source = Path(str(target["source_path"]))
    if source.exists():
        return None
    if item_state.get("phase") not in {"pending_unlink", "unlink_done"}:
        raise WarmArchiveError("source is absent without durable unlink intent")
    proof = _verify_archive_pair(
        archive=archive,
        manifest_path=manifest_path,
        operation_id=operation_id,
        expected_target=target,
        full_restore=True,
        restore_temp=restore_temp,
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "lifecycle_state": "retained",
            "source_removed": True,
            "unlink_receipt": {
                "count": 1,
                "source_identity": target["identity"],
                "source_absent": True,
                "reconciled_from_pending_intent": item_state.get("phase")
                == "pending_unlink",
                "completed_at": str(item_state.get("completed_at") or _now()),
            },
            "published_pair_readback": proof,
            "activity_evidence": {
                **dict(payload.get("activity_evidence") or {}),
                **dict(item_state.get("activity_evidence") or {}),
            },
            "finalized_at": _now(),
        }
    )
    _atomic_write_json(manifest_path, payload)
    final_proof = _verify_archive_pair(
        archive=archive,
        manifest_path=manifest_path,
        operation_id=operation_id,
        expected_target=target,
        full_restore=False,
        restore_temp=restore_temp,
    )
    return {
        "key": target["key"],
        "phase": "unlink_done",
        "unlink_reconciled_from_pending_intent": item_state.get("phase") == "pending_unlink",
        "unlink_count": 1,
        "source_absent": True,
        "archive_path": str(archive),
        "manifest_path": str(manifest_path),
        "archive_proof": final_proof,
        "reclaimed_allocated_bytes": int(target["identity"]["allocated_bytes"]),
        "completed_at": str(item_state.get("completed_at") or _now()),
    }


def _process_target(
    *,
    runtime_dir: Path,
    target: Mapping[str, Any],
    item_state: Mapping[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    operation_id: str,
    approval_reference: str,
    destination: Path,
) -> dict[str, Any]:
    index = next(i for i, item in enumerate(journal["items"], 1) if item["key"] == target["key"])
    source = Path(str(target["source_path"]))
    archive = destination / str(target["archive_name"])
    manifest_path = archive.with_name(archive.name + ".manifest.json")
    temp_archive = destination / f".wbc0008-006-{operation_id}-{index:02d}.archive.tmp"
    temp_manifest = destination / f".wbc0008-006-{operation_id}-{index:02d}.manifest.tmp"
    restore_temp = destination / f".wbc0008-006-{operation_id}-{index:02d}.restore.tmp.sqlite3"
    if restore_temp.exists():
        restore_temp.unlink()
        _fsync_directory(destination)
    if temp_archive.exists():
        if archive.exists() or manifest_path.exists():
            raise WarmArchiveError("owned archive temp coexists with a published pair")
        temp_archive.unlink()
        _fsync_directory(destination)
    if archive.exists() and not manifest_path.exists() and temp_manifest.exists():
        pending = json.loads(temp_manifest.read_text(encoding="utf-8"))
        archive_identity = _file_identity(archive)
        if (
            pending.get("contract_name") != CONTRACT_NAME
            or pending.get("operation_id") != operation_id
            or pending.get("source") != target["identity"]
            or pending.get("archive_path") != str(archive)
            or pending.get("archive_sha256") != archive_identity["sha256"]
            or int(pending.get("archive_size_bytes") or -1)
            != archive_identity["apparent_size_bytes"]
        ):
            raise WarmArchiveError("owned interrupted publish identity is invalid")
        os.replace(temp_manifest, manifest_path)
        os.chmod(manifest_path, 0o600)
        _fsync_directory(destination)
    elif temp_manifest.exists():
        if archive.exists() or manifest_path.exists():
            raise WarmArchiveError("owned manifest temp coexists with a published pair")
        temp_manifest.unlink()
        _fsync_directory(destination)
    reconciled = _reconcile_pending_unlink(
        target=target,
        item_state=item_state,
        archive=archive,
        manifest_path=manifest_path,
        operation_id=operation_id,
        restore_temp=restore_temp,
    )
    if reconciled is not None:
        return reconciled
    if item_state.get("phase") == "unlink_done":
        raise WarmArchiveError("completed unlink unexpectedly has a source")
    capacity_before = _capacity_guard(
        runtime_dir=runtime_dir,
        archive_bytes=int(target["projected_archive_size_bytes"]),
        restore_bytes=int(target["identity"]["apparent_size_bytes"]),
    )
    source_cas_before_archive = _exact_source_cas(
        target, gate_name="mutation_pre_archive", full_hash=False
    )
    journal["items"][index - 1] = {
        **dict(item_state),
        "key": target["key"],
        "phase": "archive_prechecked",
        "activity_evidence": {
            **dict(item_state.get("activity_evidence") or {}),
            "pre_archive": source_cas_before_archive,
        },
        "updated_at": _now(),
    }
    journal["updated_at"] = _now()
    _atomic_write_json(journal_path, journal)
    non_target_before = _non_target_snapshot()
    if non_target_before["digest"] != journal["non_target_digest_before"]:
        raise WarmArchiveError("non-target evidence digest drifted before archive")
    if archive.exists() != manifest_path.exists():
        raise WarmArchiveError("published archive pair is incomplete")
    if not archive.exists():
        compressed = _compress(source, temp_archive)
        if compressed["archive_size_bytes"] != int(target["projected_archive_size_bytes"]):
            raise WarmArchiveError("archive size drifted from exact dry-run")
        streamed = _stream_decompressed_identity(temp_archive)
        if (
            streamed["decompressed_size_bytes"] != int(target["identity"]["apparent_size_bytes"])
            or streamed["decompressed_sha256"] != target["identity"]["sha256"]
        ):
            raise WarmArchiveError("archive temp decompressed identity mismatch")
        restore_proof = _full_restore_proof(
            archive=temp_archive,
            expected_source=target["identity"],
            temporary=restore_temp,
        )
        source_cas_after_archive = _exact_source_cas(
            target, gate_name="mutation_post_archive_pre_publish", full_hash=False
        )
        journal["items"][index - 1] = {
            **dict(journal["items"][index - 1]),
            "phase": "archive_verified_pending_publish",
            "activity_evidence": {
                **dict(
                    journal["items"][index - 1].get("activity_evidence") or {}
                ),
                "post_archive_pre_publish": source_cas_after_archive,
            },
            "updated_at": _now(),
        }
        journal["updated_at"] = _now()
        _atomic_write_json(journal_path, journal)
        pending_manifest = {
            "contract_name": CONTRACT_NAME,
            "operation_id": operation_id,
            "approval_reference": approval_reference,
            "target_key": target["key"],
            "owner": target["owner"],
            "family": target["family"],
            "restore_role": target["restore_role"],
            "source": target["identity"],
            "source_sidecars": target["sidecars"],
            "source_sqlite": target["sqlite"],
            "source_provenance": target["provenance"],
            "hold_evidence": target["hold_evidence"],
            "activity_evidence": {
                "pre_archive": source_cas_before_archive,
                "post_archive_pre_publish": source_cas_after_archive,
            },
            "archive_path": str(archive),
            "archive_size_bytes": compressed["archive_size_bytes"],
            "archive_sha256": compressed["archive_sha256"],
            "compression": "zstd-level-1-single-thread",
            "stream_verification": streamed,
            "restore_proof": restore_proof,
            "lifecycle_state": "verified_pending_source_removal",
            "source_removed": False,
            "published_at": _now(),
        }
        _atomic_write_json(temp_manifest, pending_manifest)
        os.replace(temp_archive, archive)
        os.chmod(archive, 0o600)
        _fsync_directory(destination)
        os.replace(temp_manifest, manifest_path)
        os.chmod(manifest_path, 0o600)
        _fsync_directory(destination)
    published_proof = _verify_archive_pair(
        archive=archive,
        manifest_path=manifest_path,
        operation_id=operation_id,
        expected_target=target,
        full_restore=True,
        restore_temp=restore_temp,
    )
    source_cas = _exact_source_cas(
        target, gate_name="mutation_exact_pre_unlink", full_hash=True
    )
    non_target_pre_unlink = _non_target_snapshot()
    if non_target_pre_unlink["digest"] != journal["non_target_digest_before"]:
        raise WarmArchiveError("non-target evidence digest drifted before unlink")
    capacity_pre_unlink = _capacity_guard(runtime_dir=runtime_dir, archive_bytes=0, restore_bytes=0)
    journal_item = {
        "key": target["key"],
        "phase": "pending_unlink",
        "pending_unlink_written_at": _now(),
        "source_cas": source_cas,
        "activity_evidence": {
            **dict(
                journal["items"][index - 1].get("activity_evidence") or {}
            ),
            "exact_pre_unlink": source_cas,
        },
        "archive_proof": published_proof,
        "capacity_before": capacity_before,
        "capacity_pre_unlink": capacity_pre_unlink,
        "non_target_digest": non_target_pre_unlink["digest"],
    }
    journal["items"][index - 1] = journal_item
    journal["updated_at"] = _now()
    _atomic_write_json(journal_path, journal)
    source.unlink()
    _fsync_directory(source.parent)
    if source.exists():
        raise WarmArchiveError("source remains after the single unlink")
    capacity_after = _filesystem_snapshot(runtime_dir, Path("/opt/wb-core-runtime/backups"))
    non_target_after = _non_target_snapshot()
    if non_target_after["digest"] != journal["non_target_digest_before"]:
        raise WarmArchiveError("non-target evidence digest changed after unlink")
    final_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    final_manifest.update(
        {
            "lifecycle_state": "retained",
            "source_removed": True,
            "unlink_receipt": {
                "count": 1,
                "source_identity": target["identity"],
                "source_absent": True,
                "completed_at": _now(),
            },
            "published_pair_readback": published_proof,
            "activity_evidence": {
                **dict(final_manifest.get("activity_evidence") or {}),
                "exact_pre_unlink": source_cas,
            },
            "finalized_at": _now(),
        }
    )
    _atomic_write_json(manifest_path, final_manifest)
    final_proof = _verify_archive_pair(
        archive=archive,
        manifest_path=manifest_path,
        operation_id=operation_id,
        expected_target=target,
        full_restore=False,
        restore_temp=restore_temp,
    )
    return {
        "key": target["key"],
        "phase": "unlink_done",
        "unlink_count": 1,
        "source_absent": True,
        "archive_path": str(archive),
        "manifest_path": str(manifest_path),
        "archive_proof": final_proof,
        "reclaimed_allocated_bytes": int(target["identity"]["allocated_bytes"]),
        "capacity_after": capacity_after,
        "non_target_digest_after": non_target_after["digest"],
        "completed_at": _now(),
    }


def _monitor_after_batch(
    *, journal: dict[str, Any], journal_path: Path
) -> dict[str, Any]:
    state = dict(journal.get("monitor_operation") or {})
    execute = False
    if not state:
        state = {
            "phase": "pending_start",
            "submit_count": 1,
            "pending_at": _now(),
        }
        journal["monitor_operation"] = state
        journal["updated_at"] = _now()
        _atomic_write_json(journal_path, journal)
        execute = True
    elif state.get("phase") == "complete":
        readback = read_root_storage_status_artifact(policy=load_policy())
        if not readback.get("ok") or not readback.get("fresh"):
            raise WarmArchiveError("completed root monitor readback is unhealthy")
        return {**state, "readback": readback, "idempotent": True}
    elif state.get("phase") != "pending_start" or int(state.get("submit_count") or 0) != 1:
        raise WarmArchiveError("root monitor durable operation state is invalid")

    service_returncode: int | None = None
    if execute:
        completed = subprocess.run(
            ["systemctl", "start", "wb-core-root-storage-policy.service"],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        service_returncode = completed.returncode
        if completed.returncode != 0:
            raise WarmArchiveError(
                "root storage monitor oneshot failed: "
                + (
                    completed.stderr.strip()
                    or completed.stdout.strip()
                    or str(completed.returncode)
                )
            )
    readback = read_root_storage_status_artifact(policy=load_policy())
    collected_at = str((readback.get("status") or {}).get("collected_at") or "")
    try:
        collected = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        pending = datetime.fromisoformat(
            str(state["pending_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise WarmArchiveError("root monitor timestamp reconciliation failed") from exc
    if (
        not readback.get("ok")
        or not readback.get("fresh")
        or collected < pending
    ):
        raise WarmArchiveError("root monitor pending start is not reconciled")
    state = {
        **state,
        "phase": "complete",
        "service_returncode": service_returncode,
        "ambiguous_start_reconciled": not execute,
        "readback": readback,
        "completed_at": _now(),
    }
    journal["monitor_operation"] = state
    journal["updated_at"] = _now()
    _atomic_write_json(journal_path, journal)
    return state


def apply_batch(
    *,
    runtime_dir: Path,
    root_backups: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
    evidence_dir: Path,
    operation_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    approval_reference: str,
    own_job_id: str = "",
) -> dict[str, Any]:
    with _exclusive_finance_lock(runtime_dir):
        with _exclusive_other_lifecycle_locks(runtime_dir):
            return _apply_batch_locked(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                deployed_sha=deployed_sha,
                deployed_sha_file=deployed_sha_file,
                evidence_dir=evidence_dir,
                operation_id=operation_id,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                approval_reference=approval_reference,
                own_job_id=own_job_id,
            )


def _apply_batch_locked(
    *,
    runtime_dir: Path,
    root_backups: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
    evidence_dir: Path,
    operation_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    approval_reference: str,
    own_job_id: str = "",
) -> dict[str, Any]:
    if not approval_reference or len(approval_reference) > 500:
        raise WarmArchiveError("approval reference is invalid")
    _verify_deployed_sha(deployed_sha=deployed_sha, deployed_sha_file=deployed_sha_file)
    manifest = _load_manifest(
        evidence_dir=evidence_dir,
        operation_id=operation_id,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
    )
    if manifest.get("deployed_sha") != deployed_sha:
        raise WarmArchiveError("manifest deployed SHA drifted")
    projection_binding = manifest["readiness_projection"]
    projection = _load_readiness_projection(
        projection_path=Path(str(projection_binding["path"])),
        projection_sha256=str(projection_binding["sha256"]),
        deployed_sha=deployed_sha,
    )
    if (
        projection["readiness_id"] != projection_binding["readiness_id"]
        or projection["material_qualification_digest"]
        != manifest["material_qualification_digest"]
    ):
        raise WarmArchiveError("manifest readiness projection drifted before mutation")
    journal_path = _journal_path(evidence_dir)
    if journal_path.exists():
        journal = _read_journal(journal_path)
        if (
            journal.get("operation_id") != operation_id
            or journal.get("manifest_sha256") != manifest_sha256
            or journal.get("deployed_sha") != deployed_sha
        ):
            raise WarmArchiveError("durable apply journal belongs to another operation")
        if journal.get("approval_reference") != approval_reference:
            raise WarmArchiveError("durable apply journal approval reference drifted")
        if journal.get("status") == "complete":
            return {**journal, "idempotent": True, "applied": False}
        if journal.get("status") != "applying":
            raise WarmArchiveError("durable apply journal status is invalid")
        fresh_material = manifest["material"]
        expected_keys = [str(item["key"]) for item in fresh_material["targets"]]
        if [str(item.get("key") or "") for item in journal.get("items") or []] != expected_keys:
            raise WarmArchiveError("durable apply journal target sequence drifted")
        filesystems_now = _filesystem_snapshot(runtime_dir, root_backups)
        finance_now = _finance_snapshot(runtime_dir)
        if _active_sanitation_jobs(runtime_dir, own_job_id=own_job_id):
            raise WarmArchiveError("another sanitation operation is non-terminal")
        _store_registry(runtime_dir, fresh_material["targets"])
        non_target_now = _non_target_snapshot()
        root_policy_now = _root_policy_snapshot(
            fresh_material["targets"], require_targets=False
        )
        services_now = _systemd_snapshot()
        journald_now = _journald_snapshot()
        if (
            non_target_now["digest"] != journal["non_target_digest_before"]
            or root_policy_now["protected_path_identity_digest"]
            != journal["root_policy_protected_digest_before"]
            or not _services_healthy(services_now)
            or journald_now["service"].get("MainPID")
            != journal["journald_before"]["service"].get("MainPID")
            or journald_now["effective"]["values"]
            != journal["journald_before"]["effective"]["values"]
            or int(filesystems_now["backup"]["available_bytes"])
            < int(finance_now["required_available_floor_bytes"])
        ):
            raise WarmArchiveError("crash-resume environment reconciliation failed")
    else:
        fresh_material, fresh_observations = _material_snapshot(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            own_job_id=own_job_id,
            lifecycle_locks_held=True,
            reusable_material=manifest["material"],
            witness_name="mutation_start_lightweight_material_cas",
        )
        if _digest(fresh_material) != manifest["material_qualification_digest"]:
            raise WarmArchiveError("material CAS drifted after qualification")
        journal = {
            "contract_name": CONTRACT_NAME,
            "status": "applying",
            "operation_id": operation_id,
            "deployed_sha": deployed_sha,
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha256,
            "material_qualification_digest": manifest["material_qualification_digest"],
            "approval_reference": approval_reference,
            "expected_source_count": EXPECTED_SOURCE_COUNT,
            "expected_unlink_count": EXPECTED_SOURCE_COUNT,
            "expected_reclaimed_allocated_bytes": fresh_material["expected_reclaimed_allocated_bytes"],
            "filesystems_before": fresh_observations["filesystems_before"],
            "journald_before": fresh_observations["journald"],
            "services_before": fresh_observations["services"],
            "activity_evidence_before": fresh_observations["activity_gates"],
            "non_target_digest_before": fresh_material["non_target_digest"],
            "root_policy_protected_digest_before": fresh_material["root_policy"]["protected_path_identity_digest"],
            "items": [{"key": item["key"], "phase": "pending"} for item in fresh_material["targets"]],
            "mutation_submit_count": 1,
            "promo_action_count": 0,
            "business_data_mutation_count": 0,
            "started_at": _now(),
        }
        _atomic_write_json(journal_path, journal)
    destination = _ensure_destination_family()
    for target in fresh_material["targets"]:
        index = next(i for i, item in enumerate(journal["items"]) if item["key"] == target["key"])
        result = _process_target(
            runtime_dir=runtime_dir,
            target=target,
            item_state=journal["items"][index],
            journal=journal,
            journal_path=journal_path,
            operation_id=operation_id,
            approval_reference=approval_reference,
            destination=destination,
        )
        journal["items"][index] = result
        journal["updated_at"] = _now()
        _atomic_write_json(journal_path, journal)
    monitor = _monitor_after_batch(journal=journal, journal_path=journal_path)
    filesystems_after = _filesystem_snapshot(runtime_dir, root_backups)
    finance_after = _finance_snapshot(runtime_dir)
    non_target_after = _non_target_snapshot()
    root_policy_after = _root_policy_snapshot(
        fresh_material["targets"], require_targets=False
    )
    services_after = _systemd_snapshot()
    journald_after = _journald_snapshot()
    journal_reconciliation = _reconcile_correction_journal_inventory(
        journal["journald_before"]["inventory"], journald_after["inventory"]
    )
    if (
        non_target_after["digest"] != journal["non_target_digest_before"]
        or root_policy_after["protected_path_identity_digest"]
        != journal["root_policy_protected_digest_before"]
        or not _services_healthy(services_after)
        or journald_after["service"].get("MainPID")
        != journal["journald_before"]["service"].get("MainPID")
        or journald_after["effective"]["values"]
        != journal["journald_before"]["effective"]["values"]
        or journal_reconciliation["deleted_count"] != 0
        or journal_reconciliation["protected_drift"]
        or int(filesystems_after["root"]["available_bytes"]) < ROOT_MINIMUM_AFTER_BYTES
        or int(filesystems_after["backup"]["available_bytes"])
        < int(finance_after["required_available_floor_bytes"])
    ):
        raise WarmArchiveError("terminal non-target/capacity/service reconciliation failed")
    unlink_count = sum(int(item.get("unlink_count") or 0) for item in journal["items"])
    reclaimed = sum(int(item.get("reclaimed_allocated_bytes") or 0) for item in journal["items"])
    if unlink_count != EXPECTED_SOURCE_COUNT or reclaimed != int(journal["expected_reclaimed_allocated_bytes"]):
        raise WarmArchiveError("terminal unlink/reclaimed-byte reconciliation failed")
    completed = {
        **journal,
        "status": "complete",
        "applied": True,
        "idempotent": False,
        "raw_unlink_count": unlink_count,
        "reclaimed_allocated_bytes": reclaimed,
        "root_available_delta_bytes": int(filesystems_after["root"]["available_bytes"])
        - int(journal["filesystems_before"]["root"]["available_bytes"]),
        "root_available_delta_variance_bytes": (
            int(filesystems_after["root"]["available_bytes"])
            - int(journal["filesystems_before"]["root"]["available_bytes"])
            - reclaimed
        ),
        "filesystems_after": filesystems_after,
        "finance_after": finance_after,
        "monitor": monitor,
        "services_after": services_after,
        "journald_after": journald_after,
        "journald_reconciliation": journal_reconciliation,
        "non_target_digest_after": non_target_after["digest"],
        "root_policy_protected_digest_after": root_policy_after["protected_path_identity_digest"],
        "completed_at": _now(),
    }
    _atomic_write_json(journal_path, completed)
    return completed


def _wait_for_job(runtime_dir: Path, job_id: str, deployed_sha: str, wait_seconds: int) -> dict[str, Any] | None:
    if not job_id:
        return None
    if not JOB_ID_RE.fullmatch(job_id):
        raise WarmArchiveError("readback job id is invalid")
    from apps.storage_recovery_sanitation_job import job_status

    deadline = time.monotonic() + max(0, int(wait_seconds))
    while True:
        status_payload = job_status(
            runtime_dir=runtime_dir,
            job_id=job_id,
            deployed_sha=deployed_sha,
            include_systemd=True,
        )
        if status_payload.get("terminal") is True or time.monotonic() >= deadline:
            return status_payload
        time.sleep(5)


def readback_batch(
    *,
    runtime_dir: Path,
    root_backups: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
    evidence_dir: Path,
    operation_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    job_id: str = "",
    wait_seconds: int = 0,
) -> dict[str, Any]:
    _verify_deployed_sha(deployed_sha=deployed_sha, deployed_sha_file=deployed_sha_file)
    manifest = _load_manifest(
        evidence_dir=evidence_dir,
        operation_id=operation_id,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
    )
    job = _wait_for_job(runtime_dir, job_id, deployed_sha, wait_seconds)
    journal_path = _journal_path(evidence_dir)
    journal = _read_journal(journal_path) if journal_path.exists() else None
    destination = DESTINATION_ROOT / DESTINATION_FAMILY_NAME
    archives = []
    source_absent_count = 0
    source_present = []
    for index, target in enumerate(manifest["material"]["targets"], 1):
        source = Path(str(target["source_path"]))
        archive = destination / str(target["archive_name"])
        pair_manifest = archive.with_name(archive.name + ".manifest.json")
        if source.exists():
            source_present.append(_file_identity(source))
            continue
        source_absent_count += 1
        restore_temp = destination / f".wbc0008-006-{operation_id}-readback-{index:02d}.restore.tmp.sqlite3"
        proof = _verify_archive_pair(
            archive=archive,
            manifest_path=pair_manifest,
            operation_id=operation_id,
            expected_target=target,
            full_restore=True,
            restore_temp=restore_temp,
        )
        pair_payload = json.loads(pair_manifest.read_text(encoding="utf-8"))
        archives.append(
            {
                "key": target["key"],
                "archive_path": str(archive),
                "manifest_path": str(pair_manifest),
                "lifecycle_state": pair_payload.get("lifecycle_state"),
                "source_removed": pair_payload.get("source_removed"),
                "proof": proof,
            }
        )
    filesystems = _filesystem_snapshot(runtime_dir, root_backups)
    finance = _finance_snapshot(runtime_dir)
    non_target = _non_target_snapshot()
    services = _systemd_snapshot()
    journald = _journald_snapshot()
    root_readback = read_root_storage_status_artifact(policy=load_policy())
    journald_reconciliation = (
        _reconcile_correction_journal_inventory(journal["journald_before"]["inventory"], journald["inventory"])
        if journal
        else {"deleted_count": -1, "protected_drift": ["missing_journal"]}
    )
    raw_unlink_count = sum(int(item.get("unlink_count") or 0) for item in (journal or {}).get("items", []))
    reclaimed = sum(int(item.get("reclaimed_allocated_bytes") or 0) for item in (journal or {}).get("items", []))
    non_target_preserved = bool(
        journal
        and non_target["digest"] == journal.get("non_target_digest_before")
        and journald_reconciliation.get("deleted_count") == 0
        and not journald_reconciliation.get("protected_drift")
    )
    reconciled = bool(
        job
        and job.get("status") == "succeeded"
        and job.get("terminal") is True
        and journal
        and journal.get("status") == "complete"
        and source_absent_count == EXPECTED_SOURCE_COUNT
        and len(archives) == EXPECTED_SOURCE_COUNT
        and all(item["lifecycle_state"] == "retained" and item["source_removed"] is True for item in archives)
        and raw_unlink_count == EXPECTED_SOURCE_COUNT
        and reclaimed == int(manifest["material"]["expected_reclaimed_allocated_bytes"])
        and non_target_preserved
        and int(filesystems["root"]["available_bytes"]) >= ROOT_MINIMUM_AFTER_BYTES
        and int(filesystems["backup"]["available_bytes"]) >= int(finance["required_available_floor_bytes"])
        and _services_healthy(services)
        and root_readback.get("ok") is True
        and root_readback.get("fresh") is True
        and root_readback.get("status", {}).get("status") == "normal"
        and journald["service"].get("MainPID") == journal["journald_before"]["service"].get("MainPID")
        and journald["effective"]["values"] == journal["journald_before"]["effective"]["values"]
        and int(journal.get("promo_action_count", -1)) == 0
        and int(journal.get("business_data_mutation_count", -1)) == 0
        and int(journal.get("mutation_submit_count") or 0) == 1
    )
    return {
        "contract_name": CONTRACT_NAME,
        "status": "reconciled" if reconciled else "blocked",
        "query_only": True,
        "temporary_restore_verification_only": True,
        "deployed_sha": deployed_sha,
        "operation_id": operation_id,
        "manifest_sha256": manifest_sha256,
        "source_count": EXPECTED_SOURCE_COUNT,
        "source_absent_count": source_absent_count,
        "source_present": source_present,
        "archive_count": len(archives),
        "manifest_count": len(archives),
        "archives": archives,
        "raw_unlink_count": raw_unlink_count,
        "expected_reclaimed_allocated_bytes": manifest["material"]["expected_reclaimed_allocated_bytes"],
        "reclaimed_allocated_bytes": reclaimed,
        "root_available_delta_bytes": (
            int(filesystems["root"]["available_bytes"])
            - int((journal or {}).get("filesystems_before", {}).get("root", {}).get("available_bytes") or 0)
            if journal
            else None
        ),
        "filesystems": filesystems,
        "finance": finance,
        "backup_capacity_guard_passed": int(filesystems["backup"]["available_bytes"]) >= int(finance["required_available_floor_bytes"]),
        "root_minimum_passed": int(filesystems["root"]["available_bytes"]) >= ROOT_MINIMUM_AFTER_BYTES,
        "root_monitor": root_readback,
        "services": services,
        "services_healthy": _services_healthy(services),
        "journald": journald,
        "journald_reconciliation": journald_reconciliation,
        "non_target_digest": non_target["digest"],
        "non_target_preserved": non_target_preserved,
        "promo_action_count": int((journal or {}).get("promo_action_count") or 0),
        "business_data_mutation_count": int((journal or {}).get("business_data_mutation_count") or 0),
        "exact_manifest_apply_receipt_count": 1 if journal and journal.get("status") == "complete" else 0,
        "job": job,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    runtime_dir = Path(args.runtime_dir)
    root_backups = Path(args.root_backups)
    evidence_dir = Path(args.evidence_dir)
    deployed_sha_file = Path(args.deployed_sha_file)
    if args.command == "readiness":
        return readiness(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha=args.deployed_sha,
            deployed_sha_file=deployed_sha_file,
            evidence_dir=evidence_dir,
            readiness_id=args.readiness_id,
        )
    if args.command == "dry-run":
        return dry_run(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha=args.deployed_sha,
            deployed_sha_file=deployed_sha_file,
            evidence_dir=evidence_dir,
            operation_id=args.operation_id,
            projection_manifest=Path(args.projection_manifest),
            projection_manifest_sha256=args.projection_manifest_sha256,
        )
    if args.command == "apply":
        return apply_batch(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha=args.deployed_sha,
            deployed_sha_file=deployed_sha_file,
            evidence_dir=evidence_dir,
            operation_id=args.operation_id,
            manifest_path=Path(args.manifest),
            manifest_sha256=args.manifest_sha256,
            approval_reference=args.approval_reference,
        )
    return readback_batch(
        runtime_dir=runtime_dir,
        root_backups=root_backups,
        deployed_sha=args.deployed_sha,
        deployed_sha_file=deployed_sha_file,
        evidence_dir=evidence_dir,
        operation_id=args.operation_id,
        manifest_path=Path(args.manifest),
        manifest_sha256=args.manifest_sha256,
        job_id=args.job_id,
        wait_seconds=args.wait_seconds,
    )


def main() -> int:
    try:
        result = run(build_parser().parse_args())
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, WarmArchiveError) and exc.evidence:
            error["evidence"] = exc.evidence
        print(
            json.dumps(
                {
                    "contract_name": CONTRACT_NAME,
                    "status": "failed",
                    "error": error,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
