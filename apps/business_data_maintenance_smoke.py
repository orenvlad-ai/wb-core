#!/usr/bin/env python3
"""Focused offline checks for the business-data maintenance boundary."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import business_data_maintenance as maintenance  # noqa: E402


class FakeSystemd:
    def unit_state(self, unit: str) -> dict[str, object]:
        observer = unit in maintenance.CONTINUOUS_OBSERVER_TIMER_UNITS
        return {
            "unit": unit,
            "is_enabled": "enabled" if observer else "disabled",
            "is_active": "active" if observer else "inactive",
            "properties": {"MainPID": 0},
        }

    def discovered_timers(self) -> list[str]:
        return sorted(maintenance.CLASSIFIED_WB_CORE_TIMER_UNITS)


def main() -> int:
    source = Path(maintenance.__file__).read_text(encoding="utf-8")
    assert "abort-prepared" not in source
    assert "_resume_legacy_fbs_pause_ownership" not in source
    assert not (
        set(maintenance.ALL_BUSINESS_TIMER_UNITS)
        & set(maintenance.CONTINUOUS_OBSERVER_TIMER_UNITS)
    )

    runtime_schedule = {
        "web_vitrina": {
            "schedule_count": 0,
            "enabled_ids": [],
            "schedule_policy": {},
            "last_auto_run_status": "",
            "active": False,
        },
        "feedback_complaints": {
            "schedule_count": 0,
            "enabled_ids": [],
            "active_runs": [],
        },
        "spp": {"active_job": None},
    }
    with tempfile.TemporaryDirectory() as directory:
        runtime_dir = Path(directory)
        status = maintenance.maintenance_status(
            runtime_dir,
            systemd=FakeSystemd(),
            schedules=None,
            proc_root=runtime_dir / "missing-proc",
            runtime_schedule_readback=runtime_schedule,
        )
        assert status["quiet"] is True
        assert status["unknown_wb_core_timers"] == []
        assert status["auto_updates"]["overall_status_code"] == "unknown"

    print("business_data_maintenance_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
