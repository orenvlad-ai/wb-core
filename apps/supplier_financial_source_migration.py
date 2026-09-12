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
    RegistryUploadDbBackedRuntime,
)
from packages.application.storage_registry import StoreRegistry  # noqa: E402


CONTRACT = "supplier_financial_source_migration_v1"
MANIFEST_FILENAME = f"{CONTRACT}.json"
ORPHAN_LIFECYCLE_FILENAME = "supplier_financial_orphan_lifecycle_latest.json"
ORPHAN_MIN_AGE_SECONDS = 24 * 60 * 60
ORPHAN_QUARANTINE_RETENTION_SECONDS = 30 * 24 * 60 * 60
ORPHAN_SCAN_LIMIT = 5_000
# Diagnostic read budget only; never a storage admission or deletion threshold.
ORPHAN_HASH_READ_LIMIT_BYTES = 16 * 1024 * 1024


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
    stores = StoreRegistry(runtime_dir)
    manifest = stores.load()
    if manifest.implicit and not stores.resolve("operational", manifest=manifest).is_file():
        semantic = {"contract_name": CONTRACT, "groups": []}
        return {**semantic, "plan_fingerprint": _fingerprint(semantic),
                "source_store_status": "unavailable"}
    with stores.session(
        "operational", mode="ro", operation="supplier_source_migration_plan", manifest=manifest,
    ) as conn:
        conn.execute("BEGIN")
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
    current = build_plan(runtime_dir)
    if current.get("source_store_status") == "unavailable":
        return {"contract_name": CONTRACT, "status": "held_source_store_unavailable",
                "orphan_lifecycle": _run_orphan_lifecycle(runtime_dir)}
    if manifest_path.is_file():
        existing_manifest = dict(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
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
        plan = current
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


def _orphan_reference_readback(runtime_dir: Path) -> dict[str, Any]:
    """Positive references only; no claim of complete external-reader coverage."""
    stores = StoreRegistry(runtime_dir)
    manifest = stores.load()
    database = stores.resolve("operational", manifest=manifest)
    paths: set[str] = set()
    hashes: set[str] = set()
    tables: list[str] = []
    path_columns = {"stored_file_path", "source_file_path", "source_path", "target_path"}
    hash_columns = {"file_sha256", "source_file_sha256", "source_sha256", "sha256"}

    def add_value(key: str, raw: Any) -> None:
        value = str(raw or "").strip()
        if not value:
            return
        if key in path_columns:
            # Only this owned family can be moved/deleted by this lifecycle.
            try:
                path = _resolve_owned_path(runtime_dir, value)
            except ValueError:
                return
            paths.add(str(path))
        if key in hash_columns and len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
            hashes.add(value.lower())

    with stores.session(
        "operational", mode="ro", operation="supplier_orphan_reference_readback", manifest=manifest,
    ) as conn:
        conn.execute("BEGIN")
        required = {
            "sheet_vitrina_v1_supplier_financial_documents": {"stored_file_path", "file_sha256"},
            "sheet_vitrina_v1_cny_documents": {"stored_file_path", "file_sha256"},
            "sheet_vitrina_v1_supplier_financial_sources": {"stored_file_path", "source_sha256"},
        }
        for table in required:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({quoted})")}
            if not columns:
                raise ValueError(f"orphan reference table is missing: {table}")
            if not required[table].issubset(columns):
                raise ValueError(f"orphan reference schema is incomplete: {table}")
            selected = sorted(required[table])
            tables.append(table)
            projection = ",".join('"' + column + '"' for column in selected)
            for row in conn.execute(f"SELECT {projection} FROM {quoted}"):
                for key, value in zip(selected, row):
                    add_value(key, value)
    if stores.load().manifest_sha256 != manifest.manifest_sha256:
        raise ValueError("orphan reference manifest drifted")

    def read_manifest(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                add_value(str(key), item) if not isinstance(item, (Mapping, list)) else read_manifest(item)
        elif isinstance(value, list):
            for item in value:
                read_manifest(item)

    migration = runtime_dir / MANIFEST_FILENAME
    if migration.is_file():
        read_manifest(json.loads(migration.read_text(encoding="utf-8")))
    return {"database": str(database), "manifest_sha256": manifest.manifest_sha256,
            "tables": tables, "paths": paths, "hashes": hashes}


def _orphan_sha256(path: Path, *, remaining_bytes: int) -> tuple[str | None, int]:
    """Hash only a stable complete file that fits the remaining read budget."""
    if remaining_bytes <= 0 or path.stat().st_size > remaining_bytes:
        return None, 0
    read_bytes = 0
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if before.st_size > remaining_bytes:
            return None, 0
        while read_bytes < remaining_bytes:
            chunk = handle.read(min(1024 * 1024, remaining_bytes - read_bytes))
            if not chunk:
                break
            read_bytes += len(chunk)
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if identity(before) != identity(after) or read_bytes != after.st_size:
        return None, read_bytes
    return digest.hexdigest(), read_bytes


def _run_orphan_lifecycle(runtime_dir: Path) -> dict[str, Any]:
    # Three known owner tables and the manifest prove positive references,
    # but other tables, previews, the confirmation store and embedded payload readers
    # have no complete shared reachability contract yet. Absence from these sets
    # therefore cannot authorize a move or unlink, even after 30 days.
    reference_error = ""
    try:
        references = _orphan_reference_readback(runtime_dir)
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        references = {"paths": set(), "hashes": set(), "tables": []}
        reference_error = type(exc).__name__ + ": " + str(exc)
    paths = references["paths"]
    hashes = references["hashes"]
    held: list[dict[str, Any]] = []
    scanned = 0
    hash_read_bytes = 0
    skipped_hash_count = 0
    files_root = (runtime_dir / "supplier_financial_documents" / "files").resolve()
    quarantine_root = (runtime_dir / "supplier_financial_orphan_quarantine").resolve()
    now_epoch = time.time()
    for root, age in ((files_root, ORPHAN_MIN_AGE_SECONDS),
                      (quarantine_root, ORPHAN_QUARANTINE_RETENTION_SECONDS)):
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob("*")):
            if scanned >= ORPHAN_SCAN_LIMIT:
                break
            if candidate.is_symlink() or not candidate.is_file():
                continue
            scanned += 1
            stat = candidate.stat()
            if now_epoch - stat.st_mtime < age:
                continue
            digest, consumed = _orphan_sha256(
                candidate, remaining_bytes=ORPHAN_HASH_READ_LIMIT_BYTES - hash_read_bytes,
            )
            hash_read_bytes += consumed
            if digest is None:
                skipped_hash_count += 1
            original = files_root / candidate.relative_to(quarantine_root) if root == quarantine_root else candidate
            referenced = digest is not None and (str(candidate.resolve()) in paths
                          or str(original.resolve()) in paths or digest in hashes)
            held.append({"path": str(candidate.relative_to(runtime_dir.resolve())),
                         "sha256": digest, "size_bytes": stat.st_size,
                         "hash_status": "verified" if digest is not None else "skipped_budget_or_drift",
                         "reason": "referenced_source" if referenced else "unknown_reference_coverage"})
    result = {
        "contract_name": "supplier_financial_orphan_lifecycle_v1",
        "status": "held_unknown_reference_coverage",
        "checked_at": _now(),
        "scan_limit": ORPHAN_SCAN_LIMIT,
        "scanned_file_count": scanned,
        "hash_read_limit_bytes": ORPHAN_HASH_READ_LIMIT_BYTES,
        "hash_read_bytes": hash_read_bytes,
        "skipped_hash_count": skipped_hash_count,
        "referenced_path_count": len(paths),
        "referenced_sha256_count": len(hashes),
        "reference_tables": references["tables"],
        "reference_database": references.get("database"),
        "reference_manifest_sha256": references.get("manifest_sha256"),
        "reference_error": reference_error,
        "coverage_complete": False,
        "coverage_gap": "other table readers, external previews, confirmation store and embedded payload readers lack a complete reachability contract",
        "held": held,
        "quarantined": [],
        "expired_deleted": [],
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
