#!/usr/bin/env python3
"""Local lifecycle tests; no external transport is imported or called."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from apps.wb_autoanswers_activation import run
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
