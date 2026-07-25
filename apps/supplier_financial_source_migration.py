#!/usr/bin/env python3
"""Versioned content-addressed migration for bank-statement source files."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Mapping
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    DB_FILENAME,
    RegistryUploadDbBackedRuntime,
)


CONTRACT = "supplier_financial_source_migration_v1"
MANIFEST_FILENAME = f"{CONTRACT}.json"
ORPHAN_LIFECYCLE_FILENAME = "supplier_financial_orphan_lifecycle_latest.json"
ORPHAN_MIN_AGE_SECONDS = 24 * 60 * 60
ORPHAN_QUARANTINE_RETENTION_SECONDS = 30 * 24 * 60 * 60
ORPHAN_SCAN_LIMIT = 5_000


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical(value).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_owned_path(runtime_dir: Path, value: str) -> Path:
    root = runtime_dir.resolve()
    raw = Path(str(value or ""))
    path = (raw if raw.is_absolute() else root / raw).resolve()
    if path != root and root not in path.parents:
        raise ValueError("financial source path escapes runtime dir")
    return path


def build_plan(runtime_dir: Path) -> dict[str, Any]:
    database = runtime_dir / DB_FILENAME
    if not database.is_file():
        semantic = {"contract_name": CONTRACT, "groups": []}
        return {**semantic, "plan_fingerprint": _fingerprint(semantic)}
    uri = f"file:{database.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        table = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table'
              AND name='sheet_vitrina_v1_supplier_financial_documents'
            """
        ).fetchone()
        rows = (
            conn.execute(
                """
                SELECT document_id,file_sha256,stored_file_path
                FROM sheet_vitrina_v1_supplier_financial_documents
                WHERE document_type='bank_fee_statement'
                  AND length(file_sha256)=64
                ORDER BY file_sha256,document_id
                """
            ).fetchall()
            if table
            else []
        )
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(str(row["file_sha256"]), []).append(
            {
                "document_id": str(row["document_id"]),
                "stored_file_path": str(row["stored_file_path"]),
            }
        )
    groups: list[dict[str, Any]] = []
    for source_sha256, documents in sorted(grouped.items()):
        old_paths = [
            _resolve_owned_path(runtime_dir, item["stored_file_path"])
            for item in documents
        ]
        existing = [path for path in old_paths if path.is_file()]
        target = (
            runtime_dir
            / "supplier_financial_sources"
            / "sha256"
            / source_sha256[:2]
            / source_sha256
            / "source.pdf"
        ).resolve()
        if target.is_file():
            existing.insert(0, target)
        if not existing:
            raise ValueError(
                f"bank statement source file is missing for {source_sha256}"
            )
        for path in dict.fromkeys(existing):
            if _sha256(path) != source_sha256:
                raise ValueError(
                    f"bank statement source hash mismatch for {source_sha256}"
                )
        groups.append(
            {
                "source_sha256": source_sha256,
                "source_size_bytes": existing[0].stat().st_size,
                "source_path": str(existing[0].relative_to(runtime_dir.resolve())),
                "target_path": str(target.relative_to(runtime_dir.resolve())),
                "documents": documents,
            }
        )
    semantic = {"contract_name": CONTRACT, "groups": groups}
    return {**semantic, "plan_fingerprint": _fingerprint(semantic)}


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_canonical(dict(payload)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _migration_lock(runtime_dir: Path) -> Any:
    lock_path = runtime_dir / ".supplier_financial_source_migration.lock"
    with lock_path.open("a+b") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def apply(runtime_dir: Path) -> dict[str, Any]:
    manifest_path = runtime_dir / MANIFEST_FILENAME
    existing_manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        existing_manifest = dict(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        current = build_plan(runtime_dir)
        if (
            existing_manifest.get("contract_name") == CONTRACT
            and existing_manifest.get("status") == "applied"
            and all(
                all(
                    str(document.get("stored_file_path") or "")
                    == str(group.get("target_path") or "")
                    for document in group.get("documents") or []
                )
                for group in current.get("groups") or []
            )
        ):
            orphan_lifecycle = _run_orphan_lifecycle(runtime_dir)
            return {
                **existing_manifest,
                "status": "already_applied",
                "idempotent": True,
                "orphan_lifecycle": orphan_lifecycle,
            }
    if (
        existing_manifest.get("contract_name") == CONTRACT
        and existing_manifest.get("status") == "prepared"
        and isinstance(existing_manifest.get("plan"), Mapping)
    ):
        plan = dict(existing_manifest["plan"])
        if str(plan.get("plan_fingerprint") or "") != _fingerprint(
            {
                "contract_name": CONTRACT,
                "groups": list(plan.get("groups") or []),
            }
        ):
            raise ValueError("prepared financial source migration plan changed")
    else:
        plan = build_plan(runtime_dir)
    prepared: list[dict[str, Any]] = []
    for group in plan["groups"]:
        source = _resolve_owned_path(runtime_dir, str(group["source_path"]))
        target = _resolve_owned_path(runtime_dir, str(group["target_path"]))
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not target.is_file():
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            os.link(source, temporary)
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        if (
            _sha256(target) != str(group["source_sha256"])
            or target.stat().st_size != int(group["source_size_bytes"])
        ):
            raise ValueError("content-addressed source readback failed")
        prepared.append(
            {
                "source_sha256": str(group["source_sha256"]),
                "target_path": str(group["target_path"]),
                "inode": int(target.stat().st_ino),
            }
        )
    prepared_at = str(existing_manifest.get("prepared_at") or _now())
    prepared_manifest = {
        "contract_name": CONTRACT,
        "status": "prepared",
        "prepared_at": prepared_at,
        "plan_fingerprint": plan["plan_fingerprint"],
        "group_count": len(plan["groups"]),
        "document_count": sum(
            len(group["documents"]) for group in plan["groups"]
        ),
        "prepared": prepared,
        "plan": plan,
    }
    _write_private_json(manifest_path, prepared_manifest)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    applied_at = _now()
    readback = runtime.migrate_supplier_financial_source_paths(
        contract_version=CONTRACT,
        applied_at=applied_at,
        manifest_sha256=str(plan["plan_fingerprint"]),
        source_paths={
            str(group["source_sha256"]): str(group["target_path"])
            for group in plan["groups"]
        },
        result={"plan_fingerprint": plan["plan_fingerprint"]},
    )
    if not readback["readback_confirmed"]:
        raise ValueError("financial source database readback failed")
    removed_paths: list[str] = []
    for group in plan["groups"]:
        target = _resolve_owned_path(runtime_dir, str(group["target_path"]))
        for document in group["documents"]:
            old_path = _resolve_owned_path(
                runtime_dir,
                str(document["stored_file_path"]),
            )
            if old_path == target or not old_path.is_file():
                continue
            if _sha256(old_path) != str(group["source_sha256"]):
                raise ValueError("legacy source changed before cleanup")
            old_path.unlink()
            removed_paths.append(str(document["stored_file_path"]))
            try:
                old_path.parent.rmdir()
            except OSError:
                pass
    orphan_lifecycle = _run_orphan_lifecycle(runtime_dir)
    result = {
        "contract_name": CONTRACT,
        "status": "applied",
        "prepared_at": prepared_at,
        "applied_at": applied_at,
        "plan_fingerprint": plan["plan_fingerprint"],
        "group_count": len(plan["groups"]),
        "document_count": sum(
            len(group["documents"]) for group in plan["groups"]
        ),
        "prepared": prepared,
        "removed_legacy_paths": sorted(set(removed_paths)),
        "orphan_lifecycle": orphan_lifecycle,
        "readback": readback,
        "rollback": (
            "python3 apps/supplier_financial_source_migration.py rollback "
            f"--runtime-dir {runtime_dir}"
        ),
        "plan": plan,
    }
    _write_private_json(manifest_path, result)
    return result


def _run_orphan_lifecycle(runtime_dir: Path) -> dict[str, Any]:
    now_epoch = time.time()
    files_root = (
        runtime_dir / "supplier_financial_documents" / "files"
    ).resolve()
    quarantine_root = (
        runtime_dir / "supplier_financial_orphan_quarantine"
    ).resolve()
    referenced: set[str] = set()
    database = runtime_dir / DB_FILENAME
    if database.is_file():
        uri = f"file:{database.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as conn:
            conn.execute("PRAGMA query_only=ON")
            table = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table'
                  AND name='sheet_vitrina_v1_supplier_financial_documents'
                """
            ).fetchone()
            if table:
                referenced = {
                    str(row[0] or "").strip()
                    for row in conn.execute(
                        """
                        SELECT stored_file_path
                        FROM sheet_vitrina_v1_supplier_financial_documents
                        WHERE stored_file_path IS NOT NULL
                          AND stored_file_path <> ''
                        """
                    ).fetchall()
                    if str(row[0] or "").strip()
                }
    quarantined: list[dict[str, Any]] = []
    scanned = 0
    if files_root.is_dir():
        for candidate in sorted(files_root.rglob("*")):
            if scanned >= ORPHAN_SCAN_LIMIT:
                break
            if candidate.is_symlink() or not candidate.is_file():
                continue
            scanned += 1
            relative_runtime = str(
                candidate.resolve().relative_to(runtime_dir.resolve())
            )
            if relative_runtime in referenced:
                continue
            stat = candidate.stat()
            if now_epoch - stat.st_mtime < ORPHAN_MIN_AGE_SECONDS:
                continue
            relative_files = candidate.resolve().relative_to(files_root)
            target = quarantine_root / relative_files
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.exists():
                target = target.with_name(
                    f"{target.name}.{uuid4().hex}"
                )
            source_sha256 = _sha256(candidate)
            os.replace(candidate, target)
            _fsync_directory(target.parent)
            os.utime(target, (now_epoch, now_epoch))
            if _sha256(target) != source_sha256:
                raise ValueError("supplier financial orphan quarantine readback failed")
            quarantined.append(
                {
                    "source_path": relative_runtime,
                    "quarantine_path": str(
                        target.relative_to(runtime_dir.resolve())
                    ),
                    "sha256": source_sha256,
                    "size_bytes": int(stat.st_size),
                }
            )
            _remove_empty_parents(candidate.parent, stop=files_root)
    expired_deleted: list[dict[str, Any]] = []
    if quarantine_root.is_dir():
        for candidate in sorted(quarantine_root.rglob("*"))[:ORPHAN_SCAN_LIMIT]:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            stat = candidate.stat()
            if now_epoch - stat.st_mtime < ORPHAN_QUARANTINE_RETENTION_SECONDS:
                continue
            evidence = {
                "quarantine_path": str(
                    candidate.resolve().relative_to(runtime_dir.resolve())
                ),
                "sha256": _sha256(candidate),
                "size_bytes": int(stat.st_size),
            }
            candidate.unlink()
            expired_deleted.append(evidence)
            _remove_empty_parents(candidate.parent, stop=quarantine_root)
    result = {
        "contract_name": "supplier_financial_orphan_lifecycle_v1",
        "checked_at": _now(),
        "scan_limit": ORPHAN_SCAN_LIMIT,
        "scanned_file_count": scanned,
        "referenced_path_count": len(referenced),
        "quarantined": quarantined,
        "expired_deleted": expired_deleted,
        "quarantine_retention_seconds": ORPHAN_QUARANTINE_RETENTION_SECONDS,
    }
    _write_private_json(runtime_dir / ORPHAN_LIFECYCLE_FILENAME, result)
    return result


def _remove_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def rollback(runtime_dir: Path) -> dict[str, Any]:
    manifest_path = runtime_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ValueError("financial source migration manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan = dict(manifest.get("plan") or {})
    document_paths: dict[str, str] = {}
    for group in plan.get("groups") or []:
        target = _resolve_owned_path(runtime_dir, str(group["target_path"]))
        if not target.is_file() or _sha256(target) != str(
            group["source_sha256"]
        ):
            raise ValueError("content-addressed rollback source is invalid")
        for document in group["documents"]:
            old_path = _resolve_owned_path(
                runtime_dir,
                str(document["stored_file_path"]),
            )
            old_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not old_path.exists():
                os.link(target, old_path)
                _fsync_directory(old_path.parent)
            document_paths[str(document["document_id"])] = str(
                document["stored_file_path"]
            )
    affected = RegistryUploadDbBackedRuntime(
        runtime_dir=runtime_dir
    ).restore_supplier_financial_source_paths(
        restored_at=_now(),
        document_paths=document_paths,
    )
    result = {
        "contract_name": CONTRACT,
        "status": "rolled_back",
        "rolled_back_at": _now(),
        "affected_documents": affected,
        "readback_confirmed": affected == len(document_paths),
        "plan": plan,
    }
    _write_private_json(manifest_path, result)
    return result


def run(*, action: str, runtime_dir: Path) -> dict[str, Any]:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with _migration_lock(runtime_dir):
        if action == "dry-run":
            return {"status": "planned", **build_plan(runtime_dir)}
        if action == "apply":
            return apply(runtime_dir)
        if action == "rollback":
            return rollback(runtime_dir)
        raise ValueError(f"unsupported action: {action}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("dry-run", "apply", "rollback"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(
        action=str(args.action),
        runtime_dir=args.runtime_dir.expanduser().resolve(),
    )
    print(_canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
