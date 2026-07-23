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
    def __init__(self, *, unknown_timer: str = "", active_reads: int = 0) -> None:
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


def main() -> int:
    _assert_hold_disables_every_boundary_without_killing_service()
    _assert_unknown_timer_fails_before_mutation()
    print("business data maintenance smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
