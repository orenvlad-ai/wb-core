#!/usr/bin/env python3
"""Offline copy-manifest checks using small disposable fixtures."""

from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_recovery_extra100_manifest import compare, inventory  # noqa: E402


def main() -> int:
    with TemporaryDirectory() as directory:
        base = Path(directory)
        source = base / "source"
        target = base / "target"
        source.mkdir()
        db = source / "checkpoint.sqlite3"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE proof (value TEXT NOT NULL)")
            conn.execute("INSERT INTO proof VALUES ('retained')")
        (source / "checkpoint.sqlite3.manifest.json").write_text("{}", encoding="utf-8")
        manifest = inventory(source)
        shutil.copytree(source, target)
        (target / ".warehouse-recovery-extra100-active.json").write_text("{}", encoding="utf-8")
        assert compare(manifest, target)["ok"] is True
        (target / "checkpoint.sqlite3.manifest.json").write_text("changed", encoding="utf-8")
        try:
            compare(manifest, target)
        except ValueError:
            pass
        else:
            raise AssertionError("copy content drift was accepted")
    print("warehouse_recovery_extra100_manifest_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
