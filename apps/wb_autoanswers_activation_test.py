#!/usr/bin/env python3
"""Local lifecycle tests; no external transport is imported or called."""

from __future__ import annotations

from contextlib import closing, redirect_stderr
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from apps.wb_autoanswers_activation import (
    BACKUP_OPERATIONAL_HEADROOM_BYTES,
    _capacity_heartbeat,
    _compress_verified_backup,
    _compress_verified_current_schema_backup,
    _create_current_compressed_schema_backup,
    _create_streamed_current_compressed_schema_backup,
    _deployment_quiesce,
    _integrity_check,
    _prepare_backup_capacity,
    _schema_preparation_lock,
    run,
)
from apps.wb_autoanswers_runtime_test import MutableClock, feedback
from packages.application.wb_autoanswers_runtime import AutoanswersRepository, SCHEMA_VERSION


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

    @staticmethod
    def _systemd_show_result(
        command: list[str],
        *,
        active_state: str,
        sub_state: str,
        result: str = "success",
        main_pid: str = "0",
        exec_main_status: str = "0",
        invocation_id: str = "",
        include_process_fields: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        fields = [
            "LoadState=loaded",
            f"ActiveState={active_state}",
            f"SubState={sub_state}",
            f"Result={result}",
            f"InvocationID={invocation_id}",
        ]
        if include_process_fields:
            fields.extend(
                (
                    f"MainPID={main_pid}",
                    f"ExecMainStatus={exec_main_status}",
                )
            )
        stdout = "\n".join(fields)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def test_deploy_quiesce_drains_active_provider_job_without_service_stop(self) -> None:
        commands: list[list[str]] = []
        worker_show_count = 0

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal worker_show_count
            commands.append(command)
            if command[:2] != ["systemctl", "show"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            unit = command[2]
            if unit.endswith(".timer") or unit == "wb-core-registry-http.service":
                return self._systemd_show_result(
                    command,
                    active_state="active",
                    sub_state="waiting" if unit.endswith(".timer") else "running",
                    main_pid="321" if unit == "wb-core-registry-http.service" else "0",
                    include_process_fields=not unit.endswith(".timer"),
                )
            if unit == "wb-core-autoanswers-worker.service":
                worker_show_count += 1
                if worker_show_count < 3:
                    return self._systemd_show_result(
                        command,
                        active_state="activating",
                        sub_state="start",
                        main_pid="456",
                        invocation_id="provider-call-in-flight",
                    )
            return self._systemd_show_result(
                command,
                active_state="inactive",
                sub_state="dead",
            )

        with (
            patch.dict(
                os.environ,
                {"WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE": "true"},
                clear=False,
            ),
            patch("apps.wb_autoanswers_activation.subprocess.run", side_effect=fake_run),
            patch("apps.wb_autoanswers_activation.time.sleep", return_value=None),
        ):
            with _deployment_quiesce() as evidence:
                self.assertTrue(evidence["applied"])
                self.assertEqual(evidence["active_service_stop_submits"], 0)
                self.assertGreaterEqual(len(evidence["service_drain_samples"]), 2)

        stopped_units = [command[2] for command in commands if command[:2] == ["systemctl", "stop"]]
        self.assertIn("wb-core-autoanswers-worker.timer", stopped_units)
        self.assertIn("wb-core-autoanswers-readonly-sync.timer", stopped_units)
        self.assertIn("wb-core-registry-http.service", stopped_units)
        self.assertNotIn("wb-core-autoanswers-worker.service", stopped_units)
        self.assertNotIn("wb-core-autoanswers-readonly-sync.service", stopped_units)

    def test_deploy_quiesce_timeout_restores_timers_without_killing_worker(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[:2] != ["systemctl", "show"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            unit = command[2]
            if unit == "wb-core-autoanswers-worker.service":
                return self._systemd_show_result(
                    command,
                    active_state="activating",
                    sub_state="start",
                    main_pid="456",
                    invocation_id="hung-provider-call",
                )
            if unit.endswith(".timer") or unit == "wb-core-registry-http.service":
                return self._systemd_show_result(
                    command,
                    active_state="active",
                    sub_state="waiting" if unit.endswith(".timer") else "running",
                    main_pid="321" if unit == "wb-core-registry-http.service" else "0",
                    include_process_fields=not unit.endswith(".timer"),
                )
            return self._systemd_show_result(
                command,
                active_state="inactive",
                sub_state="dead",
            )

        with (
            patch.dict(
                os.environ,
                {"WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE": "true"},
                clear=False,
            ),
            patch("apps.wb_autoanswers_activation.DEPLOYMENT_DRAIN_TIMEOUT_SECONDS", 0),
            patch("apps.wb_autoanswers_activation.subprocess.run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(RuntimeError, "drain timed out"):
                with _deployment_quiesce():
                    self.fail("timed-out quiesce must not enter the mutation body")

        stopped_units = [command[2] for command in commands if command[:2] == ["systemctl", "stop"]]
        started_units = [command[2] for command in commands if command[:2] == ["systemctl", "start"]]
        self.assertNotIn("wb-core-autoanswers-worker.service", stopped_units)
        self.assertNotIn("wb-core-registry-http.service", stopped_units)
        self.assertEqual(
            started_units,
            [
                "wb-core-autoanswers-worker.timer",
                "wb-core-autoanswers-readonly-sync.timer",
            ],
        )

    def test_deploy_quiesce_preserves_no_active_timer(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[:2] != ["systemctl", "show"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            unit = command[2]
            if unit == "wb-core-autoanswers-worker.timer":
                return self._systemd_show_result(
                    command,
                    active_state="inactive",
                    sub_state="dead",
                    include_process_fields=False,
                )
            if unit == "wb-core-autoanswers-readonly-sync.timer":
                return self._systemd_show_result(
                    command,
                    active_state="active",
                    sub_state="waiting",
                    include_process_fields=False,
                )
            if unit == "wb-core-registry-http.service":
                return self._systemd_show_result(
                    command,
                    active_state="active",
                    sub_state="running",
                    main_pid="321",
                )
            return self._systemd_show_result(command, active_state="inactive", sub_state="dead")

        with (
            patch.dict(
                os.environ,
                {"WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE": "true"},
                clear=False,
            ),
            patch("apps.wb_autoanswers_activation.subprocess.run", side_effect=fake_run),
        ):
            with _deployment_quiesce() as evidence:
                self.assertEqual(
                    evidence["active_timers"],
                    ["wb-core-autoanswers-readonly-sync.timer"],
                )

        worker_timer_starts = [
            command
            for command in commands
            if command == ["systemctl", "start", "wb-core-autoanswers-worker.timer"]
        ]
        self.assertEqual(worker_timer_starts, [])

    def test_deploy_quiesce_rejects_unhealthy_timer(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[:2] != ["systemctl", "show"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            return self._systemd_show_result(
                command,
                active_state="failed",
                sub_state="failed",
                result="exit-code",
                include_process_fields=False,
            )

        with (
            patch.dict(
                os.environ,
                {"WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE": "true"},
                clear=False,
            ),
            patch("apps.wb_autoanswers_activation.subprocess.run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(RuntimeError, "unit is unhealthy"):
                with _deployment_quiesce():
                    self.fail("unhealthy timer must fail before the mutation body")

        self.assertFalse(any(command[:2] == ["systemctl", "stop"] for command in commands))

    def test_deploy_quiesce_rejects_unhealthy_service(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[:2] != ["systemctl", "show"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            unit = command[2]
            if unit.endswith(".timer"):
                return self._systemd_show_result(
                    command,
                    active_state="active",
                    sub_state="waiting",
                    include_process_fields=False,
                )
            if unit == "wb-core-autoanswers-worker.service":
                return self._systemd_show_result(
                    command,
                    active_state="failed",
                    sub_state="failed",
                    result="exit-code",
                    exec_main_status="1",
                )
            return self._systemd_show_result(
                command,
                active_state="inactive",
                sub_state="dead",
            )

        with (
            patch.dict(
                os.environ,
                {"WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE": "true"},
                clear=False,
            ),
            patch("apps.wb_autoanswers_activation.subprocess.run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(RuntimeError, "unit is unhealthy"):
                with _deployment_quiesce():
                    self.fail("unhealthy service must fail before the mutation body")

        self.assertFalse(any(command[:2] == ["systemctl", "stop"] for command in commands))

    def test_capacity_heartbeat_keeps_long_remote_verification_observable(self) -> None:
        output = StringIO()
        with patch("apps.wb_autoanswers_activation.CAPACITY_HEARTBEAT_SECONDS", 0.01):
            with redirect_stderr(output), _capacity_heartbeat():
                time.sleep(0.03)
        self.assertIn("backup capacity verification in progress", output.getvalue())

    def test_integrity_check_closes_snapshot_before_capacity_readback(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = ("ok",)
        with patch(
            "apps.wb_autoanswers_activation.sqlite3.connect",
            return_value=connection,
        ):
            self.assertEqual(
                _integrity_check(self.runtime_dir / "snapshot.sqlite3"),
                "ok",
            )
        connection.close.assert_called_once_with()

    @patch("apps.wb_autoanswers_activation._dependency_status", return_value=GOOD_DEPENDENCIES)
    def test_prepare_deploy_migrates_with_verified_backup_while_force_off(self, _dependency: object) -> None:
        db_path = self.runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
            conn.execute("INSERT INTO legacy_marker VALUES('preserved')")
        with patch.dict(os.environ, {"WB_AUTOANSWERS_FORCE_OFF": "true"}, clear=False):
            result = run(action="prepare-deploy", runtime_dir=self.runtime_dir)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["schema_backup"]["count"], 0)
        self.assertTrue(result["runtime"]["persistence"]["isolated_from_registry"])
        self.assertIn(
            SCHEMA_VERSION,
            {int(row["version"]) for row in result["runtime"]["schema_migrations"]},
        )
        with sqlite3.connect(db_path) as conn:
            self.assertEqual(conn.execute("SELECT value FROM legacy_marker").fetchone()[0], "preserved")

    @patch("apps.wb_autoanswers_activation._dependency_status", return_value=GOOD_DEPENDENCIES)
    def test_prepare_deploy_preserves_active_manual_mode_after_additive_schema(self, _dependency: object) -> None:
        repository = AutoanswersRepository(runtime_dir=self.runtime_dir, now_factory=MutableClock(), env={})
        repository.update_settings(master_enabled=True, mode="manual", actor_id="release-train")

        with patch.dict(os.environ, {"WB_AUTOANSWERS_FORCE_OFF": "true"}, clear=False):
            result = run(action="prepare-deploy", runtime_dir=self.runtime_dir)

        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["runtime"]["settings"]["master_enabled"])
        self.assertEqual(result["runtime"]["settings"]["mode"], "manual")
        self.assertTrue(result["runtime"]["settings"]["force_off"])
        self.assertFalse(result["runtime"]["settings"]["effective_enabled"])
        persisted = AutoanswersRepository(
            runtime_dir=self.runtime_dir,
            now_factory=MutableClock(),
            env={"WB_AUTOANSWERS_FORCE_OFF": "false"},
        ).operational_status()
        self.assertTrue(persisted["settings"]["master_enabled"])
        self.assertEqual(persisted["settings"]["mode"], "manual")
        self.assertTrue(persisted["settings"]["effective_enabled"])

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
    def test_force_off_fails_closed_and_manual_preserves_existing_work(self, _dependency: object) -> None:
        repo = AutoanswersRepository(runtime_dir=self.runtime_dir, now_factory=MutableClock(), env={})
        with patch.dict(os.environ, {"WB_AUTOANSWERS_FORCE_OFF": "true"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "FORCE_OFF=false"):
                run(action="activate-manual", runtime_dir=self.runtime_dir)

        repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        outcome = repo.upsert_feedback(feedback("queued"), source_stream="unanswered", run_kind="steady")
        repo.enqueue_processing(outcome["feedback_id"], trigger_source="automatic", actor_id="sync")
        repo.update_settings(master_enabled=False, actor_id="admin")
        with patch.dict(os.environ, {"WB_AUTOANSWERS_FORCE_OFF": "false"}, clear=False):
            activated = run(action="activate-manual", runtime_dir=self.runtime_dir)
        self.assertEqual(activated["status"], "activated")
        self.assertEqual(activated["runtime"]["settings"]["mode"], "manual")
        self.assertEqual(activated["runtime"]["ai_jobs"]["queued"], 1)
        self.assertEqual(activated["runtime"]["claimable_ai_jobs"], 0)
        self.assertEqual(activated["runtime"]["claimable_publication_writes"], 0)

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
    def test_current_snapshot_replaces_legacy_backup_and_is_accepted_for_current_schema(self) -> None:
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

        backup_dir = self.runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
        redundant = backup_dir / (
            f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__redundant.sqlite3"
        )
        shutil.copy2(database, redundant)
        sidecar = redundant.with_name(redundant.name + "-journal")
        sidecar.write_bytes(b"orphan")
        cleanup = _prepare_backup_capacity(self.runtime_dir)
        self.assertFalse(redundant.exists())
        self.assertFalse(sidecar.exists())
        self.assertEqual(cleanup["compaction"]["redundant_autoanswers_raw_removed"], 1)
        self.assertGreaterEqual(cleanup["compaction"]["orphan_autoanswers_sidecars_removed"], 1)

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required for current-schema compaction")
    def test_capacity_compacts_verified_current_schema_raw_backup_before_retry(self) -> None:
        database = self.runtime_dir / "registry_upload_runtime.sqlite3"
        conn = sqlite3.connect(database)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("CREATE TABLE live_marker(value TEXT NOT NULL)")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("INSERT INTO live_marker VALUES('committed-in-wal')")
            conn.commit()
            self.assertGreater(database.with_name(database.name + "-wal").stat().st_size, 0)

            backup_dir = self.runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
            backup_dir.mkdir(parents=True)
            raw = backup_dir / (
                f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__interrupted.sqlite3"
            )
            shutil.copy2(database, raw)
            for suffix in ("-wal", "-shm"):
                shutil.copy2(
                    database.with_name(database.name + suffix),
                    raw.with_name(raw.name + suffix),
                )
        finally:
            conn.close()

        with patch(
            "apps.wb_autoanswers_activation.shutil.disk_usage",
            return_value=SimpleNamespace(free=300 * 1024 * 1024),
        ):
            result = _prepare_backup_capacity(self.runtime_dir)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            result["compaction"]["status"],
            "compressed_current_schema_backup",
        )
        self.assertFalse(raw.exists())
        self.assertFalse(raw.with_name(raw.name + "-shm").exists())
        self.assertTrue(raw.with_suffix(raw.suffix + ".zst").is_file())
        self.assertTrue(raw.with_suffix(raw.suffix + ".zst.manifest.json").is_file())
        self.assertEqual(result["compaction"]["integrity_check"], "ok")
        restored = self.runtime_dir / "restored-from-wal.sqlite3"
        with restored.open("wb") as output:
            subprocess.run(
                [
                    "zstd",
                    "--decompress",
                    "--stdout",
                    "--quiet",
                    str(raw.with_suffix(raw.suffix + ".zst")),
                ],
                stdout=output,
                stderr=subprocess.PIPE,
                check=True,
            )
        with closing(sqlite3.connect(f"file:{restored}?mode=ro", uri=True)) as restored_db:
            self.assertEqual(
                restored_db.execute("SELECT value FROM live_marker").fetchone()[0],
                "committed-in-wal",
            )
            self.assertEqual(restored_db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required for streamed backup")
    def test_low_capacity_streams_verified_snapshot_without_raw_duplicate(self) -> None:
        database = self.runtime_dir / "registry_upload_runtime.sqlite3"
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE live_marker(value TEXT NOT NULL)")
            conn.execute("INSERT INTO live_marker VALUES('streamed')")
            conn.commit()

        mib = 1024 * 1024
        with patch(
            "apps.wb_autoanswers_activation.shutil.disk_usage",
            side_effect=[
                SimpleNamespace(free=300 * mib),
                SimpleNamespace(free=280 * mib),
            ],
        ):
            result = _prepare_backup_capacity(self.runtime_dir)

        self.assertEqual(
            result["compaction"]["status"],
            "streamed_current_schema_backup",
        )
        self.assertEqual(
            result["compaction"]["snapshot_method"],
            "exclusive_lock_checkpoint_stream",
        )
        self.assertFalse(result["compaction"]["raw_snapshot_required"])
        self.assertEqual(result["compaction"]["integrity_check"], "ok")
        self.assertFalse(
            (self.runtime_dir / ".wb_autoanswers_capacity_recovery").exists()
        )
        backup_dir = self.runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
        self.assertEqual(list(backup_dir.glob("*.sqlite3")), [])
        archive = backup_dir / str(result["compaction"]["latest_filename"])
        manifest = backup_dir / str(result["compaction"]["manifest_filename"])
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        restored = self.runtime_dir / "restored.sqlite3"
        with restored.open("wb") as output:
            subprocess.run(
                ["zstd", "--decompress", "--stdout", "--quiet", str(archive)],
                stdout=output,
                stderr=subprocess.PIPE,
                check=True,
            )
        with closing(sqlite3.connect(f"file:{restored}?mode=ro", uri=True)) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM live_marker").fetchone()[0],
                "streamed",
            )
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required for streamed backup")
    def test_streamed_backup_removes_only_its_outputs_after_failed_readback(self) -> None:
        database = self.runtime_dir / "registry_upload_runtime.sqlite3"
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE live_marker(value TEXT NOT NULL)")
            conn.execute("INSERT INTO live_marker VALUES('preserved')")
            conn.commit()

        with patch(
            "apps.wb_autoanswers_activation._verified_compressed_schema_backup_status",
            side_effect=[
                {"count": 0},
                {
                    "count": 1,
                    "integrity_check": "ok",
                    "snapshot_sha256": "sha256:not-the-source",
                },
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "readback is missing"):
                _create_streamed_current_compressed_schema_backup(self.runtime_dir)

        backup_dir = self.runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
        self.assertEqual(list(backup_dir.glob("*.zst")), [])
        self.assertEqual(list(backup_dir.glob("*.manifest.json")), [])
        with closing(sqlite3.connect(database)) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM live_marker").fetchone()[0],
                "preserved",
            )

    def test_streamed_backup_refuses_to_consume_operational_headroom(self) -> None:
        database = self.runtime_dir / "registry_upload_runtime.sqlite3"
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE live_marker(value TEXT NOT NULL)")
            conn.execute("INSERT INTO live_marker VALUES('preserved')")
            conn.commit()

        with patch(
            "apps.wb_autoanswers_activation.shutil.disk_usage",
            return_value=SimpleNamespace(free=BACKUP_OPERATIONAL_HEADROOM_BYTES - 1),
        ):
            with self.assertRaisesRegex(RuntimeError, "operational headroom"):
                _prepare_backup_capacity(self.runtime_dir)

        self.assertEqual(list((self.runtime_dir / "backups").rglob("*.zst")), [])
        with closing(sqlite3.connect(database)) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM live_marker").fetchone()[0],
                "preserved",
            )

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required for current-schema compaction")
    def test_current_schema_raw_backup_survives_failed_canonical_readback(self) -> None:
        backup_dir = self.runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
        backup_dir.mkdir(parents=True)
        raw = backup_dir / (
            f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__recoverable.sqlite3"
        )
        with sqlite3.connect(raw) as conn:
            conn.execute("CREATE TABLE backup_marker(value TEXT NOT NULL)")
            conn.execute("INSERT INTO backup_marker VALUES('recoverable')")
        sidecar = raw.with_name(raw.name + "-shm")
        sidecar.write_bytes(b"recoverable-sidecar")

        with patch(
            "apps.wb_autoanswers_activation._verified_compressed_schema_backup_status",
            return_value={
                "count": 1,
                "integrity_check": "ok",
                "snapshot_sha256": "sha256:not-the-source",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "readback is missing"):
                _compress_verified_current_schema_backup(raw)

        self.assertTrue(raw.is_file())
        self.assertTrue(sidecar.is_file())
        self.assertEqual(_integrity_check(raw), "ok")

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required for superseded backup cleanup")
    def test_verified_current_backup_prunes_minimum_legacy_pair_for_headroom(self) -> None:
        database = self.runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE live_marker(value TEXT NOT NULL)")
            conn.execute("INSERT INTO live_marker VALUES('live')")

        current_dir = self.runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
        current_dir.mkdir(parents=True)
        current_raw = current_dir / (
            f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__current.sqlite3"
        )
        shutil.copy2(database, current_raw)
        _compress_verified_current_schema_backup(current_raw)

        legacy_dir = self.runtime_dir / "backups" / "wb_autoanswers_schema_v2"
        legacy_dir.mkdir(parents=True)
        legacy_raw = legacy_dir / "registry_upload_runtime__pre_autoanswers_v2__legacy.sqlite3"
        shutil.copy2(database, legacy_raw)
        legacy_archive = legacy_raw.with_suffix(legacy_raw.suffix + ".zst")
        subprocess.run(
            ["zstd", "--quiet", "--force", "-o", str(legacy_archive), str(legacy_raw)],
            check=True,
        )
        archive_sha = hashlib.sha256(legacy_archive.read_bytes()).hexdigest()
        legacy_manifest = legacy_archive.with_suffix(legacy_archive.suffix + ".manifest.json")
        legacy_manifest.write_text(
            json.dumps(
                {
                    "contract": "wb_autoanswers_compressed_schema_backup_v2",
                    "compressed_filename": legacy_archive.name,
                    "compressed_size": legacy_archive.stat().st_size,
                    "compressed_sha256": archive_sha,
                    "snapshot_sha256": hashlib.sha256(legacy_raw.read_bytes()).hexdigest(),
                    "sqlite_integrity_check": "ok",
                }
            ),
            encoding="utf-8",
        )
        legacy_raw.unlink()
        unrelated = legacy_dir / "keep-unrelated.txt"
        unrelated.write_text("keep", encoding="utf-8")

        mib = 1024 * 1024
        with patch(
            "apps.wb_autoanswers_activation.shutil.disk_usage",
            side_effect=[
                SimpleNamespace(free=100 * mib),
                SimpleNamespace(free=100 * mib),
                SimpleNamespace(free=100 * mib),
                SimpleNamespace(free=400 * mib),
                SimpleNamespace(free=400 * mib),
            ],
        ):
            result = _prepare_backup_capacity(self.runtime_dir)

        cleanup = result["compaction"]["superseded_cleanup"]
        self.assertEqual(cleanup["status"], "superseded_backups_removed")
        self.assertEqual(len(cleanup["removed"]), 1)
        self.assertFalse(legacy_archive.exists())
        self.assertFalse(legacy_manifest.exists())
        self.assertTrue(unrelated.is_file())
        audit = current_dir / str(cleanup["audit_manifest"])
        self.assertTrue(audit.is_file())
        audit_payload = json.loads(audit.read_text(encoding="utf-8"))
        self.assertEqual(audit_payload["contract"], "wb_autoanswers_superseded_backup_cleanup_v1")
        self.assertEqual(audit_payload["status"], "applied")
        self.assertIsNone(audit_payload["planned_removal"])
        self.assertEqual(len(audit_payload["removed"]), 1)
        self.assertEqual(audit_payload["current_backup"]["integrity_check"], "ok")
        self.assertEqual(audit.stat().st_mode & 0o777, 0o600)

    def test_status_does_not_migrate_a_database_below_target_schema(self) -> None:
        database = self.runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(database) as conn:
            conn.execute(
                "CREATE TABLE sheet_vitrina_v1_wb_autoanswers_schema_migrations(version INTEGER PRIMARY KEY)"
            )
            conn.executemany(
                "INSERT INTO sheet_vitrina_v1_wb_autoanswers_schema_migrations VALUES(?)",
                [(1,), (2,)],
            )
            conn.execute(
                "CREATE TABLE sheet_vitrina_v1_wb_autoanswers_settings("
                "singleton INTEGER PRIMARY KEY, master_enabled INTEGER NOT NULL, mode TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_wb_autoanswers_settings VALUES(1, 1, 'manual')"
            )

        with patch("apps.wb_autoanswers_activation.AutoanswersRepository") as repository:
            result = run(action="status", runtime_dir=self.runtime_dir)

        repository.assert_not_called()
        self.assertEqual(result["status"], "schema_preparation_required")
        self.assertEqual(
            [row["version"] for row in result["runtime"]["schema_migrations"]],
            [1, 2],
        )
        self.assertTrue(result["runtime"]["settings"]["master_enabled"])
        self.assertEqual(result["runtime"]["settings"]["mode"], "manual")
        self.assertFalse(result["runtime"]["settings"]["effective_enabled"])

    def test_status_does_not_initialize_a_missing_database(self) -> None:
        database = self.runtime_dir / "registry_upload_runtime.sqlite3"
        with patch("apps.wb_autoanswers_activation.AutoanswersRepository") as repository:
            result = run(action="status", runtime_dir=self.runtime_dir)

        repository.assert_not_called()
        self.assertEqual(result["status"], "schema_preparation_required")
        self.assertFalse(database.exists())
        self.assertEqual(result["runtime"]["schema_migrations"], [])
        self.assertFalse(result["runtime"]["settings"]["master_enabled"])
        self.assertFalse(result["runtime"]["settings"]["effective_enabled"])

    def test_repository_can_migrate_inside_activation_owned_schema_lock(self) -> None:
        with _schema_preparation_lock(self.runtime_dir):
            repository = AutoanswersRepository(
                runtime_dir=self.runtime_dir,
                now_factory=MutableClock(),
                env={},
                schema_lock_held=True,
            )
        self.assertEqual(repository.settings().mode, "draft_only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
