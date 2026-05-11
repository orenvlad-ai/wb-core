#!/usr/bin/env python3
"""Smoke-check runtime-managed web-vitrina auto-refresh schedule state."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from types import MethodType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
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

        stale_save = block.save_schedules(
            {
                "schedules": [
                    {**final_by_id[schedules[0]["id"]], "local_time_hhmm": "12:45"},
                    {**final_by_id[schedules[1]["id"]], "enabled": False},
                    {
                        **final_by_id["custom_evening"],
                        "last_run_at": "2001-01-01T00:00:00Z",
                        "last_success_at": "2001-01-01T00:00:00Z",
                        "last_status": "success",
                    },
                ]
            }
        )
        stale_by_id = {item["id"]: item for item in stale_save["schedules"]}
        if stale_by_id["custom_evening"]["last_success_at"] != "2026-04-20T16:18:00Z":
            raise AssertionError(f"browser save must not overwrite server-owned lifecycle fields: {stale_save}")
        if stale_by_id[schedules[0]["id"]]["local_time_hhmm"] != "12:45":
            raise AssertionError(f"browser save must still persist editable fields: {stale_save}")

        block.mark_run_started(
            "custom_evening",
            started_at="2026-04-20T17:15:00Z",
            due_at="2026-04-20T17:15:00Z",
            run_id="job-2",
            trigger_source="manual_run_now_from_auto_schedule",
        )
        running_schedule = block.get_schedule("custom_evening")
        if running_schedule["last_status"] != "running" or running_schedule["last_status_label"] != "Выполняется":
            raise AssertionError(f"started run must clear stale success status immediately: {running_schedule}")
        block.mark_run_finished(
            "custom_evening",
            finished_at="2026-04-20T17:17:00Z",
            error="fixture failure",
        )
        failed = block.build_payload()
        failed_schedule = {item["id"]: item for item in failed["schedules"]}["custom_evening"]
        if failed_schedule["last_status"] != "error" or failed_schedule["last_success_at"] != "2026-04-20T16:18:00Z":
            raise AssertionError(f"failed manual run must update status without moving success time: {failed_schedule}")

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            now_factory=lambda: NOW,
            activated_at_factory=_timestamp_factory(
                [
                    "2026-04-20T18:00:00Z",
                    "2026-04-20T18:00:01Z",
                    "2026-04-20T18:03:00Z",
                    "2026-04-20T18:03:01Z",
                ]
            ),
        )
        _install_fake_auto_update(entrypoint)
        job = entrypoint.start_sheet_scheduled_auto_update_job(
            schedule_id="custom_evening",
            due_at="2026-04-20T18:00:00Z",
            trigger_source="scheduled",
        )
        terminal = _wait_for_job(entrypoint, str(job["job_id"]))
        if terminal["status"] != "success":
            raise AssertionError(f"scheduled auto-update job must finish successfully, got {terminal}")
        server_schedule = entrypoint.sheet_auto_refresh_schedules_block.get_schedule("custom_evening")
        if server_schedule["last_run_id"] != str(job["job_id"]) or server_schedule["last_status"] != "success":
            raise AssertionError(f"server scheduled runner must persist row status/run id, got {server_schedule}")

        print("web_vitrina_auto_schedules: ok ->", json.dumps({
            "schedule_mode": failed["schedule_mode"],
            "times": [item["local_time_hhmm"] for item in failed["schedules"]],
            "last_auto_job_id": server_schedule["last_run_id"],
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


def _timestamp_factory(values: list[str]):
    sequence = iter(values)
    last = values[-1]

    def factory() -> str:
        nonlocal last
        try:
            last = next(sequence)
        except StopIteration:
            pass
        return last

    return factory


def _install_fake_auto_update(entrypoint: RegistryUploadHttpEntrypoint) -> None:
    def fake_auto_update(self: RegistryUploadHttpEntrypoint, *, as_of_date: str | None, log: object) -> dict[str, object]:
        del self, as_of_date
        if callable(log):
            log("fixture scheduled auto update")
        return {
            "status": "success",
            "technical_status": "success",
            "semantic_status": "success",
            "semantic_label": "Успешно",
            "semantic_reason": "fixture ok",
            "snapshot_id": "scheduled-fixture",
            "as_of_date": "2026-04-20",
            "refreshed_at": "2026-04-20T18:02:00Z",
        }

    entrypoint._run_sheet_auto_update = MethodType(fake_auto_update, entrypoint)


def _wait_for_job(entrypoint: RegistryUploadHttpEntrypoint, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = entrypoint.handle_sheet_operator_job_request(job_id)
        if str(last.get("status") or "") != "running":
            return last
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {last}")


if __name__ == "__main__":
    main()
