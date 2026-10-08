#!/usr/bin/env python3
"""Regression tests for the feature-owned Autoanswers runtime lifecycle."""

from __future__ import annotations

from datetime import timedelta
import json
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
from packages.application.business_data_maintenance_pause import (
    SCHEMA as PAUSE_SCHEMA, STATE_FILENAME as PAUSE_STATE, fingerprint, save_state,
)
from packages.application.business_data_write_barrier import (
    STATE_FILENAME as BARRIER_STATE, acquire_barrier, confirm_barrier_hold,
    mark_barrier_restoring, release_barrier,
)


class FakeSystemd:
    def __init__(self) -> None:
        self.timers = {
            READONLY_TIMER: False,
            WORKER_TIMER: False,
        }
        self.fail_enable = ""
        self.active_services: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.service_results: dict[str, str] = {}

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
            "properties": {"Result": self.service_results.get(unit, "success")},
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

    def test_active_worker_may_replace_only_a_stale_worker_error(self) -> None:
        self.set_mode("auto_all")
        with self.repository.transaction() as conn:
            self.repository._set_stop_reason(
                conn,
                "worker_error",
                details={
                    "code": "synthetic_previous_attempt",
                    "stage": "processing",
                },
                at=self.clock(),
            )
        self.systemd.active_services.add(WORKER_SERVICE)
        starting = self.reconcile()
        self.assertTrue(starting["service_in_progress"])
        self.assertEqual(starting["lifecycle_state"], "starting")
        self.assertEqual(starting["drift_status"], "matched")
        self.assertEqual(starting["stop_reason"], "")

        self.systemd.active_services.remove(WORKER_SERVICE)
        blocked = self.lifecycle.status(suspended_by_master=False)
        self.assertEqual(blocked["lifecycle_state"], "error")
        self.assertEqual(blocked["drift_status"], "blocked")
        self.assertEqual(blocked["stop_reason"], "worker_error")

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

    def held_maintenance(self, *, kind: str = "maintenance_pause", confirm: bool = True) -> dict:
        self.set_mode("auto_all")
        self.reconcile()
        self.repository.record_scheduler_tick(errors=[])
        self.clock.value += timedelta(minutes=4)
        self.systemd.timers = dict.fromkeys(self.systemd.timers, False)
        state = {"schema_version": PAUSE_SCHEMA, "phase": "held",
                 "window_id": "synthetic-maintenance-window", "baseline": {},
                 "baseline_fingerprint": fingerprint({}), "plan_fingerprint": fingerprint({}),
                 "hold_readback": {"quiet": True}}
        save_state(self.runtime_dir, state, "test-held")
        acquire_barrier(self.runtime_dir, window_id=state["window_id"], window_kind=kind,
                        plan_fingerprint=state["plan_fingerprint"], approval_reference=state["window_id"],
                        actor="test", reason="synthetic planned maintenance")
        if confirm:
            confirm_barrier_hold(self.runtime_dir, window_id=state["window_id"],
                                 plan_fingerprint=state["plan_fingerprint"], maintenance_state=state)
        return state

    def test_held_maintenance_presentation_is_read_only_and_preserves_fault_flags(self) -> None:
        self.held_maintenance()
        files = {p: p.read_bytes() for p in self.runtime_dir.iterdir() if p.is_file()}
        calls = list(self.systemd.calls)
        status = self.lifecycle.status(suspended_by_master=False)
        self.assertTrue(status["maintenance_pause"]["confirmed"])
        self.assertEqual(status["lifecycle_state"], "error")
        self.assertEqual(status["drift_status"], "drift")
        self.assertEqual(status["stop_reason"], "worker_unavailable")
        self.assertEqual(status["last_error"], "worker_unavailable")
        self.assertFalse(status["actual"])
        self.assertFalse(status["fresh_scheduler_tick"])
        self.assertEqual(self.systemd.calls, calls)
        self.assertEqual({p: p.read_bytes() for p in files}, files)

    def test_unconfirmed_or_nonmaintenance_barrier_is_not_a_planned_pause(self) -> None:
        self.held_maintenance(confirm=False)
        self.assertFalse(self.lifecycle.status(suspended_by_master=False)["maintenance_pause"]["confirmed"])
        (self.runtime_dir / BARRIER_STATE).unlink()
        state = json.loads((self.runtime_dir / PAUSE_STATE).read_text())
        acquire_barrier(self.runtime_dir, window_id=state["window_id"], window_kind="snapshot",
                        plan_fingerprint=state["plan_fingerprint"], approval_reference=state["window_id"],
                        actor="test", reason="synthetic snapshot")
        confirm_barrier_hold(self.runtime_dir, window_id=state["window_id"],
                             plan_fingerprint=state["plan_fingerprint"], maintenance_state=state)
        self.assertFalse(self.lifecycle.status(suspended_by_master=False)["maintenance_pause"]["confirmed"])

    def test_fresh_tick_held_maintenance_also_marks_only_expected_drift(self) -> None:
        self.held_maintenance()
        self.clock.value -= timedelta(minutes=3)
        fresh = self.lifecycle.status(suspended_by_master=False)
        self.assertTrue(fresh["fresh_scheduler_tick"])
        self.assertEqual(fresh["stop_reason"], "no_eligible_jobs")
        self.assertEqual(fresh["last_error"], "no_eligible_jobs")
        self.assertTrue(fresh["maintenance_pause"]["confirmed"])
        self.assertEqual(fresh["lifecycle_state"], "error")
        self.assertEqual(fresh["drift_status"], "drift")
        self.assertFalse(fresh["actual"])

    def test_unknown_changed_or_failed_maintenance_is_not_a_planned_pause(self) -> None:
        original = self.held_maintenance()
        for change in ({"phase": "draining"}, {"phase": "restoring"}, {"phase": "restored"},
                       {"window_id": "different-window"}, {"plan_fingerprint": fingerprint("different")},
                       {"error": "restore failed"}, {"hold_readback": {"quiet": False}},
                       {"baseline_fingerprint": fingerprint("wrong")}):
            with self.subTest(change=change):
                save_state(self.runtime_dir, {**original, **change}, "test-negative")
                self.assertFalse(self.lifecycle.status(suspended_by_master=False)["maintenance_pause"]["confirmed"])
        (self.runtime_dir / PAUSE_STATE).write_text("broken json")
        self.assertFalse(self.lifecycle.status(suspended_by_master=False)["maintenance_pause"]["confirmed"])

    def test_real_faults_and_owner_controls_are_not_hidden_by_held_maintenance(self) -> None:
        self.held_maintenance()
        for result in ("exit-code", ""):
            self.systemd.service_results[WORKER_SERVICE] = result
            self.assertFalse(self.lifecycle.status(suspended_by_master=False)["maintenance_pause"]["confirmed"])
        self.systemd.service_results.clear()
        self.repository.env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
        forced_off = self.lifecycle.status(suspended_by_master=False)
        self.assertFalse(forced_off["maintenance_pause"]["confirmed"])
        self.assertEqual(forced_off["stop_reason"], "emergency_stop")
        self.repository.env["WB_AUTOANSWERS_FORCE_OFF"] = "false"
        persisted = json.loads(self.lifecycle.state_path.read_text())
        self.lifecycle.state_path.write_text(json.dumps({**persisted, "last_error": "synthetic lifecycle fault"}))
        fault = self.lifecycle.status(suspended_by_master=False)
        self.assertFalse(fault["maintenance_pause"]["confirmed"])
        self.assertEqual(fault["last_error"], "synthetic lifecycle fault")
        self.lifecycle.state_path.write_text(json.dumps(persisted))
        self.assertFalse(self.lifecycle.status(suspended_by_master=True)["maintenance_pause"]["confirmed"])
        for reason in ("worker_error", "hourly_budget_reached", "budget_state_unknown"):
            with self.subTest(reason=reason):
                with self.repository.transaction() as conn:
                    self.repository._set_stop_reason(conn, reason, details={}, at=self.clock())
                status = self.lifecycle.status(suspended_by_master=False)
                self.assertFalse(status["maintenance_pause"]["confirmed"])
                self.assertEqual(status["stop_reason"], reason)
        for mode in ("manual", "off"):
            self.set_mode(mode)
            self.assertFalse(self.lifecycle.status(suspended_by_master=False)["maintenance_pause"]["confirmed"])

    def test_restore_removes_presentation_and_requires_an_ordinary_fresh_tick(self) -> None:
        state = self.held_maintenance()
        mark_barrier_restoring(self.runtime_dir, window_id=state["window_id"], plan_fingerprint=state["plan_fingerprint"])
        self.assertFalse(self.lifecycle.status(suspended_by_master=False)["maintenance_pause"]["confirmed"])
        release_barrier(self.runtime_dir, window_id=state["window_id"], plan_fingerprint=state["plan_fingerprint"],
                        actor="test", reason="synthetic exact restore",
                        restore_readback={"status": "restored", "exact_prior_state_restored": True})
        self.systemd.timers = dict.fromkeys(self.systemd.timers, True)
        stale = self.lifecycle.status(suspended_by_master=False)
        self.assertFalse(stale["maintenance_pause"]["confirmed"])
        self.assertEqual(stale["stop_reason"], "worker_unavailable")
        self.assertEqual(stale["lifecycle_state"], "error")
        self.repository.record_scheduler_tick(errors=[])
        running = self.lifecycle.status(suspended_by_master=False)
        self.assertTrue(running["actual"])
        self.assertEqual(running["lifecycle_state"], "running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
