#!/usr/bin/env python3
"""Losslessly archive one immutable repo-owned SQLite backup with exact guards."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from typing import Any, BinaryIO
from uuid import uuid4


CHUNK_SIZE = 1024 * 1024
ARCHIVE_SUFFIX = ".zst"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Existing immutable SQLite file below a backups directory.")
    parser.add_argument("--archive", help="Output .zst path in the same directory; defaults to SOURCE.zst.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    return parser


def build_plan(*, source: Path, archive: Path | None = None) -> dict[str, Any]:
    source = source.expanduser().resolve()
    archive = (archive or Path(str(source) + ARCHIVE_SUFFIX)).expanduser().resolve()
    _validate_paths(source, archive)
    zstd = shutil.which("zstd")
    if not zstd:
        raise ValueError("zstd executable is required")
    stat = source.stat()
    integrity = _integrity_check(source)
    source_sha256 = _file_hash(source)
    evidence = {
        "contract_name": "sqlite_backup_lossless_archive_v1",
        "source_path": str(source),
        "archive_path": str(archive),
        "source_size_bytes": stat.st_size,
        "source_sha256": source_sha256,
        "source_inode": stat.st_ino,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_mode": oct(stat.st_mode & 0o777),
        "source_integrity_check": integrity,
        "compression": "zstd-level-1",
        "verification": [
            "source SQLite PRAGMA integrity_check=ok",
            "exact source stat and sha256 recheck before deletion",
            "zstd frame test",
            "streamed decompressed size and sha256 equality",
            "0600 archive and fsynced parent directory",
        ],
    }
    return {
        **evidence,
        "status": "ready",
        "mode": "dry-run",
        "fingerprint": _hash(evidence),
        "filesystem_free_bytes": shutil.disk_usage(source.parent).free,
        "would_change": True,
        "applied": False,
    }


def apply_archive(*, source: Path, archive: Path | None, fingerprint: str) -> dict[str, Any]:
    approved = str(fingerprint or "").strip()
    if not approved:
        raise ValueError("--apply requires --fingerprint from the exact current dry-run")
    plan = build_plan(source=source, archive=archive)
    if approved != plan["fingerprint"]:
        raise ValueError("apply requires the exact current dry-run fingerprint")
    source_path = Path(plan["source_path"])
    archive_path = Path(plan["archive_path"])
    temp_path = archive_path.with_name(archive_path.name + f".tmp-{uuid4().hex}")
    manifest_path = archive_path.with_name(archive_path.name + ".manifest.json")
    if manifest_path.exists():
        raise ValueError(f"archive manifest already exists: {manifest_path}")
    zstd = shutil.which("zstd")
    if not zstd:
        raise ValueError("zstd executable is required")
    try:
        with temp_path.open("xb") as output:
            completed = subprocess.run(
                [zstd, "-1", "-T0", "-q", "-c", "--", str(source_path)],
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
            output.flush()
            os.fsync(output.fileno())
        if completed.returncode:
            raise ValueError(_command_error("zstd compression failed", completed))
        tested = subprocess.run(
            [zstd, "-q", "-t", "--", str(temp_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if tested.returncode:
            raise ValueError(_command_error("zstd archive test failed", tested))
        decompressed_sha256, decompressed_size = _decompressed_hash_and_size(
            zstd=zstd,
            archive=temp_path,
        )
        if decompressed_size != int(plan["source_size_bytes"]):
            raise ValueError("decompressed archive size does not match source")
        if decompressed_sha256 != plan["source_sha256"]:
            raise ValueError("decompressed archive sha256 does not match source")
        _recheck_source_identity(source_path, plan)
        archive_sha256 = _file_hash(temp_path)
        temp_path.chmod(0o600)
        os.replace(temp_path, archive_path)
        _fsync_directory(source_path.parent)
        manifest = {
            "contract_name": plan["contract_name"],
            "fingerprint": approved,
            "source_path": str(source_path),
            "source_size_bytes": plan["source_size_bytes"],
            "source_sha256": plan["source_sha256"],
            "source_integrity_check": plan["source_integrity_check"],
            "archive_path": str(archive_path),
            "archive_size_bytes": archive_path.stat().st_size,
            "archive_sha256": archive_sha256,
            "archive_mode": "0600",
            "zstd_test": "ok",
            "decompressed_size_bytes": decompressed_size,
            "decompressed_sha256": decompressed_sha256,
            "archived_at": _now(),
        }
        _write_manifest_atomic(manifest_path, manifest)
        source_path.unlink()
        _fsync_directory(source_path.parent)
        return {
            **plan,
            "status": "archived",
            "mode": "apply",
            "would_change": False,
            "applied": True,
            "source_removed": True,
            "archive": manifest,
            "manifest_path": str(manifest_path),
            "filesystem_free_bytes_after": shutil.disk_usage(archive_path.parent).free,
        }
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source)
    archive = Path(args.archive) if str(args.archive or "").strip() else None
    if not args.apply:
        return build_plan(source=source, archive=archive)
    return apply_archive(source=source, archive=archive, fingerprint=args.fingerprint)


def _validate_paths(source: Path, archive: Path) -> None:
    if not source.is_file():
        raise ValueError(f"SQLite backup does not exist: {source}")
    if "backups" not in source.parts:
        raise ValueError("source must be below a backups directory")
    if source.name == "registry_upload_runtime.sqlite3":
        raise ValueError("live runtime SQLite cannot be archived by this runner")
    if archive.parent != source.parent:
        raise ValueError("archive must stay in the same backup directory as source")
    if archive == source or archive.suffix != ARCHIVE_SUFFIX:
        raise ValueError("archive must be a distinct .zst path")
    if archive.exists():
        raise ValueError(f"archive already exists: {archive}")


def _integrity_check(path: Path) -> str:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        value = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if value.lower() != "ok":
        raise ValueError(f"SQLite integrity_check failed: {value}")
    return "ok"


def _recheck_source_identity(source: Path, plan: dict[str, Any]) -> None:
    stat = source.stat()
    if (
        stat.st_size != int(plan["source_size_bytes"])
        or stat.st_ino != int(plan["source_inode"])
        or stat.st_mtime_ns != int(plan["source_mtime_ns"])
        or _file_hash(source) != plan["source_sha256"]
    ):
        raise ValueError("source backup changed during archive creation")


def _decompressed_hash_and_size(*, zstd: str, archive: Path) -> tuple[str, int]:
    process = subprocess.Popen(
        [zstd, "-q", "-d", "-c", "--", str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        raise ValueError("zstd decompression stdout is unavailable")
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: process.stdout.read(CHUNK_SIZE), b""):
        digest.update(chunk)
        size += len(chunk)
    stderr = process.stderr.read() if process.stderr is not None else b""
    return_code = process.wait()
    if return_code:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"zstd decompression verification failed: {message or return_code}")
    return "sha256:" + digest.hexdigest(), size


def _write_manifest_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(path.name + f".tmp-{uuid4().hex}")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
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


def _command_error(prefix: str, completed: subprocess.CompletedProcess[bytes]) -> str:
    message = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
    return f"{prefix}: {message or completed.returncode}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    try:
        payload = run(build_parser().parse_args())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
