#!/usr/bin/env python3
"""Fail-closed lifecycle control for the production manual autoanswers contour.

This command never imports a WB writer and never performs external I/O.  It is
the only repo-owned path used to migrate schema v2, activate manual mode, or
return the persisted master switch to OFF.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
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
from packages.application.wb_autoanswers_runtime import AutoanswersRepository, SCHEMA_VERSION
from packages.contracts.wb_autoanswers import MODE_MANUAL


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
BACKUP_FREE_HEADROOM_BYTES = 2 * 1024 * 1024 * 1024
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


def _prepare_backup_capacity(runtime_dir: Path) -> dict[str, Any]:
    database = runtime_dir / "registry_upload_runtime.sqlite3"
    if not database.is_file():
        return {"status": "not_required", "reason": "database_missing"}
    required_free = database.stat().st_size + BACKUP_FREE_HEADROOM_BYTES
    free_before = shutil.disk_usage(runtime_dir).free
    if free_before >= required_free:
        return {
            "status": "ready",
            "free_before": free_before,
            "required_free": required_free,
            "compaction": None,
        }
    candidates = sorted(
        (runtime_dir / "backups" / "wb_autoanswers_schema_v1").glob(
            "registry_upload_runtime__pre_autoanswers_v1__*.sqlite3"
        )
    )
    if not candidates:
        raise RuntimeError("insufficient backup capacity and no raw schema-v1 backup is available")
    with _capacity_heartbeat():
        compaction = _compress_verified_backup(candidates[-1])
    free_after = shutil.disk_usage(runtime_dir).free
    if free_after < required_free:
        raise RuntimeError("verified schema-v1 compression did not create enough backup capacity")
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
    evidence: dict[str, Any] = {"database_exists": db_path.is_file(), "schema_v2_applied": False}
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return evidence
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=30) as conn:
        migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_wb_autoanswers_schema_migrations'"
        ).fetchone()
        if migrations:
            evidence["schema_v2_applied"] = conn.execute(
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
    requires_off_preparation = action == "prepare-capacity" or (
        action == "prepare-deploy" and not before.get("schema_v2_applied")
    )
    if requires_off_preparation:
        if not force_off:
            raise RuntimeError("schema v2 preparation requires WB_AUTOANSWERS_FORCE_OFF=true")
        if bool(before.get("master_enabled")):
            raise RuntimeError("schema v2 preparation requires persisted master-switch OFF")

    if action == "prepare-capacity":
        return {
            "status": "ready",
            "action": action,
            "capacity": _prepare_backup_capacity(runtime_dir),
        }

    repository = AutoanswersRepository(runtime_dir=runtime_dir)
    status_before = repository.operational_status()

    if action == "prepare-deploy":
        dependencies = _dependency_status(verify_boundary=True)
        status_after = repository.operational_status()
        if SCHEMA_VERSION not in {
            int(row.get("version") or 0) for row in status_after["schema_migrations"]
        }:
            raise RuntimeError("schema v2 migration marker is missing")
        backup = repository.verified_schema_backup_status()
        if before.get("database_exists") and not before.get("schema_v2_applied"):
            if int(backup.get("count") or 0) < 1 or backup.get("integrity_check") != "ok":
                raise RuntimeError("verified pre-schema-v2 backup is missing")
        return {
            "status": "ready",
            "action": action,
            "runtime": status_after,
            "schema_backup": backup,
            "dependencies": dependencies,
        }

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
