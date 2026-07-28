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
    _assert_autoanswers_restore_uses_bound_lifecycle_readback()
    _assert_hold_disables_every_boundary_without_killing_service()
    _assert_prepared_quiet_hold_is_reused_without_lifecycle_replay()
    _assert_prepared_quiet_hold_reuse_fails_closed_on_drift()
    _assert_unconfirmed_hold_abort_preserves_pre_hold_service_generation()
    _assert_persisted_service_continuity_accepts_exact_completion()
    _assert_quiet_confirmed_hold_continuity_is_exact()
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
