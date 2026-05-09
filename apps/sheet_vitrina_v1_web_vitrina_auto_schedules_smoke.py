#!/usr/bin/env python3
"""Smoke-check runtime-managed web-vitrina auto-refresh schedule state."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1_auto_refresh import SheetVitrinaV1AutoRefreshSchedulesBlock


NOW = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)


def main() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-auto-schedules-") as tmp:
        runtime_dir = Path(tmp)
        block = SheetVitrinaV1AutoRefreshSchedulesBlock(
            runtime_dir=runtime_dir,
            now_factory=lambda: NOW,
        )
        initial = block.build_payload()
        _assert_payload_identity(initial)
        schedules = initial["schedules"]
        if [item["local_time_hhmm"] for item in schedules] != ["11:00", "20:00"]:
            raise AssertionError(f"default schedules mismatch: {initial}")
        if initial["next_auto_run_at"] != "2026-04-20T15:00:00Z":
            raise AssertionError(f"next run must use nearest enabled runtime schedule, got {initial}")

        saved = block.save_schedules(
            {
                "schedules": [
                    {**schedules[0], "local_time_hhmm": "12:30"},
                    {**schedules[1], "enabled": False},
                    {"id": "custom_evening", "enabled": True, "local_time_hhmm": "21:15"},
                ]
            }
        )
        _assert_payload_identity(saved)
        saved_by_id = {item["id"]: item for item in saved["schedules"]}
        if saved_by_id[schedules[0]["id"]]["local_time_hhmm"] != "12:30":
            raise AssertionError(f"schedule edit was not persisted: {saved}")
        if saved_by_id[schedules[1]["id"]]["enabled"] is not False:
            raise AssertionError(f"schedule disable was not persisted: {saved}")
        if "custom_evening" not in saved_by_id:
            raise AssertionError(f"schedule add was not persisted: {saved}")

        try:
            block.save_schedules(
                {
                    "schedules": [
                        {"id": "dup_a", "enabled": True, "local_time_hhmm": "12:30"},
                        {"id": "dup_b", "enabled": True, "local_time_hhmm": "12:30"},
                    ]
                }
            )
        except ValueError as exc:
            if "duplicate" not in str(exc):
                raise AssertionError(f"duplicate validation reason mismatch: {exc}") from exc
        else:
            raise AssertionError("duplicate enabled schedules must be rejected")

        block.mark_run_started(
            "custom_evening",
            started_at="2026-04-20T16:15:00Z",
            due_at="2026-04-20T16:15:00Z",
            run_id="job-1",
            trigger_source="scheduled",
        )
        block.mark_run_finished(
            "custom_evening",
            finished_at="2026-04-20T16:18:00Z",
            result_payload={"semantic_status": "success", "semantic_reason": "ok"},
        )
        final = block.build_payload()
        final_by_id = {item["id"]: item for item in final["schedules"]}
        if final_by_id["custom_evening"]["last_success_at"] != "2026-04-20T16:18:00Z":
            raise AssertionError(f"successful run metadata mismatch: {final}")
        if final["last_auto_job_id"] != "job-1" or final["last_auto_run_status"] != "success":
            raise AssertionError(f"global run summary mismatch: {final}")

        print("web_vitrina_auto_schedules: ok ->", json.dumps({
            "schedule_mode": final["schedule_mode"],
            "times": [item["local_time_hhmm"] for item in final["schedules"]],
            "last_auto_job_id": final["last_auto_job_id"],
        }, ensure_ascii=False))


def _assert_payload_identity(payload: dict[str, object]) -> None:
    if payload.get("contract_name") != "sheet_vitrina_v1_auto_refresh_schedules":
        raise AssertionError(f"contract identity mismatch: {payload}")
    if payload.get("schedule_mode") != "runtime_managed_json_schedule":
        raise AssertionError(f"schedule mode mismatch: {payload}")
    if payload.get("schedule_source") != "runtime_json":
        raise AssertionError(f"schedule source mismatch: {payload}")
    if payload.get("can_edit_runtime") is not True or payload.get("save_supported") is not True:
        raise AssertionError(f"schedule mutability mismatch: {payload}")
    if payload.get("run_now_supported") is not True:
        raise AssertionError(f"run-now support mismatch: {payload}")


if __name__ == "__main__":
    main()
