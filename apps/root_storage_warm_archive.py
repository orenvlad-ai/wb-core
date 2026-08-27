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
from typing import Any, Callable, Mapping
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
    CLASS_ESSENTIAL,
    MUTABLE_STORE_ACCESS_ROLES,
    collect_root_storage_status,
    load_policy,
    read_root_storage_status_artifact,
    registered_producer_for_path,
)
from packages.application.storage_registry import (  # noqa: E402
    StoreRegistry,
    manifest_payload,
)


CONTRACT_NAME = "root_storage_warm_archive_wbc0008_006_v6"
SEMANTIC_FILESYSTEM_IDENTITY_CONTRACT = (
    "wb_core_semantic_filesystem_identity_v1"
)
MATERIAL_CAS_DIFF_SCHEMA = "wb-core.root-warm-archive-material-cas-diff/v1"
MATERIAL_CAS_FAILURE_SCHEMA = "wb-core.root-warm-archive-material-cas-failure/v1"
MATERIAL_CAS_FAILURE_FILENAME = "root-warm-archive-material-cas-failure.json"
PROFILE = "root-warm-archive-six"
EXPECTED_SOURCE_COUNT = 6
DESTINATION_FAMILY_NAME = "root-warm-archive-wbc0008-006"
DESTINATION_ROOT = Path("/opt/wb-core-runtime/state/backups")
GENERATION_ROOT = Path("/opt/wb-core-runtime/state/generations")
READINESS_EVIDENCE_ROOT = Path(
    "/opt/wb-core-runtime/state/private-evidence/root-warm-archive-readiness"
)
PRODUCTION_GOAL_EVIDENCE_ROOT = Path(
    "/opt/wb-core-runtime/state/private-evidence/production-goals"
)
ROOT_MINIMUM_AFTER_BYTES = 25 * 1024**3
EMERGENCY_RESERVE_BYTES = 8 * 1024**3
CONTROL_ARTIFACT_RESERVE_BYTES = 64 * 1024**2
MANIFEST_RESERVE_BYTES_PER_SOURCE = 1024**2
CHUNK_SIZE = 8 * 1024**2
READINESS_REQUIRED_CONSECUTIVE_CLEAN = 3
READINESS_MAX_STABILIZATION_SECONDS = 60
READINESS_SAMPLE_INTERVAL_SECONDS = 2.0
SYSTEMD_PAIR_RESAMPLE_MAX_ATTEMPTS = 3
SYSTEMD_PAIR_RESAMPLE_MAX_SECONDS = 5.0
SYSTEMD_PAIR_RESAMPLE_INTERVAL_SECONDS = 0.25
JOB_ID_RE = re.compile(r"[0-9a-f]{64}")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
READINESS_ID_RE = re.compile(r"readiness-v2-[0-9a-f]{32}-a[0-9]{2}")
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
TIMER_SERVICE_PAIRS = tuple(
    (name, name.removesuffix(".timer") + ".service")
    for name in SERVICE_NAMES
    if name.endswith(".timer")
)
SYSTEMD_REQUIRED_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "MainPID",
    "ExecMainStatus",
    "UnitFileState",
)
SYSTEMD_TIMER_PROPERTIES = (
    "LastTriggerUSec",
    "NextElapseUSecRealtime",
)
OTHER_LIFECYCLE_LOCKS = (
    ".finance-storage-split.lock",
    ".finance-storage-stale-writer-recovery.lock",
    ".business-data-maintenance-restore.lock",
)
FILESYSTEM_ROLE_POLICIES: dict[str, dict[str, Any]] = {
    "root": {
        "device": 2049,
        "device_major": 8,
        "device_minor": 1,
        "source": "/dev/sda1",
        "filesystem_uuid": "d77f6a25-e90f-4292-a85d-9bcc1cecf9e2",
        "filesystem_type": "ext4",
        "family_root": "/opt/wb-core-runtime",
        "policy_owner": "root_storage_policy.filesystems.root",
        "required_mount_options": ["rw"],
    },
    "backup": {
        "device": 2065,
        "device_major": 8,
        "device_minor": 17,
        "source": "/dev/sdb1",
        "filesystem_uuid": "bd3d563f-e5ea-4e4a-a76a-be45e7f94ec0",
        "filesystem_type": "ext4",
        "family_root": "/opt/wb-core-runtime/state/backups",
        "policy_owner": "root_storage_policy.filesystems.backup",
        "required_mount_options": ["rw"],
    },
    "generation": {
        "device": 2081,
        "device_major": 8,
        "device_minor": 33,
        "source": "/dev/sdc1",
        "filesystem_uuid": "284b3362-b890-431d-a7da-7f0fcd2ee0a6",
        "filesystem_type": "ext4",
        "family_root": "/opt/wb-core-runtime/state/generations",
        "policy_owner": "root_storage_policy.filesystems.generation",
        "required_mount_options": ["rw", "noatime", "nodev", "noexec", "nosuid"],
    },
}
MOUNT_NAMESPACE_RESTRICTIVE_OPTIONS = frozenset({"nosuid", "nodev", "noexec"})
MOUNT_OBSERVATION_ONLY_OPTIONS = frozenset(
    {"relatime", "noatime", "strictatime", "nodiratime", "lazytime"}
)
MOUNT_STABLE_INTEGRITY_OPTIONS = frozenset(
    {
        "acl",
        "async",
        "barrier",
        "dax",
        "delalloc",
        "dev",
        "dirsync",
        "discard",
        "exec",
        "grpquota",
        "journal_async_commit",
        "journal_checksum",
        "noacl",
        "nobarrier",
        "nodelalloc",
        "nodiscard",
        "noquota",
        "prjquota",
        "quota",
        "suid",
        "sync",
        "user_xattr",
        "usrquota",
    }
)
MOUNT_STABLE_INTEGRITY_PREFIXES = (
    "barrier=",
    "commit=",
    "data=",
    "dax=",
    "errors=",
    "grpjquota=",
    "jqfmt=",
    "usrjquota=",
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
        "target_key": str(policy["key"]),
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


def _unescape_mountinfo(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _parse_mountinfo_line(line: str) -> dict[str, Any]:
    fields = line.split()
    try:
        separator = fields.index("-")
    except ValueError as exc:
        raise WarmArchiveError("mount identity record is malformed") from exc
    if separator < 6 or len(fields) <= separator + 3:
        raise WarmArchiveError("mount identity record is incomplete")
    try:
        mount_id = int(fields[0])
        parent_mount_id = int(fields[1])
        device_major_text, device_minor_text = fields[2].split(":", 1)
        device_major = int(device_major_text)
        device_minor = int(device_minor_text)
    except (ValueError, IndexError) as exc:
        raise WarmArchiveError("mount backing-device identity is malformed") from exc
    mount_options = sorted(
        {item.strip().lower() for item in fields[5].split(",") if item.strip()}
    )
    super_options = sorted(
        {
            item.strip().lower()
            for item in fields[separator + 3].split(",")
            if item.strip()
        }
    )
    return {
        "mount_id": mount_id,
        "parent_mount_id": parent_mount_id,
        "device_major": device_major,
        "device_minor": device_minor,
        "major_minor": f"{device_major}:{device_minor}",
        "mount_root": _unescape_mountinfo(fields[3]),
        "mount_point": _unescape_mountinfo(fields[4]),
        "mount_options": mount_options,
        "optional_fields": sorted(fields[6:separator]),
        "filesystem_type": fields[separator + 1].strip().lower(),
        "source": _unescape_mountinfo(fields[separator + 2]),
        "super_options": super_options,
    }


def _filesystem_uuid(source: str) -> str:
    if not source.startswith("/dev/"):
        return ""
    try:
        completed = subprocess.run(
            ["blkid", "-s", "UUID", "-o", "value", source],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return ""
    return completed.stdout.strip().lower() if completed.returncode == 0 else ""


def _source_device(source: str) -> int | None:
    try:
        value = Path(source).stat()
    except OSError:
        return None
    return int(value.st_rdev) if stat.S_ISBLK(value.st_mode) else None


def _mount_option_semantics(mount: Mapping[str, Any]) -> dict[str, Any]:
    mount_options = mount.get("mount_options")
    super_options = mount.get("super_options")
    if not isinstance(mount_options, list) or not isinstance(super_options, list):
        raise WarmArchiveError("mount option identity is missing or ambiguous")
    options = {
        str(item).strip().lower()
        for item in [*mount_options, *super_options]
        if str(item).strip()
    }
    if "rw" not in options or "ro" in options:
        raise WarmArchiveError("filesystem is not unambiguously writable")
    namespace_restrictive = sorted(options & MOUNT_NAMESPACE_RESTRICTIVE_OPTIONS)
    observation_only = sorted(options & MOUNT_OBSERVATION_ONLY_OPTIONS)
    stable_integrity = sorted(
        item
        for item in options
        if item in MOUNT_STABLE_INTEGRITY_OPTIONS
        or any(item.startswith(prefix) for prefix in MOUNT_STABLE_INTEGRITY_PREFIXES)
    )
    recognized = {
        "rw",
        *namespace_restrictive,
        *observation_only,
        *stable_integrity,
    }
    unknown = sorted(options - recognized)
    if unknown:
        raise WarmArchiveError(
            "mount option semantics are unknown",
            evidence={"unknown_mount_options": unknown},
        )
    return {
        "writable": True,
        "required_access_option": "rw",
        "namespace_restrictive_options": namespace_restrictive,
        "observation_only_options": observation_only,
        "stable_integrity_options": stable_integrity,
    }


def _path_matches_filesystem_role(path: Path, role: str) -> bool:
    resolved = path.resolve()
    policy = FILESYSTEM_ROLE_POLICIES[role]
    family_root = Path(str(policy["family_root"])).resolve()
    try:
        resolved.relative_to(family_root)
    except ValueError:
        return False
    if role != "root":
        return True
    for excluded in (
        Path("/opt/wb-core-runtime/state/backups"),
        Path("/opt/wb-core-runtime/state/generations"),
    ):
        try:
            resolved.relative_to(excluded)
        except ValueError:
            continue
        return False
    return True


def _semantic_mount_identity(
    path: Path,
    raw_mount: Mapping[str, Any],
    *,
    filesystem_role: str,
    policy_owner: str,
    path_device: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = FILESYSTEM_ROLE_POLICIES.get(filesystem_role)
    if policy is None or not policy_owner:
        raise WarmArchiveError("filesystem role/policy ownership is unknown")
    required_raw = {
        "device_major",
        "device_minor",
        "major_minor",
        "mount_point",
        "mount_options",
        "filesystem_type",
        "source",
        "super_options",
        "filesystem_uuid",
        "source_device",
    }
    if not required_raw.issubset(raw_mount):
        raise WarmArchiveError("semantic mount identity is missing or ambiguous")
    option_semantics = _mount_option_semantics(raw_mount)
    observed_options = {
        str(item).strip().lower()
        for item in [
            *list(raw_mount["mount_options"]),
            *list(raw_mount["super_options"]),
        ]
        if str(item).strip()
    }
    required_options = sorted(
        {str(item) for item in policy.get("required_mount_options") or []}
    )
    missing_required_options = sorted(set(required_options) - observed_options)
    if missing_required_options:
        raise WarmArchiveError(
            "declared filesystem mount safety option drifted",
            evidence={"missing_required_mount_options": missing_required_options},
        )
    expected_device = int(policy["device"])
    expected_major = int(policy["device_major"])
    expected_minor = int(policy["device_minor"])
    try:
        observed_path_device = int(path_device)
        observed_major = int(raw_mount["device_major"])
        observed_minor = int(raw_mount["device_minor"])
        observed_source_device = int(raw_mount["source_device"])
    except (TypeError, ValueError) as exc:
        raise WarmArchiveError(
            "filesystem backing-device identity is missing or ambiguous"
        ) from exc
    if (
        observed_path_device != expected_device
        or observed_major != expected_major
        or observed_minor != expected_minor
        or str(raw_mount["major_minor"]) != f"{expected_major}:{expected_minor}"
        or observed_source_device != expected_device
    ):
        raise WarmArchiveError("filesystem backing-device identity drifted")
    if (
        str(raw_mount["source"]) != str(policy["source"])
        or str(raw_mount["filesystem_uuid"]).lower()
        != str(policy["filesystem_uuid"])
        or str(raw_mount["filesystem_type"]).lower()
        != str(policy["filesystem_type"])
    ):
        raise WarmArchiveError("filesystem source/UUID/type identity drifted")
    if not _path_matches_filesystem_role(path, filesystem_role):
        raise WarmArchiveError("filesystem path-to-device role binding drifted")
    semantic = {
        "contract_version": SEMANTIC_FILESYSTEM_IDENTITY_CONTRACT,
        "filesystem_role": filesystem_role,
        "policy_owner": policy_owner,
        "path_binding": {
            "path": str(path.resolve()),
            "family_root": str(policy["family_root"]),
            "placement": "inside_declared_family",
        },
        "backing_device": {
            "device": expected_device,
            "device_major": expected_major,
            "device_minor": expected_minor,
            "major_minor": f"{expected_major}:{expected_minor}",
            "source": str(policy["source"]),
            "filesystem_uuid": str(policy["filesystem_uuid"]),
        },
        "filesystem_type": str(policy["filesystem_type"]),
        "required_writable": True,
        "declared_required_mount_options": required_options,
        "stable_integrity_options": option_semantics[
            "stable_integrity_options"
        ],
    }
    observation = {
        "raw_mount": copy.deepcopy(dict(raw_mount)),
        "option_semantics": option_semantics,
        "semantic_identity_digest": _digest(semantic),
    }
    return semantic, observation


def _filesystem(path: Path, *, filesystem_role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise WarmArchiveError(f"filesystem path is unavailable: {path}")
    value = path.stat()
    fs = os.statvfs(path)
    raw_mount = _mount_identity(path)
    policy_owner = str(FILESYSTEM_ROLE_POLICIES[filesystem_role]["policy_owner"])
    mount, mount_observation = _semantic_mount_identity(
        path,
        raw_mount,
        filesystem_role=filesystem_role,
        policy_owner=policy_owner,
        path_device=int(value.st_dev),
    )
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
        "mount_observation": mount_observation,
    }


def _select_mount_identity(
    path: Path, records: list[Mapping[str, Any]]
) -> dict[str, Any]:
    matches: list[tuple[int, dict[str, Any]]] = []
    for source_record in records:
        record = dict(source_record)
        mount_point = Path(str(record["mount_point"]))
        try:
            path.resolve().relative_to(mount_point)
        except ValueError:
            continue
        matches.append(
            (
                len(mount_point.parts),
                record,
            )
        )
    if not matches:
        raise WarmArchiveError(f"mount identity is unavailable: {path}")
    deepest = max(item[0] for item in matches)
    selected = [record for depth, record in matches if depth == deepest]
    if len(selected) != 1:
        raise WarmArchiveError(f"mount identity is ambiguous: {path}")
    return selected[0]


def _mount_identity(path: Path) -> dict[str, Any]:
    records = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        try:
            records.append(_parse_mountinfo_line(line))
        except WarmArchiveError:
            continue
    result = _select_mount_identity(path, records)
    result["filesystem_uuid"] = _filesystem_uuid(str(result["source"]))
    result["source_device"] = _source_device(str(result["source"]))
    return result


def _filesystem_snapshot(runtime_dir: Path, root_backups: Path) -> dict[str, Any]:
    result = {
        "root": _filesystem(root_backups, filesystem_role="root"),
        "backup": _filesystem(DESTINATION_ROOT, filesystem_role="backup"),
        "generation": _filesystem(GENERATION_ROOT, filesystem_role="generation"),
    }
    for name in ("root", "backup", "generation"):
        if result[name]["mount"]["filesystem_role"] != name:
            raise WarmArchiveError(f"{name} filesystem role drifted")
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
    active = {str(raw.resolve()), str(operational.resolve())}
    if any(str(item["source_path"]) in active for item in targets):
        raise WarmArchiveError("a target is an active/canonical StoreRegistry database")
    manifest_path = runtime_dir / "storage_generation_manifest.json"
    stores = {}
    for logical_store, path in (("finance_raw", raw), ("operational", operational)):
        generation = registry.generation(logical_store, manifest=manifest)
        stores[logical_store] = {
            "logical_store": logical_store,
            "path": str(path.resolve()),
            "generation_id": generation.generation_id,
            "generation_epoch": generation.generation_epoch,
            "relative_path": generation.relative_path,
            "schema_revision": generation.schema_revision,
            "source_fingerprint": manifest.source_fingerprint,
            "manifest_sha256": manifest.manifest_sha256,
        }
    return {
        "manifest": manifest_payload(manifest),
        "manifest_file_sha256": _sha256_file(manifest_path) if manifest_path.is_file() else None,
        "active_paths": sorted(active),
        "stores": stores,
        "identity_digest": _digest(stores),
    }


def _root_policy_snapshot(
    targets: list[Mapping[str, Any]],
    *,
    mutable_paths: set[str] | None = None,
    require_targets: bool = True,
    expected_protected_topology: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    policy = load_policy()
    status_payload = collect_root_storage_status(policy=policy)
    if status_payload.get("unregistered_large_root_files"):
        raise WarmArchiveError("root storage status has an unregistered producer")
    by_path = {str(item["path"]): item for item in status_payload["large_root_files"]}
    for expected in expected_protected_topology or []:
        expected_path = str(expected.get("path") or "")
        if not expected_path or expected_path in by_path:
            continue
        path = Path(expected_path)
        if path.is_symlink() or not path.is_file():
            raise WarmArchiveError(
                "protected non-target path topology drifted",
                evidence={"path": expected_path, "key": expected_path},
            )
        value = path.lstat()
        producer = registered_producer_for_path(policy, path)
        if producer is None:
            raise WarmArchiveError(
                "protected non-target owner/classification drifted",
                evidence={"path": expected_path, "key": expected_path},
            )
        by_path[expected_path] = {
            "path": expected_path,
            "device": int(value.st_dev),
            "inode": int(value.st_ino),
            "size_bytes": int(value.st_size),
            "mtime_ns": int(value.st_mtime_ns),
            "registered": producer is not None,
            "owner": None if producer is None else producer["owner"],
            "classification": (
                None if producer is None else producer["classification"]
            ),
        }
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
    mutable_paths = set(mutable_paths or set())
    protected_topology = []
    protected_observations = []
    for row in sorted(by_path.values(), key=lambda item: str(item["path"])):
        if str(row["path"]) in target_paths:
            continue
        if str(row["path"]) in mutable_paths:
            continue
        topology = {
            "path": str(row["path"]),
            "device": int(row["device"]),
            "inode": int(row["inode"]),
            "owner": str(row["owner"]),
            "classification": str(row["classification"]),
            "registered": bool(row["registered"]),
        }
        protected_topology.append(topology)
        protected_observations.append(
            {
                **topology,
                "ordinary_mutable_fields": {
                    "size_bytes": int(row["size_bytes"]),
                    "mtime_ns": int(row["mtime_ns"]),
                },
            }
        )
    return {
        "policy_sha256": status_payload["policy_sha256"],
        "target_rows": target_rows,
        "protected_path_topology": protected_topology,
        "protected_path_topology_digest": _digest(protected_topology),
        "protected_path_observations": protected_observations,
        "status": status_payload["status"],
        "available_bytes": int(status_payload["filesystems"]["root"]["available_bytes"]),
    }


def _immutable_non_target_snapshot(*, operation_id: str = "") -> dict[str, Any]:
    excluded = set()
    roots = set()
    for policy in TARGET_POLICIES:
        source = Path(str(policy["source_path"]))
        excluded.update({str(source), str(source) + "-wal", str(source) + "-shm", str(source) + "-journal"})
        roots.add(Path(str(policy["hold_root"])))
    topology_rows = []
    observation_rows = []
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
            if row["kind"] == "symlink":
                row["symlink_target"] = os.readlink(path)[:500]
            observation = dict(row)
            if row["kind"] == "file":
                observation.update(
                    {
                        "size_bytes": int(value.st_size),
                        "allocated_bytes": int(value.st_blocks * 512),
                        "mtime_ns": int(value.st_mtime_ns),
                        "ctime_ns": int(value.st_ctime_ns),
                        "sha256": _sha256_file(path),
                    }
                )
            topology_rows.append(row)
            observation_rows.append(observation)
    destination = DESTINATION_ROOT / DESTINATION_FAMILY_NAME
    destination_rows = []
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise WarmArchiveError("destination family is unsafe")
        destination_stat = destination.lstat()
        if (
            stat.S_IMODE(destination_stat.st_mode) != 0o700
            or int(destination_stat.st_uid) != 0
            or int(destination_stat.st_gid) != 0
        ):
            raise WarmArchiveError("destination family ownership/mode is unsafe")
        exact_output_names = {
            name
            for item in TARGET_POLICIES
            for name in (
                str(item["archive_name"]),
                str(item["archive_name"]) + ".manifest.json",
            )
        }
        foreign = []
        for path in sorted(destination.iterdir(), key=str):
            owned_temp = bool(
                operation_id
                and re.fullmatch(
                    rf"\.wbc0008-006-{re.escape(operation_id)}-[0-9]{{2}}\."
                    r"(?:archive\.tmp|manifest\.tmp|restore\.tmp\.sqlite3)",
                    path.name,
                )
            )
            if path.name in exact_output_names or owned_temp:
                continue
            foreign.append(str(path))
        if foreign:
            raise WarmArchiveError(
                "destination family contains an unknown/unregistered non-target artifact",
                evidence={"unknown_destination_paths": foreign},
            )
    material = {
        "exact_family_topology_rows": topology_rows,
        "destination_immutable_non_target_topology_rows": destination_rows,
    }
    return {
        **material,
        "exact_family_topology_digest": _digest(topology_rows),
        "exact_family_observation_rows": observation_rows,
        "exact_family_observation_digest": _digest(observation_rows),
        "destination_immutable_non_target_topology_digest": _digest(
            destination_rows
        ),
        "immutable_digest": _digest(material),
    }


def _stable_file_topology(
    path: Path,
    *,
    filesystem_role: str,
    policy_owner: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise WarmArchiveError(f"mutable canonical store path is unsafe: {path}")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise WarmArchiveError(f"mutable canonical store path resolution drifted: {path}")
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise WarmArchiveError(f"mutable canonical store type drifted: {path}")
    raw_mount = _mount_identity(path)
    if int(value.st_dev) != int(path.stat().st_dev):
        raise WarmArchiveError(f"mutable canonical store device drifted: {path}")
    mount, mount_observation = _semantic_mount_identity(
        path,
        raw_mount,
        filesystem_role=filesystem_role,
        policy_owner=policy_owner,
        path_device=int(value.st_dev),
    )
    topology = {
        "path": str(path),
        "device": int(value.st_dev),
        "device_major": int(os.major(value.st_dev)),
        "device_minor": int(os.minor(value.st_dev)),
        "inode": int(value.st_ino),
        "kind": "file",
        "mode": oct(stat.S_IMODE(value.st_mode)),
        "uid": int(value.st_uid),
        "gid": int(value.st_gid),
        "nlink": int(value.st_nlink),
        "mount": mount,
    }
    return topology, mount_observation


def _mutable_opener_access_relationship(
    opener: Mapping[str, Any],
    *,
    path: Path,
    topology: Mapping[str, Any],
    access_roles: tuple[Mapping[str, Any], ...],
    service_snapshot: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind one inode-proven FD to one exact healthy declared systemd MainPID."""

    relationship = {
        **dict(opener),
        "canonical_store_binding": {
            "path": str(path),
            "device": int(topology["device"]),
            "inode": int(topology["inode"]),
        },
        "matched_units": [],
        "matched_unit": None,
        "service_main_pid": None,
        "service_health": None,
        "declared_role": None,
        "allowed_access_modes": [],
        "accepted": False,
        "accepted_reason": None,
        "rejected_reason": None,
    }
    path_bound = bool(
        opener.get("source_path") == str(path)
        and opener.get("binds_source_device_inode") is True
        and _systemd_int(opener.get("target_device")) == int(topology["device"])
        and _systemd_int(opener.get("target_inode")) == int(topology["inode"])
    )
    if not path_bound:
        relationship["rejected_reason"] = "fd_device_inode_binding_mismatch"
        return relationship

    mode = str(opener.get("access_mode") or "unknown")
    if mode not in {"read_only", "read_write", "write_only"}:
        relationship["rejected_reason"] = "unknown_access_mode"
        return relationship
    pid = _systemd_int(opener.get("pid"))
    if pid is None or pid <= 0:
        relationship["rejected_reason"] = "invalid_opener_pid"
        return relationship
    matched_units = sorted(
        name
        for name in SERVICE_NAMES
        if _systemd_int((service_snapshot.get(name) or {}).get("MainPID")) == pid
    )
    relationship["matched_units"] = matched_units
    if not matched_units:
        relationship["rejected_reason"] = "undeclared_or_non_main_pid"
        return relationship
    if len(matched_units) != 1:
        relationship["rejected_reason"] = "multiple_unit_mainpid_ambiguity"
        return relationship
    matched_unit = matched_units[0]
    service_gate = _systemd_service_gate(service_snapshot)
    unit_row = next(
        row for row in service_gate["units"] if row["name"] == matched_unit
    )
    relationship["matched_unit"] = matched_unit
    relationship["service_main_pid"] = _systemd_int(unit_row.get("MainPID"))
    relationship["service_health"] = {
        "healthy": bool(unit_row.get("healthy")),
        "classification": unit_row.get("state_classification"),
        "phase": unit_row.get("phase"),
        "pair_classification": unit_row.get("pair_classification"),
        "pair_healthy": unit_row.get("pair_healthy"),
        "reason_codes": list(unit_row.get("reason_codes") or []),
    }
    if (
        unit_row.get("healthy") is not True
        or unit_row.get("phase")
        not in {"persistent_running", "oneshot_active_success"}
        or relationship["service_main_pid"] != pid
    ):
        relationship["rejected_reason"] = "matched_service_unhealthy"
        return relationship

    roles_by_service = {
        str(item.get("service") or ""): item for item in access_roles
    }
    declared = roles_by_service.get(matched_unit)
    if declared is None:
        relationship["rejected_reason"] = "undeclared_service"
        return relationship
    relationship["declared_role"] = str(declared.get("declared_role") or "")
    relationship["allowed_access_modes"] = list(
        declared.get("allowed_access_modes") or []
    )
    if mode not in relationship["allowed_access_modes"]:
        relationship["rejected_reason"] = "access_mode_not_allowed"
        return relationship
    relationship["accepted"] = True
    relationship["accepted_reason"] = "exact_healthy_declared_mainpid_and_access_mode"
    return relationship


def _active_mutable_canonical_snapshot(
    *,
    runtime_dir: Path,
    policy: Mapping[str, Any],
    store_registry: Mapping[str, Any],
    service_snapshot: Mapping[str, Mapping[str, Any]],
    opener_reader: Callable[[Path], list[dict[str, Any]]] = _process_fd_openers,
) -> dict[str, Any]:
    non_target_cas = policy.get("non_target_cas")
    bindings = (
        non_target_cas.get("active_mutable_canonical_stores")
        if isinstance(non_target_cas, Mapping)
        else None
    )
    if not isinstance(bindings, list) or not bindings:
        raise WarmArchiveError("mutable canonical store classification is unavailable")
    producers = {
        str(item.get("owner") or ""): item
        for item in policy.get("producers") or []
        if isinstance(item, Mapping)
    }
    target_paths = {
        str(Path(str(item["source_path"])))
        for item in TARGET_POLICIES
    }
    target_paths.update(
        path + suffix
        for path in list(target_paths)
        for suffix in ("-wal", "-shm", "-journal")
    )
    topology_rows = []
    observation_rows = []
    resolved_paths = set()
    for binding in bindings:
        key = str(binding.get("key") or "")
        owner = str(binding.get("owner") or "")
        classification = str(binding.get("classification") or "")
        filesystem_role = str(binding.get("filesystem_role") or "")
        producer = producers.get(owner)
        resolver = binding.get("resolver")
        raw_access_roles = binding.get("access_roles")
        access_roles = tuple(
            dict(item) for item in raw_access_roles or [] if isinstance(item, Mapping)
        )
        declared_services = tuple(
            str(item.get("service") or "") for item in access_roles
        )
        if (
            not key
            or not isinstance(resolver, Mapping)
            or producer is None
            or classification != CLASS_ESSENTIAL
            or producer.get("classification") != classification
            or filesystem_role not in {"root", "generation"}
            or not isinstance(raw_access_roles, list)
            or len(access_roles) != len(raw_access_roles)
            or not declared_services
            or len(set(declared_services)) != len(declared_services)
            or any(name not in SERVICE_NAMES for name in declared_services)
            or any(
                item.get("declared_role") not in MUTABLE_STORE_ACCESS_ROLES
                or not isinstance(item.get("allowed_access_modes"), list)
                or set(item.get("allowed_access_modes") or [])
                != set(
                    MUTABLE_STORE_ACCESS_ROLES.get(
                        str(item.get("declared_role") or ""),
                        frozenset(),
                    )
                )
                for item in access_roles
            )
        ):
            raise WarmArchiveError("unknown/unregistered mutable canonical classification")
        registry_identity = None
        if resolver.get("type") == "store_registry":
            logical_store = str(resolver.get("logical_store") or "")
            registry_identity = (store_registry.get("stores") or {}).get(logical_store)
            if not isinstance(registry_identity, Mapping):
                raise WarmArchiveError("StoreRegistry mutable resolver identity is unavailable")
            if filesystem_role != "generation":
                raise WarmArchiveError(
                    "StoreRegistry mutable filesystem role drifted"
                )
            path = Path(str(registry_identity.get("path") or ""))
        elif resolver.get("type") == "literal":
            if filesystem_role != "root":
                raise WarmArchiveError("literal mutable filesystem role drifted")
            path = Path(str(resolver.get("path") or ""))
            registered = registered_producer_for_path(policy, path)
            if (
                registered is None
                or registered.get("owner") != owner
                or registered.get("classification") != classification
            ):
                raise WarmArchiveError("literal mutable store owner/classification drifted")
        else:
            raise WarmArchiveError("mutable canonical resolver type is unknown")
        path_registration = registered_producer_for_path(policy, path)
        if path_registration is not None and (
            path_registration.get("owner") != owner
            or path_registration.get("classification") != classification
        ):
            raise WarmArchiveError(
                "mutable canonical store conflicts with its registered path owner"
            )
        if (
            str(path) in target_paths
            or str(path).startswith(str(DESTINATION_ROOT / DESTINATION_FAMILY_NAME) + "/")
        ):
            raise WarmArchiveError("mutable canonical store overlaps exact mutation scope")
        topology, mount_observation = _stable_file_topology(
            path,
            filesystem_role=filesystem_role,
            policy_owner=f"root_storage_policy.non_target_cas.{key}.{owner}",
        )
        openers = opener_reader(path)
        opener_relationships = [
            _mutable_opener_access_relationship(
                opener,
                path=path,
                topology=topology,
                access_roles=access_roles,
                service_snapshot=service_snapshot,
            )
            for opener in openers
        ]
        if any(item["accepted"] is not True for item in opener_relationships):
            raise WarmArchiveError(
                "mutable canonical store has an invalid open-handle access relationship",
                evidence={"key": key, "path": str(path), "openers": opener_relationships},
            )
        if not openers and binding.get("allow_no_open_handles") is not True:
            raise WarmArchiveError("mutable canonical store lacks its required owner handle")
        value = path.stat()
        topology_row = {
            "key": key,
            "owner": owner,
            "classification": classification,
            "filesystem_role": filesystem_role,
            "resolver": dict(resolver),
            "access_roles": [
                {
                    "service": str(item["service"]),
                    "declared_role": str(item["declared_role"]),
                    "allowed_access_modes": list(item["allowed_access_modes"]),
                }
                for item in sorted(access_roles, key=lambda role: str(role["service"]))
            ],
            "allow_no_open_handles": bool(binding.get("allow_no_open_handles")),
            "registry_identity": dict(registry_identity) if registry_identity else None,
            "topology": topology,
        }
        topology_rows.append(topology_row)
        observation_rows.append(
            {
                **topology_row,
                "ordinary_mutable_fields": {
                    "apparent_size_bytes": int(value.st_size),
                    "allocated_bytes": int(value.st_blocks * 512),
                    "mtime_ns": int(value.st_mtime_ns),
                    "ctime_ns": int(value.st_ctime_ns),
                },
                "open_handle_relationships": opener_relationships,
                "mount_observation": mount_observation,
            }
        )
        resolved_paths.add(str(path))
    return {
        "contract_version": str(non_target_cas.get("contract_version") or ""),
        "topology_rows": topology_rows,
        "topology_digest": _digest(topology_rows),
        "observation_rows": observation_rows,
        "resolved_paths": sorted(resolved_paths),
    }


def _non_target_snapshot(
    runtime_dir: Path,
    *,
    service_snapshot: Mapping[str, Mapping[str, Any]] | None = None,
    require_targets: bool = True,
    operation_id: str = "",
    expected_non_target: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    services = dict(service_snapshot or _systemd_snapshot())
    policy = load_policy()
    store_registry = _store_registry(runtime_dir, list(TARGET_POLICIES))
    mutable = _active_mutable_canonical_snapshot(
        runtime_dir=runtime_dir,
        policy=policy,
        store_registry=store_registry,
        service_snapshot=services,
    )
    immutable = _immutable_non_target_snapshot(operation_id=operation_id)
    root_policy = _root_policy_snapshot(
        list(TARGET_POLICIES),
        mutable_paths=set(mutable["resolved_paths"]),
        require_targets=require_targets,
        expected_protected_topology=list(
            ((expected_non_target or {}).get("root_policy") or {}).get(
                "protected_path_topology"
            )
            or []
        ),
    )
    return {
        "immutable": immutable,
        "immutable_digest": _digest(
            {
                "scoped": immutable["immutable_digest"],
                "root_policy_topology": root_policy[
                    "protected_path_topology_digest"
                ],
            }
        ),
        "mutable_canonical": mutable,
        "mutable_canonical_topology_digest": mutable["topology_digest"],
        "root_policy": root_policy,
        "store_registry": store_registry,
        "services": services,
    }


def _reconcile_non_target(
    before: Mapping[str, Any], after: Mapping[str, Any], *, phase: str
) -> dict[str, Any]:
    before_observations = {
        str(item["key"]): item
        for item in (before.get("mutable_canonical") or {}).get("observation_rows") or []
    }
    after_observations = {
        str(item["key"]): item
        for item in (after.get("mutable_canonical") or {}).get("observation_rows") or []
    }
    evolution = []
    for key in sorted(set(before_observations) | set(after_observations)):
        earlier = before_observations.get(key)
        later = after_observations.get(key)
        evolution.append(
            {
                "key": key,
                "before": (earlier or {}).get("ordinary_mutable_fields"),
                "after": (later or {}).get("ordinary_mutable_fields"),
                "ordinary_content_evolution_observed": bool(
                    earlier
                    and later
                    and earlier.get("ordinary_mutable_fields")
                    != later.get("ordinary_mutable_fields")
                ),
                "open_handles_before": (earlier or {}).get(
                    "open_handle_relationships"
                ),
                "open_handles_after": (later or {}).get(
                    "open_handle_relationships"
                ),
                "mount_observation_before": (earlier or {}).get(
                    "mount_observation"
                ),
                "mount_observation_after": (later or {}).get(
                    "mount_observation"
                ),
                "namespace_mount_observation_changed": bool(
                    earlier
                    and later
                    and earlier.get("mount_observation")
                    != later.get("mount_observation")
                ),
            }
        )
    before_protected = {
        str(item["path"]): item
        for item in (before.get("root_policy") or {}).get(
            "protected_path_observations"
        )
        or []
        if isinstance(item, Mapping)
    }
    after_protected = {
        str(item["path"]): item
        for item in (after.get("root_policy") or {}).get(
            "protected_path_observations"
        )
        or []
        if isinstance(item, Mapping)
    }
    protected_evolution = []
    for path in sorted(set(before_protected) | set(after_protected)):
        earlier = before_protected.get(path)
        later = after_protected.get(path)
        protected_evolution.append(
            {
                "path": path,
                "topology_before": {
                    key: (earlier or {}).get(key)
                    for key in (
                        "path",
                        "device",
                        "inode",
                        "owner",
                        "classification",
                        "registered",
                    )
                },
                "topology_after": {
                    key: (later or {}).get(key)
                    for key in (
                        "path",
                        "device",
                        "inode",
                        "owner",
                        "classification",
                        "registered",
                    )
                },
                "ordinary_mutable_fields_before": (earlier or {}).get(
                    "ordinary_mutable_fields"
                ),
                "ordinary_mutable_fields_after": (later or {}).get(
                    "ordinary_mutable_fields"
                ),
                "ordinary_writer_progress_observed": bool(
                    earlier
                    and later
                    and earlier.get("ordinary_mutable_fields")
                    != later.get("ordinary_mutable_fields")
                ),
            }
        )
    before_scoped = {
        str(item["path"]): item
        for item in (before.get("immutable") or {}).get(
            "exact_family_observation_rows"
        )
        or []
        if isinstance(item, Mapping)
    }
    after_scoped = {
        str(item["path"]): item
        for item in (after.get("immutable") or {}).get(
            "exact_family_observation_rows"
        )
        or []
        if isinstance(item, Mapping)
    }
    scoped_writer_evolution = []
    ordinary_fields = (
        "size_bytes",
        "allocated_bytes",
        "mtime_ns",
        "ctime_ns",
        "sha256",
    )
    for path in sorted(set(before_scoped) | set(after_scoped)):
        earlier = before_scoped.get(path)
        later = after_scoped.get(path)
        before_fields = {
            key: (earlier or {}).get(key) for key in ordinary_fields
        }
        after_fields = {key: (later or {}).get(key) for key in ordinary_fields}
        scoped_writer_evolution.append(
            {
                "path": path,
                "ordinary_mutable_fields_before": before_fields,
                "ordinary_mutable_fields_after": after_fields,
                "ordinary_writer_progress_observed": bool(
                    earlier and later and before_fields != after_fields
                ),
            }
        )
    result = {
        "phase": phase,
        "immutable_digest_before": before.get("immutable_digest"),
        "immutable_digest_after": after.get("immutable_digest"),
        "immutable_preserved": before.get("immutable_digest")
        == after.get("immutable_digest"),
        "mutable_canonical_topology_digest_before": before.get(
            "mutable_canonical_topology_digest"
        ),
        "mutable_canonical_topology_digest_after": after.get(
            "mutable_canonical_topology_digest"
        ),
        "mutable_canonical_topology_preserved": before.get(
            "mutable_canonical_topology_digest"
        )
        == after.get("mutable_canonical_topology_digest"),
        "mutable_canonical_evolution": evolution,
        "protected_path_observation_evolution": protected_evolution,
        "scoped_non_target_writer_evolution": scoped_writer_evolution,
    }
    if not result["immutable_preserved"] or not result[
        "mutable_canonical_topology_preserved"
    ]:
        raise WarmArchiveError(
            f"non-target topology reconciliation failed: {phase}",
            evidence=result,
        )
    return result


def _mutation_scope_reconciliation(journal: Mapping[str, Any]) -> dict[str, Any]:
    policies = {str(item["key"]): item for item in TARGET_POLICIES}
    expected_unlinks = sorted(str(item["source_path"]) for item in TARGET_POLICIES)
    expected_outputs = sorted(
        str(DESTINATION_ROOT / DESTINATION_FAMILY_NAME / name)
        for item in TARGET_POLICIES
        for name in (
            str(item["archive_name"]),
            str(item["archive_name"]) + ".manifest.json",
        )
    )
    observed_unlinks = []
    observed_outputs = []
    item_errors = []
    for item in journal.get("items") or []:
        key = str(item.get("key") or "")
        policy = policies.get(key)
        if policy is None:
            item_errors.append({"key": key, "reason": "unknown_target_key"})
            continue
        if int(item.get("unlink_count") or 0) == 1:
            observed_unlinks.append(str(policy["source_path"]))
        archive_path = str(item.get("archive_path") or "")
        manifest_path = str(item.get("manifest_path") or "")
        expected_archive = str(
            DESTINATION_ROOT
            / DESTINATION_FAMILY_NAME
            / str(policy["archive_name"])
        )
        expected_manifest = expected_archive + ".manifest.json"
        if archive_path:
            observed_outputs.append(archive_path)
        if manifest_path:
            observed_outputs.append(manifest_path)
        if archive_path and archive_path != expected_archive:
            item_errors.append({"key": key, "reason": "archive_path_escape"})
        if manifest_path and manifest_path != expected_manifest:
            item_errors.append({"key": key, "reason": "manifest_path_escape"})
    mutable_paths = {
        str(row.get("topology", {}).get("path") or "")
        for row in (
            (journal.get("non_target_before") or {})
            .get("mutable_canonical", {})
            .get("topology_rows", [])
        )
    }
    result = {
        "expected_literal_unlink_paths": expected_unlinks,
        "observed_literal_unlink_paths": sorted(observed_unlinks),
        "expected_destination_output_paths": expected_outputs,
        "observed_destination_output_paths": sorted(observed_outputs),
        "mutable_canonical_paths": sorted(mutable_paths),
        "mutable_canonical_path_overlap": sorted(
            mutable_paths & (set(observed_unlinks) | set(observed_outputs))
        ),
        "item_errors": item_errors,
        "non_target_unlink_move_write_count": 0,
    }
    result["exact"] = bool(
        sorted(observed_unlinks) == expected_unlinks
        and sorted(observed_outputs) == expected_outputs
        and not result["mutable_canonical_path_overlap"]
        and not item_errors
        and int(journal.get("promo_action_count", -1)) == 0
        and int(journal.get("business_data_mutation_count", -1)) == 0
    )
    return result


def _finance_snapshot(runtime_dir: Path) -> dict[str, Any]:
    health = backup_rotation_health(runtime_dir)
    next_replacement_required = int(
        health.get("next_replacement_required_bytes") or 0
    )
    blockers = [str(item)[:300] for item in health.get("blockers") or []]
    healthy = bool(
        health.get("status") == "healthy"
        and health.get("next_replacement_capacity") is True
        and not blockers
        and next_replacement_required > 0
    )
    return {
        "status": str(health.get("status") or "unknown"),
        "healthy": healthy,
        "blockers": blockers,
        "retained_backup_id": str(health.get("retained_backup_id") or ""),
        "retained_count": int(health.get("retained_count") or 0),
        "retained_bytes": int(health.get("retained_bytes") or 0),
        "next_replacement_required_bytes": next_replacement_required,
        "emergency_reserve_bytes": EMERGENCY_RESERVE_BYTES,
        "required_available_floor_bytes": next_replacement_required
        + EMERGENCY_RESERVE_BYTES,
        "available_bytes": int(health.get("available_bytes") or 0),
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
        values = {
            "Id": "",
            **{property_name: "" for property_name in SYSTEMD_REQUIRED_PROPERTIES},
            **{property_name: "" for property_name in SYSTEMD_TIMER_PROPERTIES},
        }
        observed_properties: set[str] = set()
        try:
            completed = subprocess.run(
                [
                    "systemctl",
                    "show",
                    name,
                    "--no-pager",
                    "--property=Id",
                    *(f"--property={item}" for item in SYSTEMD_REQUIRED_PROPERTIES),
                    *(f"--property={item}" for item in SYSTEMD_TIMER_PROPERTIES),
                ],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            values.update(
                {
                    "QueryReturnCode": None,
                    "QueryError": type(exc).__name__,
                    "QueryStderrSha256": None,
                }
            )
            result[name] = values
            continue
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
                observed_properties.add(key)
        values.update(
            {
                "QueryReturnCode": int(completed.returncode),
                "QueryError": None,
                "QueryStderrSha256": _digest(completed.stderr.encode("utf-8")),
                "ObservedProperties": sorted(observed_properties),
            }
        )
        result[name] = values
    return result


def _systemd_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _systemd_unit_row(name: str, values: Mapping[str, Any]) -> dict[str, Any]:
    kind = (
        "timer"
        if name.endswith(".timer")
        else "persistent_service"
        if name in PERSISTENT_SERVICE_NAMES
        else "oneshot_service"
    )
    row = {
        "name": name,
        "unit_kind": kind,
        "Id": values.get("Id", ""),
        **{
            property_name: values.get(property_name, "")
            for property_name in SYSTEMD_REQUIRED_PROPERTIES
        },
        "QueryReturnCode": values.get("QueryReturnCode"),
        "QueryError": values.get("QueryError"),
        "QueryStderrSha256": values.get("QueryStderrSha256"),
        "ObservedProperties": values.get("ObservedProperties"),
    }
    if kind == "timer":
        row.update(
            {
                property_name: values.get(property_name, "")
                for property_name in SYSTEMD_TIMER_PROPERTIES
            }
        )
    reasons: list[str] = []
    observed_properties = set(
        values.get("ObservedProperties")
        if isinstance(values.get("ObservedProperties"), list)
        else values
    )
    mandatory_properties = (
        ("Id", "LoadState", "ActiveState", "SubState", "Result", "UnitFileState")
        if kind == "timer"
        else ("Id", *SYSTEMD_REQUIRED_PROPERTIES)
    )
    missing_properties = [
        property_name
        for property_name in mandatory_properties
        if property_name not in observed_properties
    ]
    query_failed = (
        values.get("QueryReturnCode") != 0 or values.get("QueryError") is not None
    )
    identity_mismatch = values.get("Id") != name
    absent_or_masked = (
        values.get("LoadState") in {"not-found", "masked", "error"}
        or str(values.get("UnitFileState") or "").startswith("masked")
    )
    result_failed = values.get("Result") not in {"", "success"}
    exec_raw = values.get("ExecMainStatus")
    exec_status = _systemd_int(exec_raw)
    exec_failed = (
        exec_status != 0
        if kind != "timer"
        else exec_raw not in {None, ""} and exec_status != 0
    )
    main_pid = _systemd_int(values.get("MainPID"))
    phase = "invalid"
    resample_candidate = False

    if missing_properties or query_failed or identity_mismatch:
        classification = "predicate_or_literal_unit_list_defect"
        healthy = False
        if missing_properties:
            reasons.append("required_properties_missing")
        if query_failed:
            reasons.append("systemctl_query_failed")
        if identity_mismatch:
            reasons.append("literal_unit_identity_mismatch")
    elif absent_or_masked:
        classification = "absent_or_masked"
        healthy = False
        reasons.append("required_unit_absent_or_masked")
    elif result_failed or exec_failed:
        classification = (
            "real_unhealthy_timer_control"
            if kind == "timer"
            else "real_unhealthy_owning_service"
        )
        healthy = False
        if result_failed:
            reasons.append("failed_result")
        if exec_failed:
            reasons.append("nonzero_or_invalid_exec_main_status")
    elif kind == "persistent_service":
        if (
            values.get("LoadState") == "loaded"
            and values.get("ActiveState") == "active"
            and values.get("SubState") == "running"
            and main_pid is not None
            and main_pid > 0
        ):
            classification = "healthy_persistent_service"
            healthy = True
            phase = "persistent_running"
        else:
            classification = "real_unhealthy_owning_service"
            healthy = False
            reasons.append("persistent_service_not_active_running_with_pid")
    elif kind == "timer":
        common_timer = (
            values.get("LoadState") == "loaded"
            and values.get("ActiveState") == "active"
            and values.get("UnitFileState") == "enabled"
        )
        if common_timer and values.get("SubState") == "waiting":
            classification = "expected_waiting_timer"
            healthy = True
            phase = "timer_waiting"
        elif common_timer and values.get("SubState") == "running":
            classification = "healthy_trigger_running_timer"
            healthy = True
            phase = "timer_running"
        else:
            classification = "unrecognized_timer_state"
            healthy = False
            reasons.append("timer_not_loaded_active_waiting_or_running_enabled")
            resample_candidate = bool(
                values.get("LoadState") == "loaded"
                and values.get("ActiveState") in {"active", "activating"}
                and values.get("UnitFileState") == "enabled"
                and values.get("SubState") not in {"dead", "failed"}
            )
    else:
        inactive_success = (
            values.get("LoadState") == "loaded"
            and values.get("ActiveState") == "inactive"
            and values.get("SubState") in {"dead", "exited"}
            and main_pid == 0
        )
        active_success = (
            values.get("LoadState") == "loaded"
            and values.get("ActiveState") in {"active", "activating"}
            and values.get("SubState") in {"start", "running", "exited"}
            and main_pid is not None
            and main_pid > 0
        )
        if inactive_success:
            classification = "correct_inactive_oneshot"
            healthy = True
            phase = "oneshot_inactive_success"
        elif active_success:
            classification = "healthy_active_oneshot"
            healthy = True
            phase = "oneshot_active_success"
        else:
            classification = "unrecognized_oneshot_state"
            healthy = False
            reasons.append("oneshot_state_predicate_failed")
            resample_candidate = bool(
                values.get("LoadState") == "loaded"
                and values.get("ActiveState")
                in {"inactive", "activating", "active", "deactivating"}
                and values.get("SubState") not in {"failed"}
            )

    row.update(
        {
            "state_classification": classification,
            "classification": classification,
            "state_healthy": healthy,
            "healthy": healthy,
            "phase": phase,
            "resample_candidate": resample_candidate,
            "reason_codes": reasons,
        }
    )
    return row


def _systemd_service_gate(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Classify all literal units and every timer/owning-service pair."""

    expected_names = list(SERVICE_NAMES)
    observed_names = list(snapshot)
    missing_names = [name for name in expected_names if name not in snapshot]
    unexpected_names = [name for name in observed_names if name not in SERVICE_NAMES]
    rows = [
        _systemd_unit_row(
            name,
            snapshot.get(name) if isinstance(snapshot.get(name), Mapping) else {},
        )
        for name in expected_names
    ]
    rows_by_name = {row["name"]: row for row in rows}
    pair_rows = []
    missing_pair_owners = []
    for timer_name, owner_name in TIMER_SERVICE_PAIRS:
        timer = rows_by_name[timer_name]
        owner = rows_by_name.get(owner_name)
        if owner is None:
            missing_pair_owners.append(owner_name)
            pair_classification = "pair_definition_defect"
            pair_healthy = False
            resample_required = False
            reasons = ["paired_owning_service_missing_from_literal_scope"]
        elif not timer["state_healthy"] or not owner["state_healthy"]:
            transition_candidate = bool(
                (timer["state_healthy"] or timer["resample_candidate"])
                and (owner["state_healthy"] or owner["resample_candidate"])
            )
            pair_classification = (
                "bounded_snapshot_transition"
                if transition_candidate
                else "failed_timer_or_owner"
            )
            pair_healthy = False
            resample_required = transition_candidate
            reasons = [
                "paired_snapshot_requires_bounded_resample"
                if transition_candidate
                else "paired_timer_or_owner_state_unhealthy"
            ]
        elif (
            timer["phase"] == "timer_waiting"
            and owner["phase"] == "oneshot_inactive_success"
        ):
            pair_classification = "waiting_with_inactive_success_owner"
            pair_healthy = True
            resample_required = False
            reasons = []
        elif (
            timer["phase"] == "timer_running"
            and owner["phase"] == "oneshot_active_success"
        ):
            pair_classification = "trigger_in_progress_with_active_owner"
            pair_healthy = True
            resample_required = False
            reasons = []
        else:
            pair_classification = "bounded_snapshot_transition"
            pair_healthy = False
            resample_required = True
            reasons = ["paired_snapshot_requires_bounded_resample"]
        pair = {
            "timer_name": timer_name,
            "owner_name": owner_name,
            "classification": pair_classification,
            "healthy": pair_healthy,
            "resample_required": resample_required,
            "reason_codes": reasons,
            "timer_state_classification": timer["state_classification"],
            "owner_state_classification": (
                owner["state_classification"] if owner is not None else None
            ),
        }
        pair_rows.append(pair)
        for unit in (timer, owner):
            if unit is None:
                continue
            unit["paired_unit_name"] = owner_name if unit is timer else timer_name
            unit["pair_classification"] = pair_classification
            unit["pair_healthy"] = pair_healthy
            unit["pair_resample_required"] = resample_required
            if not pair_healthy:
                unit["healthy"] = False
                if reasons[0] not in unit["reason_codes"]:
                    unit["reason_codes"].append(reasons[0])

    list_defect = bool(
        missing_names
        or unexpected_names
        or missing_pair_owners
        or len(rows) != len(SERVICE_NAMES)
        or len(pair_rows) != len(TIMER_SERVICE_PAIRS)
    )
    failing = [row for row in rows if row.get("healthy") is not True]
    failing_pairs = [pair for pair in pair_rows if pair.get("healthy") is not True]
    resample_pairs = [
        pair for pair in pair_rows if pair.get("resample_required") is True
    ]
    counts: dict[str, int] = {}
    for row in rows:
        classification = str(row["classification"])
        counts[classification] = counts.get(classification, 0) + 1
    pair_counts: dict[str, int] = {}
    for pair in pair_rows:
        classification = str(pair["classification"])
        pair_counts[classification] = pair_counts.get(classification, 0) + 1
    return {
        "expected_unit_count": len(SERVICE_NAMES),
        "observed_unit_count": len(snapshot),
        "expected_unit_names": expected_names,
        "missing_unit_names": missing_names,
        "unexpected_unit_names": unexpected_names,
        "expected_pair_count": len(TIMER_SERVICE_PAIRS),
        "observed_pair_count": len(pair_rows),
        "missing_pair_owner_names": missing_pair_owners,
        "classification": (
            "predicate_or_literal_unit_list_defect"
            if list_defect
            else "required_units_unhealthy"
            if failing or failing_pairs
            else "healthy"
        ),
        "classification_counts": counts,
        "pair_classification_counts": pair_counts,
        "healthy": not list_defect and not failing and not failing_pairs,
        "failing_unit_count": len(failing),
        "failing_units": failing,
        "failing_pair_count": len(failing_pairs),
        "failing_pairs": failing_pairs,
        "resample_required_pair_names": [
            {"timer_name": pair["timer_name"], "owner_name": pair["owner_name"]}
            for pair in resample_pairs
        ],
        "pairs": pair_rows,
        "units": rows,
    }


def _systemd_service_gate_with_resample(
    initial_snapshot: Mapping[str, Any] | None = None,
    *,
    snapshot_reader: Callable[[tuple[str, ...]], Mapping[str, Any]] | None = None,
    max_attempts: int = SYSTEMD_PAIR_RESAMPLE_MAX_ATTEMPTS,
    max_seconds: float = SYSTEMD_PAIR_RESAMPLE_MAX_SECONDS,
    interval_seconds: float = SYSTEMD_PAIR_RESAMPLE_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Bound only plausible paired-snapshot races and retain every sample."""

    reader = snapshot_reader or _systemd_snapshot
    current_snapshot = dict(initial_snapshot or reader(SERVICE_NAMES))
    initial_gate = _systemd_service_gate(current_snapshot)
    gate = initial_gate
    started = time.monotonic()
    initial_resample_names = {
        name
        for pair in initial_gate["resample_required_pair_names"]
        for name in (pair["owner_name"], pair["timer_name"])
    }
    samples: list[dict[str, Any]] = []
    if initial_resample_names:
        samples.append(
            {
                "attempt": 0,
                "captured_at": _now(),
                "unit_names": sorted(initial_resample_names),
                "units": [
                    row
                    for row in initial_gate["units"]
                    if row["name"] in initial_resample_names
                ],
                "pairs": [
                    pair
                    for pair in initial_gate["pairs"]
                    if pair["timer_name"] in initial_resample_names
                ],
            }
        )
    attempts = 0
    while gate["resample_required_pair_names"] and attempts < max_attempts:
        if time.monotonic() - started >= max_seconds:
            break
        if interval_seconds > 0:
            time.sleep(interval_seconds)
        attempts += 1
        requested_names = tuple(
            dict.fromkeys(
                name
                for pair in gate["resample_required_pair_names"]
                for name in (pair["owner_name"], pair["timer_name"])
            )
        )
        resampled = reader(requested_names)
        for name in requested_names:
            current_snapshot[name] = resampled.get(name, {})
        gate = _systemd_service_gate(current_snapshot)
        sampled_names = set(requested_names)
        samples.append(
            {
                "attempt": attempts,
                "captured_at": _now(),
                "unit_names": list(requested_names),
                "units": [
                    row for row in gate["units"] if row["name"] in sampled_names
                ],
                "pairs": [
                    pair
                    for pair in gate["pairs"]
                    if pair["timer_name"] in sampled_names
                ],
            }
        )
    gate["pair_resample_evidence"] = {
        "attempted": attempts > 0,
        "max_attempts": max_attempts,
        "max_seconds": max_seconds,
        "interval_seconds": interval_seconds,
        "attempt_count": attempts,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "resolved_healthy": bool(
            initial_gate["resample_required_pair_names"] and gate["healthy"]
        ),
        "remaining_resample_required_pair_names": gate[
            "resample_required_pair_names"
        ],
        "samples": samples,
    }
    return gate


def _services_healthy(snapshot: Mapping[str, Any]) -> bool:
    return bool(_systemd_service_gate(snapshot)["healthy"])


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
        or material.get("material_partition") != "immutable_safety_v1"
        or int(material.get("source_count") or 0) != EXPECTED_SOURCE_COUNT
        or material.get("destination_root") != str(DESTINATION_ROOT)
        or material.get("destination_family")
        != str(DESTINATION_ROOT / DESTINATION_FAMILY_NAME)
        or material.get("compression") != "zstd-level-1-single-thread"
        or not SHA256_RE.fullmatch(
            str(material.get("immutable_non_target_digest") or "")
        )
        or not SHA256_RE.fullmatch(
            str(material.get("mutable_canonical_topology_digest") or "")
        )
        or not isinstance(material.get("mutable_canonical_topology"), list)
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
    finance = _finance_snapshot(runtime_dir)
    active_jobs = _active_sanitation_jobs(runtime_dir, own_job_id=own_job_id)
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
    non_target = _non_target_snapshot(
        runtime_dir,
        expected_non_target=(
            {
                "root_policy": {
                    "protected_path_topology": (
                        (reusable_material.get("root_policy") or {}).get(
                            "protected_path_topology"
                        )
                        or []
                    )
                }
            }
            if reusable_material is not None
            else None
        ),
    )
    store_registry = non_target["store_registry"]
    root_policy = non_target["root_policy"]
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
    expected_reclaimed = sum(int(item["identity"]["allocated_bytes"]) for item in targets)
    projected_root = int(filesystems["root"]["available_bytes"]) + expected_reclaimed
    services = non_target["services"]
    service_gate = _systemd_service_gate_with_resample(services)
    material = {
        "contract_name": CONTRACT_NAME,
        "profile": PROFILE,
        "material_partition": "immutable_safety_v1",
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
        "store_registry": store_registry,
        "root_policy": {
            "policy_sha256": root_policy["policy_sha256"],
            "target_rows": root_policy["target_rows"],
            "protected_path_topology": root_policy[
                "protected_path_topology"
            ],
            "protected_path_topology_digest": root_policy[
                "protected_path_topology_digest"
            ],
        },
        "immutable_non_target_digest": non_target["immutable_digest"],
        "mutable_canonical_topology": non_target["mutable_canonical"][
            "topology_rows"
        ],
        "mutable_canonical_topology_digest": non_target[
            "mutable_canonical_topology_digest"
        ],
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
        "root_policy_protected_path_observations": root_policy[
            "protected_path_observations"
        ],
        "finance": finance,
        "finance_available_bytes": finance["available_bytes"],
        "capacity_stages": stages,
        "projected_root_available_bytes": projected_root,
        "active_sanitation_jobs": active_jobs,
        "lifecycle_locks": lifecycle_locks,
        "non_target": non_target,
        "journald": _journald_snapshot(),
        "services": services,
        "systemd_service_gate": service_gate,
    }
    return material, observations


def _safe_identity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    fields = (
        "path",
        "device",
        "device_major",
        "device_minor",
        "inode",
        "apparent_size_bytes",
        "allocated_blocks_512",
        "allocated_bytes",
        "mode",
        "uid",
        "gid",
        "mtime_ns",
        "ctime_ns",
        "nlink",
        "sha256",
        "kind",
    )
    result = {key: value.get(key) for key in fields if key in value}
    if isinstance(value.get("mount"), Mapping):
        result["mount"] = copy.deepcopy(dict(value["mount"]))
    return result


def _json_pointer_token(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _cas_classification(path: str) -> str:
    if path.startswith("/targets/") and "/sidecars" in path:
        return "target_sidecar"
    if path.startswith("/targets/"):
        return "exact_target_source"
    if path.startswith("/immutable_non_target_digest") or path.startswith(
        "/root_policy"
    ):
        return "immutable_non_target_or_policy"
    if path.startswith("/mutable_canonical_topology") or path.startswith(
        "/store_registry"
    ):
        return "mutable_store_topology_or_registry"
    if path.startswith("/filesystems") or path.startswith("/destination"):
        return "destination_or_mount_topology"
    if path.startswith("/observations/systemd_service_gate"):
        return "service_health_observation"
    if path.startswith("/observations/capacity"):
        return "capacity_observation"
    if path.startswith("/observations/target_activity"):
        return "target_activity_observation"
    if path.startswith("/observations/lifecycle_locks") or path.startswith(
        "/observations/sanitation_jobs"
    ):
        return "storage_lifecycle_observation"
    if path.startswith("/observations/non_target"):
        return "non_target_live_observation"
    return "immutable_material"


def _safe_target_component_evidence(
    target: Mapping[str, Any], field: str
) -> dict[str, Any]:
    base = {
        "key": target.get("key"),
        "source_path": target.get("source_path"),
    }
    if field == "identity":
        return {**base, "identity": _safe_identity(target.get("identity"))}
    if field == "sidecars":
        return {
            **base,
            "sidecars": [
                {
                    "suffix": item.get("suffix"),
                    "path": item.get("path"),
                    "present": item.get("present"),
                    "kind": item.get("kind"),
                    "identity": _safe_identity(item.get("identity")),
                }
                for item in target.get("sidecars") or []
                if isinstance(item, Mapping)
            ],
        }
    if field == "provenance":
        provenance = target.get("provenance") or {}
        return {
            **base,
            "provenance_digest": provenance.get("digest"),
            "record_identities": [
                {
                    "path": item.get("path"),
                    "identity": _safe_identity(item.get("identity")),
                    "status": item.get("status"),
                    "deployed_sha": item.get("deployed_sha"),
                    "approval_reference_present": item.get(
                        "approval_reference_present"
                    ),
                }
                for item in provenance.get("records") or []
                if isinstance(item, Mapping)
            ],
        }
    if field == "hold_evidence":
        hold = target.get("hold_evidence") or {}
        return {
            **base,
            "hold": {
                "classification": hold.get("classification"),
                "marker_paths": list(hold.get("marker_paths") or []),
                "hold_xattr_names": list(hold.get("hold_xattr_names") or []),
                "protected_prefix_match": hold.get("protected_prefix_match"),
            },
        }
    if field == "sqlite":
        sqlite_evidence = target.get("sqlite") or {}
        return {
            **base,
            "sqlite": {
                "header": sqlite_evidence.get("header"),
                "quick_check": sqlite_evidence.get("quick_check"),
                "integrity_check": sqlite_evidence.get("integrity_check"),
                "schema_identity_sha256": sqlite_evidence.get(
                    "schema_identity_sha256"
                ),
                "schema_object_count": sqlite_evidence.get(
                    "schema_object_count"
                ),
                "table_count": sqlite_evidence.get("table_count"),
                "pragmas": sqlite_evidence.get("pragmas"),
            },
        }
    return base


def _safe_topology_evidence(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    resolver = row.get("resolver") or {}
    topology = row.get("topology") or {}
    return {
        "key": row.get("key"),
        "owner": row.get("owner"),
        "classification": row.get("classification"),
        "filesystem_role": row.get("filesystem_role"),
        "resolver": {
            "type": resolver.get("type") if isinstance(resolver, Mapping) else None,
            "logical_store": (
                resolver.get("logical_store")
                if isinstance(resolver, Mapping)
                else None
            ),
            "path": resolver.get("path") if isinstance(resolver, Mapping) else None,
        },
        "topology": _safe_identity(topology),
        "registry_identity_digest": (
            _digest(row.get("registry_identity"))
            if isinstance(row.get("registry_identity"), Mapping)
            else None
        ),
        "access_roles_digest": _digest(row.get("access_roles") or []),
        "allow_no_open_handles": row.get("allow_no_open_handles"),
    }


def _safe_opener_evidence(rows: Any, *, limit: int = 16) -> dict[str, Any]:
    values = [item for item in rows or [] if isinstance(item, Mapping)]
    return {
        "count": len(values),
        "rows": [
            {
                key: item.get(key)
                for key in (
                    "source_path",
                    "pid",
                    "fd",
                    "access_mode",
                    "comm",
                    "target_device",
                    "target_inode",
                    "binds_source_device_inode",
                    "matched_unit",
                    "declared_role",
                    "accepted",
                    "accepted_reason",
                    "rejected_reason",
                )
            }
            for item in values[:limit]
        ],
        "truncated": len(values) > limit,
        "full_digest": _digest(values),
    }


def _component(
    *, path: str, value: Any, safe_evidence: Any = None, cas_role: str = "immutable"
) -> dict[str, Any]:
    return {
        "json_path": path,
        "classification": _cas_classification(path),
        "cas_role": cas_role,
        "digest": _digest(value),
        "safe_evidence": safe_evidence,
    }


def _material_cas_components(
    material: Mapping[str, Any], observations: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    handled = {
        "targets",
        "filesystems",
        "store_registry",
        "root_policy",
        "mutable_canonical_topology",
    }
    targets = material.get("targets") or []
    target_keys = [
        str(item.get("key") or "")
        for item in targets
        if isinstance(item, Mapping)
    ]
    result.append(
        _component(
            path="/targets/@keys",
            value=target_keys,
            safe_evidence={"keys": target_keys},
        )
    )
    for target_index, target in enumerate(targets):
        if not isinstance(target, Mapping):
            continue
        metadata = {
            field: target.get(field)
            for field in (
                "key",
                "source_path",
                "archive_name",
                "owner",
                "family",
                "restore_role",
            )
        }
        result.append(
            _component(
                path=f"/targets/{target_index}/metadata",
                value=metadata,
                safe_evidence=metadata,
            )
        )
        for field in (
            "identity",
            "sidecars",
            "sqlite",
            "provenance",
            "hold_evidence",
            "projected_archive_size_bytes",
        ):
            result.append(
                _component(
                    path=f"/targets/{target_index}/{field}",
                    value=target.get(field),
                    safe_evidence=_safe_target_component_evidence(target, field),
                )
            )
    filesystems = material.get("filesystems") or {}
    if isinstance(filesystems, Mapping):
        for name in sorted(filesystems):
            row = filesystems[name]
            result.append(
                _component(
                    path=f"/filesystems/{_json_pointer_token(name)}",
                    value=row,
                    safe_evidence=row,
                )
            )
    result.append(
        _component(
            path="/store_registry",
            value=material.get("store_registry"),
            safe_evidence={
                "identity_digest": (material.get("store_registry") or {}).get(
                    "identity_digest"
                ),
                "active_paths": list(
                    (material.get("store_registry") or {}).get("active_paths")
                    or []
                ),
                "manifest_file_sha256": (
                    material.get("store_registry") or {}
                ).get("manifest_file_sha256"),
            },
        )
    )
    root_policy = material.get("root_policy") or {}
    result.append(
        _component(
            path="/root_policy",
            value=root_policy,
            safe_evidence={
                "policy_sha256": root_policy.get("policy_sha256"),
                "target_row_count": len(root_policy.get("target_rows") or []),
                "protected_path_topology_digest": root_policy.get(
                    "protected_path_topology_digest"
                ),
                "protected_path_topology_count": len(
                    root_policy.get("protected_path_topology") or []
                ),
            },
        )
    )
    topology_rows = material.get("mutable_canonical_topology") or []
    topology_keys = [
        str(row.get("key") or "")
        for row in topology_rows
        if isinstance(row, Mapping)
    ]
    result.append(
        _component(
            path="/mutable_canonical_topology/@keys",
            value=topology_keys,
            safe_evidence={"keys": topology_keys},
        )
    )
    for topology_index, row in enumerate(topology_rows):
        if not isinstance(row, Mapping):
            continue
        result.append(
            _component(
                path=(
                    "/mutable_canonical_topology/"
                    + str(topology_index)
                ),
                value=row,
                safe_evidence=_safe_topology_evidence(row),
            )
        )
    for key in sorted(set(material) - handled):
        result.append(
            _component(
                path=f"/{_json_pointer_token(key)}",
                value=material.get(key),
                safe_evidence=(
                    material.get(key)
                    if key
                    in {
                        "contract_name",
                        "profile",
                        "source_count",
                        "destination_root",
                        "destination_family",
                        "immutable_non_target_digest",
                        "mutable_canonical_topology_digest",
                        "expected_unlink_count",
                        "expected_reclaimed_allocated_bytes",
                        "root_minimum_after_bytes",
                        "control_artifact_reserve_bytes",
                        "compression",
                    }
                    else None
                ),
            )
        )
    if observations is None:
        return sorted(result, key=lambda item: item["json_path"])
    service_gate = observations.get("systemd_service_gate") or {}
    service_summary = {
        "classification": service_gate.get("classification"),
        "healthy": service_gate.get("healthy"),
        "failing_units": service_gate.get("failing_units"),
        "failing_pairs": service_gate.get("failing_pairs"),
        "units": [
            {
                key: row.get(key)
                for key in (
                    "name",
                    "LoadState",
                    "ActiveState",
                    "SubState",
                    "Result",
                    "MainPID",
                    "ExecMainStatus",
                    "UnitFileState",
                    "classification",
                    "healthy",
                    "reason_codes",
                )
            }
            for row in service_gate.get("units") or []
            if isinstance(row, Mapping)
        ],
        "pairs": [
            {
                key: row.get(key)
                for key in (
                    "timer_name",
                    "owner_name",
                    "classification",
                    "healthy",
                    "reason_codes",
                )
            }
            for row in service_gate.get("pairs") or []
            if isinstance(row, Mapping)
        ],
    }
    result.append(
        _component(
            path="/observations/systemd_service_gate",
            value=service_summary,
            safe_evidence=service_summary,
            cas_role="observation_only",
        )
    )
    filesystem_mount_observations = [
        {
            "filesystem_role": str(name),
            "path": row.get("path"),
            "device": row.get("device"),
            "semantic_mount": row.get("mount"),
            "mount_observation": row.get("mount_observation"),
        }
        for name, row in sorted(
            (observations.get("filesystems_before") or {}).items()
        )
        if isinstance(row, Mapping)
    ]
    result.append(
        _component(
            path="/observations/filesystem_mounts",
            value=filesystem_mount_observations,
            safe_evidence=filesystem_mount_observations,
            cas_role="observation_only",
        )
    )
    capacity_summary = {
        "finance": {
            key: (observations.get("finance") or {}).get(key)
            for key in (
                "status",
                "healthy",
                "blockers",
                "retained_backup_id",
                "retained_count",
                "retained_bytes",
                "next_replacement_required_bytes",
                "required_available_floor_bytes",
                "available_bytes",
            )
        },
        "capacity_stages": observations.get("capacity_stages"),
        "projected_root_available_bytes": observations.get(
            "projected_root_available_bytes"
        ),
        "filesystem_available_bytes": {
            name: (row or {}).get("available_bytes")
            for name, row in (observations.get("filesystems_before") or {}).items()
            if isinstance(row, Mapping)
        },
    }
    result.append(
        _component(
            path="/observations/capacity",
            value=capacity_summary,
            safe_evidence=capacity_summary,
            cas_role="observation_only",
        )
    )
    protected_observations = observations.get(
        "root_policy_protected_path_observations"
    ) or []
    result.append(
        _component(
            path="/observations/non_target/protected_path_observations",
            value=protected_observations,
            safe_evidence={
                "count": len(protected_observations),
                "rows": list(protected_observations)[:64],
                "truncated": len(protected_observations) > 64,
                "full_digest": _digest(protected_observations),
            },
            cas_role="observation_only",
        )
    )
    scoped_observations = (
        ((observations.get("non_target") or {}).get("immutable") or {}).get(
            "exact_family_observation_rows"
        )
        or []
    )
    result.append(
        _component(
            path="/observations/non_target/exact_family_writer_observations",
            value=scoped_observations,
            safe_evidence={
                "count": len(scoped_observations),
                "rows": list(scoped_observations)[:64],
                "truncated": len(scoped_observations) > 64,
                "full_digest": _digest(scoped_observations),
            },
            cas_role="observation_only",
        )
    )
    mutable_observations = (
        ((observations.get("non_target") or {}).get("mutable_canonical") or {}).get(
            "observation_rows"
        )
        or []
    )
    result.append(
        _component(
            path="/observations/non_target/mutable_canonical_observations",
            value=mutable_observations,
            safe_evidence={
                "count": len(mutable_observations),
                "rows": [
                    {
                        "key": row.get("key"),
                        "owner": row.get("owner"),
                        "classification": row.get("classification"),
                        "filesystem_role": row.get("filesystem_role"),
                        "topology": _safe_identity(row.get("topology")),
                        "mount_observation": row.get("mount_observation"),
                        "ordinary_mutable_fields": row.get(
                            "ordinary_mutable_fields"
                        ),
                        "open_handle_relationships": _safe_opener_evidence(
                            row.get("open_handle_relationships")
                        ),
                    }
                    for row in mutable_observations[:16]
                    if isinstance(row, Mapping)
                ],
                "truncated": len(mutable_observations) > 16,
                "full_digest": _digest(mutable_observations),
            },
            cas_role="observation_only",
        )
    )
    active_jobs = observations.get("active_sanitation_jobs") or []
    result.append(
        _component(
            path="/observations/sanitation_jobs",
            value=active_jobs,
            safe_evidence={
                "count": len(active_jobs),
                "rows": list(active_jobs)[:16],
                "truncated": len(active_jobs) > 16,
                "full_digest": _digest(active_jobs),
            },
            cas_role="observation_only",
        )
    )
    lifecycle_locks = observations.get("lifecycle_locks") or []
    result.append(
        _component(
            path="/observations/lifecycle_locks",
            value=lifecycle_locks,
            safe_evidence=list(lifecycle_locks)[:16],
            cas_role="observation_only",
        )
    )
    journald = observations.get("journald") or {}
    result.append(
        _component(
            path="/observations/journald",
            value=journald,
            safe_evidence={
                "service": journald.get("service"),
                "effective": journald.get("effective"),
                "inventory_digest": _digest(journald.get("inventory") or []),
            },
            cas_role="observation_only",
        )
    )
    for gate_index, gate in enumerate(observations.get("activity_gates") or []):
        if not isinstance(gate, Mapping):
            continue
        source_path = str(gate.get("source_path") or "")
        target = next(
            (
                item
                for item in targets
                if isinstance(item, Mapping)
                and str(item.get("source_path") or "") == source_path
            ),
            {},
        )
        safe_gate = {
            "source_path": source_path,
            "classification": gate.get("classification"),
            "identity_before": _safe_identity(gate.get("identity_before")),
            "identity_after": _safe_identity(gate.get("identity_after")),
            "identity_matches_expected": gate.get("identity_matches_expected"),
            "material_stable_during_gate": gate.get(
                "material_stable_during_gate"
            ),
            "sidecars": gate.get("sidecars"),
            "fd_openers": _safe_opener_evidence(gate.get("fd_openers")),
            "kernel_lock_count": len(gate.get("kernel_locks") or []),
            "kernel_locks_digest": _digest(gate.get("kernel_locks") or []),
            "hold_evidence": gate.get("hold_evidence"),
            "provenance_matches_expected": gate.get(
                "provenance_matches_expected"
            ),
            "blockers": gate.get("blockers"),
        }
        result.append(
            _component(
                path=(
                    "/observations/target_activity/"
                    + str(gate_index)
                ),
                value=safe_gate,
                safe_evidence=safe_gate,
                cas_role="observation_only",
            )
        )
    return sorted(result, key=lambda item: item["json_path"])


def _material_cas_diff(
    expected_material: Mapping[str, Any],
    observed_material: Mapping[str, Any],
    *,
    expected_observations: Mapping[str, Any] | None = None,
    observed_observations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    before = {
        row["json_path"]: row
        for row in _material_cas_components(
            expected_material, expected_observations
        )
    }
    after = {
        row["json_path"]: row
        for row in _material_cas_components(
            observed_material, observed_observations
        )
    }
    changed = []
    observation_changes = []
    for path in sorted(set(before) | set(after)):
        earlier = before.get(path)
        later = after.get(path)
        if (earlier or {}).get("digest") == (later or {}).get("digest"):
            continue
        row = {
            "json_path": path,
            "classification": _cas_classification(path),
            "before_component_digest": (earlier or {}).get("digest"),
            "after_component_digest": (later or {}).get("digest"),
            "before_safe_evidence": (earlier or {}).get("safe_evidence"),
            "after_safe_evidence": (later or {}).get("safe_evidence"),
        }
        if (earlier or later or {}).get("cas_role") == "observation_only":
            observation_changes.append(row)
        else:
            changed.append(row)
    return {
        "schema": MATERIAL_CAS_DIFF_SCHEMA,
        "exact_immutable_match": _digest(expected_material)
        == _digest(observed_material),
        "before_material_digest": _digest(expected_material),
        "after_material_digest": _digest(observed_material),
        "changed_component_count": len(changed),
        "changed_json_paths": [row["json_path"] for row in changed],
        "components": changed,
        "observation_change_count": len(observation_changes),
        "observation_changes": observation_changes,
    }


def _mutable_safety_predicates(
    observations: Mapping[str, Any], *, minimum_backup_floor_bytes: int = 0
) -> dict[str, Any]:
    finance = observations.get("finance") or {}
    current_floor = int(finance.get("required_available_floor_bytes") or 0)
    effective_floor = max(current_floor, int(minimum_backup_floor_bytes))
    expected_targets = {
        str(policy["key"]): {
            "source_path": str(policy["source_path"]),
            "identity": dict(policy["expected_identity"]),
        }
        for policy in TARGET_POLICIES
    }
    expected_paths = {
        row["source_path"]: key for key, row in expected_targets.items()
    }
    target_policy_valid = bool(
        len(TARGET_POLICIES) == EXPECTED_SOURCE_COUNT
        and len(expected_targets) == EXPECTED_SOURCE_COUNT
        and len(expected_paths) == EXPECTED_SOURCE_COUNT
    )

    raw_capacity = observations.get("capacity_stages")
    capacity_stages = [
        dict(item) for item in raw_capacity or [] if isinstance(item, Mapping)
    ]
    capacity_counts = {key: 0 for key in expected_targets}
    capacity_issues: list[dict[str, Any]] = []
    if not isinstance(raw_capacity, list):
        capacity_issues.append({"code": "capacity_stage_inventory_malformed"})
    for index, item in enumerate(
        raw_capacity if isinstance(raw_capacity, list) else []
    ):
        if not isinstance(item, Mapping):
            capacity_issues.append(
                {"code": "capacity_stage_malformed", "sample_index": index}
            )
            continue
        key = str(item.get("key") or "")
        if key not in expected_targets:
            capacity_issues.append(
                {
                    "code": "foreign_capacity_target",
                    "sample_index": index,
                    "target_key": key or None,
                }
            )
            continue
        capacity_counts[key] += 1
        projected = item.get("projected_available_at_peak_bytes")
        if (
            not isinstance(projected, int)
            or isinstance(projected, bool)
            or projected < effective_floor
        ):
            capacity_issues.append(
                {
                    "code": "capacity_floor_not_preserved",
                    "sample_index": index,
                    "target_key": key,
                    "projected_available_at_peak_bytes": projected,
                }
            )
    for key, count in capacity_counts.items():
        if count == 0:
            capacity_issues.append(
                {"code": "missing_capacity_target", "target_key": key}
            )
        elif count > 1:
            capacity_issues.append(
                {
                    "code": "duplicate_capacity_target",
                    "target_key": key,
                    "sample_count": count,
                }
            )
    capacity_passed = bool(
        target_policy_valid and effective_floor > 0 and not capacity_issues
    )

    raw_activity = observations.get("activity_gates")
    activity = [
        dict(item) for item in raw_activity or [] if isinstance(item, Mapping)
    ]
    activity_counts = {key: 0 for key in expected_targets}
    activity_issues: list[dict[str, Any]] = []
    if not isinstance(raw_activity, list):
        activity_issues.append({"code": "target_activity_inventory_malformed"})
    for index, item in enumerate(
        raw_activity if isinstance(raw_activity, list) else []
    ):
        if not isinstance(item, Mapping):
            activity_issues.append(
                {"code": "target_activity_sample_malformed", "sample_index": index}
            )
            continue
        key = str(item.get("target_key") or "")
        source_path = str(item.get("source_path") or "")
        binding = expected_targets.get(key)
        if binding is None or source_path != binding["source_path"]:
            activity_issues.append(
                {
                    "code": "foreign_or_misbound_activity_target",
                    "sample_index": index,
                    "target_key": key or None,
                    "source_path": source_path or None,
                }
            )
            continue
        activity_counts[key] += 1
        expected_identity = item.get("expected_identity")
        identity_before = item.get("identity_before")
        identity_after = item.get("identity_after")
        literal_identity = binding["identity"]
        identity_fields = tuple(literal_identity)
        stat_fields = tuple(field for field in identity_fields if field != "sha256")
        sidecars = item.get("sidecars")
        sidecar_suffixes = {
            str(row.get("suffix") or "")
            for row in sidecars or []
            if isinstance(row, Mapping)
        }
        fd_openers = item.get("fd_openers")
        hold = item.get("hold_evidence")
        provenance = item.get("provenance")
        sample_reasons: list[str] = []
        if (
            not isinstance(expected_identity, Mapping)
            or any(
                expected_identity.get(field) != literal_identity[field]
                for field in identity_fields
            )
            or (
                "path" in expected_identity
                and expected_identity.get("path") != binding["source_path"]
            )
        ):
            sample_reasons.append("expected_identity_mismatch")
        if (
            not isinstance(identity_before, Mapping)
            or identity_before.get("path") != binding["source_path"]
            or any(
                identity_before.get(field) != literal_identity[field]
                for field in stat_fields
            )
            or (
                item.get("sha256_verified") is True
                and identity_before.get("sha256") != literal_identity["sha256"]
            )
        ):
            sample_reasons.append("source_identity_before_mismatch")
        if (
            not isinstance(identity_after, Mapping)
            or identity_after.get("path") != binding["source_path"]
            or any(
                identity_after.get(field) != literal_identity[field]
                for field in stat_fields
            )
        ):
            sample_reasons.append("source_identity_after_mismatch")
        if item.get("identity_matches_expected") is not True:
            sample_reasons.append("source_identity_not_accepted")
        if item.get("material_stable_during_gate") is not True:
            sample_reasons.append("source_material_not_stable")
        sha256_verified = item.get("sha256_verified")
        if sha256_verified is not True and sha256_verified is not False:
            sample_reasons.append("sha256_verification_state_unknown")
        elif (
            sha256_verified is True
            and item.get("sha256_matches_expected") is not True
        ):
            sample_reasons.append("source_sha256_mismatch")
        if (
            not isinstance(sidecars, list)
            or sidecar_suffixes != {"-wal", "-shm", "-journal"}
            or any(
                not isinstance(row, Mapping) or row.get("present") is not False
                for row in sidecars or []
            )
        ):
            sample_reasons.append("sidecar_inventory_not_clear")
        if not isinstance(fd_openers, list):
            sample_reasons.append("fd_opener_inventory_malformed")
        else:
            for opener in fd_openers:
                if (
                    not isinstance(opener, Mapping)
                    or opener.get("source_path") != binding["source_path"]
                    or opener.get("access_mode") != "read_only"
                    or opener.get("binds_source_device_inode") is not True
                    or opener.get("target_device") != literal_identity["device"]
                    or opener.get("target_inode") != literal_identity["inode"]
                ):
                    sample_reasons.append("unsafe_or_misbound_fd_opener")
                    break
        if not isinstance(item.get("kernel_locks"), list) or item.get("kernel_locks"):
            sample_reasons.append("kernel_lock_inventory_not_clear")
        if (
            not isinstance(hold, Mapping)
            or not isinstance(hold.get("marker_paths"), list)
            or hold.get("marker_paths")
            or not isinstance(hold.get("hold_xattr_names"), list)
            or hold.get("hold_xattr_names")
            or hold.get("protected_prefix_match") is not False
        ):
            sample_reasons.append("hold_inventory_not_clear")
        if (
            not isinstance(provenance, Mapping)
            or not SHA256_RE.fullmatch(str(provenance.get("digest") or ""))
            or not isinstance(provenance.get("records"), list)
            or item.get("provenance_error") is not None
            or item.get("provenance_matches_expected") is not True
        ):
            sample_reasons.append("provenance_not_accepted")
        if not isinstance(item.get("blockers"), list) or item.get("blockers"):
            sample_reasons.append("activity_blockers_present_or_malformed")
        if item.get("classification") not in {"clean", "clean_with_read_only_openers"}:
            sample_reasons.append("activity_classification_not_clear")
        if sample_reasons:
            activity_issues.append(
                {
                    "code": "unsafe_or_malformed_activity_sample",
                    "sample_index": index,
                    "target_key": key,
                    "source_path": source_path,
                    "classification": item.get("classification"),
                    "reasons": sorted(set(sample_reasons)),
                    "blockers": item.get("blockers"),
                }
            )
    for key, count in activity_counts.items():
        if count == 0:
            activity_issues.append(
                {
                    "code": "missing_activity_target",
                    "target_key": key,
                    "source_path": expected_targets[key]["source_path"],
                }
            )
    activity_passed = bool(
        target_policy_valid
        and isinstance(raw_activity, list)
        and len(raw_activity) >= EXPECTED_SOURCE_COUNT
        and not activity_issues
    )

    raw_lifecycle_locks = observations.get("lifecycle_locks")
    lifecycle_locks = [
        dict(item)
        for item in raw_lifecycle_locks or []
        if isinstance(item, Mapping)
    ]
    lifecycle_lock_counts = {name: 0 for name in OTHER_LIFECYCLE_LOCKS}
    lifecycle_lock_issues: list[dict[str, Any]] = []
    if not isinstance(raw_lifecycle_locks, list):
        lifecycle_lock_issues.append({"code": "lifecycle_lock_inventory_malformed"})
    for index, item in enumerate(
        raw_lifecycle_locks if isinstance(raw_lifecycle_locks, list) else []
    ):
        if not isinstance(item, Mapping):
            lifecycle_lock_issues.append(
                {"code": "lifecycle_lock_sample_malformed", "sample_index": index}
            )
            continue
        path = str(item.get("path") or "")
        name = Path(path).name if path else ""
        if name not in lifecycle_lock_counts:
            lifecycle_lock_issues.append(
                {
                    "code": "foreign_lifecycle_lock",
                    "sample_index": index,
                    "path": path or None,
                }
            )
            continue
        lifecycle_lock_counts[name] += 1
        if item.get("held_by_batch") is not True and item.get("locked") is True:
            lifecycle_lock_issues.append(
                {
                    "code": "lifecycle_lock_busy",
                    "sample_index": index,
                    "path": path,
                }
            )
    for name, count in lifecycle_lock_counts.items():
        if count == 0:
            lifecycle_lock_issues.append(
                {"code": "missing_lifecycle_lock", "lock_name": name}
            )
        elif count > 1:
            lifecycle_lock_issues.append(
                {
                    "code": "duplicate_lifecycle_lock",
                    "lock_name": name,
                    "sample_count": count,
                }
            )
    lifecycle_locks_passed = not lifecycle_lock_issues
    service_gate = observations.get("systemd_service_gate") or {}
    root_projected = int(observations.get("projected_root_available_bytes") or 0)
    predicates = {
        "finance_health_passed": finance.get("healthy") is True,
        "capacity_passed": capacity_passed,
        "service_health_passed": service_gate.get("healthy") is True,
        "target_activity_passed": activity_passed,
        "root_minimum_passed": root_projected >= ROOT_MINIMUM_AFTER_BYTES,
        "no_other_sanitation_job": not observations.get("active_sanitation_jobs"),
        "lifecycle_locks_passed": lifecycle_locks_passed,
        "journald_health_passed": isinstance(observations.get("journald"), Mapping),
    }
    return {
        "classification": (
            "mutable_live_predicates_satisfied"
            if all(predicates.values())
            else "mutable_live_predicates_blocked"
        ),
        "passed": all(predicates.values()),
        "predicates": predicates,
        "current_finance_required_floor_bytes": current_floor,
        "minimum_preserved_backup_floor_bytes": int(minimum_backup_floor_bytes),
        "effective_required_backup_floor_bytes": effective_floor,
        "minimum_projected_backup_available_bytes": (
            min(
                int(item.get("projected_available_at_peak_bytes") or 0)
                for item in capacity_stages
            )
            if capacity_stages
            else 0
        ),
        "projected_root_available_bytes": root_projected,
        "service_gate_classification": service_gate.get("classification"),
        "failing_units": service_gate.get("failing_units"),
        "failing_pairs": service_gate.get("failing_pairs"),
        "target_activity_coverage": {
            "expected_target_count": EXPECTED_SOURCE_COUNT,
            "observed_sample_count": (
                len(raw_activity) if isinstance(raw_activity, list) else 0
            ),
            "expected_target_keys": sorted(expected_targets),
            "expected_source_paths": sorted(expected_paths),
            "sample_counts_by_target": activity_counts,
            "issues": activity_issues,
        },
        "activity_blockers": activity_issues,
        "capacity_target_coverage": {
            "expected_target_count": EXPECTED_SOURCE_COUNT,
            "observed_stage_count": (
                len(raw_capacity) if isinstance(raw_capacity, list) else 0
            ),
            "stage_counts_by_target": capacity_counts,
            "issues": capacity_issues,
        },
        "lifecycle_lock_coverage": {
            "expected_lock_count": len(OTHER_LIFECYCLE_LOCKS),
            "observed_sample_count": (
                len(raw_lifecycle_locks)
                if isinstance(raw_lifecycle_locks, list)
                else 0
            ),
            "sample_counts_by_lock": lifecycle_lock_counts,
            "issues": lifecycle_lock_issues,
        },
        "lifecycle_lock_blockers": lifecycle_lock_issues,
        "finance": {
            key: finance.get(key)
            for key in (
                "status",
                "healthy",
                "blockers",
                "retained_backup_id",
                "retained_count",
                "retained_bytes",
                "next_replacement_required_bytes",
                "available_bytes",
            )
        },
    }


def _validate_evidence_scope(evidence_dir: Path, operation_id: str) -> Path:
    if not re.fullmatch(r"production-goal-v1-[0-9a-f]{32}", operation_id):
        raise WarmArchiveError("operation id is invalid")
    evidence_dir = evidence_dir.resolve()
    expected = PRODUCTION_GOAL_EVIDENCE_ROOT / operation_id
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


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> bool:
    """Publish immutable evidence without replacing an earlier failure."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.exclusive.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(_canonical_json_bytes(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _safe_guard_failure_diff(
    expected_material: Mapping[str, Any],
    exc: WarmArchiveError,
    *,
    expected_observations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = dict(exc.evidence)
    before_components = {
        row["json_path"]: row
        for row in _material_cas_components(
            expected_material, expected_observations
        )
    }
    source_path = str(evidence.get("source_path") or "")
    target_index = next(
        (
            index
            for index, item in enumerate(expected_material.get("targets") or [])
            if isinstance(item, Mapping)
            and str(item.get("source_path") or "") == source_path
        ),
        None,
    )
    target = (
        (expected_material.get("targets") or [])[target_index]
        if target_index is not None
        else None
    )
    all_blockers = [
        dict(item)
        for item in evidence.get("blockers") or []
        if isinstance(item, Mapping)
    ]
    blockers = all_blockers[:16]
    blocker_codes = sorted(str(item.get("code") or "") for item in all_blockers)
    if target is not None:
        field = (
            "sidecars"
            if "sqlite_sidecar_present" in blocker_codes
            else "hold_evidence"
            if "hold_evidence_present" in blocker_codes
            else "provenance"
            if "provenance_drift" in blocker_codes
            else "identity"
        )
        path = (
            f"/targets/{target_index}/{field}"
        )
        before_component = next(
            row
            for row in _material_cas_components(expected_material)
            if row["json_path"] == path
        )
        safe_after = {
            "source_path": source_path,
            "classification": evidence.get("classification"),
            "identity_before": _safe_identity(evidence.get("identity_before")),
            "identity_after": _safe_identity(evidence.get("identity_after")),
            "sidecars": evidence.get("sidecars"),
            "fd_openers": _safe_opener_evidence(evidence.get("fd_openers")),
            "kernel_lock_count": len(evidence.get("kernel_locks") or []),
            "kernel_locks_digest": _digest(evidence.get("kernel_locks") or []),
            "hold_evidence": evidence.get("hold_evidence"),
            "provenance_digest": (evidence.get("provenance") or {}).get(
                "digest"
            ),
            "provenance_error": evidence.get("provenance_error"),
            "blockers": blockers,
            "blocker_count": len(all_blockers),
            "blockers_truncated": len(all_blockers) > len(blockers),
        }
    elif isinstance(evidence.get("systemd_service_gate"), Mapping):
        path = "/observations/systemd_service_gate"
        before_component = before_components.get(
            path, {"digest": None, "safe_evidence": None}
        )
        gate = evidence["systemd_service_gate"]
        safe_after = {
            "classification": gate.get("classification"),
            "healthy": gate.get("healthy"),
            "failing_units": gate.get("failing_units"),
            "failing_pairs": gate.get("failing_pairs"),
        }
    elif evidence.get("classification") == "mutable_live_predicates_blocked":
        predicates = evidence.get("predicates") or {}
        path_by_predicate = {
            "finance_health_passed": "/observations/capacity",
            "capacity_passed": "/observations/capacity",
            "root_minimum_passed": "/observations/capacity",
            "service_health_passed": "/observations/systemd_service_gate",
            "target_activity_passed": "/observations/target_activity",
            "no_other_sanitation_job": "/observations/sanitation_jobs",
            "lifecycle_locks_passed": "/observations/lifecycle_locks",
            "journald_health_passed": "/observations/journald",
        }
        changed_paths = sorted(
            {
                path_by_predicate[name]
                for name, passed in predicates.items()
                if passed is not True and name in path_by_predicate
            }
        )
        safe_by_path = {
            "/observations/capacity": {
                key: evidence.get(key)
                for key in (
                    "current_finance_required_floor_bytes",
                    "minimum_preserved_backup_floor_bytes",
                    "effective_required_backup_floor_bytes",
                    "minimum_projected_backup_available_bytes",
                    "projected_root_available_bytes",
                    "finance",
                )
            },
            "/observations/systemd_service_gate": {
                "service_gate_classification": evidence.get(
                    "service_gate_classification"
                ),
                "failing_units": evidence.get("failing_units"),
                "failing_pairs": evidence.get("failing_pairs"),
            },
            "/observations/target_activity": {
                "activity_blockers": evidence.get("activity_blockers")
            },
            "/observations/sanitation_jobs": {
                "predicate": predicates.get("no_other_sanitation_job")
            },
            "/observations/lifecycle_locks": {
                "lifecycle_lock_blockers": evidence.get(
                    "lifecycle_lock_blockers"
                )
            },
            "/observations/journald": {
                "predicate": predicates.get("journald_health_passed")
            },
        }
        components = [
            {
                "json_path": changed_path,
                "classification": _cas_classification(changed_path),
                "before_component_digest": (
                    before_components.get(changed_path) or {}
                ).get("digest"),
                "after_component_digest": _digest(safe_by_path[changed_path]),
                "before_safe_evidence": (
                    before_components.get(changed_path) or {}
                ).get("safe_evidence"),
                "after_safe_evidence": safe_by_path[changed_path],
            }
            for changed_path in changed_paths
        ]
        return {
            "schema": MATERIAL_CAS_DIFF_SCHEMA,
            "exact_immutable_match": True,
            "before_material_digest": _digest(expected_material),
            "after_material_digest": _digest(expected_material),
            "changed_component_count": 0,
            "changed_json_paths": [],
            "components": [],
            "observation_change_count": len(components),
            "observation_changes": components,
            "blocked_observation_json_paths": changed_paths,
            "collection_error": {
                "type": type(exc).__name__,
                "message": str(exc)[:500],
            },
        }
    elif evidence.get("key") or evidence.get("path"):
        key = evidence.get("key") or evidence.get("path")
        topology_rows = expected_material.get("mutable_canonical_topology") or []
        topology_index = next(
            (
                index
                for index, row in enumerate(topology_rows)
                if isinstance(row, Mapping)
                and (row.get("key") == key or (row.get("topology") or {}).get("path") == key)
            ),
            None,
        )
        path = (
            "/mutable_canonical_topology/" + str(topology_index)
            if topology_index is not None
            else "/mutable_canonical_topology/@unknown"
        )
        before_component = next(
            (
                row
                for row in _material_cas_components(expected_material)
                if row["json_path"] == path
            ),
            {"digest": None, "safe_evidence": None},
        )
        safe_after = {
            "key": evidence.get("key"),
            "path": evidence.get("path"),
            "openers": evidence.get("openers"),
        }
    else:
        path = "/material_collection"
        before_component = {
            "digest": _digest(expected_material),
            "safe_evidence": None,
        }
        safe_after = {
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
            "evidence_digest": _digest(evidence),
        }
    component = {
        "json_path": path,
        "classification": _cas_classification(path),
        "before_component_digest": before_component.get("digest"),
        "after_component_digest": _digest(safe_after),
        "before_safe_evidence": before_component.get("safe_evidence"),
        "after_safe_evidence": safe_after,
    }
    return {
        "schema": MATERIAL_CAS_DIFF_SCHEMA,
        "exact_immutable_match": False,
        "before_material_digest": _digest(expected_material),
        "after_material_digest": None,
        "changed_component_count": 1,
        "changed_json_paths": [path],
        "components": [component],
        "observation_change_count": 0,
        "observation_changes": [],
        "collection_error": {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        },
    }


def _persist_material_cas_failure(
    *,
    evidence_dir: Path,
    phase: str,
    readiness_id: str,
    operation_id: str,
    job_id: str,
    deployed_sha: str,
    manifest_path: Path | None,
    manifest_sha256: str,
    component_diff: Mapping[str, Any],
) -> dict[str, Any]:
    path = evidence_dir / MATERIAL_CAS_FAILURE_FILENAME
    payload = {
        "schema": MATERIAL_CAS_FAILURE_SCHEMA,
        "status": "blocked",
        "phase": phase,
        "readiness_id": readiness_id,
        "operation_id": operation_id,
        "job": {
            "job_id": job_id or None,
            "state": "bound_worker" if job_id else "not_created_pre_submit",
        },
        "deployed_sha": deployed_sha,
        "manifest_path": str(manifest_path) if manifest_path else None,
        "manifest_sha256": manifest_sha256 or None,
        "mutation_journal_created": False,
        "archive_mutation_started": False,
        "component_diff": dict(component_diff),
        "created_at": _now(),
    }
    created = _write_json_exclusive(path, payload)
    if not created:
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise WarmArchiveError("immutable material CAS failure evidence is unsafe")
        existing = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(existing, Mapping)
            or existing.get("schema") != MATERIAL_CAS_FAILURE_SCHEMA
            or existing.get("operation_id") != operation_id
            or existing.get("readiness_id") != readiness_id
            or existing.get("deployed_sha") != deployed_sha
        ):
            raise WarmArchiveError(
                "immutable material CAS failure evidence binding drifted"
            )
        payload = dict(existing)
    return {
        "artifact_path": str(path),
        "artifact_sha256": _sha256_file(path),
        "artifact_created": created,
        "original_failure_preserved": not created,
        "component_diff": payload["component_diff"],
        "mutation_journal_created": False,
        "archive_mutation_started": False,
    }


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
        READINESS_EVIDENCE_ROOT / readiness_id
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

    initial_systemd_service_gate = _systemd_service_gate_with_resample()
    if not initial_systemd_service_gate["healthy"]:
        result = {
            "contract_name": CONTRACT_NAME,
            "status": "blocked",
            "query_only": True,
            "database_written": False,
            "readiness_id": readiness_id,
            "deployed_sha": deployed_sha,
            "reason": "required_systemd_service_gate_blocked",
            "initial_systemd_service_gate": initial_systemd_service_gate,
            "systemd_service_gate": initial_systemd_service_gate,
            "callback": [
                {
                    "message": "required production service/timer health is not ready",
                    "classification": initial_systemd_service_gate["classification"],
                    "systemd_service_gate": initial_systemd_service_gate,
                }
            ],
            "completed_at": _now(),
        }
        _atomic_write_json(receipt_path, result)
        return result

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
            "initial_systemd_service_gate": initial_systemd_service_gate,
            "systemd_service_gate": initial_systemd_service_gate,
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
            "initial_systemd_service_gate": initial_systemd_service_gate,
            "systemd_service_gate": exc.evidence.get(
                "systemd_service_gate", initial_systemd_service_gate
            ),
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
    full_mutable_predicates = _mutable_safety_predicates(full_observations)
    if full_mutable_predicates["passed"] is not True:
        result = {
            "contract_name": CONTRACT_NAME,
            "status": "blocked",
            "query_only": True,
            "database_written": False,
            "readiness_id": readiness_id,
            "deployed_sha": deployed_sha,
            "reason": "mutable_live_predicates_blocked_before_projection",
            "initial_systemd_service_gate": initial_systemd_service_gate,
            "systemd_service_gate": full_observations[
                "systemd_service_gate"
            ],
            "pre_projection_stabilization": pre_stabilization,
            "mutable_safety_predicates": full_mutable_predicates,
            "callback": [full_mutable_predicates],
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
        "initial_systemd_service_gate": initial_systemd_service_gate,
        "material": material,
        "material_qualification_digest": _digest(material),
        "immutable_non_target_digest": material["immutable_non_target_digest"],
        "mutable_canonical_topology_digest": material[
            "mutable_canonical_topology_digest"
        ],
        "mutable_canonical_observations": full_observations["non_target"][
            "mutable_canonical"
        ]["observation_rows"],
        "mutable_safety_predicates": full_mutable_predicates,
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
    final_mutable_predicates = None
    final_error = None
    if stabilization["status"] == "clean":
        try:
            final_material, final_observations = _material_snapshot(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                reusable_material=material,
                witness_name="readiness_final_capacity_and_material_cas",
            )
            final_mutable_predicates = _mutable_safety_predicates(
                final_observations,
                minimum_backup_floor_bytes=int(
                    full_mutable_predicates[
                        "effective_required_backup_floor_bytes"
                    ]
                ),
            )
        except WarmArchiveError as exc:
            final_error = {"message": str(exc), "evidence": exc.evidence}
    ready = bool(
        stabilization["status"] == "clean"
        and final_error is None
        and final_material is not None
        and final_mutable_predicates is not None
        and final_mutable_predicates["passed"] is True
        and _digest(final_material) == _digest(material)
    )
    final_component_diff = (
        _material_cas_diff(
            material,
            final_material,
            expected_observations=full_observations,
            observed_observations=final_observations,
        )
        if final_material is not None
        else None
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
        "material_partition": material["material_partition"],
        "material_cas_components": _material_cas_components(
            material, full_observations
        ),
        "immutable_non_target_digest": material["immutable_non_target_digest"],
        "mutable_canonical_topology_digest": material[
            "mutable_canonical_topology_digest"
        ],
        "mutable_canonical_observations": (
            final_observations["non_target"]["mutable_canonical"][
                "observation_rows"
            ]
            if final_observations
            else []
        ),
        "expected_reclaimed_allocated_bytes": material[
            "expected_reclaimed_allocated_bytes"
        ],
        "required_backup_floor_bytes": (
            final_mutable_predicates[
                "effective_required_backup_floor_bytes"
            ]
            if final_mutable_predicates
            else full_mutable_predicates[
                "effective_required_backup_floor_bytes"
            ]
        ),
        "root_minimum_after_bytes": ROOT_MINIMUM_AFTER_BYTES,
        "initial_systemd_service_gate": initial_systemd_service_gate,
        "systemd_service_gate": (
            final_observations.get("systemd_service_gate")
            if final_observations
            else (final_error or {}).get("evidence", {}).get(
                "systemd_service_gate", initial_systemd_service_gate
            )
        ),
        "capacity_guard_passed": bool(
            final_mutable_predicates
            and final_mutable_predicates["predicates"]["capacity_passed"] is True
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
        "immutable_material_diff": final_component_diff,
        "component_diff": final_component_diff,
        "mutable_safety_predicates": final_mutable_predicates,
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
    expected_root = READINESS_EVIDENCE_ROOT
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
        or payload.get("immutable_non_target_digest")
        != (payload.get("material") or {}).get("immutable_non_target_digest")
        or payload.get("mutable_canonical_topology_digest")
        != (payload.get("material") or {}).get(
            "mutable_canonical_topology_digest"
        )
        or (payload.get("material") or {}).get("material_partition")
        != "immutable_safety_v1"
        or (payload.get("mutable_safety_predicates") or {}).get("passed")
        is not True
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
    prior_failure_path = evidence_dir / MATERIAL_CAS_FAILURE_FILENAME
    if prior_failure_path.exists():
        if (
            prior_failure_path.is_symlink()
            or not prior_failure_path.is_file()
            or stat.S_IMODE(prior_failure_path.stat().st_mode) != 0o600
        ):
            raise WarmArchiveError("prior immutable material CAS evidence is unsafe")
        prior_failure = json.loads(prior_failure_path.read_text(encoding="utf-8"))
        if (
            not isinstance(prior_failure, Mapping)
            or prior_failure.get("schema") != MATERIAL_CAS_FAILURE_SCHEMA
            or prior_failure.get("operation_id") != operation_id
            or prior_failure.get("deployed_sha") != deployed_sha
        ):
            raise WarmArchiveError("prior immutable material CAS evidence drifted")
        raise WarmArchiveError(
            "operation already terminalized by immutable material CAS evidence",
            evidence={
                "artifact_path": str(prior_failure_path),
                "artifact_sha256": _sha256_file(prior_failure_path),
                "original_failure_preserved": True,
                "component_diff": prior_failure.get("component_diff"),
                "mutation_journal_created": False,
                "archive_mutation_started": False,
            },
        )
    projection = _load_readiness_projection(
        projection_path=projection_manifest,
        projection_sha256=projection_manifest_sha256,
        deployed_sha=deployed_sha,
    )
    try:
        material, observations = _material_snapshot(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            reusable_material=projection["material"],
            witness_name="jit_lightweight_material_qualification",
        )
    except WarmArchiveError as exc:
        component_diff = _safe_guard_failure_diff(
            projection["material"],
            exc,
            expected_observations=projection.get("observations"),
        )
        persisted = _persist_material_cas_failure(
            evidence_dir=evidence_dir,
            phase="jit_material_collection",
            readiness_id=str(projection["readiness_id"]),
            operation_id=operation_id,
            job_id="",
            deployed_sha=deployed_sha,
            manifest_path=None,
            manifest_sha256="",
            component_diff=component_diff,
        )
        raise WarmArchiveError(
            "JIT immutable material or live predicate collection blocked",
            evidence={
                **persisted,
                "original_error": {
                    "type": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            },
        ) from exc
    material_digest = _digest(material)
    component_diff = _material_cas_diff(
        projection["material"],
        material,
        expected_observations=projection.get("observations"),
        observed_observations=observations,
    )
    if component_diff["exact_immutable_match"] is not True:
        persisted = _persist_material_cas_failure(
            evidence_dir=evidence_dir,
            phase="jit_immutable_material_cas",
            readiness_id=str(projection["readiness_id"]),
            operation_id=operation_id,
            job_id="",
            deployed_sha=deployed_sha,
            manifest_path=None,
            manifest_sha256="",
            component_diff=component_diff,
        )
        raise WarmArchiveError(
            "JIT immutable material CAS drifted from readiness",
            evidence=persisted,
        )
    readiness_floor = int(
        (projection.get("mutable_safety_predicates") or {}).get(
            "effective_required_backup_floor_bytes"
        )
        or 0
    )
    mutable_predicates = _mutable_safety_predicates(
        observations,
        minimum_backup_floor_bytes=readiness_floor,
    )
    if mutable_predicates["passed"] is not True:
        predicate_error = WarmArchiveError(
            "JIT mutable live predicates blocked",
            evidence=mutable_predicates,
        )
        persisted = _persist_material_cas_failure(
            evidence_dir=evidence_dir,
            phase="jit_mutable_live_predicates",
            readiness_id=str(projection["readiness_id"]),
            operation_id=operation_id,
            job_id="",
            deployed_sha=deployed_sha,
            manifest_path=None,
            manifest_sha256="",
            component_diff=_safe_guard_failure_diff(
                projection["material"],
                predicate_error,
                expected_observations=projection.get("observations"),
            ),
        )
        raise WarmArchiveError(
            "JIT mutable live predicates blocked", evidence=persisted
        )
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
        "mutable_safety_predicates": mutable_predicates,
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
        "finance_next_replacement_required_bytes": observations["finance"]["next_replacement_required_bytes"],
        "emergency_reserve_bytes": EMERGENCY_RESERVE_BYTES,
        "required_backup_floor_bytes": mutable_predicates[
            "effective_required_backup_floor_bytes"
        ],
        "minimum_projected_backup_available_bytes": min(int(item["projected_available_at_peak_bytes"]) for item in stages),
        "capacity_guard_passed": mutable_predicates["predicates"][
            "capacity_passed"
        ],
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
        "immutable_non_target_digest": material["immutable_non_target_digest"],
        "mutable_canonical_topology_digest": material[
            "mutable_canonical_topology_digest"
        ],
        "mutable_canonical_observations": observations["non_target"][
            "mutable_canonical"
        ]["observation_rows"],
        "readiness_id": projection["readiness_id"],
        "projection_manifest_path": str(projection_manifest),
        "projection_manifest_sha256": projection_manifest_sha256,
        "activity_evidence": observations["activity_gates"],
        "root_policy_sha256": material["root_policy"]["policy_sha256"],
        "material_partition": material["material_partition"],
        "material_cas_components": _material_cas_components(
            material, observations
        ),
        "mutable_safety_predicates": mutable_predicates,
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
    projection_path_valid = False
    if isinstance(projection, Mapping):
        try:
            projection_relative = Path(str(projection.get("path") or "")).relative_to(
                READINESS_EVIDENCE_ROOT
            )
        except ValueError:
            projection_relative = Path()
        projection_path_valid = bool(
            len(projection_relative.parts) == 2
            and READINESS_ID_RE.fullmatch(projection_relative.parts[0]) is not None
            and re.fullmatch(
                r"root-warm-archive-readiness-projection-[0-9]{8}T[0-9]{6}Z\.json",
                projection_relative.parts[1],
            )
            is not None
        )
    if (
        not isinstance(payload, dict)
        or payload.get("contract_name") != CONTRACT_NAME
        or payload.get("status") != "ready"
        or payload.get("operation_id") != operation_id
        or (payload.get("material") or {}).get("material_partition")
        != "immutable_safety_v1"
        or payload.get("material_qualification_digest") != _digest(payload.get("material"))
        or int((payload.get("material") or {}).get("source_count") or 0) != EXPECTED_SOURCE_COUNT
        or not isinstance(projection, Mapping)
        or READINESS_ID_RE.fullmatch(str(projection.get("readiness_id") or "")) is None
        or not projection_path_valid
        or not SHA256_RE.fullmatch(str(projection.get("sha256") or ""))
        or projection.get("material_qualification_digest")
        != payload.get("material_qualification_digest")
        or (payload.get("mutable_safety_predicates") or {}).get("passed")
        is not True
    ):
        raise WarmArchiveError("manifest contract/material binding is invalid")
    return payload


class _exclusive_finance_lock:
    def __init__(self, runtime_dir: Path):
        self.path = runtime_dir / FINANCE_STORAGE_LOCK_FILENAME
        self.handle: Any = None

    def __enter__(self) -> Any:
        if self.path.is_symlink():
            raise WarmArchiveError("Finance storage lock is a symlink")
        try:
            self.handle = self.path.open("a+b")
            os.chmod(self.path, 0o600)
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            if self.handle is not None:
                self.handle.close()
            raise WarmArchiveError(
                "Finance storage operation/reservation is active"
            ) from exc
        except BaseException:
            if self.handle is not None and not self.handle.closed:
                self.handle.close()
            raise
        return self.handle

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        assert self.handle is not None
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
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
                handle: Any = None
                try:
                    handle = path.open("a+b")
                    os.chmod(path, 0o600)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    if handle is not None:
                        handle.close()
                    raise WarmArchiveError(
                        "another storage lifecycle operation is active"
                    ) from exc
                except BaseException:
                    if handle is not None and not handle.closed:
                        handle.close()
                    raise
                self.handles.append(handle)
        except BaseException:
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
    *,
    runtime_dir: Path,
    archive_bytes: int,
    restore_bytes: int,
    minimum_floor_bytes: int = 0,
) -> dict[str, Any]:
    finance = _finance_snapshot(runtime_dir)
    capacity = _filesystem(DESTINATION_ROOT, filesystem_role="backup")
    required_floor = max(
        int(finance["required_available_floor_bytes"]),
        int(minimum_floor_bytes),
    )
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


def _assert_exact_source_unlink_authority(
    source: Path, target: Mapping[str, Any]
) -> None:
    """Keep the destructive primitive structurally bound to one literal target."""

    policy = next(
        (item for item in TARGET_POLICIES if item["key"] == target.get("key")),
        None,
    )
    if (
        policy is None
        or str(source) != str(policy["source_path"])
        or str(target.get("source_path") or "") != str(policy["source_path"])
        or str(source) in {
            str(item.get("path") or "")
            for item in target.get("sidecars") or []
        }
    ):
        raise WarmArchiveError("source unlink escaped the literal six-path authority")


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
    created = False
    if destination.exists():
        if not destination.is_dir():
            raise WarmArchiveError("destination family is not a directory")
    else:
        destination.mkdir(mode=0o700)
        created = True
    if destination.resolve().parent != DESTINATION_ROOT.resolve():
        raise WarmArchiveError("destination family escaped the backup mount")
    value = destination.lstat()
    if (
        stat.S_IMODE(value.st_mode) != 0o700
        or int(value.st_uid) != 0
        or int(value.st_gid) != 0
    ):
        raise WarmArchiveError("destination family ownership/mode is unsafe")
    if created:
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


def _remove_owned_operation_temp(
    path: Path,
    *,
    item_state: Mapping[str, Any],
    allowed_phases: set[str],
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    phase = str(item_state.get("phase") or "")
    if (
        phase not in allowed_phases
        or path.is_symlink()
        or not path.is_file()
    ):
        raise WarmArchiveError(f"unknown/unowned destination temp blocks apply: {path}")
    value = path.lstat()
    if (
        stat.S_IMODE(value.st_mode) != 0o600
        or int(value.st_uid) != 0
        or int(value.st_gid) != 0
        or int(value.st_nlink) != 1
    ):
        raise WarmArchiveError(f"owned destination temp identity is unsafe: {path}")
    path.unlink()
    _fsync_directory(path.parent)


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
    if any(
        path.is_symlink()
        for path in (archive, manifest_path, temp_archive, temp_manifest, restore_temp)
    ):
        raise WarmArchiveError("destination archive/control path is a symlink")
    _remove_owned_operation_temp(
        restore_temp,
        item_state=item_state,
        allowed_phases={
            "archive_prechecked",
            "archive_verified_pending_publish",
            "pending_unlink",
            "unlink_done",
        },
    )
    if temp_archive.exists():
        if archive.exists() or manifest_path.exists():
            raise WarmArchiveError("owned archive temp coexists with a published pair")
        _remove_owned_operation_temp(
            temp_archive,
            item_state=item_state,
            allowed_phases={"archive_prechecked"},
        )
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
        _remove_owned_operation_temp(
            temp_manifest,
            item_state=item_state,
            allowed_phases={"archive_verified_pending_publish"},
        )
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
        minimum_floor_bytes=int(
            (journal.get("mutable_safety_predicates_before") or {}).get(
                "effective_required_backup_floor_bytes"
            )
            or 0
        ),
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
    non_target_before = _non_target_snapshot(
        runtime_dir,
        require_targets=False,
        expected_non_target=journal["non_target_before"],
    )
    non_target_before_reconciliation = _reconcile_non_target(
        journal["non_target_before"],
        non_target_before,
        phase=f"{target['key']}:before_archive",
    )
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
    non_target_pre_unlink = _non_target_snapshot(
        runtime_dir,
        require_targets=False,
        expected_non_target=journal["non_target_before"],
    )
    non_target_pre_unlink_reconciliation = _reconcile_non_target(
        journal["non_target_before"],
        non_target_pre_unlink,
        phase=f"{target['key']}:exact_pre_unlink",
    )
    capacity_pre_unlink = _capacity_guard(
        runtime_dir=runtime_dir,
        archive_bytes=0,
        restore_bytes=0,
        minimum_floor_bytes=int(
            (journal.get("mutable_safety_predicates_before") or {}).get(
                "effective_required_backup_floor_bytes"
            )
            or 0
        ),
    )
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
        "non_target_reconciliation_before_archive": non_target_before_reconciliation,
        "non_target_reconciliation_pre_unlink": non_target_pre_unlink_reconciliation,
    }
    journal["items"][index - 1] = journal_item
    journal["updated_at"] = _now()
    _atomic_write_json(journal_path, journal)
    _assert_exact_source_unlink_authority(source, target)
    source.unlink()
    _fsync_directory(source.parent)
    if source.exists():
        raise WarmArchiveError("source remains after the single unlink")
    capacity_after = _filesystem_snapshot(runtime_dir, Path("/opt/wb-core-runtime/backups"))
    non_target_after = _non_target_snapshot(
        runtime_dir,
        require_targets=False,
        expected_non_target=journal["non_target_before"],
    )
    non_target_after_reconciliation = _reconcile_non_target(
        journal["non_target_before"],
        non_target_after,
        phase=f"{target['key']}:after_unlink",
    )
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
        "non_target_reconciliation_after": non_target_after_reconciliation,
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
    material_failure_path = evidence_dir / MATERIAL_CAS_FAILURE_FILENAME
    if material_failure_path.exists() and not journal_path.exists():
        if (
            material_failure_path.is_symlink()
            or not material_failure_path.is_file()
            or stat.S_IMODE(material_failure_path.stat().st_mode) != 0o600
        ):
            raise WarmArchiveError("immutable material CAS failure evidence is unsafe")
        prior_failure = json.loads(
            material_failure_path.read_text(encoding="utf-8")
        )
        if (
            not isinstance(prior_failure, Mapping)
            or prior_failure.get("schema") != MATERIAL_CAS_FAILURE_SCHEMA
            or prior_failure.get("operation_id") != operation_id
            or prior_failure.get("readiness_id")
            != projection_binding["readiness_id"]
            or prior_failure.get("deployed_sha") != deployed_sha
        ):
            raise WarmArchiveError("immutable material CAS failure binding drifted")
        raise WarmArchiveError(
            "operation already terminalized before mutation journal creation",
            evidence={
                "artifact_path": str(material_failure_path),
                "artifact_sha256": _sha256_file(material_failure_path),
                "original_failure_preserved": True,
                "component_diff": prior_failure.get("component_diff"),
                "mutation_journal_created": False,
                "archive_mutation_started": False,
            },
        )
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
        non_target_now = _non_target_snapshot(
            runtime_dir,
            require_targets=False,
            operation_id=operation_id,
            expected_non_target=journal["non_target_before"],
        )
        crash_resume_non_target_reconciliation = _reconcile_non_target(
            journal["non_target_before"],
            non_target_now,
            phase="crash_resume",
        )
        services_now = _systemd_snapshot()
        services_gate_now = _systemd_service_gate_with_resample(services_now)
        journald_now = _journald_snapshot()
        if (
            not crash_resume_non_target_reconciliation["immutable_preserved"]
            or not crash_resume_non_target_reconciliation[
                "mutable_canonical_topology_preserved"
            ]
            or not services_gate_now["healthy"]
            or journald_now["service"].get("MainPID")
            != journal["journald_before"]["service"].get("MainPID")
            or journald_now["effective"]["values"]
            != journal["journald_before"]["effective"]["values"]
            or finance_now.get("healthy") is not True
            or int(filesystems_now["backup"]["available_bytes"])
            < max(
                int(finance_now["required_available_floor_bytes"]),
                int(
                    (
                        journal.get("mutable_safety_predicates_before") or {}
                    ).get("effective_required_backup_floor_bytes")
                    or 0
                ),
            )
        ):
            raise WarmArchiveError("crash-resume environment reconciliation failed")
    else:
        try:
            fresh_material, fresh_observations = _material_snapshot(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                own_job_id=own_job_id,
                lifecycle_locks_held=True,
                reusable_material=manifest["material"],
                witness_name="mutation_start_lightweight_material_cas",
            )
        except WarmArchiveError as exc:
            persisted = _persist_material_cas_failure(
                evidence_dir=evidence_dir,
                phase="mutation_start_material_collection",
                readiness_id=str(projection_binding["readiness_id"]),
                operation_id=operation_id,
                job_id=own_job_id,
                deployed_sha=deployed_sha,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                component_diff=_safe_guard_failure_diff(
                    manifest["material"],
                    exc,
                    expected_observations=manifest.get("observations"),
                ),
            )
            raise WarmArchiveError(
                "mutation-start immutable material or live predicate collection blocked",
                evidence={
                    **persisted,
                    "original_error": {
                        "type": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                },
            ) from exc
        component_diff = _material_cas_diff(
            manifest["material"],
            fresh_material,
            expected_observations=manifest.get("observations"),
            observed_observations=fresh_observations,
        )
        if component_diff["exact_immutable_match"] is not True:
            persisted = _persist_material_cas_failure(
                evidence_dir=evidence_dir,
                phase="mutation_start_immutable_material_cas",
                readiness_id=str(projection_binding["readiness_id"]),
                operation_id=operation_id,
                job_id=own_job_id,
                deployed_sha=deployed_sha,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                component_diff=component_diff,
            )
            raise WarmArchiveError(
                "immutable material CAS drifted after qualification",
                evidence=persisted,
            )
        prior_floor = int(
            (manifest.get("mutable_safety_predicates") or {}).get(
                "effective_required_backup_floor_bytes"
            )
            or 0
        )
        mutable_predicates = _mutable_safety_predicates(
            fresh_observations,
            minimum_backup_floor_bytes=prior_floor,
        )
        if mutable_predicates["passed"] is not True:
            predicate_error = WarmArchiveError(
                "mutation-start mutable live predicates blocked",
                evidence=mutable_predicates,
            )
            persisted = _persist_material_cas_failure(
                evidence_dir=evidence_dir,
                phase="mutation_start_mutable_live_predicates",
                readiness_id=str(projection_binding["readiness_id"]),
                operation_id=operation_id,
                job_id=own_job_id,
                deployed_sha=deployed_sha,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                component_diff=_safe_guard_failure_diff(
                    manifest["material"],
                    predicate_error,
                    expected_observations=manifest.get("observations"),
                ),
            )
            raise WarmArchiveError(
                "mutation-start mutable live predicates blocked",
                evidence=persisted,
            )
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
            "systemd_service_gate_before": fresh_observations[
                "systemd_service_gate"
            ],
            "mutable_safety_predicates_before": mutable_predicates,
            "activity_evidence_before": fresh_observations["activity_gates"],
            "non_target_before": fresh_observations["non_target"],
            "immutable_non_target_digest_before": fresh_material[
                "immutable_non_target_digest"
            ],
            "mutable_canonical_topology_digest_before": fresh_material[
                "mutable_canonical_topology_digest"
            ],
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
    non_target_after = _non_target_snapshot(
        runtime_dir,
        require_targets=False,
        expected_non_target=journal["non_target_before"],
    )
    terminal_non_target_reconciliation = _reconcile_non_target(
        journal["non_target_before"],
        non_target_after,
        phase="terminal",
    )
    services_after = _systemd_snapshot()
    services_gate_after = _systemd_service_gate_with_resample(services_after)
    journald_after = _journald_snapshot()
    journal_reconciliation = _reconcile_correction_journal_inventory(
        journal["journald_before"]["inventory"], journald_after["inventory"]
    )
    mutation_scope_reconciliation = _mutation_scope_reconciliation(journal)
    if (
        not terminal_non_target_reconciliation["immutable_preserved"]
        or not terminal_non_target_reconciliation[
            "mutable_canonical_topology_preserved"
        ]
        or not services_gate_after["healthy"]
        or journald_after["service"].get("MainPID")
        != journal["journald_before"]["service"].get("MainPID")
        or journald_after["effective"]["values"]
        != journal["journald_before"]["effective"]["values"]
        or journal_reconciliation["deleted_count"] != 0
        or journal_reconciliation["protected_drift"]
        or mutation_scope_reconciliation["exact"] is not True
        or int(filesystems_after["root"]["available_bytes"]) < ROOT_MINIMUM_AFTER_BYTES
        or finance_after.get("healthy") is not True
        or int(filesystems_after["backup"]["available_bytes"])
        < max(
            int(finance_after["required_available_floor_bytes"]),
            int(
                (journal.get("mutable_safety_predicates_before") or {}).get(
                    "effective_required_backup_floor_bytes"
                )
                or 0
            ),
        )
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
        "services_after": services_after,
        "systemd_service_gate_after": services_gate_after,
        "finance_after": finance_after,
        "monitor": monitor,
        "journald_after": journald_after,
        "journald_reconciliation": journal_reconciliation,
        "mutation_scope_reconciliation": mutation_scope_reconciliation,
        "terminal_non_target_reconciliation": terminal_non_target_reconciliation,
        "immutable_non_target_digest_after": non_target_after["immutable_digest"],
        "mutable_canonical_topology_digest_after": non_target_after[
            "mutable_canonical_topology_digest"
        ],
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
    non_target = _non_target_snapshot(
        runtime_dir,
        require_targets=False,
        expected_non_target=(
            journal.get("non_target_before") if isinstance(journal, Mapping) else None
        ),
    )
    services = _systemd_snapshot()
    systemd_service_gate = _systemd_service_gate_with_resample(services)
    journald = _journald_snapshot()
    root_readback = read_root_storage_status_artifact(policy=load_policy())
    journald_reconciliation = (
        _reconcile_correction_journal_inventory(journal["journald_before"]["inventory"], journald["inventory"])
        if journal
        else {"deleted_count": -1, "protected_drift": ["missing_journal"]}
    )
    raw_unlink_count = sum(int(item.get("unlink_count") or 0) for item in (journal or {}).get("items", []))
    reclaimed = sum(int(item.get("reclaimed_allocated_bytes") or 0) for item in (journal or {}).get("items", []))
    mutation_scope_reconciliation = (
        _mutation_scope_reconciliation(journal) if journal else None
    )
    try:
        non_target_reconciliation = (
            _reconcile_non_target(
                journal["non_target_before"],
                non_target,
                phase="query_only_terminal_readback",
            )
            if journal
            else None
        )
    except WarmArchiveError as exc:
        non_target_reconciliation = {
            "phase": "query_only_terminal_readback",
            "immutable_preserved": False,
            "mutable_canonical_topology_preserved": False,
            "error": str(exc),
            "evidence": exc.evidence,
        }
    non_target_preserved = bool(
        journal
        and non_target_reconciliation
        and non_target_reconciliation.get("immutable_preserved") is True
        and non_target_reconciliation.get("mutable_canonical_topology_preserved")
        is True
        and journald_reconciliation.get("deleted_count") == 0
        and not journald_reconciliation.get("protected_drift")
    )
    effective_terminal_floor = max(
        int(finance["required_available_floor_bytes"]),
        int(
            ((journal or {}).get("mutable_safety_predicates_before") or {}).get(
                "effective_required_backup_floor_bytes"
            )
            or 0
        ),
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
        and mutation_scope_reconciliation
        and mutation_scope_reconciliation.get("exact") is True
        and int(filesystems["root"]["available_bytes"]) >= ROOT_MINIMUM_AFTER_BYTES
        and finance.get("healthy") is True
        and int(filesystems["backup"]["available_bytes"])
        >= effective_terminal_floor
        and systemd_service_gate["healthy"]
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
        "backup_capacity_guard_passed": bool(
            finance.get("healthy") is True
            and int(filesystems["backup"]["available_bytes"])
            >= effective_terminal_floor
        ),
        "effective_required_backup_floor_bytes": effective_terminal_floor,
        "root_minimum_passed": int(filesystems["root"]["available_bytes"]) >= ROOT_MINIMUM_AFTER_BYTES,
        "root_monitor": root_readback,
        "services": services,
        "systemd_service_gate": systemd_service_gate,
        "services_healthy": systemd_service_gate["healthy"],
        "journald": journald,
        "journald_reconciliation": journald_reconciliation,
        "immutable_non_target_digest": non_target["immutable_digest"],
        "mutable_canonical_topology_digest": non_target[
            "mutable_canonical_topology_digest"
        ],
        "non_target_reconciliation": non_target_reconciliation,
        "non_target_preserved": non_target_preserved,
        "mutation_scope_reconciliation": mutation_scope_reconciliation,
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
