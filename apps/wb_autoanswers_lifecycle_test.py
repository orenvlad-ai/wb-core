#!/usr/bin/env python3
"""Regression tests for the feature-owned Autoanswers runtime lifecycle."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from apps.wb_autoanswers_lifecycle import run as run_lifecycle_cli
from apps.wb_autoanswers_runtime_test import MutableClock
from packages.application.wb_autoanswers_lifecycle import (
    AutoanswersLifecycle,
    READONLY_SERVICE,
    READONLY_TIMER,
    WORKER_SERVICE,
    WORKER_TIMER,
)
from packages.application.wb_autoanswers_runtime import AutoanswersRepository


class FakeSystemd:
    def __init__(self) -> None:
        self.timers = {
            READONLY_TIMER: False,
            WORKER_TIMER: False,
        }
        self.fail_enable = ""
        self.active_services: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def unit_state(self, unit: str) -> dict:
        if unit in self.timers:
            active = self.timers[unit]
            return {
                "unit": unit,
                "is_enabled": "enabled" if active else "disabled",
                "is_active": "active" if active else "inactive",
                "properties": {
                    "UnitFileState": "enabled" if active else "disabled",
                    "ActiveState": "active" if active else "inactive",
                    "LastTriggerUSec": "",
                    "NextElapseUSecRealtime": "",
                },
            }
        if unit not in {READONLY_SERVICE, WORKER_SERVICE}:
            raise AssertionError(f"unexpected unit {unit}")
        return {
            "unit": unit,
            "is_enabled": "static",
            "is_active": (
                "activating" if unit in self.active_services else "inactive"
            ),
            "properties": {"Result": "success"},
        }

    def disable_now(self, unit: str) -> None:
        self.calls.append(("disable", unit))
        self.timers[unit] = False

    def enable_now(self, unit: str) -> None:
        self.calls.append(("enable", unit))
        if unit == self.fail_enable:
            raise RuntimeError("synthetic lifecycle enable failure")
        self.timers[unit] = True


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.runtime_dir = Path(self.temp.name)
        self.clock = MutableClock()
        self.repository = AutoanswersRepository(
            runtime_dir=self.runtime_dir,
            now_factory=self.clock,
            env={"WB_AUTOANSWERS_FORCE_OFF": "false"},
        )
        self.systemd = FakeSystemd()
        self.lifecycle = AutoanswersLifecycle(
            runtime_dir=self.runtime_dir,
            repository=self.repository,
            systemd=self.systemd,
            now_factory=self.clock,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def set_mode(self, mode: str) -> None:
        if mode in {"draft_only", "auto_safe", "auto_all"}:
            preview = self.repository.preview_mode_transition(
                mode,
                actor_id="test",
                run_max_usd="0.50",
            )
            self.repository.apply_mode_transition(
                mode,
                actor_id="test",
                preview_id=preview["preview_id"],
            )
            return
        self.repository.update_settings(
            master_enabled=mode != "off",
            mode=None if mode == "off" else mode,
            actor_id="test",
        )

    def reconcile(self, *, suspended: bool = False) -> dict:
        reconciliation = self.repository.reconciliation_status() or {}
        return self.lifecycle.reconcile(
            suspended_by_master=suspended,
            actor="test",
            reason="lifecycle test",
            transition_run_id=reconciliation.get("transition_run_id"),
        )

    def test_off_keeps_readonly_sync_and_stops_worker(self) -> None:
        status = self.reconcile()
        self.assertEqual(status["business_mode"], "off")
        self.assertEqual(status["lifecycle_state"], "off")
        self.assertTrue(status["components"]["readonly_sync"]["actual"])
        self.assertFalse(status["components"]["worker"]["actual"])

    def test_status_does_not_create_an_absent_schema(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            status = run_lifecycle_cli(
                action="status",
                runtime_dir=runtime_dir,
                actor="test",
                reason="read-only status",
            )
            self.assertEqual(status["status"], "schema_preparation_required")
            self.assertFalse(
                (runtime_dir / "registry_upload_runtime.sqlite3").exists()
            )

    def test_every_enabled_mode_owns_both_timers(self) -> None:
        for mode in ("manual", "draft_only", "auto_safe", "auto_all"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                status = self.reconcile()
                self.assertEqual(status["business_mode"], mode)
                self.assertEqual(status["lifecycle_state"], "starting")
                self.assertTrue(status["components"]["readonly_sync"]["actual"])
                self.assertTrue(status["components"]["worker"]["actual"])

    def test_fresh_tick_is_required_before_running(self) -> None:
        self.set_mode("auto_all")
        starting = self.reconcile()
        self.assertEqual(starting["lifecycle_state"], "starting")
        self.assertFalse(starting["actual"])
        self.repository.record_scheduler_tick(errors=[])
        running = self.lifecycle.status(suspended_by_master=False)
        self.assertEqual(running["lifecycle_state"], "running")
        self.assertTrue(running["actual"])
        self.clock.value += timedelta(minutes=4)
        stale = self.lifecycle.status(suspended_by_master=False)
        self.assertEqual(stale["lifecycle_state"], "error")
        self.assertEqual(stale["stop_reason"], "worker_unavailable")
        self.assertFalse(stale["fresh_scheduler_tick"])

    def test_active_bounded_worker_extends_starting_readback(self) -> None:
        self.set_mode("auto_all")
        starting = self.reconcile()
        self.assertEqual(starting["lifecycle_state"], "starting")
        self.clock.value += timedelta(minutes=4)
        self.systemd.active_services.add(READONLY_SERVICE)
        readonly_only = self.lifecycle.status(suspended_by_master=False)
        self.assertFalse(readonly_only["service_in_progress"])
        self.assertEqual(readonly_only["lifecycle_state"], "error")
        self.assertEqual(readonly_only["stop_reason"], "worker_unavailable")
        self.systemd.active_services.add(WORKER_SERVICE)
        still_starting = self.lifecycle.status(
            suspended_by_master=False
        )
        self.assertTrue(still_starting["service_in_progress"])
        self.assertEqual(still_starting["drift_status"], "matched")
        self.assertEqual(still_starting["lifecycle_state"], "starting")
        self.assertEqual(still_starting["stop_reason"], "")

    def test_master_pause_preserves_feature_mode_and_resume_uses_latest_mode(self) -> None:
        self.set_mode("manual")
        self.reconcile()
        paused = self.reconcile(suspended=True)
        self.assertEqual(paused["lifecycle_state"], "suspended_by_master")
        self.assertFalse(paused["components"]["readonly_sync"]["actual"])
        self.assertFalse(paused["components"]["worker"]["actual"])
        self.set_mode("auto_safe")
        still_paused = self.reconcile(suspended=True)
        self.assertEqual(still_paused["business_mode"], "auto_safe")
        resumed = self.reconcile(suspended=False)
        self.assertEqual(resumed["business_mode"], "auto_safe")
        self.assertTrue(resumed["components"]["readonly_sync"]["actual"])
        self.assertTrue(resumed["components"]["worker"]["actual"])

    def test_timer_drift_and_partial_failure_fail_closed(self) -> None:
        self.set_mode("draft_only")
        self.reconcile()
        self.systemd.timers[WORKER_TIMER] = False
        drift = self.lifecycle.status(suspended_by_master=False)
        self.assertEqual(drift["lifecycle_state"], "error")
        self.assertEqual(drift["drift_status"], "drift")

        self.systemd.fail_enable = WORKER_TIMER
        with self.assertRaisesRegex(RuntimeError, "synthetic lifecycle"):
            self.reconcile()
        self.assertTrue(self.systemd.timers[READONLY_TIMER])
        self.assertFalse(self.systemd.timers[WORKER_TIMER])
        readback = self.lifecycle.status(suspended_by_master=False)
        self.assertIn("synthetic lifecycle", readback["last_error"])

    def test_automatic_mode_without_a_transition_run_cap_fails_closed(self) -> None:
        self.repository.update_settings(
            master_enabled=True,
            mode="auto_all",
            actor_id="test",
        )
        with self.assertRaisesRegex(RuntimeError, "run_cap_missing"):
            self.reconcile()
        self.assertTrue(self.systemd.timers[READONLY_TIMER])
        self.assertFalse(self.systemd.timers[WORKER_TIMER])
        self.assertNotIn(("enable", WORKER_TIMER), self.systemd.calls)

    def test_budget_unknown_blocks_worker_and_survives_restart(self) -> None:
        self.set_mode("auto_all")
        self.repository.record_scheduler_tick(
            errors=[{"code": "node_process_exit_1", "retryable": False}]
        )
        with self.assertRaisesRegex(RuntimeError, "budget_state_unknown"):
            self.reconcile()
        self.assertTrue(self.systemd.timers[READONLY_TIMER])
        self.assertFalse(self.systemd.timers[WORKER_TIMER])
        self.assertNotIn(("enable", WORKER_TIMER), self.systemd.calls)
        restarted = AutoanswersLifecycle(
            runtime_dir=self.runtime_dir,
            repository=self.repository,
            systemd=self.systemd,
            now_factory=self.clock,
        ).status(suspended_by_master=False)
        self.assertEqual(restarted["lifecycle_state"], "error")
        self.assertEqual(restarted["stop_reason"], "budget_state_unknown")

    def test_master_pause_succeeds_while_budget_state_is_unknown(self) -> None:
        self.set_mode("auto_all")
        self.repository.record_scheduler_tick(
            errors=[{"code": "node_process_exit_1", "retryable": False}]
        )
        paused = self.reconcile(suspended=True)
        self.assertEqual(paused["lifecycle_state"], "suspended_by_master")
        self.assertEqual(paused["drift_status"], "matched")
        self.assertEqual(paused["stop_reason"], "budget_state_unknown")
        self.assertFalse(paused["components"]["readonly_sync"]["actual"])
        self.assertFalse(paused["components"]["worker"]["actual"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
