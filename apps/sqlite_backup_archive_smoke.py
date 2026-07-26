"""Safety smoke for lossless immutable SQLite backup archiving."""

from __future__ import annotations

from argparse import Namespace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sqlite_backup_archive import run, verify_archive_manifest  # noqa: E402


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_dir = root / "backups" / "canonical"
        backup_dir.mkdir(parents=True)
        source = backup_dir / "runtime.canonical-backup.sqlite3"
        with sqlite3.connect(source) as conn:
            conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
            conn.executemany(
                "INSERT INTO evidence(value) VALUES(?)",
                [("supplier-factual-correction-" + str(index),) for index in range(1000)],
            )
            conn.commit()
        source_sha = _sha256(source.read_bytes())
        source_link = backup_dir / "linked-source.sqlite3"
        source_link.symlink_to(source)
        try:
            run(_args(source_link))
        except ValueError as exc:
            _assert("must not be symlinks" in str(exc), "source symlink rejected")
        else:
            raise AssertionError("source symlink unexpectedly accepted")
        archive_link = backup_dir / "linked-archive.sqlite3.zst"
        archive_link.symlink_to(source)
        try:
            run(_args(source, archive=archive_link))
        except ValueError as exc:
            _assert("must not be symlinks" in str(exc), "archive symlink rejected")
        else:
            raise AssertionError("archive symlink unexpectedly accepted")
        dry = run(_args(source))
        _assert(dry["status"] == "ready", "dry-run ready")
        _assert(dry["source_integrity_check"] == "ok", "source integrity")
        _assert(
            dry["source_sqlite_open"]
            == {"mode": "ro", "immutable": True, "query_only": True},
            "dry-run uses immutable query-only SQLite",
        )
        _assert(
            dry["capacity"]["sufficient"],
            "dry-run proves archive capacity",
        )
        with mock.patch(
            "apps.sqlite_backup_archive.shutil.disk_usage",
            return_value=shutil._ntuple_diskusage(1, 1, 0),
        ):
            try:
                run(
                    _args(
                        source,
                        apply=True,
                        fingerprint=dry["fingerprint"],
                    )
                )
            except ValueError as exc:
                _assert(
                    "insufficient archive headroom" in str(exc),
                    "capacity rejection is explicit",
                )
            else:
                raise AssertionError("insufficient archive headroom was accepted")
        _assert(
            source.is_file()
            and not list(backup_dir.glob("*.tmp-*")),
            "capacity failure creates no archive artifact",
        )
        try:
            run(_args(source, apply=True, fingerprint="sha256:wrong"))
        except ValueError as exc:
            _assert("exact current dry-run fingerprint" in str(exc), "wrong fingerprint rejected")
        else:
            raise AssertionError("wrong fingerprint unexpectedly applied")
        _assert(source.is_file(), "wrong fingerprint preserves source")
        real_subprocess_run = subprocess.run
        temporary_modes: list[int] = []

        def observing_subprocess_run(*args, **kwargs):
            command = list(args[0]) if args else []
            if "-1" in command and "-c" in command:
                temporary_modes.extend(
                    path.stat().st_mode & 0o777
                    for path in backup_dir.glob("*.zst.tmp-*")
                )
            return real_subprocess_run(*args, **kwargs)

        with mock.patch(
            "apps.sqlite_backup_archive.subprocess.run",
            side_effect=observing_subprocess_run,
        ):
            applied = run(_args(source, apply=True, fingerprint=dry["fingerprint"]))
        archive = Path(applied["archive"]["archive_path"])
        _assert(applied["applied"] is True and applied["source_removed"] is True, "archive applied")
        _assert(temporary_modes == [0o600], "temporary archive private mode")
        _assert(not source.exists() and archive.is_file(), "source replaced by archive")
        _assert((archive.stat().st_mode & 0o777) == 0o600, "archive private mode")
        decompressed = subprocess.check_output(["zstd", "-q", "-d", "-c", "--", str(archive)])
        _assert(_sha256(decompressed) == source_sha, "lossless decompressed sha")
        manifest = json.loads(Path(applied["manifest_path"]).read_text(encoding="utf-8"))
        _assert(manifest["decompressed_sha256"] == source_sha, "manifest sha")
        _assert(
            manifest["lifecycle_state"] == "retained"
            and manifest["source_removed"] is True
            and not applied["orphan_artifacts"],
            "retained lifecycle has no raw or orphan artifact",
        )
        manifest_link = backup_dir / "linked-manifest.json"
        manifest_link.symlink_to(Path(applied["manifest_path"]))
        try:
            verify_archive_manifest(manifest_link)
        except ValueError as exc:
            _assert("manifest is unavailable" in str(exc), "manifest symlink rejected")
        else:
            raise AssertionError("manifest symlink unexpectedly accepted")

        resumed_source = backup_dir / "runtime.resume.sqlite3"
        with sqlite3.connect(resumed_source) as conn:
            conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT)")
            conn.execute("INSERT INTO evidence(value) VALUES('resume')")
            conn.commit()
        Path(str(resumed_source) + "-wal").write_bytes(b"")
        Path(str(resumed_source) + "-shm").write_bytes(b"\0" * 32768)
        unrelated = backup_dir / "unrelated.retained"
        unrelated.write_text("do-not-change", encoding="utf-8")
        unrelated_identity = (
            unrelated.stat().st_ino,
            unrelated.stat().st_size,
            unrelated.stat().st_mtime_ns,
        )
        resumed_dry = run(_args(resumed_source))
        real_verify = verify_archive_manifest
        with mock.patch(
            "apps.sqlite_backup_archive.verify_archive_manifest",
            side_effect=ValueError("injected independent readback failure"),
        ):
            try:
                run(
                    _args(
                        resumed_source,
                        apply=True,
                        fingerprint=resumed_dry["fingerprint"],
                    )
                )
            except ValueError as exc:
                _assert(
                    "injected independent readback failure" in str(exc),
                    "independent archive readback failure is surfaced",
                )
            else:
                raise AssertionError("archive readback failure was ignored")
        _assert(
            resumed_source.is_file()
            and Path(str(resumed_source) + ".zst").is_file()
            and Path(str(resumed_source) + ".zst.manifest.json").is_file(),
            "failed readback preserves raw and verified resume artifacts",
        )
        _assert(
            Path(str(resumed_source) + "-wal").is_file()
            and Path(str(resumed_source) + "-shm").is_file(),
            "failed readback preserves owned sidecars",
        )
        _assert(
            real_verify(
                Path(str(resumed_source) + ".zst.manifest.json")
            )["lifecycle_state"]
            == "verified_pending_source_removal",
            "interrupted lifecycle is machine-readable",
        )
        resumed = run(
            _args(
                resumed_source,
                apply=True,
                fingerprint=resumed_dry["fingerprint"],
            )
        )
        _assert(
            resumed["source_removed"]
            and not resumed_source.exists()
            and not Path(str(resumed_source) + "-wal").exists()
            and not Path(str(resumed_source) + "-shm").exists(),
            "exact resume removes only raw source and owned sidecars",
        )
        _assert(
            (
                unrelated.stat().st_ino,
                unrelated.stat().st_size,
                unrelated.stat().st_mtime_ns,
            )
            == unrelated_identity,
            "unrelated backup entry is unchanged",
        )

        live = root / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(live) as conn:
            conn.execute("CREATE TABLE live(id INTEGER)")
        try:
            run(_args(live))
        except ValueError as exc:
            _assert("backups directory" in str(exc), "live path rejected")
        else:
            raise AssertionError("non-backup SQLite unexpectedly accepted")
    print("sqlite_backup_archive_smoke: ok")
    return 0


def _args(
    source: Path,
    *,
    archive: Path | None = None,
    apply: bool = False,
    fingerprint: str = "",
) -> Namespace:
    return Namespace(
        source=str(source),
        archive=str(archive) if archive is not None else None,
        apply=apply,
        fingerprint=fingerprint,
    )


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _assert(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
