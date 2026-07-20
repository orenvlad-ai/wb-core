#!/usr/bin/env python3
"""Local lifecycle tests; no external transport is imported or called."""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from apps.wb_autoanswers_activation import (
    _capacity_heartbeat,
    _compress_verified_backup,
    _create_current_compressed_schema_backup,
    run,
)
from apps.wb_autoanswers_runtime_test import MutableClock, feedback
from packages.application.wb_autoanswers_runtime import AutoanswersRepository


GOOD_DEPENDENCIES = {
    "node_present": True,
    "node_major": 20,
    "node_supported": True,
    "ffmpeg_present": True,
    "frozen_boundary_verified": True,
}


class ActivationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.runtime_dir = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_capacity_heartbeat_keeps_long_remote_verification_observable(self) -> None:
        output = StringIO()
        with patch("apps.wb_autoanswers_activation.CAPACITY_HEARTBEAT_SECONDS", 0.01):
            with redirect_stderr(output), _capacity_heartbeat():
                time.sleep(0.03)
        self.assertIn("backup capacity verification in progress", output.getvalue())

    @patch("apps.wb_autoanswers_activation._dependency_status", return_value=GOOD_DEPENDENCIES)
    def test_prepare_deploy_migrates_with_verified_backup_while_force_off(self, _dependency: object) -> None:
        db_path = self.runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
            conn.execute("INSERT INTO legacy_marker VALUES('preserved')")
        with patch.dict(os.environ, {"WB_AUTOANSWERS_FORCE_OFF": "true"}, clear=False):
            result = run(action="prepare-deploy", runtime_dir=self.runtime_dir)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["schema_backup"]["integrity_check"], "ok")
        self.assertIn(2, {int(row["version"]) for row in result["runtime"]["schema_migrations"]})
        with sqlite3.connect(db_path) as conn:
            self.assertEqual(conn.execute("SELECT value FROM legacy_marker").fetchone()[0], "preserved")

    @patch("apps.wb_autoanswers_activation._dependency_status", return_value=GOOD_DEPENDENCIES)
    def test_activate_manual_is_idempotent_and_deactivate_returns_off(self, _dependency: object) -> None:
        AutoanswersRepository(runtime_dir=self.runtime_dir, now_factory=MutableClock(), env={})
        with patch.dict(os.environ, {"WB_AUTOANSWERS_FORCE_OFF": "false"}, clear=False):
            activated = run(action="activate-manual", runtime_dir=self.runtime_dir)
            repeated = run(action="activate-manual", runtime_dir=self.runtime_dir)
            deactivated = run(action="deactivate", runtime_dir=self.runtime_dir)
        self.assertEqual(activated["status"], "activated")
        self.assertEqual(activated["runtime"]["settings"]["mode"], "manual")
        self.assertTrue(activated["runtime"]["settings"]["master_enabled"])
        self.assertEqual(repeated["status"], "already_active")
        self.assertFalse(deactivated["runtime"]["settings"]["master_enabled"])

    @patch("apps.wb_autoanswers_activation._dependency_status", return_value=GOOD_DEPENDENCIES)
    def test_force_off_and_existing_work_fail_closed(self, _dependency: object) -> None:
        repo = AutoanswersRepository(runtime_dir=self.runtime_dir, now_factory=MutableClock(), env={})
        with patch.dict(os.environ, {"WB_AUTOANSWERS_FORCE_OFF": "true"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "FORCE_OFF=false"):
                run(action="activate-manual", runtime_dir=self.runtime_dir)

        repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        outcome = repo.upsert_feedback(feedback("queued"), source_stream="unanswered", run_kind="steady")
        repo.enqueue_processing(outcome["feedback_id"], trigger_source="automatic", actor_id="sync")
        repo.update_settings(master_enabled=False, actor_id="admin")
        with patch.dict(os.environ, {"WB_AUTOANSWERS_FORCE_OFF": "false"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "empty AI/publication queue"):
                run(action="activate-manual", runtime_dir=self.runtime_dir)

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required for backup compaction acceptance")
    def test_schema_v1_backup_is_removed_only_after_verified_byte_exact_compression(self) -> None:
        backup_dir = self.runtime_dir / "backups" / "wb_autoanswers_schema_v1"
        backup_dir.mkdir(parents=True)
        source = backup_dir / "registry_upload_runtime__pre_autoanswers_v1__test.sqlite3"
        with sqlite3.connect(source) as conn:
            conn.execute("CREATE TABLE evidence(value BLOB NOT NULL)")
            conn.execute("INSERT INTO evidence VALUES(?)", (os.urandom(1024 * 1024),))
        original = source.read_bytes()
        result = _compress_verified_backup(source)
        compressed = backup_dir / result["compressed_filename"]
        manifest = backup_dir / result["manifest_filename"]
        self.assertFalse(source.exists())
        self.assertTrue(compressed.is_file())
        self.assertTrue(manifest.is_file())
        restored = subprocess.run(
            ["zstd", "--decompress", "--stdout", "--quiet", str(compressed)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
        self.assertEqual(restored, original)
        self.assertTrue(result["raw_source_removed_after_verification"])

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required for backup replacement acceptance")
    def test_current_snapshot_replaces_legacy_backup_and_is_accepted_for_schema_v2(self) -> None:
        database = self.runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
            conn.execute("INSERT INTO legacy_marker VALUES('current')")
        legacy_dir = self.runtime_dir / "backups" / "wb_autoanswers_schema_v1"
        legacy_dir.mkdir(parents=True)
        legacy = legacy_dir / "registry_upload_runtime__pre_autoanswers_v1__test.sqlite3"
        with sqlite3.connect(legacy) as conn:
            conn.execute("CREATE TABLE old_marker(value TEXT NOT NULL)")
            conn.execute("INSERT INTO old_marker VALUES('old')")

        result = _create_current_compressed_schema_backup(
            self.runtime_dir,
            legacy_source=legacy,
        )
        self.assertEqual(result["status"], "replaced_with_current_compressed_backup")
        self.assertFalse(legacy.exists())
        self.assertTrue(result["snapshot_raw_removed_after_verification"])

        repository = AutoanswersRepository(runtime_dir=self.runtime_dir, now_factory=MutableClock(), env={})
        backup = repository.verified_schema_backup_status()
        self.assertEqual(backup["integrity_check"], "ok")
        self.assertEqual(backup["format"], "zstd")
        with sqlite3.connect(database) as conn:
            self.assertEqual(conn.execute("SELECT value FROM legacy_marker").fetchone()[0], "current")


if __name__ == "__main__":
    unittest.main(verbosity=2)
