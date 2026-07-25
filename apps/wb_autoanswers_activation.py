#!/usr/bin/env python3
"""Fail-closed lifecycle control for the production manual autoanswers contour.

This command never imports a WB writer and never performs external I/O.  It is
the only repo-owned path used to migrate the current additive schema, activate manual mode, or
return the persisted master switch to OFF.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import fcntl
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_autoanswers_node_bridge import NodeAutoanswersBridge
from packages.application.wb_autoanswers_runtime import (
    AUTOANSWERS_DB_FILENAME,
    COMPRESSED_SCHEMA_BACKUP_CONTRACT,
    LEGACY_RUNTIME_DB_FILENAME,
    AutoanswersRepository,
    SCHEMA_VERSION,
    _verified_compressed_schema_backup_status,
)
from packages.contracts.wb_autoanswers import MODE_MANUAL


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
BACKUP_FREE_HEADROOM_BYTES = 2 * 1024 * 1024 * 1024
BACKUP_OPERATIONAL_HEADROOM_BYTES = 256 * 1024 * 1024
CAPACITY_HEARTBEAT_SECONDS = 20


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _sum_counts(values: dict[str, int]) -> int:
    return sum(int(value) for value in values.values())


@contextmanager
def _capacity_heartbeat() -> Any:
    """Keep the bounded production SSH operation observable without leaking paths."""

    stopped = threading.Event()

    def emit() -> None:
        while not stopped.wait(CAPACITY_HEARTBEAT_SECONDS):
            print("wb-autoanswers backup capacity verification in progress", file=sys.stderr, flush=True)

    thread = threading.Thread(target=emit, name="wb-autoanswers-capacity-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)


@contextmanager
def _schema_preparation_lock(runtime_dir: Path) -> Any:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / ".wb_autoanswers_schema.lock"
    with lock_path.open("a+b") as lock_handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _deployment_quiesce() -> Any:
    """Bound the one-time store split inside a repo-owned quiet window."""

    if not _truthy(os.environ.get("WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE")):
        yield {"applied": False, "units": []}
        return
    timers = (
        "wb-core-autoanswers-worker.timer",
        "wb-core-autoanswers-readonly-sync.timer",
    )
    services = (
        "wb-core-autoanswers-worker.service",
        "wb-core-autoanswers-readonly-sync.service",
    )
    registry_service = "wb-core-registry-http.service"

    def active(unit: str) -> bool:
        return (
            subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                check=False,
                timeout=20,
            ).returncode
            == 0
        )

    active_timers = [unit for unit in timers if active(unit)]
    registry_was_active = active(registry_service)
    stopped: list[str] = []
    try:
        for unit in (*timers, *services, registry_service):
            subprocess.run(
                ["systemctl", "stop", unit],
                check=True,
                timeout=120,
            )
            stopped.append(unit)
        yield {
            "applied": True,
            "units": stopped,
            "registry_was_active": registry_was_active,
            "active_timers": active_timers,
        }
    finally:
        if registry_was_active:
            subprocess.run(
                ["systemctl", "start", registry_service],
                check=True,
                timeout=120,
            )
        for unit in active_timers:
            subprocess.run(
                ["systemctl", "start", unit],
                check=True,
                timeout=120,
            )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zstd_decompressed_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    process = subprocess.Popen(
        ["zstd", "--decompress", "--stdout", "--quiet", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(chunk)
    _stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            "compressed backup byte verification failed: "
            + stderr.decode("utf-8", errors="replace").strip()
        )
    return digest.hexdigest()


def _compress_verified_backup(source: Path) -> dict[str, Any]:
    """Replace one autoanswers-owned raw backup with a byte-verified zstd copy."""

    source = source.resolve()
    expected_parent = source.parent
    if expected_parent.name != "wb_autoanswers_schema_v1" or source.suffix != ".sqlite3":
        raise RuntimeError("capacity recovery accepts only a raw autoanswers schema-v1 backup")
    compressed = source.with_suffix(source.suffix + ".zst")
    manifest = compressed.with_suffix(compressed.suffix + ".manifest.json")
    source_sha256 = _sha256_file(source)
    with closing(
        sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
    ) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError("schema-v1 backup failed integrity_check before compression")

    if compressed.exists() or manifest.exists():
        if not compressed.is_file():
            raise RuntimeError("compressed schema-v1 backup manifest exists without its archive")
        if not manifest.exists():
            subprocess.run(
                ["zstd", "--test", "--quiet", str(compressed)],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=7200,
                check=True,
            )
            if _zstd_decompressed_sha256(compressed) != source_sha256:
                raise RuntimeError("partial compressed backup does not restore the source bytes")
            metadata = {
                "contract": "wb_autoanswers_compressed_backup_v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_filename": source.name,
                "source_size": source.stat().st_size,
                "source_sha256": source_sha256,
                "compressed_filename": compressed.name,
                "compressed_size": compressed.stat().st_size,
                "compressed_sha256": _sha256_file(compressed),
                "sqlite_integrity_check": integrity,
                "restore_command": f"zstd --decompress --stdout {compressed.name} > {source.name}",
            }
            temporary_manifest = manifest.with_name(f".{manifest.name}.tmp-{os.getpid()}")
            temporary_manifest.write_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary_manifest, 0o600)
            os.replace(temporary_manifest, manifest)
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if metadata.get("source_sha256") != source_sha256:
            raise RuntimeError("existing compressed backup manifest has a different source hash")
        if metadata.get("compressed_sha256") != _sha256_file(compressed):
            raise RuntimeError("existing compressed backup does not match its manifest hash")
    else:
        temporary = compressed.with_name(f".{compressed.name}.tmp-{os.getpid()}")
        temporary_manifest = manifest.with_name(f".{manifest.name}.tmp-{os.getpid()}")
        try:
            subprocess.run(
                [
                    "zstd",
                    "-T0",
                    "-6",
                    "--no-progress",
                    "--force",
                    "-o",
                    str(temporary),
                    str(source),
                ],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=7200,
                check=True,
            )
            os.chmod(temporary, 0o600)
            subprocess.run(
                ["zstd", "--test", "--quiet", str(temporary)],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=7200,
                check=True,
            )
            compressed_sha256 = _sha256_file(temporary)
            if _zstd_decompressed_sha256(temporary) != source_sha256:
                raise RuntimeError("compressed backup does not restore the exact source bytes")
            metadata = {
                "contract": "wb_autoanswers_compressed_backup_v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_filename": source.name,
                "source_size": source.stat().st_size,
                "source_sha256": source_sha256,
                "compressed_filename": compressed.name,
                "compressed_size": temporary.stat().st_size,
                "compressed_sha256": compressed_sha256,
                "sqlite_integrity_check": integrity,
                "restore_command": f"zstd --decompress --stdout {compressed.name} > {source.name}",
            }
            temporary_manifest.write_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary_manifest, 0o600)
            os.replace(temporary, compressed)
            os.replace(temporary_manifest, manifest)
        except Exception:
            temporary.unlink(missing_ok=True)
            temporary_manifest.unlink(missing_ok=True)
            raise

    subprocess.run(
        ["zstd", "--test", "--quiet", str(compressed)],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=7200,
        check=True,
    )
    if _zstd_decompressed_sha256(compressed) != source_sha256:
        raise RuntimeError("persisted compressed backup does not restore the exact source bytes")
    source.unlink()
    return {
        "status": "compressed",
        "source_filename": source.name,
        "compressed_filename": compressed.name,
        "manifest_filename": manifest.name,
        "source_size": int(metadata["source_size"]),
        "compressed_size": int(metadata["compressed_size"]),
        "source_sha256": source_sha256,
        "compressed_sha256": str(metadata["compressed_sha256"]),
        "sqlite_integrity_check": integrity,
        "raw_source_removed_after_verification": True,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _integrity_check(path: Path) -> str:
    with closing(
        sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=60)
    ) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def _compress_verified_current_schema_backup(source: Path) -> dict[str, Any]:
    """Replace one complete current-schema raw backup with an exact zstd archive."""

    source = source.resolve()
    expected_parent = f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
    expected_prefix = f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__"
    if (
        source.parent.name != expected_parent
        or source.suffix != ".sqlite3"
        or not source.name.startswith(expected_prefix)
    ):
        raise RuntimeError("current-schema capacity recovery target is outside the owned backup boundary")
    integrity = _integrity_check(source)
    if integrity != "ok":
        raise RuntimeError("current-schema raw backup failed integrity_check before compression")

    source_size = source.stat().st_size
    source_sha256 = _sha256_file(source)
    archive = source.with_suffix(source.suffix + ".zst")
    manifest = archive.with_suffix(archive.suffix + ".manifest.json")
    temporary_archive = archive.with_name(f".{archive.name}.tmp-{os.getpid()}")
    temporary_manifest = manifest.with_name(f".{manifest.name}.tmp-{os.getpid()}")
    try:
        if manifest.exists() and not archive.is_file():
            raise RuntimeError("current-schema compressed manifest exists without its archive")
        if not archive.exists():
            completed = subprocess.run(
                [
                    "zstd",
                    "-T1",
                    "-6",
                    "--no-progress",
                    "--force",
                    "-o",
                    str(temporary_archive),
                    str(source),
                ],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=7200,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "current-schema backup compression failed: " + completed.stderr.strip()
                )
            os.chmod(temporary_archive, 0o600)
            subprocess.run(
                ["zstd", "--test", "--quiet", str(temporary_archive)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=7200,
                check=True,
            )
            if _zstd_decompressed_sha256(temporary_archive) != source_sha256:
                raise RuntimeError("current-schema compressed backup does not restore exact bytes")
            os.replace(temporary_archive, archive)
        else:
            subprocess.run(
                ["zstd", "--test", "--quiet", str(archive)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=7200,
                check=True,
            )
            if _zstd_decompressed_sha256(archive) != source_sha256:
                raise RuntimeError("partial current-schema archive has a different restore hash")

        metadata = {
            "contract": COMPRESSED_SCHEMA_BACKUP_CONTRACT,
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_filename": source.name,
            "snapshot_size": source_size,
            "snapshot_sha256": source_sha256,
            "compressed_filename": archive.name,
            "compressed_size": archive.stat().st_size,
            "compressed_sha256": _sha256_file(archive),
            "sqlite_integrity_check": integrity,
            "restore_command": f"zstd --decompress --stdout {archive.name} > {source.name}",
            "replaces_legacy_autoanswers_backup": None,
        }
        temporary_manifest.write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary_manifest, 0o600)
        os.replace(temporary_manifest, manifest)
        verified = _verified_compressed_schema_backup_status(source.parents[2], verify_bytes=True)
        if (
            int(verified.get("count") or 0) < 1
            or verified.get("integrity_check") != "ok"
            or verified.get("snapshot_sha256") != f"sha256:{source_sha256}"
        ):
            raise RuntimeError("current-schema compressed backup readback is missing")
    finally:
        temporary_archive.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)

    source.unlink()
    sidecars_removed = 0
    for suffix in ("-journal", "-shm", "-wal"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
            sidecars_removed += 1
    return {
        "status": "compressed_current_schema_backup",
        **verified,
        "source_filename": source.name,
        "source_size": source_size,
        "raw_source_removed_after_verification": True,
        "sidecars_removed_after_verification": sidecars_removed,
    }


def _prune_superseded_compressed_backups(
    runtime_dir: Path,
    *,
    current_backup: dict[str, Any],
    required_free: int,
) -> dict[str, Any]:
    """Remove the minimum older owned archives after current-schema restore proof."""

    snapshot_sha256 = str(current_backup.get("snapshot_sha256") or "")
    current_filename = str(current_backup.get("latest_filename") or "")
    if (
        int(current_backup.get("count") or 0) < 1
        or current_backup.get("integrity_check") != "ok"
        or current_backup.get("format") != "zstd"
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot_sha256)
        or Path(current_filename).name != current_filename
    ):
        raise RuntimeError("superseded backup pruning requires a verified current-schema restore")

    backup_root = runtime_dir / "backups"
    free_before = shutil.disk_usage(backup_root).free
    if free_before >= required_free:
        return {
            "status": "not_required",
            "free_before": free_before,
            "free_after": free_before,
            "removed": [],
            "audit_manifest": None,
        }

    candidates: list[tuple[int, Path, Path, dict[str, Any], str]] = []
    for version in range(1, SCHEMA_VERSION):
        directory = backup_root / f"wb_autoanswers_schema_v{version}"
        if not directory.is_dir():
            continue
        prefix = f"registry_upload_runtime__pre_autoanswers_v{version}__"
        for archive in sorted(directory.glob(f"{prefix}*.sqlite3.zst")):
            manifest = archive.with_suffix(archive.suffix + ".manifest.json")
            if not manifest.is_file():
                continue
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            contract = str(metadata.get("contract") or "")
            archive_sha256 = _sha256_file(archive)
            if (
                archive.parent != directory
                or Path(str(metadata.get("compressed_filename") or "")).name != archive.name
                or int(metadata.get("compressed_size") or -1) != archive.stat().st_size
                or str(metadata.get("compressed_sha256") or "") != archive_sha256
                or str(metadata.get("sqlite_integrity_check") or "") != "ok"
                or not contract.startswith("wb_autoanswers_compressed_")
            ):
                raise RuntimeError("superseded autoanswers backup manifest mismatch")
            candidates.append((version, archive, manifest, metadata, archive_sha256))

    if not candidates:
        raise RuntimeError("insufficient operational headroom and no superseded backup pair exists")

    current_dir = backup_root / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
    current_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    removed: list[dict[str, Any]] = []
    audit_path: Path | None = None
    for version, archive, manifest, metadata, archive_sha256 in candidates:
        entry = {
            "schema_version": version,
            "archive_filename": archive.name,
            "manifest_filename": manifest.name,
            "archive_size": archive.stat().st_size,
            "archive_sha256": f"sha256:{archive_sha256}",
            "snapshot_sha256": f"sha256:{str(metadata.get('snapshot_sha256') or metadata.get('source_sha256') or '')}",
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        audit_path = current_dir / f"superseded_backup_cleanup__{stamp}__{os.getpid()}.json"
        audit_base = {
            "contract": "wb_autoanswers_superseded_backup_cleanup_v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "current_backup": {
                "filename": current_filename,
                "snapshot_sha256": snapshot_sha256,
                "integrity_check": "ok",
                "format": "zstd",
            },
            "required_free": required_free,
            "free_before": free_before,
        }
        _write_json_atomic(
            audit_path,
            {
                **audit_base,
                "status": "planned",
                "removed": removed,
                "planned_removal": entry,
            },
        )
        archive.unlink()
        manifest.unlink(missing_ok=True)
        removed.append(entry)
        free_after = shutil.disk_usage(backup_root).free
        _write_json_atomic(
            audit_path,
            {
                **audit_base,
                "status": "applied",
                "removed": removed,
                "planned_removal": None,
                "free_after": free_after,
            },
        )
        if free_after >= required_free:
            return {
                "status": "superseded_backups_removed",
                "free_before": free_before,
                "free_after": free_after,
                "removed": removed,
                "audit_manifest": audit_path.name,
            }
    raise RuntimeError("verified superseded backup pruning left insufficient operational headroom")


def _create_current_compressed_schema_backup(
    runtime_dir: Path,
    *,
    legacy_source: Path | None,
) -> dict[str, Any]:
    """Replace the old backup only after a newer coherent snapshot is recoverable."""

    database = runtime_dir / "registry_upload_runtime.sqlite3"
    staging_dir = runtime_dir / ".wb_autoanswers_capacity_recovery"
    staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(staging_dir, 0o700)
    staging = staging_dir / f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__current.sqlite3"
    staging_manifest = staging.with_suffix(staging.suffix + ".manifest.json")
    backup_dir = runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    archive = backup_dir / (
        f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__capacity_recovery.sqlite3.zst"
    )
    manifest = archive.with_suffix(archive.suffix + ".manifest.json")

    existing = _verified_compressed_schema_backup_status(runtime_dir, verify_bytes=True)
    if int(existing.get("count") or 0) > 0:
        if legacy_source is not None and legacy_source.exists():
            legacy_source.unlink()
        staging.unlink(missing_ok=True)
        staging_manifest.unlink(missing_ok=True)
        return {"status": "already_verified", **existing, "legacy_raw_replaced": True}

    snapshot_metadata: dict[str, Any] | None = None
    if staging.is_file() and staging_manifest.is_file():
        candidate = json.loads(staging_manifest.read_text(encoding="utf-8"))
        if (
            candidate.get("contract") == "wb_autoanswers_capacity_snapshot_v1"
            and int(candidate.get("size") or -1) == staging.stat().st_size
            and candidate.get("sha256") == _sha256_file(staging)
            and _integrity_check(staging) == "ok"
        ):
            snapshot_metadata = candidate
    if snapshot_metadata is None:
        staging.unlink(missing_ok=True)
        staging_manifest.unlink(missing_ok=True)
        root_free = shutil.disk_usage(runtime_dir).free
        root_required = database.stat().st_size + BACKUP_OPERATIONAL_HEADROOM_BYTES
        if root_free < root_required:
            raise RuntimeError("insufficient root-volume capacity for coherent replacement backup")
        source_uri = f"file:{database.resolve()}?mode=ro"
        try:
            with closing(
                sqlite3.connect(source_uri, uri=True, timeout=60)
            ) as source:
                with closing(sqlite3.connect(staging, timeout=60)) as target:
                    source.backup(target, pages=4096)
            os.chmod(staging, 0o600)
            integrity = _integrity_check(staging)
            if integrity != "ok":
                raise RuntimeError("current replacement backup failed integrity_check")
            snapshot_metadata = {
                "contract": "wb_autoanswers_capacity_snapshot_v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "size": staging.stat().st_size,
                "sha256": _sha256_file(staging),
                "sqlite_integrity_check": integrity,
            }
            _write_json_atomic(staging_manifest, snapshot_metadata)
        except Exception:
            staging.unlink(missing_ok=True)
            staging_manifest.unlink(missing_ok=True)
            raise

    # The newer current snapshot is now coherent, integrity-checked and hashed.
    # Only at this point may the older autoanswers-owned raw backup be replaced.
    if legacy_source is not None and legacy_source.exists():
        legacy_source.unlink()

    archive_temporary = archive.with_name(f".{archive.name}.tmp-{os.getpid()}")
    manifest_temporary = manifest.with_name(f".{manifest.name}.tmp-{os.getpid()}")
    try:
        if archive.exists() and not manifest.exists():
            if _zstd_decompressed_sha256(archive) != str(snapshot_metadata["sha256"]):
                raise RuntimeError("partial current compressed backup has a different restore hash")
        elif not archive.exists():
            completed = subprocess.run(
                [
                    "zstd",
                    "-T1",
                    "-6",
                    "--no-progress",
                    "--force",
                    "-o",
                    str(archive_temporary),
                    str(staging),
                ],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=7200,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "current backup compression failed: " + completed.stderr.strip()
                )
            os.chmod(archive_temporary, 0o600)
            subprocess.run(
                ["zstd", "--test", "--quiet", str(archive_temporary)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=7200,
                check=True,
            )
            if _zstd_decompressed_sha256(archive_temporary) != str(snapshot_metadata["sha256"]):
                raise RuntimeError("current compressed backup does not restore exact snapshot bytes")
            os.replace(archive_temporary, archive)

        metadata = {
            "contract": COMPRESSED_SCHEMA_BACKUP_CONTRACT,
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_filename": staging.name,
            "snapshot_size": int(snapshot_metadata["size"]),
            "snapshot_sha256": str(snapshot_metadata["sha256"]),
            "compressed_filename": archive.name,
            "compressed_size": archive.stat().st_size,
            "compressed_sha256": _sha256_file(archive),
            "sqlite_integrity_check": "ok",
            "restore_command": f"zstd --decompress --stdout {archive.name} > {staging.name}",
            "replaces_legacy_autoanswers_backup": legacy_source.name if legacy_source else None,
        }
        manifest_temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_temporary, 0o600)
        os.replace(manifest_temporary, manifest)
        verified = _verified_compressed_schema_backup_status(runtime_dir, verify_bytes=True)
        if int(verified.get("count") or 0) < 1:
            raise RuntimeError("current compressed schema backup readback is missing")
    finally:
        archive_temporary.unlink(missing_ok=True)
        manifest_temporary.unlink(missing_ok=True)

    orphan_journals_removed = 0
    for journal in backup_dir.glob(
        f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__*.sqlite3-journal"
    ):
        database_candidate = Path(str(journal).removesuffix("-journal"))
        if not database_candidate.exists():
            journal.unlink()
            orphan_journals_removed += 1
    staging.unlink()
    staging_manifest.unlink(missing_ok=True)
    return {
        "status": "replaced_with_current_compressed_backup",
        **verified,
        "legacy_raw_replaced": True,
        "snapshot_raw_removed_after_verification": True,
        "orphan_autoanswers_journals_removed": orphan_journals_removed,
    }


def _prepare_backup_capacity(runtime_dir: Path) -> dict[str, Any]:
    database = runtime_dir / "registry_upload_runtime.sqlite3"
    if not database.is_file():
        return {"status": "not_required", "reason": "database_missing"}
    backup_root = runtime_dir / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    required_free = database.stat().st_size + BACKUP_FREE_HEADROOM_BYTES
    free_before = shutil.disk_usage(backup_root).free
    compressed = _verified_compressed_schema_backup_status(runtime_dir, verify_bytes=True)
    if int(compressed.get("count") or 0) > 0:
        current_schema_dir = runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
        redundant_raw_removed = 0
        for raw in current_schema_dir.glob(
            f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__*.sqlite3"
        ):
            raw.unlink()
            redundant_raw_removed += 1
        orphan_sidecars_removed = 0
        for suffix in ("*.sqlite3-journal", "*.sqlite3-shm", "*.sqlite3-wal"):
            for sidecar in current_schema_dir.glob(suffix):
                database_candidate = Path(
                    str(sidecar).removesuffix("-journal").removesuffix("-shm").removesuffix("-wal")
                )
                if not database_candidate.exists():
                    sidecar.unlink()
                    orphan_sidecars_removed += 1
        free_after_cleanup = shutil.disk_usage(backup_root).free
        superseded_cleanup = None
        if free_after_cleanup < BACKUP_OPERATIONAL_HEADROOM_BYTES:
            superseded_cleanup = _prune_superseded_compressed_backups(
                runtime_dir,
                current_backup=compressed,
                required_free=BACKUP_OPERATIONAL_HEADROOM_BYTES,
            )
            free_after_cleanup = shutil.disk_usage(backup_root).free
        if free_after_cleanup < BACKUP_OPERATIONAL_HEADROOM_BYTES:
            raise RuntimeError("verified backup cleanup left insufficient operational headroom")
        return {
            "status": "ready",
            "free_before": free_before,
            "free_after": free_after_cleanup,
            "required_free": required_free,
            "compaction": {
                **compressed,
                "redundant_autoanswers_raw_removed": redundant_raw_removed,
                "orphan_autoanswers_sidecars_removed": orphan_sidecars_removed,
                "superseded_cleanup": superseded_cleanup,
            },
        }
    current_schema_dir = runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
    current_raw = sorted(
        current_schema_dir.glob(
            f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__*.sqlite3"
        )
    ) if current_schema_dir.is_dir() else []
    if current_raw:
        with _capacity_heartbeat():
            compaction = _compress_verified_current_schema_backup(current_raw[-1])
        free_after = shutil.disk_usage(backup_root).free
        if free_after < BACKUP_OPERATIONAL_HEADROOM_BYTES:
            raise RuntimeError("verified current-schema compression left insufficient operational headroom")
        return {
            "status": "ready",
            "free_before": free_before,
            "free_after": free_after,
            "required_free": required_free,
            "compaction": compaction,
        }
    if free_before >= required_free:
        return {
            "status": "ready",
            "free_before": free_before,
            "required_free": required_free,
            "compaction": None,
        }
    candidates = sorted(
        path
        for path in (runtime_dir / "backups").glob(
            "wb_autoanswers_schema_v*/registry_upload_runtime__pre_autoanswers_v*__*.sqlite3"
        )
        if path.parent.name != f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
    )
    staging = runtime_dir / ".wb_autoanswers_capacity_recovery" / (
        f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__current.sqlite3"
    )
    if not candidates and not staging.is_file():
        raise RuntimeError("insufficient backup capacity and no recoverable autoanswers backup exists")
    with _capacity_heartbeat():
        compaction = _create_current_compressed_schema_backup(
            runtime_dir,
            legacy_source=candidates[-1] if candidates else None,
        )
    free_after = shutil.disk_usage(backup_root).free
    if free_after < BACKUP_OPERATIONAL_HEADROOM_BYTES:
        raise RuntimeError("verified replacement backup left insufficient operational headroom")
    return {
        "status": "ready",
        "free_before": free_before,
        "free_after": free_after,
        "required_free": required_free,
        "compaction": compaction,
    }


def _pre_migration_safety(runtime_dir: Path) -> dict[str, Any]:
    """Inspect only the two fields needed before constructor-triggered DDL."""

    isolated_path = runtime_dir / AUTOANSWERS_DB_FILENAME
    legacy_path = runtime_dir / LEGACY_RUNTIME_DB_FILENAME
    db_path = isolated_path if isolated_path.is_file() else legacy_path
    evidence: dict[str, Any] = {
        "database_exists": isolated_path.is_file() or legacy_path.is_file(),
        "database": db_path.name,
        "isolated_store_exists": isolated_path.is_file(),
        "autoanswers_initialized": False,
        "target_schema_applied": False,
        "schema_versions": [],
    }
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return evidence
    with closing(
        sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=30)
    ) as conn:
        migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_wb_autoanswers_schema_migrations'"
        ).fetchone()
        if migrations:
            evidence["schema_versions"] = [
                int(row[0])
                for row in conn.execute(
                    "SELECT version FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations ORDER BY version"
                ).fetchall()
            ]
            evidence["autoanswers_initialized"] = bool(evidence["schema_versions"])
            evidence["target_schema_applied"] = SCHEMA_VERSION in evidence["schema_versions"]
        settings = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_wb_autoanswers_settings'"
        ).fetchone()
        if settings:
            row = conn.execute(
                "SELECT master_enabled, mode FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
            ).fetchone()
            if row:
                evidence["master_enabled"] = bool(row[0])
                evidence["mode"] = str(row[1])
    return evidence


def _dependency_status(*, verify_boundary: bool) -> dict[str, Any]:
    node = shutil.which("node")
    ffmpeg = shutil.which("ffmpeg")
    node_major: int | None = None
    if node:
        completed = subprocess.run(
            [node, "-p", "Number(process.versions.node.split('.')[0])"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0:
            try:
                node_major = int(completed.stdout.strip())
            except ValueError:
                node_major = None
    result: dict[str, Any] = {
        "node_present": bool(node),
        "node_major": node_major,
        "node_supported": bool(node_major is not None and node_major >= 20),
        "ffmpeg_present": bool(ffmpeg),
        "frozen_boundary_verified": False,
    }
    if verify_boundary:
        if not result["node_supported"]:
            raise RuntimeError("Node.js >=20 is required before manual activation")
        if not result["ffmpeg_present"]:
            raise RuntimeError("ffmpeg is required before manual activation")
        verified = NodeAutoanswersBridge(node_binary=str(node)).verify().get("verified") or {}
        result["frozen_boundary_verified"] = int(verified.get("artifact_count") or 0) == 28
        if not result["frozen_boundary_verified"]:
            raise RuntimeError("frozen Node boundary verification failed")
    return result


def run(*, action: str, runtime_dir: Path) -> dict[str, Any]:
    before = _pre_migration_safety(runtime_dir)
    force_off = _truthy(os.environ.get("WB_AUTOANSWERS_FORCE_OFF"))
    requires_persisted_off = action in {"prepare-capacity", "prepare-deploy"} and not before.get(
        "autoanswers_initialized"
    )
    if action in {"prepare-capacity", "prepare-deploy"}:
        if not force_off:
            raise RuntimeError("schema preparation requires WB_AUTOANSWERS_FORCE_OFF=true")
        if requires_persisted_off and bool(before.get("master_enabled")):
            raise RuntimeError("initial schema preparation requires persisted master-switch OFF")

    if action == "prepare-capacity":
        with _schema_preparation_lock(runtime_dir), _capacity_heartbeat():
            return {
                "status": "ready",
                "action": action,
                "capacity": _prepare_backup_capacity(runtime_dir),
            }

    if action == "store-rollback-plan":
        from apps.wb_autoanswers_store_rollback import build_plan

        return {
            "status": "planned",
            "action": action,
            "rollback": build_plan(runtime_dir),
        }

    if action == "store-rollback-apply":
        if not force_off:
            raise RuntimeError(
                "store rollback requires WB_AUTOANSWERS_FORCE_OFF=true"
            )
        fingerprint = str(
            os.environ.get("WB_AUTOANSWERS_STORE_ROLLBACK_FINGERPRINT") or ""
        )
        if not fingerprint:
            raise RuntimeError("store rollback requires an exact fingerprint")
        with (
            _schema_preparation_lock(runtime_dir),
            _capacity_heartbeat(),
            _deployment_quiesce() as quiesce,
        ):
            if not bool(quiesce.get("applied")):
                raise RuntimeError(
                    "store rollback requires the deployment service quiet window"
                )
            from apps.wb_autoanswers_store_rollback import (
                apply_rollback_export,
            )

            rollback = apply_rollback_export(
                runtime_dir,
                expected_fingerprint=fingerprint,
            )
        return {
            "status": "ready_for_older_release",
            "action": action,
            "rollback": rollback,
            "quiesce": quiesce,
        }

    if action == "prepare-deploy":
        with (
            _schema_preparation_lock(runtime_dir),
            _capacity_heartbeat(),
            _deployment_quiesce() as quiesce,
        ):
            locked_before = _pre_migration_safety(runtime_dir)
            if not force_off:
                raise RuntimeError("schema preparation requires WB_AUTOANSWERS_FORCE_OFF=true")
            if (
                not bool(locked_before.get("autoanswers_initialized"))
                and bool(locked_before.get("master_enabled"))
            ):
                raise RuntimeError("initial schema preparation requires persisted master-switch OFF")
            capacity = _prepare_backup_capacity(runtime_dir)
            repository = AutoanswersRepository(runtime_dir=runtime_dir, schema_lock_held=True)
            financial_sources: dict[str, Any] = {"status": "not_requested"}
            if bool(quiesce.get("applied")):
                from apps.supplier_financial_source_migration import (
                    run as run_supplier_financial_source_migration,
                )

                financial_sources = run_supplier_financial_source_migration(
                    action="apply",
                    runtime_dir=runtime_dir,
                )
            dependencies = _dependency_status(verify_boundary=True)
            status_after = repository.operational_status()
            if SCHEMA_VERSION not in {
                int(row.get("version") or 0) for row in status_after["schema_migrations"]
            }:
                raise RuntimeError("current schema migration marker is missing")
            backup = repository.verified_schema_backup_status()
            if before.get("autoanswers_initialized") and not before.get(
                "target_schema_applied"
            ):
                if int(backup.get("count") or 0) < 1 or backup.get("integrity_check") != "ok":
                    raise RuntimeError("verified pre-schema backup is missing")
        return {
            "status": "ready",
            "action": action,
            "runtime": status_after,
            "schema_backup": backup,
            "capacity": capacity,
            "dependencies": dependencies,
            "quiesce": quiesce,
            "supplier_financial_sources": financial_sources,
        }

    if action == "status" and not before.get("target_schema_applied"):
        return {
            "status": "schema_preparation_required",
            "action": action,
            "runtime": {
                "schema_migrations": [
                    {"version": int(version)} for version in before.get("schema_versions") or []
                ],
                "settings": {
                    "master_enabled": bool(before.get("master_enabled")),
                    "mode": str(before.get("mode") or ""),
                    "force_off": force_off,
                    "effective_enabled": False,
                },
            },
            "schema_backup": _verified_compressed_schema_backup_status(
                runtime_dir,
                verify_bytes=False,
            ),
            "dependencies": _dependency_status(verify_boundary=False),
        }

    repository = AutoanswersRepository(runtime_dir=runtime_dir)
    status_before = repository.operational_status()

    if action == "status":
        return {
            "status": "ready",
            "action": action,
            "runtime": status_before,
            "schema_backup": repository.verified_schema_backup_status(),
            "dependencies": _dependency_status(verify_boundary=False),
        }

    if action == "activate-manual":
        if force_off:
            raise RuntimeError("manual activation requires WB_AUTOANSWERS_FORCE_OFF=false")
        settings = repository.settings()
        if settings.master_enabled:
            if settings.mode == MODE_MANUAL and settings.effective_enabled:
                return {"status": "already_active", "action": action, "runtime": status_before}
            raise RuntimeError("manual activation requires persisted master-switch OFF")
        if int(status_before.get("ai_jobs", {}).get("processing") or 0):
            raise RuntimeError("manual activation requires no actively processing AI lease")
        if int(status_before.get("publication_jobs", {}).get("publishing") or 0):
            raise RuntimeError("manual activation requires no actively publishing WB write")
        _dependency_status(verify_boundary=True)
        repository.update_settings(master_enabled=True, mode=MODE_MANUAL, actor_id="release-train")
        status_after = repository.operational_status()
        settings_after = status_after["settings"]
        if not settings_after["master_enabled"] or not settings_after["effective_enabled"]:
            raise RuntimeError("manual activation readback did not prove effective ON")
        if settings_after["mode"] != MODE_MANUAL:
            raise RuntimeError("manual activation readback mode mismatch")
        if status_after["ai_jobs"] != status_before["ai_jobs"]:
            raise RuntimeError("manual activation changed AI jobs")
        if status_after["publication_jobs"] != status_before["publication_jobs"]:
            raise RuntimeError("manual activation changed publication jobs")
        if int(status_after.get("claimable_ai_jobs") or 0):
            raise RuntimeError("manual activation left a claimable background AI job")
        if int(status_after.get("claimable_publication_writes") or 0):
            raise RuntimeError("manual activation left a claimable WB write")
        return {"status": "activated", "action": action, "runtime": status_after}

    if action == "deactivate":
        repository.update_settings(master_enabled=False, actor_id="release-train")
        status_after = repository.operational_status()
        if status_after["settings"]["master_enabled"] or status_after["settings"]["effective_enabled"]:
            raise RuntimeError("deactivation readback did not prove OFF")
        return {"status": "deactivated", "action": action, "runtime": status_after}

    raise ValueError(f"unsupported action: {action}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "status",
            "prepare-capacity",
            "prepare-deploy",
            "store-rollback-plan",
            "store-rollback-apply",
            "activate-manual",
            "deactivate",
        ),
    )
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(action=str(args.action), runtime_dir=args.runtime_dir.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
