#!/usr/bin/env python3
"""Remove one proven-invalid partial SQLite backup through an exact audit gate."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, BinaryIO
from uuid import uuid4


CHUNK_SIZE = 1024 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Invalid partial SQLite file below a backups directory.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    return parser


def build_plan(*, source: Path) -> dict[str, Any]:
    source = _validated_source(source)
    stat = source.stat()
    invalid_reason = _invalid_sqlite_reason(source)
    if not invalid_reason:
        raise ValueError("backup is a coherent SQLite database; use the lossless archive runner")
    evidence = {
        "contract_name": "sqlite_failed_backup_cleanup_v1",
        "source_path": str(source),
        "source_size_bytes": stat.st_size,
        "source_sha256": _file_hash(source),
        "source_inode": stat.st_ino,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_mode": oct(stat.st_mode & 0o777),
        "invalid_reason": invalid_reason,
        "source_header_hex": _read_header(source).hex(),
    }
    return {
        **evidence,
        "status": "ready",
        "mode": "dry-run",
        "fingerprint": _hash(evidence),
        "filesystem_free_bytes": shutil.disk_usage(source.parent).free,
        "would_remove_invalid_bytes": stat.st_size,
        "applied": False,
    }


def apply_cleanup(*, source: Path, fingerprint: str) -> dict[str, Any]:
    approved = str(fingerprint or "").strip()
    if not approved:
        raise ValueError("--apply requires --fingerprint from the exact current dry-run")
    source_path = _validated_candidate_path(source)
    manifest_path = source_path.with_name(source_path.name + ".failed-cleanup.json")
    if not source_path.exists():
        manifest = _load_matching_manifest(manifest_path, source_path=source_path, fingerprint=approved)
        status = str(manifest.get("status") or "")
        if status == "cleanup_pending":
            manifest["status"] = "invalid_partial_removed"
            manifest["removed_at"] = _now()
            _write_json_atomic(manifest_path, manifest, replace=True)
        elif status != "invalid_partial_removed":
            raise ValueError("cleanup manifest has an invalid terminal status")
        return _completed_result(manifest_path, manifest, idempotent=True)
    plan = build_plan(source=source)
    if approved != plan["fingerprint"]:
        raise ValueError("apply requires the exact current dry-run fingerprint")
    source_path = Path(plan["source_path"])
    _recheck_source_identity(source_path, plan)
    if manifest_path.exists():
        manifest = _load_matching_manifest(
            manifest_path,
            source_path=source_path,
            fingerprint=approved,
        )
        if str(manifest.get("status") or "") != "cleanup_pending":
            raise ValueError("cleanup manifest/source state is inconsistent")
        for key in (
            "source_size_bytes",
            "source_sha256",
            "source_inode",
            "source_mtime_ns",
            "source_mode",
            "source_header_hex",
            "invalid_reason",
        ):
            if manifest.get(key) != plan.get(key):
                raise ValueError("cleanup pending manifest no longer matches the invalid partial")
    else:
        manifest = {
            "contract_name": plan["contract_name"],
            "fingerprint": approved,
            "source_path": str(source_path),
            "source_size_bytes": plan["source_size_bytes"],
            "source_sha256": plan["source_sha256"],
            "source_inode": plan["source_inode"],
            "source_mtime_ns": plan["source_mtime_ns"],
            "source_mode": plan["source_mode"],
            "source_header_hex": plan["source_header_hex"],
            "invalid_reason": plan["invalid_reason"],
            "status": "cleanup_pending",
            "created_at": _now(),
        }
        _write_json_atomic(manifest_path, manifest)
    source_path.unlink()
    _fsync_directory(source_path.parent)
    manifest["status"] = "invalid_partial_removed"
    manifest["removed_at"] = _now()
    _write_json_atomic(manifest_path, manifest, replace=True)
    return {**plan, **_completed_result(manifest_path, manifest, idempotent=False)}


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source)
    if not args.apply:
        return build_plan(source=source)
    return apply_cleanup(source=source, fingerprint=args.fingerprint)


def _validated_source(source: Path) -> Path:
    resolved = _validated_candidate_path(source)
    if not resolved.is_file():
        raise ValueError(f"partial backup does not exist: {resolved}")
    return resolved


def _validated_candidate_path(source: Path) -> Path:
    requested = source.expanduser()
    if requested.is_symlink():
        raise ValueError("source symlink is not allowed")
    resolved = requested.resolve()
    if "backups" not in resolved.parts:
        raise ValueError("source must be below a backups directory")
    if resolved.name == "registry_upload_runtime.sqlite3":
        raise ValueError("live runtime SQLite cannot be removed by this runner")
    if resolved.suffix != ".sqlite3":
        raise ValueError("source must be a .sqlite3 backup candidate")
    return resolved


def _load_matching_manifest(
    manifest_path: Path,
    *,
    source_path: Path,
    fingerprint: str,
) -> dict[str, Any]:
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("cleanup source is absent without a valid audit manifest")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cleanup audit manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("cleanup audit manifest must be an object")
    if (
        payload.get("contract_name") != "sqlite_failed_backup_cleanup_v1"
        or payload.get("fingerprint") != fingerprint
        or payload.get("source_path") != str(source_path)
    ):
        raise ValueError("cleanup audit manifest identity mismatch")
    evidence_keys = (
        "contract_name",
        "source_path",
        "source_size_bytes",
        "source_sha256",
        "source_inode",
        "source_mtime_ns",
        "source_mode",
        "invalid_reason",
        "source_header_hex",
    )
    evidence = {key: payload.get(key) for key in evidence_keys}
    if _hash(evidence) != fingerprint:
        raise ValueError("cleanup audit manifest fingerprint is invalid")
    if (manifest_path.stat().st_mode & 0o777) != 0o600:
        raise ValueError("cleanup audit manifest permissions are not 0600")
    return payload


def _completed_result(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    idempotent: bool,
) -> dict[str, Any]:
    return {
        "status": "invalid_partial_removed",
        "mode": "apply",
        "fingerprint": manifest["fingerprint"],
        "would_remove_invalid_bytes": 0,
        "applied": True,
        "idempotent": idempotent,
        "source_removed": True,
        "released_bytes": int(manifest["source_size_bytes"]),
        "manifest_path": str(manifest_path),
        "filesystem_free_bytes_after": shutil.disk_usage(manifest_path.parent).free,
    }


def _invalid_sqlite_reason(source: Path) -> str:
    header = _read_header(source)
    if header != SQLITE_HEADER:
        return "invalid_sqlite_header"
    try:
        uri = f"file:{source}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as conn:
            value = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.Error as exc:
        return f"integrity_check_error:{exc.__class__.__name__}"
    if value.lower() != "ok":
        return "integrity_check_failed"
    return ""


def _read_header(source: Path) -> bytes:
    with source.open("rb") as handle:
        return handle.read(len(SQLITE_HEADER))


def _recheck_source_identity(source: Path, plan: dict[str, Any]) -> None:
    stat = source.stat()
    if (
        stat.st_size != int(plan["source_size_bytes"])
        or stat.st_ino != int(plan["source_inode"])
        or stat.st_mtime_ns != int(plan["source_mtime_ns"])
        or _file_hash(source) != plan["source_sha256"]
        or _invalid_sqlite_reason(source) != plan["invalid_reason"]
    ):
        raise ValueError("partial backup changed after dry-run")


def _write_json_atomic(path: Path, payload: dict[str, Any], *, replace: bool = False) -> None:
    temp = path.with_name(path.name + f".tmp-{uuid4().hex}")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        if not replace and path.exists():
            raise ValueError(f"cleanup manifest already exists: {path}")
        os.replace(temp, path)
        _fsync_directory(path.parent)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        _update_hash(handle, digest)
    return "sha256:" + digest.hexdigest()


def _update_hash(handle: BinaryIO, digest: Any) -> None:
    for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
        digest.update(chunk)


def _hash(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    try:
        payload = run(build_parser().parse_args())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
