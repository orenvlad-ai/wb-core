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

    def read_all(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.payloads)

    def disable_all(self, current: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        assert (current.get("web_vitrina") or {}).get("schedule_policy", {}).get("mode") == "interval"
        self.disable_calls += 1
        self.payloads["web_vitrina"]["schedule_policy"] = {"mode": "manual", "interval_hours": None}
        for item in self.payloads["web_vitrina"]["schedules"]:
            item["enabled"] = False
        for item in self.payloads["feedback_complaints"]["schedules"]:
            item["enabled"] = False
        self.payloads["spp"]["schedule"]["enabled"] = False
        return self.read_all()

    def restore_selected(
        self,
        baseline: Mapping[str, Mapping[str, Any]],
        *,
        desired: Mapping[str, bool],
    ) -> dict[str, dict[str, Any]]:
        self.payloads = copy.deepcopy(dict(baseline))
        for item in self.payloads["web_vitrina"]["schedules"]:
            item["enabled"] = bool(item.get("enabled")) and bool(
                desired.get("vitrina_refresh")
            )
        for item in self.payloads["feedback_complaints"]["schedules"]:
            item["enabled"] = bool(item.get("enabled")) and bool(
                desired.get("feedback_complaints")
            )
        self.payloads["spp"]["schedule"]["enabled"] = bool(
            self.payloads["spp"]["schedule"].get("enabled")
        ) and bool(desired.get("spp_test"))
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
        assert systemd.mutations == list(maintenance.CORE_TIMER_UNITS)
        assert schedules.disable_calls == 1
        assert result["runtime_schedules"]["web_vitrina"]["schedule_policy"]["mode"] == "manual"
        assert result["runtime_schedules"]["web_vitrina"]["enabled_ids"] == []
        assert result["runtime_schedules"]["feedback_complaints"]["enabled_ids"] == []
        assert result["runtime_schedules"]["spp"]["enabled"] is False
        state_path = runtime_dir / maintenance.STATE_FILENAME
        audit_path = runtime_dir / maintenance.AUDIT_FILENAME
        assert state_path.stat().st_mode & 0o777 == 0o600
        assert audit_path.stat().st_mode & 0o777 == 0o600
        state = json.loads(state_path.read_text())
        assert state["phase"] == "held"
        assert state["runtime_schedule_baseline"]["web_vitrina"]["schedule_policy"]["mode"] == "interval"


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
            assert policy["processes"]["autoanswers_readonly"]["desired"] is False
            assert policy["processes"]["autoanswers_worker"]["desired"] is False
            policy = maintenance.update_process_desired_state(
                runtime_dir,
                process_key="spp_test",
                desired=False,
                expected_revision=int(policy["revision"]),
                actor="smoke",
                reason="intentionally off",
            )

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
            assert rows["autoanswers_worker"]["actual"] is False
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
                    process_key="spp_test",
                    desired=True,
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
            for desired, expected_text in (
                (True, "отдельный Autoanswers lifecycle"),
                (False, "no-op desired state"),
            ):
                try:
                    maintenance.update_process_desired_state(
                        runtime_dir,
                        process_key="autoanswers_readonly",
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
                    process_key="spp_test",
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
        assert "runtime_confirmed=False" in str(exc)
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


def main() -> int:
    _assert_hold_disables_every_boundary_without_killing_service()
    _assert_unknown_timer_fails_before_mutation()
    _assert_status_does_not_initialize_owner_policy()
    _assert_exact_policy_restore_and_revision_guards()
    _assert_unknown_policy_state_blocks_resume()
    _assert_unsupported_enable_and_noop_are_preflighted()
    _assert_failed_resume_stays_paused_and_audited()
    _assert_success_requires_persisted_runtime_readback()
    print("business data maintenance smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
