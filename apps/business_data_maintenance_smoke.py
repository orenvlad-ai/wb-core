#!/usr/bin/env python3
"""Regression checks for the all-writer business-data quiet window."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.business_data_maintenance as maintenance
import packages.application.business_data_write_barrier as write_barrier


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
                "is_enabled": (
                    "enabled"
                    if unit
                    in (
                        maintenance.CORE_TIMER_UNITS
                        + maintenance.INDEPENDENT_WRITER_TIMER_UNITS
                    )
                    else "disabled"
                ),
                "is_active": (
                    "active"
                    if unit
                    in (
                        maintenance.CORE_TIMER_UNITS
                        + maintenance.INDEPENDENT_WRITER_TIMER_UNITS
                    )
                    else "inactive"
                ),
                "properties": {},
            }
            for unit in maintenance.ALL_BUSINESS_TIMER_UNITS
        }
        self.timer_states.update(
            {
                unit: {
                    "unit": unit,
                    "is_enabled": "enabled",
                    "is_active": "active",
                    "properties": {},
                }
                for unit in maintenance.CONTINUOUS_OBSERVER_TIMER_UNITS
            }
        )
        self.service_states = {
            unit: {
                "unit": unit,
                "is_enabled": "static",
                "is_active": "inactive",
                "properties": {},
            }
            for unit in maintenance.ALL_BUSINESS_SERVICE_UNITS
        }
        self.service_states.update(
            {
                timer.removesuffix(".timer") + ".service": {
                    "unit": timer.removesuffix(".timer") + ".service",
                    "is_enabled": "static",
                    "is_active": "inactive",
                    "properties": {"MainPID": 0},
                }
                for timer in maintenance.CONTINUOUS_OBSERVER_TIMER_UNITS
            }
        )
        self.unknown_timer = unknown_timer
        self.unknown_active_service = ""
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
        rows = list(maintenance.CLASSIFIED_WB_CORE_TIMER_UNITS)
        if self.unknown_timer:
            rows.append(self.unknown_timer)
        return sorted(rows)

    def discovered_active_services(self) -> list[str]:
        rows = [
            unit
            for unit, state in self.service_states.items()
            if str(state.get("is_active") or "")
            not in maintenance.QUIESCENT_SERVICE_STATES
        ]
        if self.unknown_active_service:
            rows.append(self.unknown_active_service)
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


class UnavailableSchedules(FakeSchedules):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls = 0

    def read_all(self) -> dict[str, dict[str, Any]]:
        self.read_calls += 1
        raise TimeoutError("synthetic loopback timeout")


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


def _autoanswers_restore_fixture() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    components: dict[str, Any] = {}
    timers: dict[str, Any] = {}
    services: dict[str, Any] = {}
    spec = next(
        item
        for item in maintenance.PROCESS_SPECS
        if item["key"] == "autoanswers"
    )
    for component_key, timer_unit in spec["components"].items():
        service_unit = timer_unit.removesuffix(".timer") + ".service"
        timer = {
            "unit": timer_unit,
            "is_enabled": "enabled",
            "is_active": "active",
            "properties": {},
        }
        service = {
            "unit": service_unit,
            "is_enabled": "static",
            "is_active": (
                "activating" if component_key == "worker" else "inactive"
            ),
            "properties": {"Result": "success"},
        }
        timers[timer_unit] = timer
        services[service_unit] = service
        components[component_key] = {
            "component_key": component_key,
            "desired": True,
            "actual": True,
            "drift_status": "matched",
            "timer": timer,
            "service": service,
            "last_error": "",
        }
    preflight = {
        "process_key": "autoanswers",
        "desired": True,
        "business_mode": "auto_safe",
        "runtime_schedule": {
            "policy_epoch": 17,
            "transition_run_id": "autoanswers-transition-17",
        },
    }
    lifecycle = {
        "contract": "wb_autoanswers_lifecycle_v1",
        "process_key": "autoanswers",
        "desired": True,
        "business_mode": "auto_safe",
        "actual": False,
        "lifecycle_state": "starting",
        "drift_status": "matched",
        "suspended_by_master": False,
        "stop_reason": "",
        "last_error": "",
        "requested_at": "2026-07-28T08:55:28Z",
        "readback_captured_at": "2026-07-28T08:57:00Z",
        "policy_epoch": 17,
        "transition_run_id": "autoanswers-transition-17",
        "runtime_schedule": {
            "policy_epoch": 17,
            "transition_run_id": "autoanswers-transition-17",
        },
        "components": components,
    }
    status = {
        "captured_at": "2026-07-28T08:57:01Z",
        "timers": timers,
        "services": services,
        # This is the redundant, later feature-store view that triggered the
        # production false negative. It is deliberately not authoritative for
        # a restore already confirmed by the feature-owned reconcile call.
        "auto_updates": {
            "processes": [
                {
                    "process_key": "autoanswers",
                    "drift_status": "blocked",
                    "stop_reason": "worker_unavailable",
                }
            ]
        },
    }
    return dict(spec), preflight, lifecycle, status


def _assert_autoanswers_restore_uses_bound_lifecycle_readback() -> None:
    spec, preflight, lifecycle, status = _autoanswers_restore_fixture()
    lifecycle["stop_reason"] = "reconciliation_in_progress"
    accepted = maintenance._validated_autoanswers_restore_readback(
        lifecycle_readback=lifecycle,
        preflight_state=preflight,
        status=status,
        spec=spec,
    )
    assert accepted["drift_status"] == "matched"
    assert accepted["lifecycle_state"] == "starting"
    assert accepted["post_resume_validation"]["accepted"] is True

    blocked = maintenance._validated_autoanswers_restore_readback(
        lifecycle_readback={
            **lifecycle,
            "stop_reason": "worker_error",
        },
        preflight_state=preflight,
        status=status,
        spec=spec,
    )
    assert blocked["drift_status"] == "unknown"
    assert blocked["post_resume_validation"]["failures"] == [
        "stop_reason"
    ]
    assert accepted["post_resume_validation"]["fingerprint"].startswith(
        "sha256:"
    )

    for label, mutate in (
        (
            "feature lifecycle block",
            lambda candidate, _outer: candidate.update(
                {
                    "drift_status": "blocked",
                    "stop_reason": "worker_unavailable",
                }
            ),
        ),
        (
            "feature identity drift",
            lambda candidate, _outer: candidate.update(
                {"transition_run_id": "different-run"}
            ),
        ),
        (
            "outer timer drift",
            lambda _candidate, outer: outer["timers"][
                "wb-core-autoanswers-worker.timer"
            ].update(
                {
                    "is_enabled": "disabled",
                    "is_active": "inactive",
                }
            ),
        ),
    ):
        candidate = copy.deepcopy(lifecycle)
        outer = copy.deepcopy(status)
        mutate(candidate, outer)
        rejected = maintenance._validated_autoanswers_restore_readback(
            lifecycle_readback=candidate,
            preflight_state=preflight,
            status=outer,
            spec=spec,
        )
        assert rejected["drift_status"] == "unknown", label
        assert rejected["post_resume_validation"]["accepted"] is False, label
        assert rejected["post_resume_validation"]["failures"], label


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
        fbs_writer = result["timers"][
            "wb-core-fbs-shadow-collector.timer"
        ]
        assert fbs_writer["is_enabled"] == "disabled"
        assert fbs_writer["is_active"] == "inactive"
        root_storage_observer = result["continuous_observer_timers"][
            "wb-core-root-storage-policy.timer"
        ]
        assert root_storage_observer["is_enabled"] == "enabled"
        assert root_storage_observer["is_active"] == "active"
        assert systemd.mutations == [
            "wb-core-autoanswers-worker.timer",
            "wb-core-autoanswers-readonly-sync.timer",
            *maintenance.CORE_TIMER_UNITS,
            *maintenance.INDEPENDENT_WRITER_TIMER_UNITS,
        ]
        assert schedules.disable_calls == 1
        assert result["runtime_schedules"]["web_vitrina"]["schedule_policy"]["mode"] == "interval"
        assert result["runtime_schedules"]["web_vitrina"]["enabled_ids"]
        assert result["runtime_schedules"]["feedback_complaints"]["enabled_ids"]
        assert result["runtime_schedules"]["spp"]["active_job"] is None
        state_path = runtime_dir / maintenance.STATE_FILENAME
        audit_path = runtime_dir / maintenance.AUDIT_FILENAME
        assert state_path.stat().st_mode & 0o777 == 0o600
        assert audit_path.stat().st_mode & 0o777 == 0o600
        state = json.loads(state_path.read_text())
        assert state["phase"] == "held"
        assert state["runtime_schedule_baseline"]["web_vitrina"]["schedule_policy"]["mode"] == "interval"


def _assert_prepared_quiet_hold_is_reused_without_lifecycle_replay() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        systemd = FakeSystemd()
        schedules = FakeSchedules()
        old = _with_quiet_local_boundaries()
        try:
            prepared = maintenance.maintenance_prepare(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
                actor="snapshot-smoke",
                reason="first exact drain",
            )
            assert prepared["status"] == "prepared"
            assert prepared["quiet"] is True
            policy_path = runtime_dir / maintenance.POLICY_FILENAME
            policy_before = policy_path.read_bytes()
            mutations_before = list(systemd.mutations)
            disable_calls_before = schedules.disable_calls
            state_before = json.loads(
                (runtime_dir / maintenance.STATE_FILENAME).read_text()
            )

            held = maintenance.maintenance_hold(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
                actor="snapshot-smoke",
                reason="same exact drain",
            )
            prepared_again = maintenance.maintenance_prepare(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
                actor="snapshot-smoke",
                reason="reuse held drain",
            )
        finally:
            _restore_local_boundaries(old)

        assert held["status"] == "held"
        assert held["quiet"] is True
        assert held["idempotent"] is True
        assert held["reused_phase"] == "prepared"
        assert prepared_again["status"] == "prepared"
        assert prepared_again["quiet"] is True
        assert prepared_again["idempotent"] is True
        assert prepared_again["reused_phase"] == "held"
        assert systemd.mutations == mutations_before
        assert schedules.disable_calls == disable_calls_before
        assert policy_path.read_bytes() == policy_before
        state_after = json.loads(
            (runtime_dir / maintenance.STATE_FILENAME).read_text()
        )
        assert state_after["phase"] == "held"
        assert (
            state_after["hold_started_at"]
            == state_before["hold_started_at"]
        )
        assert (
            state_after["control_signature_before_hold"]
            == state_before["control_signature_before_hold"]
        )
        audit_rows = [
            json.loads(line)
            for line in (
                runtime_dir / maintenance.AUDIT_FILENAME
            ).read_text().splitlines()
            if line.strip()
        ]
        reused = [
            row for row in audit_rows if row.get("event") == "prepare_reused"
        ]
        assert [row["phase"] for row in reused] == [
            "prepared",
            "held",
        ]
        assert all(row["paused_policy_revision"] > 0 for row in reused)


def _assert_prepared_quiet_hold_reuse_fails_closed_on_drift() -> None:
    cases = (
        (
            "policy identity",
            "active maintenance hold paused-policy identity drifted",
        ),
        (
            "control intent",
            "active maintenance hold control intent drifted",
        ),
        (
            "baseline signature",
            "active maintenance hold baseline signature drifted",
        ),
        (
            "quiet boundary",
            "active maintenance hold is no longer quiet",
        ),
    )
    for case, expected_error in cases:
        with tempfile.TemporaryDirectory() as raw:
            runtime_dir = Path(raw)
            proc_root = runtime_dir / "proc"
            proc_root.mkdir()
            systemd = FakeSystemd()
            schedules = FakeSchedules()
            old = _with_quiet_local_boundaries()
            try:
                prepared = maintenance.maintenance_prepare(
                    runtime_dir,
                    systemd=systemd,
                    schedules=schedules,
                    proc_root=proc_root,
                    actor="snapshot-smoke",
                    reason="first exact drain",
                )
                assert prepared["status"] == "prepared"
                mutations_before = list(systemd.mutations)
                disable_calls_before = schedules.disable_calls

                if case == "policy identity":
                    policy_path = runtime_dir / maintenance.POLICY_FILENAME
                    policy = json.loads(policy_path.read_text())
                    policy["policy_fingerprint"] = "sha256:drifted"
                    policy_path.write_text(json.dumps(policy))
                elif case == "control intent":
                    schedules.payloads["web_vitrina"]["schedule_policy"][
                        "interval_hours"
                    ] = 6
                elif case == "baseline signature":
                    state_path = runtime_dir / maintenance.STATE_FILENAME
                    state = json.loads(state_path.read_text())
                    state["control_signature_before_hold"][
                        "fingerprint"
                    ] = "sha256:drifted"
                    state_path.write_text(json.dumps(state))
                else:
                    unit = maintenance.CORE_TIMER_UNITS[0]
                    systemd.timer_states[unit]["is_enabled"] = "enabled"
                    systemd.timer_states[unit]["is_active"] = "active"

                try:
                    maintenance.maintenance_prepare(
                        runtime_dir,
                        systemd=systemd,
                        schedules=schedules,
                        proc_root=proc_root,
                        actor="snapshot-smoke",
                        reason=f"reject {case}",
                    )
                except RuntimeError as exc:
                    assert expected_error in str(exc)
                else:
                    raise AssertionError(f"{case} drift was accepted")
            finally:
                _restore_local_boundaries(old)

            assert systemd.mutations == mutations_before
            assert schedules.disable_calls == disable_calls_before


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


def _assert_quiet_confirmed_hold_continuity_is_exact() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        fingerprint = "sha256:" + "6" * 64
        state = {
            "schema_version": maintenance.SCHEMA_VERSION,
            "phase": "held",
            "hold_started_at": "2026-07-28T15:44:46Z",
            "hold_readback": {"quiet": True},
        }
        maintenance.acquire_barrier(
            runtime_dir,
            window_id="snapshot-quiet-held-smoke",
            window_kind="snapshot",
            plan_fingerprint=fingerprint,
            approval_reference="quiet-held-smoke-approval",
            actor="smoke",
            reason="prove quiet confirmed restore",
        )
        (runtime_dir / maintenance.STATE_FILENAME).write_text(
            json.dumps(state),
            encoding="utf-8",
        )
        maintenance.confirm_barrier_hold(
            runtime_dir,
            window_id="snapshot-quiet-held-smoke",
            plan_fingerprint=fingerprint,
            maintenance_state=state,
        )
        maintenance.mark_barrier_restoring(
            runtime_dir,
            window_id="snapshot-quiet-held-smoke",
            plan_fingerprint=fingerprint,
        )
        status = {"quiet": True}
        evidence = maintenance._restore_service_continuity(
            runtime_dir,
            maintenance_state=state,
            current_status=status,
        )
        assert evidence["boundary_kind"] == (
            maintenance.QUIET_CONFIRMED_HOLD_CONTINUITY_KIND
        )
        assert evidence["services"] == []
        assert maintenance._validated_pre_hold_service_continuity_evidence(
            runtime_dir,
            maintenance_state=state,
            evidence=evidence,
            current_status=status,
        ) == evidence
        readback = maintenance._verify_pre_hold_service_continuity(
            FakeSystemd(),
            evidence,
        )
        assert readback["boundary_kind"] == (
            maintenance.QUIET_CONFIRMED_HOLD_CONTINUITY_KIND
        )
        assert readback["services"] == []

        try:
            maintenance._validated_pre_hold_service_continuity_evidence(
                runtime_dir,
                maintenance_state=state,
                evidence=evidence,
                current_status={"quiet": False},
            )
        except RuntimeError as exc:
            assert "quiet confirmed-hold continuity drifted" in str(exc)
        else:
            raise AssertionError(
                "non-quiet confirmed hold accepted restore continuity"
            )


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


def _assert_exact_fbs_shadow_process_detection() -> None:
    with tempfile.TemporaryDirectory() as raw:
        proc_root = Path(raw)
        exact = proc_root / "4101"
        exact.mkdir()
        (exact / "cmdline").write_bytes(
            b"/usr/bin/python3\0apps/wb_fbs_shadow.py\0poll\0"
        )
        absolute = proc_root / "4102"
        absolute.mkdir()
        (absolute / "cmdline").write_bytes(
            b"/usr/bin/python3\0"
            b"/opt/wb-core-runtime/app/apps/wb_fbs_shadow.py\0poll\0"
        )
        lookalike = proc_root / "4103"
        lookalike.mkdir()
        (lookalike / "cmdline").write_bytes(
            b"/usr/bin/python3\0apps/wb_fbs_shadow.py.backup\0poll\0"
        )
        legacy_nonexistent = proc_root / "4104"
        legacy_nonexistent.mkdir()
        (legacy_nonexistent / "cmdline").write_bytes(
            b"/usr/bin/python3\0apps/wb_fbs_shadow_collector.py\0poll\0"
        )
        assert maintenance._writer_processes(proc_root) == [
            {"pid": 4101, "marker": maintenance.FBS_SHADOW_PROCESS_MARKER},
            {"pid": 4102, "marker": maintenance.FBS_SHADOW_PROCESS_MARKER},
        ]


def _assert_legacy_control_signature_bytes_are_stable() -> None:
    legacy_units = (
        "wb-core-fbs-warehouse-registry.timer",
        "wb-core-sheet-vitrina-canary-restore.timer",
        "wb-core-sheet-vitrina-health-candidate.timer",
        "wb-core-sheet-vitrina-health-confirmation.timer",
    )
    status = {
        "auto_updates": {"master_desired": True, "processes": []},
        "runtime_schedules": {
            "web_vitrina": {
                "schedule_count": 0,
                "enabled_ids": [],
                "schedule_policy": {},
            },
            "feedback_complaints": {
                "schedule_count": 0,
                "enabled_ids": [],
            },
        },
        "timers": {
            unit: {"is_enabled": "enabled", "is_active": "active"}
            for unit in legacy_units
        },
        "unknown_wb_core_timers": [],
        "cron_entries": [],
    }
    assert maintenance.maintenance_control_signature(status)[
        "fingerprint"
    ] == "sha256:9300e94cf51d7189104a931064ae77f6f394451f406a7f92532f2b6ef4e47d9a"


def _assert_unstarted_hold_abort_is_exact_and_drift_safe() -> None:
    production_unknown_five = {
        "wb-core-fbs-warehouse-registry.timer",
        "wb-core-root-storage-policy.timer",
        "wb-core-sheet-vitrina-canary-restore.timer",
        "wb-core-sheet-vitrina-health-candidate.timer",
        "wb-core-sheet-vitrina-health-confirmation.timer",
    }
    assert production_unknown_five <= set(
        maintenance.CLASSIFIED_WB_CORE_TIMER_UNITS
    )
    assert production_unknown_five - {
        "wb-core-root-storage-policy.timer"
    } <= set(maintenance.INDEPENDENT_WRITER_TIMER_UNITS)
    assert "wb-core-root-storage-policy.timer" in (
        maintenance.CONTINUOUS_OBSERVER_TIMER_UNITS
    )
    assert "wb-core-fbs-shadow-collector.timer" in (
        maintenance.INDEPENDENT_WRITER_TIMER_UNITS
    )
    assert "wb-core-fbs-shadow-collector.timer" not in (
        maintenance.CONTINUOUS_OBSERVER_TIMER_UNITS
    )

    def current_status() -> dict[str, Any]:
        fake = FakeSystemd()
        return {
            "schema_version": maintenance.SCHEMA_VERSION,
            "status": "not_quiet",
            "quiet": False,
            "captured_at": maintenance._utc_now(),
            "timers": {
                unit: copy.deepcopy(fake.timer_states[unit])
                for unit in maintenance.ALL_BUSINESS_TIMER_UNITS
            },
            "continuous_observer_timers": {
                unit: copy.deepcopy(fake.timer_states[unit])
                for unit in maintenance.CONTINUOUS_OBSERVER_TIMER_UNITS
            },
            "services": {},
            "discovered_wb_core_timers": list(
                maintenance.CLASSIFIED_WB_CORE_TIMER_UNITS
            ),
            "unknown_wb_core_timers": [],
            "runtime_schedules": {},
            "writer_processes": [{"pid": 101, "marker": "ordinary"}],
            "writer_locks": {},
            "cron_entries": [],
            "auto_updates": {
                "master_desired": True,
                "revision": 54,
                "processes": [],
                "unknown_processes": [],
                "drift_processes": [],
            },
        }

    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        maintenance._save_json_0600(
            runtime_dir / maintenance.STATE_FILENAME,
            {
                "schema_version": maintenance.SCHEMA_VERSION,
                "phase": "restored",
                "restored_at": "2026-08-14T00:00:00Z",
                "exact_prior_state_restored": True,
            },
        )
        maintenance._append_audit_0600(
            runtime_dir / maintenance.AUDIT_FILENAME,
            {
                "event": "hold_restored",
                "captured_at": "2026-08-14T00:00:00Z",
            },
        )
        plan = "sha256:" + "7" * 64
        window = "unstarted-hold-abort-smoke"
        maintenance.acquire_barrier(
            runtime_dir,
            window_id=window,
            window_kind="final_cutover",
            plan_fingerprint=plan,
            approval_reference="approval-comment-971",
            actor="smoke",
            reason="prove pre-prepare recovery",
        )
        original_status = maintenance.maintenance_status
        maintenance.maintenance_status = lambda *args, **kwargs: current_status()
        systemd = FakeSystemd()
        timer_prestate = copy.deepcopy(systemd.timer_states)
        try:
            readback = maintenance.maintenance_barrier_abort_readback(
                runtime_dir,
                systemd=systemd,
                schedules=FakeSchedules(),
                proc_root=runtime_dir / "proc",
            )
        finally:
            maintenance.maintenance_status = original_status
        assert readback["status"] == "restored"
        assert readback["exact_prior_state_restored"] is True
        assert readback["restore_boundary_kind"] == (
            "no_maintenance_hold_started"
        )
        assert readback["no_hold_proof"]["last_maintenance_event"] == (
            "hold_restored"
        )
        assert systemd.mutations == []
        assert systemd.timer_states == timer_prestate
        aborted = maintenance.abort_barrier_acquire(
            runtime_dir,
            window_id=window,
            plan_fingerprint=plan,
            actor="smoke",
            reason="no maintenance hold started",
            restore_readback=readback,
        )
        assert aborted["active"] is False
        barrier_state = json.loads(
            (
                runtime_dir
                / write_barrier.STATE_FILENAME
            ).read_text()
        )
        assert barrier_state["restore"]["restore_boundary_kind"] == (
            "no_maintenance_hold_started"
        )
        assert barrier_state["restore"]["no_hold_proof_fingerprint"] == (
            readback["no_hold_proof_fingerprint"]
        )
        assert barrier_state["restore"]["no_hold_proof"] == readback[
            "no_hold_proof"
        ]
        repeated_readback = maintenance.maintenance_barrier_abort_readback(
            runtime_dir,
            systemd=FakeSystemd(),
            schedules=FakeSchedules(),
            proc_root=runtime_dir / "proc",
        )
        assert maintenance.abort_barrier_acquire(
            runtime_dir,
            window_id=window,
            plan_fingerprint=plan,
            actor="smoke",
            reason="idempotent unstarted-hold abort",
            restore_readback=repeated_readback,
        )["idempotent"] is True

    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        maintenance._save_json_0600(
            runtime_dir / maintenance.STATE_FILENAME,
            {
                "schema_version": maintenance.SCHEMA_VERSION,
                "phase": "restored",
                "restored_at": "2026-08-14T00:00:00Z",
                "exact_prior_state_restored": True,
            },
        )
        maintenance._append_audit_0600(
            runtime_dir / maintenance.AUDIT_FILENAME,
            {
                "event": "hold_restored",
                "captured_at": "2026-08-14T00:00:00Z",
            },
        )
        maintenance.acquire_barrier(
            runtime_dir,
            window_id="unstarted-hold-drift-smoke",
            window_kind="final_cutover",
            plan_fingerprint="sha256:" + "8" * 64,
            approval_reference="approval-comment-971",
            actor="smoke",
            reason="prove drift rejection",
        )
        maintenance._append_audit_0600(
            runtime_dir / maintenance.AUDIT_FILENAME,
            {
                "event": "hold_started",
                "captured_at": maintenance._utc_now(),
            },
        )
        try:
            maintenance.maintenance_barrier_abort_readback(
                runtime_dir,
                systemd=FakeSystemd(),
                schedules=FakeSchedules(),
                proc_root=runtime_dir / "proc",
            )
        except RuntimeError as exc:
            assert "neither an unstarted-hold proof" in str(exc)
        else:
            raise AssertionError(
                "post-barrier maintenance audit drift was accepted"
            )
        assert maintenance.barrier_status(runtime_dir)["active"] is True


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


def _assert_prepared_nonquiet_restart_resume_is_exact() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        _warehouse_baseline(runtime_dir)
        operational = runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(operational) as connection:
            connection.execute(
                "CREATE TABLE business_sentinel (id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.execute(
                "INSERT INTO business_sentinel (value) VALUES (?)",
                ("production-shaped-business-payload",),
            )
        source_digest = hashlib.sha256(operational.read_bytes()).hexdigest()
        window_id = "prepared-restart-smoke"
        plan_fingerprint = "sha256:" + "9" * 64
        maintenance.acquire_barrier(
            runtime_dir,
            window_id=window_id,
            window_kind="snapshot",
            plan_fingerprint=plan_fingerprint,
            approval_reference="root-gate-smoke",
            actor="smoke",
            reason="prove exact prepared restart continuation",
        )
        first_process = FakeSystemd()
        warehouse_timer = "wb-core-warehouse-functional-sync.timer"
        first_process.timer_states[warehouse_timer].update(
            {"is_enabled": "enabled", "is_active": "active"}
        )
        fbs_service = "wb-core-fbs-shadow-collector.service"
        first_process.service_states[fbs_service].update(
            {
                "is_active": "activating",
                "properties": {
                    "MainPID": 4242,
                    "ExecMainStartTimestamp": (
                        "Sat 2000-01-01 00:00:00 UTC"
                    ),
                },
            }
        )
        schedules = FakeSchedules()
        old = _with_quiet_local_boundaries()
        try:
            prepared = maintenance.maintenance_prepare(
                runtime_dir,
                systemd=first_process,
                schedules=schedules,
                proc_root=proc_root,
                actor="smoke-first-process",
                reason="interrupt after durable core prepare",
            )
            assert prepared["status"] == "prepared"
            assert prepared["quiet"] is False
            prepared_state_path = runtime_dir / maintenance.STATE_FILENAME
            original_prepared_state = prepared_state_path.read_bytes()
            original_prepared = json.loads(original_prepared_state)
            original_baseline = copy.deepcopy(original_prepared["baseline"])
            original_signature = copy.deepcopy(
                original_prepared["control_signature_before_hold"]
            )
            initial_mutations = list(first_process.mutations)
            initial_disable_calls = schedules.disable_calls
            revision = int(
                maintenance.load_or_initialize_owner_policy(runtime_dir)[
                    "revision"
                ]
            )

            restarted = FakeSystemd()
            restarted.timer_states = copy.deepcopy(
                first_process.timer_states
            )
            restarted.service_states = copy.deepcopy(
                first_process.service_states
            )
            restarted.service_states[fbs_service].update(
                {
                    "is_active": "inactive",
                    "properties": {"MainPID": 0},
                }
            )
            restarted.mutations = []

            for label, kwargs, expected in (
                (
                    "window",
                    {
                        "window_id": "different-window",
                        "plan_fingerprint": plan_fingerprint,
                        "expected_revision": revision,
                    },
                    "barrier identity drifted",
                ),
                (
                    "revision",
                    {
                        "window_id": window_id,
                        "plan_fingerprint": plan_fingerprint,
                        "expected_revision": revision + 1,
                    },
                    "stale policy revision",
                ),
            ):
                try:
                    maintenance.maintenance_prepare(
                        runtime_dir,
                        systemd=restarted,
                        schedules=schedules,
                        proc_root=proc_root,
                        actor="smoke-restarted-process",
                        reason=f"reject mismatched {label}",
                        **kwargs,
                    )
                except RuntimeError as exc:
                    assert expected in str(exc)
                else:
                    raise AssertionError(
                        f"prepared continuation accepted mismatched {label}"
                    )

            drifted_state = json.loads(original_prepared_state)
            drifted_state["baseline"][
                "discovered_wb_core_timers"
            ] = drifted_state["baseline"][
                "discovered_wb_core_timers"
            ][:-1]
            prepared_state_path.write_text(json.dumps(drifted_state))
            try:
                maintenance.maintenance_prepare(
                    runtime_dir,
                    systemd=restarted,
                    schedules=schedules,
                    proc_root=proc_root,
                    expected_revision=revision,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                )
            except RuntimeError as exc:
                assert "timer inventory drifted" in str(exc)
            else:
                raise AssertionError(
                    "prepared continuation accepted changed prestate"
                )
            prepared_state_path.write_bytes(original_prepared_state)
            prepared_state_path.chmod(0o600)

            lock_active = {"held": True}

            def lock_summary(runtime: Path) -> dict[str, Any]:
                return {
                    "warehouse_functional": {
                        "path": str(runtime / "warehouse.lock"),
                        "held": lock_active["held"],
                    },
                    "web_schedule": {
                        "path": str(runtime / "web.lock"),
                        "held": False,
                    },
                    "spp_execution": {
                        "path": str(runtime / "spp.lock"),
                        "held": False,
                    },
                    "seller_portal": {"busy": False},
                }

            maintenance._lock_summary = lock_summary
            try:
                maintenance.maintenance_prepare(
                    runtime_dir,
                    systemd=restarted,
                    schedules=schedules,
                    proc_root=proc_root,
                    expected_revision=revision,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                )
            except RuntimeError as exc:
                assert "lock is active" in str(exc)
            else:
                raise AssertionError(
                    "prepared continuation accepted an active writer lock"
                )
            lock_active["held"] = False

            journal = Path(str(operational) + "-journal")
            journal.write_bytes(b"hot")
            try:
                maintenance.maintenance_prepare(
                    runtime_dir,
                    systemd=restarted,
                    schedules=schedules,
                    proc_root=proc_root,
                    expected_revision=revision,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                )
            except RuntimeError as exc:
                assert "hot SQLite sidecar" in str(exc)
            else:
                raise AssertionError(
                    "prepared continuation accepted a hot journal"
                )
            journal.unlink()

            resumed = maintenance.maintenance_prepare(
                runtime_dir,
                systemd=restarted,
                schedules=schedules,
                proc_root=proc_root,
                actor="smoke-restarted-process",
                reason="resume same exact prepared revision",
                expected_revision=revision,
                window_id=window_id,
                plan_fingerprint=plan_fingerprint,
            )
            assert resumed["resume_pending"] is True
            assert resumed["reused_phase"] == "prepared"
            assert resumed["prepared_resume_binding"]["window_id"] == (
                window_id
            )
            bound_state_bytes = prepared_state_path.read_bytes()
            bound_state = json.loads(bound_state_bytes)
            assert bound_state["baseline"] == original_baseline
            assert (
                bound_state["control_signature_before_hold"]
                == original_signature
            )
            audit_before_retry = (
                runtime_dir / maintenance.AUDIT_FILENAME
            ).read_bytes()
            repeated = maintenance.maintenance_prepare(
                runtime_dir,
                systemd=restarted,
                schedules=schedules,
                proc_root=proc_root,
                expected_revision=revision,
                window_id=window_id,
                plan_fingerprint=plan_fingerprint,
            )
            assert repeated["prepared_resume_binding"] == resumed[
                "prepared_resume_binding"
            ]
            assert prepared_state_path.read_bytes() == bound_state_bytes
            assert (
                runtime_dir / maintenance.AUDIT_FILENAME
            ).read_bytes() == audit_before_retry
            assert restarted.mutations == []
            assert schedules.disable_calls == initial_disable_calls

            restarted.disable_now(
                warehouse_timer
            )
            restarted.service_states[fbs_service].update(
                {
                    "is_active": "activating",
                    "properties": {"MainPID": 4343},
                }
            )
            try:
                maintenance.maintenance_hold(
                    runtime_dir,
                    systemd=restarted,
                    schedules=schedules,
                    proc_root=proc_root,
                    wait_timeout_seconds=0,
                    poll_interval_seconds=0.01,
                    expected_revision=revision,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                )
            except TimeoutError as exc:
                assert "timed out waiting" in str(exc)
            else:
                raise AssertionError(
                    "resumed hold accepted a newly active FBS writer"
                )
            assert json.loads(prepared_state_path.read_text())[
                "phase"
            ] == "prepared"
            restarted.service_states[fbs_service].update(
                {
                    "is_active": "inactive",
                    "properties": {"MainPID": 0},
                }
            )
            held = maintenance.maintenance_hold(
                runtime_dir,
                systemd=restarted,
                schedules=schedules,
                proc_root=proc_root,
                poll_interval_seconds=0.01,
                expected_revision=revision,
                window_id=window_id,
                plan_fingerprint=plan_fingerprint,
            )
            assert held["status"] == "held"
            assert held["quiet"] is True
            assert held["stable_quiet_readback"][
                "fingerprint"
            ].startswith("sha256:")
            maintenance_state = json.loads(
                prepared_state_path.read_text()
            )
            maintenance.confirm_barrier_hold(
                runtime_dir,
                window_id=window_id,
                plan_fingerprint=plan_fingerprint,
                maintenance_state=maintenance_state,
            )

            def restore_warehouse(_: Path) -> dict[str, Any]:
                restarted.enable_now(
                    warehouse_timer
                )
                return {"status": "restored"}

            restored = maintenance.maintenance_restore(
                runtime_dir,
                systemd=restarted,
                schedules=schedules,
                proc_root=proc_root,
                actor="smoke",
                reason="restore exact pre-interruption controls",
                expected_revision=revision,
                warehouse_restore=restore_warehouse,
            )
            assert restored["status"] == "restored"
            assert restored["exact_prior_state_restored"] is True
            released = maintenance.release_barrier(
                runtime_dir,
                window_id=window_id,
                plan_fingerprint=plan_fingerprint,
                actor="smoke",
                reason="exact restore proven",
                restore_readback=restored,
            )
            assert released["active"] is False
            assert released["phase"] == "released"
            assert hashlib.sha256(operational.read_bytes()).hexdigest() == (
                source_digest
            )
            assert not any(
                Path(str(operational) + suffix).exists()
                for suffix in ("-journal", "-wal", "-shm")
            )
            assert initial_mutations == [
                "wb-core-autoanswers-worker.timer",
                "wb-core-autoanswers-readonly-sync.timer",
                *maintenance.CORE_TIMER_UNITS,
                *maintenance.INDEPENDENT_WRITER_TIMER_UNITS,
            ]
        finally:
            _restore_local_boundaries(old)


def _assert_legacy_prepared_fbs_writer_is_pause_owned_exactly() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        _warehouse_baseline(runtime_dir)
        operational = runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(operational) as connection:
            connection.execute(
                "CREATE TABLE business_sentinel (id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.execute(
                "INSERT INTO business_sentinel (value) VALUES ('unchanged')"
            )
        source_digest = hashlib.sha256(operational.read_bytes()).hexdigest()
        window_id = "legacy-fbs-prepared-restart"
        plan_fingerprint = "sha256:" + "7" * 64
        maintenance.acquire_barrier(
            runtime_dir,
            window_id=window_id,
            window_kind="snapshot",
            plan_fingerprint=plan_fingerprint,
            approval_reference="root-gate-legacy-fbs-smoke",
            actor="smoke",
            reason="bind exact legacy FBS timer prestate",
        )
        original_systemd = FakeSystemd()
        fbs_timer = maintenance.FBS_SHADOW_TIMER_UNIT
        fbs_service = maintenance.FBS_SHADOW_SERVICE_UNIT
        original_systemd.service_states[fbs_service].update(
            {
                "is_active": "activating",
                "properties": {
                    "MainPID": 5151,
                    "ExecMainStartTimestamp": "Sat 2000-01-01 00:00:00 UTC",
                },
            }
        )
        schedules = FakeSchedules()
        old = _with_quiet_local_boundaries()
        try:
            maintenance.maintenance_prepare(
                runtime_dir,
                systemd=original_systemd,
                schedules=schedules,
                proc_root=proc_root,
                actor="legacy-runtime",
                reason="partial prepare before inventory correction",
            )
            state_path = runtime_dir / maintenance.STATE_FILENAME
            audit_path = runtime_dir / maintenance.AUDIT_FILENAME
            legacy_state = json.loads(state_path.read_text())
            baseline_fbs = copy.deepcopy(
                legacy_state["baseline"]["timers"].pop(fbs_timer)
            )
            legacy_state["baseline"][
                "continuous_observer_timers"
            ][fbs_timer] = copy.deepcopy(baseline_fbs)
            legacy_state["baseline"]["services"].pop(fbs_service)
            legacy_prepared = legacy_state["prepare_readback"]
            legacy_prepared["timers"].pop(fbs_timer)
            legacy_prepared["continuous_observer_timers"][fbs_timer] = (
                copy.deepcopy(baseline_fbs)
            )
            legacy_prepared["services"].pop(fbs_service)
            legacy_state["prepare_readback"] = legacy_prepared
            state_path.write_text(json.dumps(legacy_state))
            state_path.chmod(0o600)
            audit_rows = [
                json.loads(line)
                for line in audit_path.read_text().splitlines()
                if line.strip()
            ]
            assert audit_rows[-1]["event"] == "core_freeze_prepared"
            audit_rows[-1]["status"] = copy.deepcopy(legacy_prepared)
            audit_path.write_text(
                "".join(
                    json.dumps(row, sort_keys=True, separators=(",", ":"))
                    + "\n"
                    for row in audit_rows
                )
            )
            audit_path.chmod(0o600)
            durable_baseline = copy.deepcopy(legacy_state["baseline"])
            durable_signature = copy.deepcopy(
                legacy_state["control_signature_before_hold"]
            )
            revision = int(
                legacy_prepared["auto_updates"]["revision"]
            )

            restarted = FakeSystemd()
            restarted.timer_states = copy.deepcopy(
                original_systemd.timer_states
            )
            deploy_reactivated_timers = {
                "wb-core-fbs-warehouse-registry.timer",
                "wb-core-finance-backup-rotation.timer",
                "wb-core-sheet-vitrina-canary-restore.timer",
                "wb-core-sheet-vitrina-health-candidate.timer",
                "wb-core-sheet-vitrina-health-confirmation.timer",
                "wb-core-warehouse-functional-sync.timer",
                fbs_timer,
            }
            for unit in deploy_reactivated_timers:
                restarted.timer_states[unit].update(
                    {
                        "is_enabled": "enabled",
                        "is_active": "active",
                        "properties": {
                            "LastTriggerUSec": (
                                "Mon 2000-01-03 00:00:00 UTC " + unit
                            )
                        },
                    }
                )
            restarted.service_states = copy.deepcopy(
                original_systemd.service_states
            )
            restarted.mutations = []
            warehouse_service = "wb-core-warehouse-functional-sync.service"
            restarted.service_states[warehouse_service].update(
                {
                    "is_active": "activating",
                    "properties": {
                        "MainPID": 5152,
                        "ExecMainStartTimestamp": (
                            "Sun 2000-01-02 00:00:00 UTC"
                        ),
                    },
                }
            )
            fbs_writer_proc = proc_root / "5151"
            fbs_writer_proc.mkdir()
            (fbs_writer_proc / "cmdline").write_bytes(
                b"/usr/bin/python3\0apps/wb_fbs_shadow.py\0poll\0"
            )
            warehouse_writer_proc = proc_root / "5152"
            warehouse_writer_proc.mkdir()
            (warehouse_writer_proc / "cmdline").write_bytes(
                b"/usr/bin/python3\0apps/warehouse_functional_runner.py\0"
                b"hourly-sync\0"
            )
            warehouse_lock = {"held": True}

            def lock_summary(runtime: Path) -> dict[str, Any]:
                return {
                    "warehouse_functional": {
                        "path": str(runtime / "warehouse.lock"),
                        "held": warehouse_lock["held"],
                    },
                    "web_schedule": {
                        "path": str(runtime / "web.lock"),
                        "held": False,
                    },
                    "spp_execution": {
                        "path": str(runtime / "spp.lock"),
                        "held": False,
                    },
                    "seller_portal": {"busy": False},
                    "finance_backup": {
                        "path": str(runtime / "finance.lock"),
                        "held": False,
                    },
                }

            maintenance._lock_summary = lock_summary
            state_before_unknown = state_path.read_bytes()
            audit_before_unknown = audit_path.read_bytes()
            restarted.unknown_timer = "wb-core-unknown-writer.timer"
            blocked_schedules = UnavailableSchedules()
            try:
                maintenance.maintenance_prepare(
                    runtime_dir,
                    systemd=restarted,
                    schedules=blocked_schedules,
                    proc_root=proc_root,
                    actor="corrected-runtime",
                    reason="reject unknown deploy timer drift",
                    expected_revision=revision,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                )
            except RuntimeError as exc:
                assert "unknown wb-core timers" in str(exc)
            else:
                raise AssertionError(
                    "prepared resume accepted an unknown deploy timer"
                )
            assert blocked_schedules.read_calls == 0
            assert restarted.mutations == []
            assert state_path.read_bytes() == state_before_unknown
            assert audit_path.read_bytes() == audit_before_unknown
            restarted.unknown_timer = ""

            restarted.unknown_active_service = (
                "wb-core-unknown-writer.service"
            )
            blocked_service_schedules = UnavailableSchedules()
            try:
                maintenance.maintenance_prepare(
                    runtime_dir,
                    systemd=restarted,
                    schedules=blocked_service_schedules,
                    proc_root=proc_root,
                    actor="corrected-runtime",
                    reason="reject unknown active service",
                    expected_revision=revision,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                )
            except RuntimeError as exc:
                assert "unknown active wb-core service" in str(exc)
            else:
                raise AssertionError(
                    "prepared resume accepted an unknown active service"
                )
            assert blocked_service_schedules.read_calls == 0
            assert restarted.mutations == []
            assert state_path.read_bytes() == state_before_unknown
            assert audit_path.read_bytes() == audit_before_unknown
            restarted.unknown_active_service = ""

            fake_disable_now = restarted.disable_now

            def systemd_disable_now(unit: str) -> None:
                fake_disable_now(unit)
                restarted.timer_states[unit]["properties"][
                    "LastTriggerUSec"
                ] = ""

            original_disable_now = systemd_disable_now
            interrupted_disables = {"count": 0}

            def interrupted_disable_now(unit: str) -> None:
                durable = json.loads(state_path.read_text())
                assert durable[
                    "pause_owned_inventory_resume_binding"
                ]["schema_version"] == (
                    "business_data_pause_owned_inventory_resume_v2"
                )
                original_disable_now(unit)
                interrupted_disables["count"] += 1
                if interrupted_disables["count"] == 2:
                    raise RuntimeError("synthetic process restart")

            restarted.disable_now = interrupted_disable_now
            interrupted_schedules = UnavailableSchedules()
            try:
                maintenance.maintenance_prepare(
                    runtime_dir,
                    systemd=restarted,
                    schedules=interrupted_schedules,
                    proc_root=proc_root,
                    actor="corrected-runtime",
                    reason="interrupt exact timer pause",
                    expected_revision=revision,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                )
            except RuntimeError as exc:
                assert "synthetic process restart" in str(exc)
            else:
                raise AssertionError(
                    "prepared resume did not preserve an interrupted pause"
                )
            assert interrupted_schedules.read_calls == 0
            interrupted_state = json.loads(state_path.read_text())
            assert interrupted_state[
                "pause_owned_inventory_resume_binding"
            ]["repaused_timer_units"] == sorted(
                deploy_reactivated_timers
            )
            assert len(restarted.mutations) == 2
            state_before_rogue = state_path.read_bytes()
            audit_before_rogue = audit_path.read_bytes()
            rogue_proc = proc_root / "6161"
            rogue_proc.mkdir()
            (rogue_proc / "cmdline").write_bytes(
                b"/usr/bin/python3\0apps/wb_fbs_shadow.py\0poll\0"
            )
            rogue_schedules = UnavailableSchedules()
            try:
                maintenance.maintenance_prepare(
                    runtime_dir,
                    systemd=restarted,
                    schedules=rogue_schedules,
                    proc_root=proc_root,
                    actor="corrected-runtime",
                    reason="reject new writer after durable binding",
                    expected_revision=revision,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                )
            except RuntimeError as exc:
                assert "another writer" in str(exc)
            else:
                raise AssertionError(
                    "prepared resume accepted a new writer process"
                )
            assert rogue_schedules.read_calls == 0
            assert len(restarted.mutations) == 2
            assert state_path.read_bytes() == state_before_rogue
            assert audit_path.read_bytes() == audit_before_rogue
            (rogue_proc / "cmdline").unlink()
            rogue_proc.rmdir()
            retriggered_unit = restarted.mutations[0]
            recorded_trigger = restarted.timer_states[retriggered_unit][
                "properties"
            ]["LastTriggerUSec"]
            assert recorded_trigger == ""
            restarted.timer_states[retriggered_unit]["properties"][
                "LastTriggerUSec"
            ] = "Tue 2000-01-04 00:00:00 UTC"
            retrigger_schedules = UnavailableSchedules()
            try:
                maintenance.maintenance_prepare(
                    runtime_dir,
                    systemd=restarted,
                    schedules=retrigger_schedules,
                    proc_root=proc_root,
                    actor="corrected-runtime",
                    reason="reject timer retrigger after durable binding",
                    expected_revision=revision,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                )
            except RuntimeError as exc:
                assert "timer retriggered" in str(exc)
            else:
                raise AssertionError(
                    "prepared resume accepted a timer retrigger"
                )
            assert retrigger_schedules.read_calls == 0
            assert len(restarted.mutations) == 2
            assert state_path.read_bytes() == state_before_rogue
            assert audit_path.read_bytes() == audit_before_rogue
            restarted.timer_states[retriggered_unit]["properties"][
                "LastTriggerUSec"
            ] = recorded_trigger
            (fbs_writer_proc / "cmdline").unlink()
            fbs_writer_proc.rmdir()
            restarted.service_states[fbs_service].update(
                {
                    "is_active": "inactive",
                    "properties": {
                        "MainPID": 0,
                        "ExecMainStartTimestamp": (
                            "Sat 2000-01-01 00:00:00 UTC"
                        ),
                    },
                }
            )

            resume_schedules = FakeSchedules()
            original_schedule_read_all = resume_schedules.read_all
            resume_schedule_reads = {"count": 0}

            def bound_schedule_read_all() -> dict[str, dict[str, Any]]:
                resume_schedule_reads["count"] += 1
                assert json.loads(state_path.read_text()).get(
                    "pause_owned_inventory_resume_binding"
                )
                return original_schedule_read_all()

            resume_schedules.read_all = bound_schedule_read_all
            original_unit_state = restarted.unit_state
            active_reads = {fbs_service: 0, warehouse_service: 0}

            def draining_unit_state(unit: str) -> dict[str, Any]:
                if unit in active_reads:
                    active_reads[unit] += 1
                    if (
                        active_reads[unit] > 2
                        and restarted.service_states[unit]["is_active"]
                        not in maintenance.QUIESCENT_SERVICE_STATES
                    ):
                        for timer in deploy_reactivated_timers:
                            assert restarted.timer_states[timer][
                                "is_enabled"
                            ] == "disabled"
                            assert restarted.timer_states[timer][
                                "is_active"
                            ] == "inactive"
                        pid = int(
                            restarted.service_states[unit]["properties"][
                                "MainPID"
                            ]
                        )
                        process_dir = proc_root / str(pid)
                        (process_dir / "cmdline").unlink()
                        process_dir.rmdir()
                        restarted.service_states[unit].update(
                            {
                                "is_active": "inactive",
                                "properties": {
                                    "MainPID": 0,
                                    "ExecMainStartTimestamp": (
                                        "Sat 2000-01-01 00:00:00 UTC"
                                        if unit == fbs_service
                                        else "Sun 2000-01-02 00:00:00 UTC"
                                    ),
                                },
                            }
                        )
                        if all(
                            restarted.service_states[name]["is_active"]
                            == "inactive"
                            for name in active_reads
                        ):
                            warehouse_lock["held"] = False
                return original_unit_state(unit)

            def bound_disable_now(unit: str) -> None:
                durable = json.loads(state_path.read_text())
                assert durable[
                    "pause_owned_inventory_resume_binding"
                ]["schema_version"] == (
                    "business_data_pause_owned_inventory_resume_v2"
                )
                original_disable_now(unit)

            restarted.unit_state = draining_unit_state
            restarted.disable_now = bound_disable_now
            resumed = maintenance.maintenance_hold(
                runtime_dir,
                systemd=restarted,
                schedules=resume_schedules,
                proc_root=proc_root,
                actor="corrected-runtime",
                reason="resume exact legacy FBS prestate",
                poll_interval_seconds=0.01,
                expected_revision=revision,
                window_id=window_id,
                plan_fingerprint=plan_fingerprint,
            )
            assert resume_schedule_reads["count"] > 0
            assert resumed["status"] == "held"
            assert resumed["quiet"] is True
            rebound_state = json.loads(state_path.read_text())
            binding = rebound_state[
                "pause_owned_inventory_resume_binding"
            ]
            assert binding["baseline_timer_source"] == (
                "continuous_observer_timers"
            )
            assert set(binding["deploy_drift_timer_states"]) == (
                deploy_reactivated_timers
            )
            assert set(binding["repaused_timer_units"]) == (
                deploy_reactivated_timers
            )
            assert set(binding["draining_service_units"]) == {
                fbs_service,
                warehouse_service,
            }
            assert binding["deploy_drift_service_generations"][
                fbs_service
            ]["main_pid"] == 5151
            assert binding["deploy_drift_service_generations"][
                warehouse_service
            ]["main_pid"] == 5152
            for unit in deploy_reactivated_timers:
                assert restarted.timer_states[unit]["is_enabled"] == (
                    "disabled"
                )
                assert restarted.timer_states[unit]["is_active"] == (
                    "inactive"
                )
                assert restarted.timer_states[unit]["properties"][
                    "LastTriggerUSec"
                ] == ""
                assert binding["deploy_drift_timer_states"][unit][
                    "properties"
                ]["LastTriggerUSec"] == (
                    "Mon 2000-01-03 00:00:00 UTC " + unit
                )
            assert restarted.mutations == [
                unit
                for unit in maintenance.ALL_BUSINESS_TIMER_UNITS
                if unit in deploy_reactivated_timers
            ]
            assert rebound_state["baseline"] == durable_baseline
            assert (
                rebound_state["control_signature_before_hold"]
                == durable_signature
            )
            assert resumed["stable_quiet_readback"]["fingerprint"].startswith(
                "sha256:"
            )

            def restore_warehouse(_: Path) -> dict[str, Any]:
                restarted.enable_now(
                    "wb-core-warehouse-functional-sync.timer"
                )
                return {"status": "restored"}

            restored = maintenance.maintenance_restore(
                runtime_dir,
                systemd=restarted,
                schedules=schedules,
                proc_root=proc_root,
                actor="smoke",
                reason="restore exact legacy FBS timer prestate",
                expected_revision=revision,
                warehouse_restore=restore_warehouse,
            )
            assert restored["status"] == "restored"
            assert restored["exact_prior_state_restored"] is True
            assert restarted.timer_states[fbs_timer]["is_enabled"] == (
                "enabled"
            )
            assert restarted.timer_states[fbs_timer]["is_active"] == "active"
            assert hashlib.sha256(operational.read_bytes()).hexdigest() == (
                source_digest
            )

            disabled_legacy = copy.deepcopy(durable_baseline)
            disabled_legacy["continuous_observer_timers"][fbs_timer].update(
                {"is_enabled": "disabled", "is_active": "inactive"}
            )
            disabled_plan = maintenance._independent_writer_timer_restore_plan(
                disabled_legacy
            )
            assert disabled_plan[fbs_timer] is False
            maintenance._restore_independent_writer_timers(
                restarted,
                disabled_plan,
            )
            assert restarted.timer_states[fbs_timer]["is_enabled"] == (
                "disabled"
            )
            assert restarted.timer_states[fbs_timer]["is_active"] == (
                "inactive"
            )
        finally:
            _restore_local_boundaries(old)


def _assert_exact_policy_restore_and_revision_guards() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw)
        proc_root = runtime_dir / "proc"
        proc_root.mkdir()
        _warehouse_baseline(runtime_dir)
        systemd = FakeSystemd()
        disabled_independent = (
            "wb-core-sheet-vitrina-health-confirmation.timer"
        )
        systemd.timer_states[disabled_independent]["is_enabled"] = "disabled"
        systemd.timer_states[disabled_independent]["is_active"] = "inactive"
        independent_prestate = {
            unit: copy.deepcopy(systemd.timer_states[unit])
            for unit in maintenance.INDEPENDENT_WRITER_TIMER_UNITS
        }
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
            for unit in maintenance.INDEPENDENT_WRITER_TIMER_UNITS:
                assert systemd.timer_states[unit] == independent_prestate[unit]
            assert systemd.timer_states[
                "wb-core-root-storage-policy.timer"
            ]["is_enabled"] == "enabled"
            assert restored["auto_updates"]["master_desired"] is True
            rows = {
                item["process_key"]: item
                for item in restored["auto_updates"]["processes"]
            }
            assert rows["warehouse_functional"]["actual"] is True
            assert rows["autoanswers"]["actual"] is False
            assert rows["autoanswers"]["component_states"]["readonly_sync"]["actual"] is True
            persisted_policy = json.loads(
                (
                    runtime_dir / maintenance.POLICY_FILENAME
                ).read_text()
            )
            autoanswers_validation = dict(
                (
                    persisted_policy.get("post_resume_readback") or {}
                ).get("autoanswers_validation")
                or {}
            )
            assert autoanswers_validation["accepted"] is True
            assert autoanswers_validation["source"] == (
                "feature_lifecycle_reconcile+outer_systemd"
            )
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


def _assert_production_timer_execstart_roles_are_exact() -> None:
    unit_root = (
        ROOT
        / "artifacts"
        / "registry_upload_http_entrypoint"
        / "systemd"
    )
    expected_writer_commands = {
        "wb-core-fbs-warehouse-registry": (
            "apps/wb_fbs_warehouse_registry.py",
            "collect",
        ),
        "wb-core-sheet-vitrina-canary-restore": (
            "apps/sheet_vitrina_v1_auto_refresh_tick.py",
            "--restore-expired-control-canary-pauses",
        ),
        "wb-core-sheet-vitrina-health-candidate": (
            "apps/sheet_vitrina_v1_health_tick.py",
            "--phase candidate",
        ),
        "wb-core-sheet-vitrina-health-confirmation": (
            "apps/sheet_vitrina_v1_health_tick.py",
            "--phase confirmation",
        ),
    }
    for unit, markers in expected_writer_commands.items():
        timer = unit_root / f"{unit}.timer"
        service = unit_root / f"{unit}.service"
        assert timer.is_file() and service.is_file()
        body = service.read_text(encoding="utf-8")
        assert all(marker in body for marker in markers)
        assert f"{unit}.timer" in maintenance.INDEPENDENT_WRITER_TIMER_UNITS

    root_storage_service = (
        unit_root / "wb-core-root-storage-policy.service"
    ).read_text(encoding="utf-8")
    assert "apps/root_storage_policy.py status" in root_storage_service
    assert "--output /var/lib/wb-core-root-storage-policy/status.json" in (
        root_storage_service
    )
    assert "wb-core-root-storage-policy.timer" in (
        maintenance.CONTINUOUS_OBSERVER_TIMER_UNITS
    )


def main() -> int:
    _assert_production_timer_execstart_roles_are_exact()
    _assert_autoanswers_restore_uses_bound_lifecycle_readback()
    _assert_hold_disables_every_boundary_without_killing_service()
    _assert_prepared_quiet_hold_is_reused_without_lifecycle_replay()
    _assert_prepared_quiet_hold_reuse_fails_closed_on_drift()
    _assert_exact_fbs_shadow_process_detection()
    _assert_legacy_control_signature_bytes_are_stable()
    _assert_unconfirmed_hold_abort_preserves_pre_hold_service_generation()
    _assert_persisted_service_continuity_accepts_exact_completion()
    _assert_quiet_confirmed_hold_continuity_is_exact()
    _assert_unknown_timer_fails_before_mutation()
    _assert_unstarted_hold_abort_is_exact_and_drift_safe()
    _assert_status_does_not_initialize_owner_policy()
    _assert_legacy_active_hold_is_not_guessed()
    _assert_prepared_nonquiet_restart_resume_is_exact()
    _assert_legacy_prepared_fbs_writer_is_pause_owned_exactly()
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
