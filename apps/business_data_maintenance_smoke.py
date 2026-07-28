#!/usr/bin/env python3
"""Regression checks for the all-writer business-data quiet window."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.business_data_maintenance as maintenance


class FakeSystemd:
    def __init__(
        self,
        *,
        unknown_timer: str = "",
        active_reads: int = 0,
        fail_enable_unit: str = "",
    ) -> None:
        self.timer_states = {
            unit: {
                "unit": unit,
                "is_enabled": "enabled" if unit in maintenance.CORE_TIMER_UNITS else "disabled",
                "is_active": "active" if unit in maintenance.CORE_TIMER_UNITS else "inactive",
                "properties": {},
            }
            for unit in maintenance.ALL_BUSINESS_TIMER_UNITS
        }
        self.service_states = {
            unit: {
                "unit": unit,
                "is_enabled": "static",
                "is_active": "inactive",
                "properties": {},
            }
            for unit in maintenance.ALL_BUSINESS_SERVICE_UNITS
        }
        self.unknown_timer = unknown_timer
        self.active_reads = active_reads
        self.fail_enable_unit = fail_enable_unit
        self.mutations: list[str] = []

    def unit_state(self, unit: str) -> dict[str, Any]:
        if unit in self.timer_states:
            return copy.deepcopy(self.timer_states[unit])
        result = copy.deepcopy(self.service_states[unit])
        if unit == "wb-core-wb-finance-weekly.service" and self.active_reads > 0:
            self.active_reads -= 1
            result["is_active"] = "activating"
        return result

    def disable_now(self, unit: str) -> None:
        self.mutations.append(unit)
        self.timer_states[unit]["is_enabled"] = "disabled"
        self.timer_states[unit]["is_active"] = "inactive"

    def enable_now(self, unit: str) -> None:
        self.mutations.append("enable:" + unit)
        if unit == self.fail_enable_unit:
            raise RuntimeError("synthetic enable failure")
        self.timer_states[unit]["is_enabled"] = "enabled"
        self.timer_states[unit]["is_active"] = "active"

    def discovered_timers(self) -> list[str]:
        rows = list(maintenance.ALL_BUSINESS_TIMER_UNITS)
        if self.unknown_timer:
            rows.append(self.unknown_timer)
        return sorted(rows)


class FakeSchedules:
    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, Any]] = {
            "web_vitrina": {
                "schedule_policy": {"mode": "interval", "interval_hours": 4},
                "schedules": [
                    {"id": "interval_4h_10_00_ekt", "enabled": True},
                    {"id": "interval_4h_14_00_ekt", "enabled": True},
                ],
                "last_auto_run_status": "success",
            },
            "feedback_complaints": {
                "schedules": [{"id": "daily", "enabled": True}],
                "recent_runs": [],
            },
            "spp": {"schedule": {"id": "daily_spp", "enabled": True}},
            "spp_status": {"status": "ready", "job": None},
        }
        self.disable_calls = 0
        self.legacy_restore_calls = 0

    def read_all(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.payloads)

    def disable_all(self, current: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        assert (current.get("web_vitrina") or {}).get("schedule_policy", {}).get("mode") == "interval"
        self.disable_calls += 1
        # The master hold owns execution; feature schedule JSON is immutable
        # desired state and must not be rewritten.
        return self.read_all()

    def restore_selected(
        self,
        baseline: Mapping[str, Mapping[str, Any]],
        *,
        desired: Mapping[str, bool],
    ) -> dict[str, dict[str, Any]]:
        assert dict(baseline)
        assert dict(desired)
        return self.read_all()

    def restore_legacy_hold(
        self,
        baseline: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        self.legacy_restore_calls += 1
        self.payloads = copy.deepcopy(dict(baseline))
        return self.read_all()


def _with_quiet_local_boundaries() -> tuple[Any, Any]:
    old_cron = maintenance._cron_entries
    old_locks = maintenance._lock_summary
    maintenance._cron_entries = lambda: []
    maintenance._lock_summary = lambda runtime_dir: {
        "warehouse_functional": {"path": str(runtime_dir / "warehouse.lock"), "held": False},
        "web_schedule": {"path": str(runtime_dir / "web.lock"), "held": False},
        "spp_execution": {"path": str(runtime_dir / "spp.lock"), "held": False},
        "seller_portal": {"busy": False},
    }
    return old_cron, old_locks


def _restore_local_boundaries(old: tuple[Any, Any]) -> None:
    maintenance._cron_entries, maintenance._lock_summary = old


def _assert_hold_disables_every_boundary_without_killing_service() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        systemd = FakeSystemd(active_reads=3)
        schedules = FakeSchedules()
        old = _with_quiet_local_boundaries()
        try:
            result = maintenance.maintenance_hold(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
                wait_timeout_seconds=1,
                poll_interval_seconds=0.01,
            )
        finally:
            _restore_local_boundaries(old)
        assert result["status"] == "held"
        assert result["quiet"] is True
        assert systemd.mutations == [
            "wb-core-autoanswers-worker.timer",
            "wb-core-autoanswers-readonly-sync.timer",
            *maintenance.CORE_TIMER_UNITS,
        ]
        assert schedules.disable_calls == 1
        assert result["runtime_schedules"]["web_vitrina"]["schedule_policy"]["mode"] == "interval"
        assert result["runtime_schedules"]["web_vitrina"]["enabled_ids"]
        assert result["runtime_schedules"]["feedback_complaints"]["enabled_ids"]
        assert result["runtime_schedules"]["spp"]["enabled"] is True
        state_path = runtime_dir / maintenance.STATE_FILENAME
        audit_path = runtime_dir / maintenance.AUDIT_FILENAME
        assert state_path.stat().st_mode & 0o777 == 0o600
        assert audit_path.stat().st_mode & 0o777 == 0o600
        state = json.loads(state_path.read_text())
        assert state["phase"] == "held"
        assert state["runtime_schedule_baseline"]["web_vitrina"]["schedule_policy"]["mode"] == "interval"


def _assert_unconfirmed_hold_abort_preserves_pre_hold_service_generation() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        _warehouse_baseline(runtime_dir)
        systemd = FakeSystemd()
        schedules = FakeSchedules()
        unit = "wb-core-sheet-vitrina-closure-retry.service"
        systemd.service_states[unit] = {
            "unit": unit,
            "is_enabled": "static",
            "is_active": "activating",
            "properties": {
                "LoadState": "loaded",
                "UnitFileState": "static",
                "ActiveState": "activating",
                "SubState": "start",
                "Result": "success",
                "ExecMainCode": "0",
                "ExecMainStatus": "0",
                "MainPID": "4242",
                "ExecMainStartTimestamp": "Sat 2000-01-01 00:00:00 UTC",
            },
        }
        fingerprint = "sha256:" + "4" * 64
        maintenance.acquire_barrier(
            runtime_dir,
            window_id="snapshot-abort-smoke",
            window_kind="snapshot",
            plan_fingerprint=fingerprint,
            approval_reference="smoke-approval",
            actor="smoke",
            reason="prove abort restore",
        )
        old = _with_quiet_local_boundaries()
        try:
            prepared = maintenance.maintenance_prepare(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            assert prepared["quiet"] is False
            # The production hold that motivated this recovery was captured by
            # the previous runner version, before MainPID/start-time evidence
            # was part of the persisted systemd baseline.  Recovery must still
            # prove the current generation predates the hold, while accepting
            # that those two fields are absent from the legacy baseline.
            state_path = runtime_dir / maintenance.STATE_FILENAME
            legacy_state = json.loads(state_path.read_text())
            legacy_properties = legacy_state["baseline"]["services"][unit][
                "properties"
            ]
            legacy_properties.pop("MainPID")
            legacy_properties.pop("ExecMainStartTimestamp")
            state_path.write_text(json.dumps(legacy_state))
            policy = maintenance.load_or_initialize_owner_policy(runtime_dir)
            try:
                maintenance.maintenance_restore(
                    runtime_dir,
                    systemd=systemd,
                    schedules=schedules,
                    proc_root=proc_root,
                    expected_revision=int(policy["revision"]),
                )
            except RuntimeError as exc:
                assert "not quiet before resume" in str(exc)
            else:
                raise AssertionError(
                    "ordinary restore must reject a continuing service"
                )

            def restore_warehouse(_: Path) -> dict[str, Any]:
                systemd.enable_now(
                    "wb-core-warehouse-functional-sync.timer"
                )
                return {"status": "restored"}

            restored = maintenance.maintenance_restore(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
                actor="smoke",
                reason="abort unconfirmed hold",
                expected_revision=int(policy["revision"]),
                warehouse_restore=restore_warehouse,
                allow_pre_hold_service_continuity=True,
            )
        finally:
            _restore_local_boundaries(old)
        assert restored["status"] == "restored"
        assert restored["exact_prior_state_restored"] is True
        continuity = restored[
            "pre_hold_service_continuity_readback"
        ]
        assert continuity["services"] == [
            {
                "unit": unit,
                "outcome": "continued",
                "main_pid": 4242,
                "started_at": "Sat 2000-01-01 00:00:00 UTC",
            }
        ]
        state = json.loads(
            (runtime_dir / maintenance.STATE_FILENAME).read_text()
        )
        assert state["phase"] == "restored"
        assert state["pre_hold_service_continuity_readback"] == continuity
        assert maintenance.barrier_status(runtime_dir)["phase"] == "acquiring"


def _assert_persisted_service_continuity_accepts_exact_completion() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        _warehouse_baseline(runtime_dir)
        systemd = FakeSystemd()
        schedules = FakeSchedules()
        unit = "wb-core-sheet-vitrina-closure-retry.service"
        systemd.service_states[unit] = {
            "unit": unit,
            "is_enabled": "static",
            "is_active": "activating",
            "properties": {
                "LoadState": "loaded",
                "UnitFileState": "static",
                "ActiveState": "activating",
                "SubState": "start",
                "Result": "success",
                "ExecMainCode": "0",
                "ExecMainStatus": "0",
                "MainPID": "4242",
                "ExecMainStartTimestamp": (
                    "Sat 2000-01-01 00:00:00 UTC"
                ),
            },
        }
        maintenance.acquire_barrier(
            runtime_dir,
            window_id="snapshot-completed-service-smoke",
            window_kind="snapshot",
            plan_fingerprint="sha256:" + "5" * 64,
            approval_reference="smoke-approval",
            actor="smoke",
            reason="prove detached completion recovery",
        )
        old = _with_quiet_local_boundaries()
        try:
            maintenance.maintenance_prepare(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            maintenance_state = json.loads(
                (runtime_dir / maintenance.STATE_FILENAME).read_text()
            )
            current = maintenance.maintenance_status(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            continuity = maintenance._pre_hold_service_continuity(
                runtime_dir,
                maintenance_state=maintenance_state,
                current_status=current,
            )
            assert (
                continuity["barrier_plan_fingerprint"]
                == "sha256:" + "5" * 64
            )
            policy = maintenance.load_or_initialize_owner_policy(runtime_dir)
            systemd.service_states[unit].update(
                {
                    "is_active": "inactive",
                    "properties": {
                        "LoadState": "loaded",
                        "UnitFileState": "static",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainCode": "1",
                        "ExecMainStatus": "0",
                        "MainPID": "0",
                        "ExecMainStartTimestamp": "",
                    },
                }
            )

            def restore_warehouse(_: Path) -> dict[str, Any]:
                systemd.enable_now(
                    "wb-core-warehouse-functional-sync.timer"
                )
                return {"status": "restored"}

            restored = maintenance.maintenance_restore(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
                actor="smoke",
                reason="restore after exact service completion",
                expected_revision=int(policy["revision"]),
                warehouse_restore=restore_warehouse,
                allow_pre_hold_service_continuity=True,
                pre_hold_service_continuity_evidence=continuity,
            )
        finally:
            _restore_local_boundaries(old)
        assert restored["status"] == "restored"
        assert restored["pre_hold_service_continuity_readback"]["services"] == [
            {
                "unit": unit,
                "outcome": "completed",
                "main_pid": 0,
                "started_at": "",
            }
        ]


def _assert_unknown_timer_fails_before_mutation() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        systemd = FakeSystemd(unknown_timer="wb-core-unclassified-writer.timer")
        schedules = FakeSchedules()
        old = _with_quiet_local_boundaries()
        try:
            try:
                maintenance.maintenance_prepare(
                    runtime_dir,
                    systemd=systemd,
                    schedules=schedules,
                    proc_root=proc_root,
                )
            except RuntimeError as exc:
                assert "explicit classification" in str(exc)
            else:
                raise AssertionError("unknown timer must fail closed")
        finally:
            _restore_local_boundaries(old)
        assert systemd.mutations == []
        assert schedules.disable_calls == 0


def _assert_status_does_not_initialize_owner_policy() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        (runtime_dir / maintenance.STATE_FILENAME).write_text(
            json.dumps({"phase": "held", "baseline": {}})
        )
        systemd = FakeSystemd()
        schedules = FakeSchedules()
        old = _with_quiet_local_boundaries()
        try:
            status = maintenance.maintenance_status(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
        finally:
            _restore_local_boundaries(old)
        assert status["auto_updates"]["revision"] == 0
        assert not (runtime_dir / maintenance.POLICY_FILENAME).exists()


def _assert_legacy_active_hold_is_not_guessed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        (runtime_dir / maintenance.STATE_FILENAME).write_text(
            json.dumps(
                {
                    "schema_version": maintenance.SCHEMA_VERSION,
                    "phase": "held",
                    "baseline": {"quiet": False},
                }
            ),
            encoding="utf-8",
        )
        systemd = FakeSystemd()
        schedules = FakeSchedules()
        try:
            maintenance.maintenance_prepare(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
            )
        except RuntimeError as exc:
            assert "prior state is unknown" in str(exc)
        else:
            raise AssertionError(
                "legacy active hold without exact signature was guessed"
            )
        assert systemd.mutations == []
        assert schedules.disable_calls == 0
        assert not (runtime_dir / maintenance.POLICY_FILENAME).exists()


def _warehouse_baseline(runtime_dir: Path) -> None:
    (runtime_dir / maintenance.WAREHOUSE_MAINTENANCE_STATE_FILENAME).write_text(
        json.dumps(
            {
                "baseline": {
                    "units": {
                        "timer": {
                            "unit": "wb-core-warehouse-functional-sync.timer",
                            "is_enabled": "enabled",
                            "is_active": "active",
                            "properties": {},
                        }
                    }
                }
            }
        )
    )


def _assert_exact_policy_restore_and_revision_guards() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        _warehouse_baseline(runtime_dir)
        systemd = FakeSystemd()
        schedules = FakeSchedules()
        schedules.payloads["spp"]["schedule"]["enabled"] = False
        old = _with_quiet_local_boundaries()
        try:
            maintenance.maintenance_hold(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            policy = maintenance.load_or_initialize_owner_policy(runtime_dir)
            assert policy["processes"]["warehouse_functional"]["desired"] is True
            assert "autoanswers_readonly" not in policy["processes"]
            assert "autoanswers_worker" not in policy["processes"]

            def restore_warehouse(_: Path) -> dict[str, Any]:
                systemd.enable_now("wb-core-warehouse-functional-sync.timer")
                return {"status": "restored"}

            restored = maintenance.maintenance_restore(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
                actor="smoke",
                expected_revision=int(policy["revision"]),
                warehouse_restore=restore_warehouse,
            )
            assert restored["status"] == "restored"
            assert restored["auto_updates"]["master_desired"] is True
            rows = {
                item["process_key"]: item
                for item in restored["auto_updates"]["processes"]
            }
            assert rows["warehouse_functional"]["actual"] is True
            assert rows["spp_test"]["desired"] is False
            assert rows["spp_test"]["actual"] is False
            assert rows["autoanswers"]["actual"] is False
            assert rows["autoanswers"]["component_states"]["readonly_sync"]["actual"] is True
            repeated = maintenance.maintenance_restore(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
                expected_revision=int(restored["auto_updates"]["revision"]),
                warehouse_restore=restore_warehouse,
            )
            assert repeated["idempotent"] is True
            first_state = json.loads(
                (runtime_dir / maintenance.STATE_FILENAME).read_text()
            )
            assert first_state["phase"] == "restored"
            first_hold_started_at = str(first_state["hold_started_at"])
            systemd.disable_now("wb-core-warehouse-functional-sync.timer")
            maintenance.maintenance_hold(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            second_state = json.loads(
                (runtime_dir / maintenance.STATE_FILENAME).read_text()
            )
            assert second_state["phase"] == "held"
            assert str(second_state["hold_started_at"]) != first_hold_started_at
            try:
                maintenance.update_process_desired_state(
                    runtime_dir,
                    process_key="wb_finance_weekly",
                    desired=False,
                    expected_revision=1,
                    actor="smoke",
                    reason="stale",
                )
            except RuntimeError as exc:
                assert "stale policy revision" in str(exc)
            else:
                raise AssertionError("stale owner-policy revision must fail")
        finally:
            _restore_local_boundaries(old)


def _assert_unknown_policy_state_blocks_resume() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        systemd = FakeSystemd()
        schedules = FakeSchedules()
        old = _with_quiet_local_boundaries()
        try:
            maintenance.maintenance_hold(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            policy_path = runtime_dir / maintenance.POLICY_FILENAME
            policy = json.loads(policy_path.read_text())
            policy["processes"]["warehouse_functional"]["desired"] = None
            policy_path.write_text(json.dumps(policy))
            try:
                maintenance.maintenance_restore(
                    runtime_dir,
                    systemd=systemd,
                    schedules=schedules,
                    proc_root=proc_root,
                )
            except RuntimeError as exc:
                assert "unknown intended process states" in str(exc)
            else:
                raise AssertionError("unknown intended state must remain fail-closed")
        finally:
            _restore_local_boundaries(old)


def _assert_policy_v1_hold_restores_exact_feature_schedules_once() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        _warehouse_baseline(runtime_dir)
        systemd = FakeSystemd()
        schedules = FakeSchedules()
        exact_baseline = schedules.read_all()
        old = _with_quiet_local_boundaries()
        try:
            maintenance.maintenance_hold(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            policy_path = runtime_dir / maintenance.POLICY_FILENAME
            policy = json.loads(policy_path.read_text())
            policy["schema_version"] = "auto_updates_owner_policy_v1"
            policy_path.write_text(json.dumps(policy))

            schedules.payloads["web_vitrina"]["schedule_policy"] = {
                "mode": "manual",
                "interval_hours": None,
            }
            for row in schedules.payloads["web_vitrina"]["schedules"]:
                row["enabled"] = False
            for row in schedules.payloads["feedback_complaints"]["schedules"]:
                row["enabled"] = False
            schedules.payloads["spp"]["schedule"]["enabled"] = False

            def restore_warehouse(_: Path) -> dict[str, Any]:
                systemd.enable_now("wb-core-warehouse-functional-sync.timer")
                return {"status": "restored"}

            restored = maintenance.maintenance_restore(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
                expected_revision=int(policy["revision"]),
                warehouse_restore=restore_warehouse,
            )
        finally:
            _restore_local_boundaries(old)
        assert restored["status"] == "restored"
        assert schedules.legacy_restore_calls == 1
        assert schedules.payloads == exact_baseline
        migrated = json.loads(policy_path.read_text())
        assert migrated["schema_version"] == maintenance.POLICY_SCHEMA_VERSION
        assert "autoanswers_readonly" not in migrated["processes"]
        assert "autoanswers_worker" not in migrated["processes"]


def _assert_unsupported_enable_and_noop_are_preflighted() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        _warehouse_baseline(runtime_dir)
        systemd = FakeSystemd()
        schedules = FakeSchedules()
        old = _with_quiet_local_boundaries()
        try:
            maintenance.maintenance_hold(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            policy_path = runtime_dir / maintenance.POLICY_FILENAME
            audit_path = runtime_dir / maintenance.POLICY_AUDIT_FILENAME
            before_policy = policy_path.read_bytes()
            before_audit = audit_path.read_bytes()
            policy = json.loads(before_policy)
            revision = int(policy["revision"])
            for process_key, desired, expected_text in (
                ("autoanswers", True, "monitoring-only"),
                ("spp_test", False, "monitoring-only"),
                ("warehouse_functional", True, "no-op desired state"),
            ):
                try:
                    maintenance.update_process_desired_state(
                        runtime_dir,
                        process_key=process_key,
                        desired=desired,
                        expected_revision=revision,
                        actor="smoke",
                        reason="must not mutate",
                    )
                except RuntimeError as exc:
                    assert expected_text in str(exc)
                else:
                    raise AssertionError("blocked/no-op desired state must fail")
                assert policy_path.read_bytes() == before_policy
                assert audit_path.read_bytes() == before_audit

            changed = maintenance.update_process_desired_state(
                runtime_dir,
                process_key="warehouse_functional",
                desired=False,
                expected_revision=revision,
                actor="smoke",
                reason="supported desired change",
            )
            changed_bytes = policy_path.read_bytes()
            try:
                maintenance.update_process_desired_state(
                    runtime_dir,
                    process_key="wb_finance_weekly",
                    desired=False,
                    expected_revision=revision,
                    actor="concurrent-smoke",
                    reason="stale concurrent hold",
                )
            except RuntimeError as exc:
                assert "stale policy revision" in str(exc)
            else:
                raise AssertionError("concurrent stale mutation must fail")
            assert int(changed["revision"]) == revision + 1
            assert policy_path.read_bytes() == changed_bytes
        finally:
            _restore_local_boundaries(old)


def _assert_failed_resume_stays_paused_and_audited() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        _warehouse_baseline(runtime_dir)
        systemd = FakeSystemd(
            fail_enable_unit="wb-core-wb-finance-weekly.timer"
        )
        schedules = FakeSchedules()
        old = _with_quiet_local_boundaries()
        try:
            maintenance.maintenance_hold(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            policy = maintenance.load_or_initialize_owner_policy(runtime_dir)

            def restore_warehouse(_: Path) -> dict[str, Any]:
                systemd.enable_now("wb-core-warehouse-functional-sync.timer")
                return {"status": "restored"}

            try:
                maintenance.maintenance_restore(
                    runtime_dir,
                    systemd=systemd,
                    schedules=schedules,
                    proc_root=proc_root,
                    expected_revision=int(policy["revision"]),
                    warehouse_restore=restore_warehouse,
                )
            except RuntimeError as exc:
                assert "synthetic enable failure" in str(exc)
            else:
                raise AssertionError("backend enable failure must remain fail-closed")
            final = maintenance.maintenance_status(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            assert final["quiet"] is True
            assert final["auto_updates"]["master_desired"] is False
            assert all(
                item["actual"] is False
                for item in final["auto_updates"]["processes"]
            )
            audit_rows = [
                json.loads(line)
                for line in (
                    runtime_dir / maintenance.POLICY_AUDIT_FILENAME
                ).read_text().splitlines()
                if line.strip()
            ]
            assert audit_rows[-1]["event"] == "master_resume_failed"
        finally:
            _restore_local_boundaries(old)


def _assert_success_requires_persisted_runtime_readback() -> None:
    from packages.application.registry_upload_http_entrypoint import (
        _confirmed_auto_updates_update_payload,
    )

    paused = {
        "revision": 2,
        "master_desired": False,
        "policy_fingerprint": "sha256:paused",
        "unknown_processes": [],
        "drift_processes": [],
        "processes": [
            {
                "process_key": "warehouse_functional",
                "desired": False,
                "actual": False,
            }
        ],
    }
    confirmed = _confirmed_auto_updates_update_payload(
        paused,
        action="set_process",
        desired=False,
        expected_revision=1,
        process_key="warehouse_functional",
    )
    assert confirmed["mutation"]["persisted"] is True
    assert confirmed["mutation"]["runtime_readback_confirmed"] is True
    assert confirmed["mutation"]["lifecycle_readback_confirmed"] is True

    failed_readback = copy.deepcopy(paused)
    failed_readback.update(
        {
            "revision": 3,
            "master_desired": True,
            "drift_processes": ["warehouse_functional"],
        }
    )
    try:
        _confirmed_auto_updates_update_payload(
            failed_readback,
            action="set_master",
            desired=True,
            expected_revision=2,
        )
    except RuntimeError as exc:
        assert "lifecycle_readback_confirmed=False" in str(exc)
    else:
        raise AssertionError("successful write with failed readback must not succeed")

    no_revision_advance = copy.deepcopy(paused)
    no_revision_advance["revision"] = 1
    try:
        _confirmed_auto_updates_update_payload(
            no_revision_advance,
            action="set_process",
            desired=False,
            expected_revision=1,
            process_key="warehouse_functional",
        )
    except RuntimeError as exc:
        assert "revision_advanced=False" in str(exc)
    else:
        raise AssertionError("no-op mutation must not return success")


def _assert_restore_lock_rejects_overlap() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        with maintenance._ExclusiveRestoreLock(runtime_dir):
            try:
                with maintenance._ExclusiveRestoreLock(runtime_dir):
                    raise AssertionError("overlapping restore lock was acquired")
            except RuntimeError as exc:
                assert "another business-data maintenance restore" in str(exc)
        with maintenance._ExclusiveRestoreLock(runtime_dir):
            pass


def main() -> int:
    _assert_hold_disables_every_boundary_without_killing_service()
    _assert_unconfirmed_hold_abort_preserves_pre_hold_service_generation()
    _assert_persisted_service_continuity_accepts_exact_completion()
    _assert_unknown_timer_fails_before_mutation()
    _assert_status_does_not_initialize_owner_policy()
    _assert_legacy_active_hold_is_not_guessed()
    _assert_exact_policy_restore_and_revision_guards()
    _assert_unknown_policy_state_blocks_resume()
    _assert_policy_v1_hold_restores_exact_feature_schedules_once()
    _assert_unsupported_enable_and_noop_are_preflighted()
    _assert_failed_resume_stays_paused_and_audited()
    _assert_success_requires_persisted_runtime_readback()
    _assert_restore_lock_rejects_overlap()
    print("business data maintenance smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
