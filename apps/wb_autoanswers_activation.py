#!/usr/bin/env python3
"""Fail-closed lifecycle control for the production manual autoanswers contour.

This command never imports a WB writer and never performs external I/O.  It is
the only repo-owned path used to migrate schema v2, activate manual mode, or
return the persisted master switch to OFF.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_autoanswers_node_bridge import NodeAutoanswersBridge
from packages.application.wb_autoanswers_runtime import AutoanswersRepository, SCHEMA_VERSION
from packages.contracts.wb_autoanswers import MODE_MANUAL


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _sum_counts(values: dict[str, int]) -> int:
    return sum(int(value) for value in values.values())


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
    if action == "prepare-deploy" and not before.get("schema_v2_applied"):
        if not force_off:
            raise RuntimeError("schema v2 migration requires WB_AUTOANSWERS_FORCE_OFF=true")
        if bool(before.get("master_enabled")):
            raise RuntimeError("schema v2 migration requires persisted master-switch OFF")

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
        "action", choices=("status", "prepare-deploy", "activate-manual", "deactivate")
    )
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(action=str(args.action), runtime_dir=args.runtime_dir.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
