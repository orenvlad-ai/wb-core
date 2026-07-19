#!/usr/bin/env python3
"""Safety smoke for exact cleanup of a proven-invalid partial SQLite backup."""

from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sqlite_failed_backup_cleanup import run  # noqa: E402
import packages.application.registry_upload_db_backed_runtime as runtime_module  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_dir = root / "backups" / "warehouse-functional"
        backup_dir.mkdir(parents=True)
        partial = backup_dir / "warehouse_functional_cutover_v1-partial.sqlite3"
        partial.write_bytes(b"\x00" * 8192)
        dry = run(_args(partial))
        _assert(dry["status"] == "ready", "invalid partial cleanup dry-run ready")
        _assert(dry["invalid_reason"] == "invalid_sqlite_header", "invalid header proven")
        _assert(dry["would_remove_invalid_bytes"] == 8192, "exact invalid byte count")
        try:
            run(_args(partial, apply=True, fingerprint="sha256:wrong"))
        except ValueError as exc:
            _assert("exact current dry-run fingerprint" in str(exc), "wrong fingerprint rejected")
        else:
            raise AssertionError("wrong cleanup fingerprint unexpectedly applied")
        _assert(partial.is_file(), "wrong fingerprint preserves partial")
        applied = run(_args(partial, apply=True, fingerprint=dry["fingerprint"]))
        manifest_path = Path(applied["manifest_path"])
        _assert(applied["source_removed"] is True and not partial.exists(), "invalid partial removed")
        _assert(manifest_path.is_file(), "cleanup audit manifest retained")
        _assert((manifest_path.stat().st_mode & 0o777) == 0o600, "cleanup manifest private")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _assert(manifest["status"] == "invalid_partial_removed", "cleanup completion audited")
        _assert(manifest["source_sha256"] == dry["source_sha256"], "cleanup SHA audited")
        repeated = run(_args(partial, apply=True, fingerprint=dry["fingerprint"]))
        _assert(repeated["idempotent"] is True, "repeated exact cleanup is idempotent")

        coherent = backup_dir / "coherent.sqlite3"
        with sqlite3.connect(coherent) as conn:
            conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY)")
        try:
            run(_args(coherent))
        except ValueError as exc:
            _assert("coherent SQLite" in str(exc), "coherent backup rejected")
        else:
            raise AssertionError("coherent backup unexpectedly eligible for cleanup")
        _assert(coherent.is_file(), "coherent backup preserved")

        outside = root / "partial.sqlite3"
        outside.write_bytes(b"\x00" * 16)
        try:
            run(_args(outside))
        except ValueError as exc:
            _assert("backups directory" in str(exc), "non-backup path rejected")
        else:
            raise AssertionError("non-backup path unexpectedly eligible")

    with TemporaryDirectory() as tmp:
        runtime = RegistryUploadDbBackedRuntime(Path(tmp) / "runtime")
        runtime.runtime_dir.mkdir(parents=True)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute("CREATE TABLE source(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO source(value) VALUES('evidence')")
        completed_destination = Path(tmp) / "backups" / "warehouse-functional" / "complete.sqlite3"
        completed = runtime.backup_database(completed_destination)
        _assert(completed["integrity_check"] == "ok", "coherent backup succeeds")
        _assert((completed_destination.stat().st_mode & 0o777) == 0o600, "backup private from creation")
        completed_destination.unlink()
        destination = Path(tmp) / "backups" / "warehouse-functional" / "attempt.sqlite3"
        with mock.patch.object(
            runtime_module.shutil,
            "disk_usage",
            return_value=mock.Mock(free=0),
        ):
            try:
                runtime.backup_database(destination)
            except ValueError as exc:
                _assert("insufficient filesystem capacity" in str(exc), "capacity preflight fails closed")
            else:
                raise AssertionError("insufficient-capacity backup unexpectedly started")
        _assert(not destination.exists(), "capacity preflight creates no partial file")

        class FakeConnection:
            def __init__(self, path: Path, *, source: bool) -> None:
                self.path = path
                self.source = source

            def __enter__(self) -> "FakeConnection":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def backup(self, target: "FakeConnection") -> None:
                target.path.parent.mkdir(parents=True, exist_ok=True)
                target.path.write_bytes(b"\x00" * 4096)
                Path(str(target.path) + "-journal").write_bytes(b"partial")
                raise sqlite3.OperationalError("database or disk is full")

        def fake_connect(path: object, *_args: object, **_kwargs: object) -> FakeConnection:
            resolved = Path(path)
            return FakeConnection(resolved, source=(resolved == runtime.db_path))

        with mock.patch.object(runtime_module.sqlite3, "connect", side_effect=fake_connect):
            try:
                runtime.backup_database(destination)
            except sqlite3.OperationalError as exc:
                _assert("disk is full" in str(exc), "original backup failure preserved")
            else:
                raise AssertionError("simulated full-disk backup unexpectedly succeeded")
        _assert(not destination.exists(), "failed backup destination removed automatically")
        _assert(not Path(str(destination) + "-journal").exists(), "failed backup sidecar removed")
    print("sqlite_failed_backup_cleanup_smoke: ok")
    return 0


def _args(source: Path, *, apply: bool = False, fingerprint: str = "") -> Namespace:
    return Namespace(source=str(source), apply=apply, fingerprint=fingerprint)


def _assert(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
