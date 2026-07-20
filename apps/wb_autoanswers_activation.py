#!/usr/bin/env python3
"""Fail-closed lifecycle control for the production manual autoanswers contour.

This command never imports a WB writer and never performs external I/O.  It is
the only repo-owned path used to migrate the current additive schema, activate manual mode, or
return the persisted master switch to OFF.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import fcntl
import os
from pathlib import Path
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
    COMPRESSED_SCHEMA_BACKUP_CONTRACT,
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
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60) as conn:
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
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=60) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


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
            with sqlite3.connect(source_uri, uri=True, timeout=60) as source:
                with sqlite3.connect(staging, timeout=60) as target:
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
        return {
            "status": "ready",
            "free_before": free_before,
            "free_after": free_after_cleanup,
            "required_free": required_free,
            "compaction": {
                **compressed,
                "redundant_autoanswers_raw_removed": redundant_raw_removed,
                "orphan_autoanswers_sidecars_removed": orphan_sidecars_removed,
            },
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

    db_path = runtime_dir / "registry_upload_runtime.sqlite3"
    evidence: dict[str, Any] = {
        "database_exists": db_path.is_file(),
        "autoanswers_initialized": False,
        "target_schema_applied": False,
    }
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return evidence
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=30) as conn:
        migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_wb_autoanswers_schema_migrations'"
        ).fetchone()
        if migrations:
            evidence["autoanswers_initialized"] = conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations LIMIT 1"
            ).fetchone() is not None
            evidence["target_schema_applied"] = conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations WHERE version=?",
                (SCHEMA_VERSION,),
            ).fetchone() is not None
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

    if action == "prepare-deploy":
        with _schema_preparation_lock(runtime_dir), _capacity_heartbeat():
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
            dependencies = _dependency_status(verify_boundary=True)
            status_after = repository.operational_status()
            if SCHEMA_VERSION not in {
                int(row.get("version") or 0) for row in status_after["schema_migrations"]
            }:
                raise RuntimeError("current schema migration marker is missing")
            backup = repository.verified_schema_backup_status()
            if before.get("database_exists") and not before.get("target_schema_applied"):
                if int(backup.get("count") or 0) < 1 or backup.get("integrity_check") != "ok":
                    raise RuntimeError("verified pre-schema backup is missing")
        return {
            "status": "ready",
            "action": action,
            "runtime": status_after,
            "schema_backup": backup,
            "capacity": capacity,
            "dependencies": dependencies,
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
        if _sum_counts(status_before["ai_jobs"]) or _sum_counts(status_before["publication_jobs"]):
            raise RuntimeError("manual activation requires an empty AI/publication queue")
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
        choices=("status", "prepare-capacity", "prepare-deploy", "activate-manual", "deactivate"),
    )
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(action=str(args.action), runtime_dir=args.runtime_dir.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
