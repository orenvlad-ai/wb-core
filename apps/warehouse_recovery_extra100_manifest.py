#!/usr/bin/env python3
"""Read-only inventory and exact copy readback for warehouse-recovery migration.

`scan` writes a portable manifest outside the source tree. `compare` never
changes either tree and accepts only the one placement marker as an additional
file. Mount and writer-lock checks remain part of the governed cutover runbook.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote


CONTRACT = "wb_core_warehouse_recovery_copy_manifest_v1"
PLACEMENT_MARKER = ".warehouse-recovery-extra100-active.json"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_check(path: Path) -> None:
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    if rows != ["ok"]:
        raise ValueError(f"SQLite quick_check failed: {path}: {rows[:3]}")


def inventory(root: Path) -> dict:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"recovery root is not a regular directory: {root}")
    entries: list[dict] = []
    directories: list[dict] = []
    root_device = root.stat().st_dev

    def fail_walk(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail_walk):
        parent = Path(directory)
        parent_stat = parent.stat()
        if parent_stat.st_dev != root_device:
            raise ValueError(f"nested filesystem in recovery tree: {parent}")
        directories.append({
            "path": parent.relative_to(root).as_posix(),
            "mode": parent_stat.st_mode & 0o7777,
            "uid": parent_stat.st_uid,
            "gid": parent_stat.st_gid,
        })
        for name in dirs:
            if (parent / name).is_symlink():
                raise ValueError(f"symlink directory in recovery tree: {parent / name}")
        for name in sorted(files):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if relative == PLACEMENT_MARKER:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"nonregular file in recovery tree: {path}")
            before = path.stat()
            if before.st_dev != root_device:
                raise ValueError(f"file on another filesystem: {path}")
            digest = _sha256(path)
            if path.suffix == ".sqlite3":
                _sqlite_check(path)
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise ValueError(f"recovery file changed during inventory: {path}")
            entries.append(
                {
                    "path": relative,
                    "size_bytes": before.st_size,
                    "allocated_bytes": before.st_blocks * 512,
                    "sha256": digest,
                    "mode": before.st_mode & 0o7777,
                    "uid": before.st_uid,
                    "gid": before.st_gid,
                }
            )
    entries.sort(key=lambda item: item["path"])
    directories.sort(key=lambda item: item["path"])
    return {
        "contract_version": CONTRACT,
        "file_count": len(entries),
        "total_size_bytes": sum(item["size_bytes"] for item in entries),
        "total_allocated_bytes": sum(item["allocated_bytes"] for item in entries),
        "entries": entries,
        "entries_sha256": hashlib.sha256(_canonical(entries)).hexdigest(),
        "directories": directories,
        "directories_sha256": hashlib.sha256(_canonical(directories)).hexdigest(),
    }


def compare(manifest: dict, root: Path) -> dict:
    if manifest.get("contract_version") != CONTRACT:
        raise ValueError("copy manifest contract mismatch")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or hashlib.sha256(_canonical(entries)).hexdigest() != manifest.get("entries_sha256"):
        raise ValueError("copy manifest entries fingerprint mismatch")
    directories = manifest.get("directories")
    if not isinstance(directories, list) or hashlib.sha256(_canonical(directories)).hexdigest() != manifest.get("directories_sha256"):
        raise ValueError("copy manifest directories fingerprint mismatch")
    observed = inventory(root)
    if directories != observed["directories"]:
        raise ValueError("recovery directory topology or metadata differs from manifest")
    # Allocation may legitimately differ across ext4 volumes; compare content.
    expected_content = [
        (item["path"], item["size_bytes"], item["sha256"], item["mode"], item["uid"], item["gid"])
        for item in entries
    ]
    observed_content = [
        (item["path"], item["size_bytes"], item["sha256"], item["mode"], item["uid"], item["gid"])
        for item in observed["entries"]
    ]
    if expected_content != observed_content:
        raise ValueError("recovery copy differs from manifest")
    return {
        "ok": True,
        "file_count": observed["file_count"],
        "total_size_bytes": observed["total_size_bytes"],
        "root": str(Path(root).resolve()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--root", required=True, type=Path)
    scan.add_argument("--output", required=True, type=Path)
    verify = sub.add_parser("compare")
    verify.add_argument("--root", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            result = inventory(args.root)
            output = args.output.resolve()
            if output == args.root.resolve() or args.root.resolve() in output.parents:
                raise ValueError("copy manifest output must stay outside recovery tree")
            with output.open("xb") as handle:
                handle.write(_canonical(result) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            print(json.dumps({"ok": True, "manifest": str(output), "file_count": result["file_count"], "entries_sha256": result["entries_sha256"]}))
        else:
            result = compare(json.loads(args.manifest.read_text(encoding="utf-8")), args.root)
            print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
