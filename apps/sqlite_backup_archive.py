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
import tempfile
from typing import Any, BinaryIO
from urllib.parse import quote
from uuid import uuid4


CHUNK_SIZE = 1024 * 1024
ARCHIVE_SUFFIX = ".zst"
DEFAULT_RESERVED_FREE_BYTES = 256 * 1024 * 1024
MIN_ARCHIVE_EXPANSION_BYTES = 64 * 1024 * 1024
SOURCE_SIDECAR_SUFFIXES = ("-wal", "-shm")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Existing immutable SQLite file below a backups directory.")
    parser.add_argument("--archive", help="Output .zst path in the same directory; defaults to SOURCE.zst.")
    parser.add_argument(
        "--staging-directory",
        help=(
            "Existing directory for a private unnamed compression staging file. "
            "Defaults to the archive directory."
        ),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument(
        "--reserved-free-bytes",
        type=int,
        default=DEFAULT_RESERVED_FREE_BYTES,
        help="Free bytes that must remain after worst-case archive creation.",
    )
    return parser


def build_plan(
    *,
    source: Path,
    archive: Path | None = None,
    staging_directory: Path | None = None,
    reserved_free_bytes: int = DEFAULT_RESERVED_FREE_BYTES,
) -> dict[str, Any]:
    source_input = source.expanduser()
    archive_input = (archive or Path(str(source_input) + ARCHIVE_SUFFIX)).expanduser()
    if source_input.is_symlink() or archive_input.is_symlink():
        raise ValueError("SQLite backup source and archive paths must not be symlinks")
    source = source_input.resolve()
    archive = archive_input.resolve()
    staging_input = (staging_directory or archive.parent).expanduser()
    if staging_input.is_symlink():
        raise ValueError("SQLite backup staging directory must not be a symlink")
    staging_directory = staging_input.resolve()
    if not staging_directory.is_dir():
        raise ValueError("SQLite backup staging directory must already exist")
    manifest_path = archive.with_name(archive.name + ".manifest.json")
    if manifest_path.is_symlink():
        raise ValueError("SQLite backup archive manifest must not be a symlink")
    _validate_paths(source, archive, manifest_path=manifest_path)
    zstd = shutil.which("zstd")
    if not zstd:
        raise ValueError("zstd executable is required")
    if int(reserved_free_bytes) < 0:
        raise ValueError("reserved free bytes must be non-negative")
    if not source.is_file():
        verified = verify_archive_manifest(manifest_path)
        if not bool(verified.get("source_removed", True)):
            raise ValueError("archive lifecycle is incomplete while source is unavailable")
        return {
            "contract_name": "sqlite_backup_lossless_archive_v1",
            "source_path": str(source),
            "archive_path": str(archive),
            "status": "already_archived",
            "mode": "dry-run",
            "fingerprint": str(verified.get("fingerprint") or ""),
            "would_change": False,
            "applied": False,
            "idempotent": True,
            "archive": verified,
            "manifest_path": str(manifest_path),
            "filesystem_free_bytes": shutil.disk_usage(source.parent).free,
            "staging_directory": str(staging_directory),
        }
    stat = source.stat()
    sidecars_before = _source_sidecars(source)
    _validate_source_sidecars(sidecars_before)
    integrity = _integrity_check(source)
    source_sha256 = _file_hash(source)
    staging_same_filesystem = _same_filesystem(
        staging_directory,
        archive.parent,
    )
    projected_archive_size_bytes = (
        None
        if staging_same_filesystem
        else _compressed_size(zstd=zstd, source=source)
    )
    sidecars_after = _source_sidecars(source)
    if sidecars_after != sidecars_before:
        raise ValueError("query-only immutable planning changed SQLite sidecars")
    destination_available_free_bytes = shutil.disk_usage(archive.parent).free
    staging_available_free_bytes = shutil.disk_usage(staging_directory).free
    archive_expansion_bytes = max(
        MIN_ARCHIVE_EXPANSION_BYTES,
        (int(stat.st_size) + 19) // 20,
    )
    staging_required_free_bytes = (
        int(stat.st_size)
        + int(archive_expansion_bytes)
        + int(reserved_free_bytes)
    )
    destination_required_free_bytes = (
        staging_required_free_bytes
        if staging_same_filesystem
        else int(projected_archive_size_bytes or 0) + int(reserved_free_bytes)
    )
    required_free_bytes = staging_required_free_bytes
    directory_non_target_digest = _directory_non_target_digest(
        source=source,
        archive=archive,
    )
    capacity_requirement = {
        "source_size_bytes": int(stat.st_size),
        "archive_worst_case_expansion_bytes": int(archive_expansion_bytes),
        "projected_archive_size_bytes": projected_archive_size_bytes,
        "reserved_free_bytes": int(reserved_free_bytes),
        "staging_directory": str(staging_directory),
        "staging_same_filesystem": staging_same_filesystem,
        "staging_required_free_bytes": int(staging_required_free_bytes),
        "destination_required_free_bytes": int(destination_required_free_bytes),
        "required_free_bytes": int(required_free_bytes),
    }
    staging_sufficient = (
        int(staging_available_free_bytes) >= int(staging_required_free_bytes)
    )
    destination_sufficient = (
        int(destination_available_free_bytes)
        >= int(destination_required_free_bytes)
    )
    capacity = {
        **capacity_requirement,
        "available_free_bytes": int(staging_available_free_bytes),
        "shortfall_bytes": max(
            0,
            int(staging_required_free_bytes) - int(staging_available_free_bytes),
        ),
        "staging_available_free_bytes": int(staging_available_free_bytes),
        "staging_shortfall_bytes": max(
            0,
            int(staging_required_free_bytes) - int(staging_available_free_bytes),
        ),
        "staging_sufficient": staging_sufficient,
        "destination_available_free_bytes": int(destination_available_free_bytes),
        "destination_shortfall_bytes": max(
            0,
            int(destination_required_free_bytes)
            - int(destination_available_free_bytes),
        ),
        "destination_sufficient": destination_sufficient,
        "sufficient": staging_sufficient and destination_sufficient,
    }
    evidence = {
        "contract_name": "sqlite_backup_lossless_archive_v1",
        "source_path": str(source),
        "archive_path": str(archive),
        "staging_directory": str(staging_directory),
        "staging_same_filesystem": staging_same_filesystem,
        "source_size_bytes": stat.st_size,
        "source_sha256": source_sha256,
        "source_inode": stat.st_ino,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_mode": oct(stat.st_mode & 0o777),
        "source_integrity_check": integrity,
        "source_sqlite_open": {
            "mode": "ro",
            "immutable": True,
            "query_only": True,
        },
        "source_sidecars": sidecars_before,
        "directory_non_target_digest": directory_non_target_digest,
        "compression": "zstd-level-1",
        "capacity_requirement": capacity_requirement,
        "verification": [
            "source SQLite immutable mode=ro, query_only=ON, PRAGMA integrity_check=ok",
            "source WAL is absent or empty and no rollback journal exists",
            "exact source stat and sha256 recheck before deletion",
            "zstd frame test",
            "streamed decompressed size and sha256 equality",
            "cross-filesystem staging capacity plus measured destination publication capacity",
            "independent retained archive/manifest readback before source deletion",
            "0600 archive and manifest plus fsynced parent directory",
            "non-target directory digest equality and owned-sidecar lifecycle",
        ],
    }
    existing_archive = archive.is_file() or manifest_path.is_file()
    if existing_archive:
        verified = verify_archive_manifest(manifest_path)
        _validate_manifest_source_identity(verified, evidence)
    return {
        **evidence,
        "status": "resume_ready" if existing_archive else "ready",
        "mode": "dry-run",
        "fingerprint": _hash(evidence),
        "filesystem_free_bytes": int(destination_available_free_bytes),
        "capacity": capacity,
        "would_change": True,
        "applied": False,
        "resume_from_verified_archive": bool(existing_archive),
    }


def apply_archive(
    *,
    source: Path,
    archive: Path | None,
    staging_directory: Path | None = None,
    fingerprint: str,
    reserved_free_bytes: int = DEFAULT_RESERVED_FREE_BYTES,
) -> dict[str, Any]:
    approved = str(fingerprint or "").strip()
    if not approved:
        raise ValueError("--apply requires --fingerprint from the exact current dry-run")
    plan = build_plan(
        source=source,
        archive=archive,
        staging_directory=staging_directory,
        reserved_free_bytes=reserved_free_bytes,
    )
    if approved != plan["fingerprint"]:
        raise ValueError("apply requires the exact current dry-run fingerprint")
    if not plan.get("would_change"):
        return {
            **plan,
            "status": "archived",
            "mode": "apply",
            "applied": False,
            "idempotent": True,
            "source_removed": True,
        }
    if not bool((plan.get("capacity") or {}).get("sufficient")):
        raise ValueError(
            "insufficient archive headroom: "
            f"staging_required_free_bytes={plan['capacity']['staging_required_free_bytes']}, "
            f"staging_available_free_bytes={plan['capacity']['staging_available_free_bytes']}, "
            f"staging_shortfall_bytes={plan['capacity']['staging_shortfall_bytes']}, "
            f"destination_required_free_bytes={plan['capacity']['destination_required_free_bytes']}, "
            f"destination_available_free_bytes={plan['capacity']['destination_available_free_bytes']}, "
            f"destination_shortfall_bytes={plan['capacity']['destination_shortfall_bytes']}"
        )
    source_path = Path(plan["source_path"])
    archive_path = Path(plan["archive_path"])
    staging_path = Path(str(plan["staging_directory"]))
    temp_path = archive_path.with_name(archive_path.name + f".tmp-{uuid4().hex}")
    manifest_path = archive_path.with_name(archive_path.name + ".manifest.json")
    zstd = shutil.which("zstd")
    if not zstd:
        raise ValueError("zstd executable is required")
    try:
        if archive_path.is_file() and manifest_path.is_file():
            manifest = verify_archive_manifest(manifest_path)
            _validate_manifest_source_identity(manifest, plan)
        else:
            if bool(plan["staging_same_filesystem"]):
                (
                    archive_sha256,
                    decompressed_sha256,
                    decompressed_size,
                ) = _compress_to_named_temp(
                    zstd=zstd,
                    source=source_path,
                    temp_path=temp_path,
                )
            else:
                with tempfile.TemporaryFile(
                    mode="w+b",
                    dir=staging_path,
                ) as staged:
                    (
                        archive_sha256,
                        decompressed_sha256,
                        decompressed_size,
                        archive_size,
                    ) = _compress_to_unnamed_stage(
                        zstd=zstd,
                        source=source_path,
                        staged=staged,
                    )
                    projected_size = int(
                        plan["capacity_requirement"][
                            "projected_archive_size_bytes"
                        ]
                    )
                    if archive_size != projected_size:
                        raise ValueError(
                            "compressed archive size changed from exact dry-run"
                        )
                    destination_free = shutil.disk_usage(archive_path.parent).free
                    destination_required = (
                        archive_size + int(reserved_free_bytes)
                    )
                    if destination_free < destination_required:
                        raise ValueError(
                            "insufficient destination publication headroom: "
                            f"required_free_bytes={destination_required}, "
                            f"available_free_bytes={destination_free}, "
                            f"shortfall_bytes={destination_required - destination_free}"
                        )
                    _copy_stage_to_named_temp(
                        staged=staged,
                        temp_path=temp_path,
                        expected_sha256=archive_sha256,
                    )
            if decompressed_size != int(plan["source_size_bytes"]):
                raise ValueError("decompressed archive size does not match source")
            if decompressed_sha256 != plan["source_sha256"]:
                raise ValueError("decompressed archive sha256 does not match source")
            _recheck_source_identity(source_path, plan)
            os.replace(temp_path, archive_path)
            _fsync_directory(source_path.parent)
            manifest = {
                "contract_name": plan["contract_name"],
                "fingerprint": approved,
                "source_path": str(source_path),
                "source_size_bytes": plan["source_size_bytes"],
                "source_sha256": plan["source_sha256"],
                "source_inode": plan["source_inode"],
                "source_mtime_ns": plan["source_mtime_ns"],
                "source_mode": plan["source_mode"],
                "source_integrity_check": plan["source_integrity_check"],
                "source_sqlite_open": plan["source_sqlite_open"],
                "source_sidecars": plan["source_sidecars"],
                "directory_non_target_digest": plan["directory_non_target_digest"],
                "capacity": plan["capacity"],
                "staging_directory": str(staging_path),
                "staging_same_filesystem": bool(
                    plan["staging_same_filesystem"]
                ),
                "archive_path": str(archive_path),
                "archive_size_bytes": archive_path.stat().st_size,
                "archive_sha256": archive_sha256,
                "archive_mode": "0600",
                "manifest_mode": "0600",
                "zstd_test": "ok",
                "decompressed_size_bytes": decompressed_size,
                "decompressed_sha256": decompressed_sha256,
                "lifecycle_state": "verified_pending_source_removal",
                "source_removed": False,
                "archived_at": _now(),
            }
            _write_manifest_atomic(manifest_path, manifest)
            manifest = verify_archive_manifest(manifest_path)
            _validate_manifest_source_identity(manifest, plan)
        source_path.unlink()
        removed_sidecars = _remove_owned_sidecars(
            source=source_path,
            expected=plan.get("source_sidecars") or [],
        )
        _fsync_directory(source_path.parent)
        non_target_after = _directory_non_target_digest(
            source=source_path,
            archive=archive_path,
        )
        if non_target_after != str(plan["directory_non_target_digest"]):
            raise ValueError("non-target backup directory entries changed during archiving")
        manifest = {
            **dict(manifest),
            "lifecycle_state": "retained",
            "source_removed": True,
            "removed_source_sidecars": removed_sidecars,
            "directory_non_target_digest_after": non_target_after,
            "finalized_at": _now(),
        }
        _write_manifest_atomic(manifest_path, manifest)
        final_readback = verify_archive_manifest(manifest_path)
        if (
            not bool(final_readback.get("source_removed"))
            or str(final_readback.get("lifecycle_state") or "") != "retained"
        ):
            raise ValueError("archive lifecycle final readback is incomplete")
        return {
            **plan,
            "status": "archived",
            "mode": "apply",
            "would_change": False,
            "applied": True,
            "idempotent": False,
            "source_removed": True,
            "archive": final_readback,
            "manifest_path": str(manifest_path),
            "filesystem_free_bytes_after": shutil.disk_usage(archive_path.parent).free,
            "orphan_artifacts": _owned_orphan_artifacts(
                source=source_path,
                archive=archive_path,
                staging_directory=staging_path,
            ),
        }
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def verify_archive_manifest(manifest_path: Path) -> dict[str, Any]:
    """Revalidate the retained archive bytes, not only their recorded manifest."""

    manifest_path = manifest_path.expanduser()
    if manifest_path.is_symlink():
        raise ValueError("SQLite backup archive manifest is unavailable")
    manifest_path = manifest_path.resolve()
    if not manifest_path.is_file():
        raise ValueError("SQLite backup archive manifest is unavailable")
    if manifest_path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("SQLite backup archive manifest failed provenance validation")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive = Path(str(manifest.get("archive_path") or "")).expanduser()
    archive_is_symlink = archive.is_symlink()
    archive = archive.resolve()
    if (
        str(manifest.get("contract_name") or "") != "sqlite_backup_lossless_archive_v1"
        or not archive.is_file()
        or archive_is_symlink
        or archive.parent != manifest_path.parent
        or manifest_path != archive.with_name(archive.name + ".manifest.json")
        or archive.stat().st_mode & 0o777 != 0o600
        or int(manifest.get("archive_size_bytes") or -1) != archive.stat().st_size
        or str(manifest.get("source_integrity_check") or "") != "ok"
        or str(manifest.get("zstd_test") or "") != "ok"
        or str(manifest.get("decompressed_sha256") or "")
        != str(manifest.get("source_sha256") or "")
        or int(manifest.get("decompressed_size_bytes") or -1)
        != int(manifest.get("source_size_bytes") or -2)
        or str(manifest.get("lifecycle_state") or "retained")
        not in {"verified_pending_source_removal", "retained"}
    ):
        raise ValueError("SQLite backup archive manifest failed provenance validation")
    if _file_hash(archive) != str(manifest.get("archive_sha256") or ""):
        raise ValueError("SQLite backup archive SHA-256 does not match its manifest")
    zstd = shutil.which("zstd")
    if not zstd:
        raise ValueError("zstd executable is required")
    tested = subprocess.run(
        [zstd, "-q", "-t", "--", str(archive)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if tested.returncode:
        raise ValueError(_command_error("zstd archive test failed", tested))
    decompressed_sha256, decompressed_size = _decompressed_hash_and_size(
        zstd=zstd,
        archive=archive,
    )
    if (
        decompressed_sha256 != str(manifest.get("source_sha256") or "")
        or decompressed_size != int(manifest.get("source_size_bytes") or -1)
    ):
        raise ValueError("SQLite backup archive decompressed fingerprint does not match")
    return {
        **manifest,
        "integrity_check": "ok",
        "actual_archive_sha256": str(manifest["archive_sha256"]),
        "actual_decompressed_sha256": decompressed_sha256,
        "actual_decompressed_size_bytes": decompressed_size,
        "reverified": True,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source)
    archive = Path(args.archive) if str(args.archive or "").strip() else None
    staging_directory = (
        Path(args.staging_directory)
        if str(getattr(args, "staging_directory", "") or "").strip()
        else None
    )
    reserved_free_bytes = int(
        getattr(args, "reserved_free_bytes", DEFAULT_RESERVED_FREE_BYTES)
    )
    if not args.apply:
        return build_plan(
            source=source,
            archive=archive,
            staging_directory=staging_directory,
            reserved_free_bytes=reserved_free_bytes,
        )
    return apply_archive(
        source=source,
        archive=archive,
        staging_directory=staging_directory,
        fingerprint=args.fingerprint,
        reserved_free_bytes=reserved_free_bytes,
    )


def _validate_paths(source: Path, archive: Path, *, manifest_path: Path) -> None:
    if not source.is_file() and not (
        archive.is_file() and manifest_path.is_file()
    ):
        raise ValueError(f"SQLite backup does not exist: {source}")
    if "backups" not in source.parts:
        raise ValueError("source must be below a backups directory")
    if source.name == "registry_upload_runtime.sqlite3":
        raise ValueError("live runtime SQLite cannot be archived by this runner")
    if archive.parent != source.parent:
        raise ValueError("archive must stay in the same backup directory as source")
    if archive == source or archive.suffix != ARCHIVE_SUFFIX:
        raise ValueError("archive must be a distinct .zst path")
    if archive.exists() != manifest_path.exists():
        raise ValueError("archive and manifest lifecycle is incomplete")


def _integrity_check(path: Path) -> str:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise ValueError("SQLite immutable plan could not enable query_only")
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
    sidecars = _source_sidecars(source)
    if sidecars != list(plan.get("source_sidecars") or []):
        raise ValueError("source SQLite sidecars changed during archive creation")
    if _directory_non_target_digest(
        source=source,
        archive=Path(str(plan["archive_path"])),
    ) != str(plan["directory_non_target_digest"]):
        raise ValueError("non-target backup directory entries changed during archiving")


def _source_sidecars(source: Path) -> list[dict[str, Any]]:
    result = []
    for suffix in SOURCE_SIDECAR_SUFFIXES:
        path = Path(str(source) + suffix)
        if path.is_symlink():
            raise ValueError(f"SQLite source sidecar is not a regular file: {path}")
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError(f"SQLite source sidecar is not a regular file: {path}")
        stat = path.stat()
        result.append(
            {
                "path": str(path),
                "suffix": suffix,
                "size_bytes": int(stat.st_size),
                "inode": int(stat.st_ino),
                "mtime_ns": int(stat.st_mtime_ns),
                "mode": oct(stat.st_mode & 0o777),
            }
        )
    rollback_journal = Path(str(source) + "-journal")
    if rollback_journal.is_symlink():
        raise ValueError("SQLite source rollback journal is not a regular file")
    if rollback_journal.exists():
        raise ValueError("SQLite source rollback journal exists")
    return result


def _validate_source_sidecars(sidecars: list[dict[str, Any]]) -> None:
    for item in sidecars:
        if str(item.get("suffix") or "") == "-wal" and int(
            item.get("size_bytes") or 0
        ) != 0:
            raise ValueError("SQLite source WAL is non-empty; backup is not immutable")


def _remove_owned_sidecars(
    *,
    source: Path,
    expected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actual = _source_sidecars(source)
    if actual != list(expected):
        raise ValueError("SQLite source sidecars drifted before owned cleanup")
    _validate_source_sidecars(actual)
    removed = []
    for item in actual:
        path = Path(str(item["path"]))
        path.unlink()
        removed.append(dict(item))
    return removed


def _directory_non_target_digest(*, source: Path, archive: Path) -> str:
    excluded = {
        source.name,
        *(source.name + suffix for suffix in SOURCE_SIDECAR_SUFFIXES),
        source.name + "-journal",
        archive.name,
        archive.name + ".manifest.json",
    }
    rows = []
    for path in sorted(source.parent.iterdir(), key=lambda item: item.name):
        if path.name in excluded or path.name.startswith(archive.name + ".tmp-"):
            continue
        stat = path.lstat()
        rows.append(
            {
                "name": path.name,
                "mode": oct(stat.st_mode & 0o7777),
                "size_bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "inode": int(stat.st_ino),
                "symlink": path.is_symlink(),
            }
        )
    return _hash(rows)


def _validate_manifest_source_identity(
    manifest: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    for key in ("source_path", "source_size_bytes", "source_sha256"):
        if str(manifest.get(key)) != str(plan.get(key)):
            raise ValueError("verified archive does not match current source identity")
    if str(manifest.get("fingerprint") or "") != str(plan.get("fingerprint") or _hash(plan)):
        raise ValueError("verified archive fingerprint does not match current dry-run")


def _owned_orphan_artifacts(
    *,
    source: Path,
    archive: Path,
    staging_directory: Path,
) -> list[str]:
    candidates = [
        *source.parent.glob(archive.name + ".tmp-*"),
        *(
            staging_directory.glob(archive.name + ".tmp-*")
            if staging_directory != source.parent
            else []
        ),
        Path(str(source) + "-wal"),
        Path(str(source) + "-shm"),
        Path(str(source) + "-journal"),
    ]
    return sorted(str(path) for path in candidates if path.exists())


def _same_filesystem(left: Path, right: Path) -> bool:
    return left.stat().st_dev == right.stat().st_dev


def _compressed_size(*, zstd: str, source: Path) -> int:
    process = subprocess.Popen(
        [zstd, "-1", "-T0", "-q", "-c", "--", str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        raise ValueError("zstd compression measurement stdout is unavailable")
    size = 0
    for chunk in iter(lambda: process.stdout.read(CHUNK_SIZE), b""):
        size += len(chunk)
    stderr = process.stderr.read() if process.stderr is not None else b""
    return_code = process.wait()
    if return_code:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            f"zstd compression measurement failed: {message or return_code}"
        )
    return size


def _compress_to_named_temp(
    *,
    zstd: str,
    source: Path,
    temp_path: Path,
) -> tuple[str, str, int]:
    temp_descriptor = os.open(
        temp_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(temp_descriptor, "wb") as output:
        completed = subprocess.run(
            [zstd, "-1", "-T0", "-q", "-c", "--", str(source)],
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
    return _file_hash(temp_path), decompressed_sha256, decompressed_size


def _compress_to_unnamed_stage(
    *,
    zstd: str,
    source: Path,
    staged: BinaryIO,
) -> tuple[str, str, int, int]:
    completed = subprocess.run(
        [zstd, "-1", "-T0", "-q", "-c", "--", str(source)],
        stdout=staged,
        stderr=subprocess.PIPE,
        check=False,
    )
    staged.flush()
    os.fsync(staged.fileno())
    if completed.returncode:
        raise ValueError(_command_error("zstd compression failed", completed))
    archive_size = int(os.fstat(staged.fileno()).st_size)
    staged.seek(0)
    tested = subprocess.run(
        [zstd, "-q", "-t"],
        stdin=staged,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if tested.returncode:
        raise ValueError(_command_error("zstd archive test failed", tested))
    staged.seek(0)
    decompressed_sha256, decompressed_size = _decompressed_stream_hash_and_size(
        zstd=zstd,
        source=staged,
    )
    staged.seek(0)
    archive_sha256 = _stream_hash(staged)
    staged.seek(0)
    return (
        archive_sha256,
        decompressed_sha256,
        decompressed_size,
        archive_size,
    )


def _copy_stage_to_named_temp(
    *,
    staged: BinaryIO,
    temp_path: Path,
    expected_sha256: str,
) -> None:
    descriptor = os.open(
        temp_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    staged.seek(0)
    with os.fdopen(descriptor, "wb") as output:
        shutil.copyfileobj(staged, output, length=CHUNK_SIZE)
        output.flush()
        os.fsync(output.fileno())
    if _file_hash(temp_path) != expected_sha256:
        raise ValueError("cross-filesystem archive publication copy changed")


def _decompressed_hash_and_size(*, zstd: str, archive: Path) -> tuple[str, int]:
    with archive.open("rb") as source:
        return _decompressed_stream_hash_and_size(zstd=zstd, source=source)


def _decompressed_stream_hash_and_size(
    *,
    zstd: str,
    source: BinaryIO,
) -> tuple[str, int]:
    process = subprocess.Popen(
        [zstd, "-q", "-d", "-c"],
        stdin=source,
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


def _stream_hash(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    _update_hash(handle, digest)
    return "sha256:" + digest.hexdigest()


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
