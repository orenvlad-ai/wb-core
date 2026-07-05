#!/usr/bin/env python3
"""Smoke-check runtime-managed web-vitrina auto-refresh schedule state."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import MethodType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_auto_refresh import SheetVitrinaV1AutoRefreshSchedulesBlock
from apps.sheet_vitrina_v1_auto_refresh_tick import _mark_missed_due_slots, _select_due_for_tick


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
        _assert_default_timezone_schedule(runtime_dir)
        _assert_old_state_read_migrates(runtime_dir / "old-state")
        _assert_interval_policy(runtime_dir / "interval-policy")
        _assert_cross_process_schedule_writes(runtime_dir / "cross-process")

        saved = block.save_schedules(
            {
                "schedule_policy": {"mode": "manual"},
                "schedules": [
                    {**schedules[0], "local_time_hhmm": "12:30"},
                    {**schedules[1], "enabled": False},
                    {"id": "custom_night", "enabled": True, "local_time_hhmm": "02:15"},
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
        if saved_by_id["custom_night"]["local_time_hhmm"] != "02:15" or saved.get("schedule_mode_type") != "manual":
            raise AssertionError(f"manual mode must persist arbitrary times outside interval window, got {saved}")

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

        context_newer = block.build_payload(
            auto_context={
                "last_auto_run_status": "success",
                "last_auto_run_time": "2026-04-21T20:01:00+05:00",
                "last_auto_run_finished_at": "2026-04-21T20:03:00+05:00",
                "last_successful_auto_update_at": "2026-04-21T20:03:00+05:00",
                "last_auto_run_technical_status": "success",
            }
        )
        if context_newer["last_auto_run_at"] != "2026-04-21T20:01:00+05:00":
            raise AssertionError(f"newer canonical auto-update state must win over stale schedule rows: {context_newer}")
        if context_newer["last_auto_success_at"] != "2026-04-21T20:03:00+05:00":
            raise AssertionError(f"newer canonical success must win over stale schedule rows: {context_newer}")

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
        _assert_scheduled_parallel_block(entrypoint)

        print("web_vitrina_auto_schedules: ok ->", json.dumps({
            "schedule_mode": failed["schedule_mode"],
            "times": [item["local_time_hhmm"] for item in failed["schedules"]],
            "last_auto_job_id": server_schedule["last_run_id"],
        }, ensure_ascii=False))


def _assert_old_state_read_migrates(runtime_dir: Path) -> None:
    block = SheetVitrinaV1AutoRefreshSchedulesBlock(
        runtime_dir=runtime_dir,
        now_factory=lambda: NOW,
    )
    runtime_dir.mkdir(parents=True, exist_ok=True)
    block.path.write_text(
        json.dumps(
            {
                "contract_name": "sheet_vitrina_v1_auto_refresh_schedules",
                "contract_version": "v1",
                "schedules": [
                    {"id": "legacy_11", "enabled": True, "local_time_hhmm": "11:00", "timezone": "Asia/Yekaterinburg"},
                    {"id": "legacy_20", "enabled": True, "local_time_hhmm": "20:00", "timezone": "Asia/Yekaterinburg"},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    payload = block.build_payload()
    policy = payload.get("schedule_policy") or {}
    if policy.get("mode") != "manual" or payload.get("schedule_mode_type") != "manual":
        raise AssertionError(f"old schedule state must read-migrate as manual policy, got {payload}")
    if [item["local_time_hhmm"] for item in payload["schedules"]] != ["11:00", "20:00"]:
        raise AssertionError(f"old schedule rows must be preserved, got {payload}")


def _assert_interval_policy(runtime_dir: Path) -> None:
    previews = {
        3: ["10:00", "13:00", "16:00", "19:00", "22:00"],
        4: ["10:00", "14:00", "18:00", "22:00"],
        6: ["10:00", "16:00", "22:00"],
    }
    for hours, expected_slots in previews.items():
        block = SheetVitrinaV1AutoRefreshSchedulesBlock(
            runtime_dir=runtime_dir / f"{hours}h",
            now_factory=lambda: datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc),
        )
        payload = block.save_schedules(
            {
                "schedule_policy": {"mode": "interval", "interval_hours": hours},
                "schedules": [
                    {
                        "id": "browser_stale_lifecycle",
                        "enabled": True,
                        "local_time_hhmm": "03:00",
                        "last_success_at": "2001-01-01T00:00:00Z",
                    }
                ],
            }
        )
        if payload.get("schedule_mode_type") != "interval":
            raise AssertionError(f"interval mode type mismatch: {payload}")
        policy = payload.get("schedule_policy") or {}
        if policy.get("interval_hours") != hours or policy.get("window_start_hhmm") != "10:00" or policy.get("window_end_hhmm") != "22:00":
            raise AssertionError(f"canonical interval policy mismatch: {payload}")
        if payload.get("interval_preview_slots") != expected_slots:
            raise AssertionError(f"interval preview mismatch for {hours}h: {payload}")
        materialized_times = [item["local_time_hhmm"] for item in payload["schedules"]]
        if materialized_times != expected_slots:
            raise AssertionError(f"interval schedules must be materialized from policy, got {payload}")
        if any(item.get("editable") for item in payload["schedules"]):
            raise AssertionError(f"interval materialized rows must be read-only, got {payload}")
        if any(int(item["local_time_hhmm"].split(":", 1)[0]) < 10 or int(item["local_time_hhmm"].split(":", 1)[0]) > 22 for item in payload["schedules"]):
            raise AssertionError(f"interval schedules must not include night slots, got {payload}")
        if hours == 4:
            block.mark_run_started(
                "interval_4h_10_00_ekt",
                started_at="2026-05-12T05:01:00Z",
                due_at="2026-05-12T05:00:00Z",
                run_id="interval-10-job",
                trigger_source="scheduled",
            )
            block.mark_run_finished(
                "interval_4h_10_00_ekt",
                finished_at="2026-05-12T05:05:00Z",
                result_payload={"semantic_status": "success", "semantic_reason": "ok"},
            )
            raw = json.loads(block.path.read_text(encoding="utf-8"))
            raw["schedules"][0]["id"] = "legacy_interval_slot_10_00_ekt"
            block.path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            rematerialized = block.save_schedules(
                {"schedule_policy": {"mode": "interval", "interval_hours": 4}, "schedules": []}
            )
            rematerialized_by_time = {item["local_time_hhmm"]: item for item in rematerialized["schedules"]}
            ten_slot = rematerialized_by_time["10:00"]
            if ten_slot["id"] != "interval_4h_10_00_ekt" or ten_slot["last_run_at"] != "2026-05-12T05:01:00Z" or ten_slot["last_success_at"] != "2026-05-12T05:05:00Z":
                raise AssertionError(f"interval materialization must preserve lifecycle across id drift, got {ten_slot}")
            cadence_changed = block.save_schedules(
                {"schedule_policy": {"mode": "interval", "interval_hours": 3}, "schedules": []}
            )
            cadence_by_time = {item["local_time_hhmm"]: item for item in cadence_changed["schedules"]}
            if cadence_by_time["10:00"]["last_success_at"] != "2026-05-12T05:05:00Z":
                raise AssertionError(f"interval cadence switch must preserve matching slot lifecycle, got {cadence_changed}")
            payload = block.save_schedules(
                {"schedule_policy": {"mode": "interval", "interval_hours": 4}, "schedules": []}
            )
            manualized = block.save_schedules(
                {
                    "schedule_policy": {"mode": "manual"},
                    "schedules": payload["schedules"],
                }
            )
            if manualized.get("schedule_mode_type") != "manual" or any(item.get("schedule_type") != "manual" or item.get("editable") is not True for item in manualized["schedules"]):
                raise AssertionError(f"switching interval rows back to manual must canonicalize editable manual rows, got {manualized}")
    block = SheetVitrinaV1AutoRefreshSchedulesBlock(
        runtime_dir=runtime_dir / "invalid",
        now_factory=lambda: NOW,
    )
    try:
        block.save_schedules({"schedule_policy": {"mode": "interval", "interval_hours": 2}, "schedules": []})
    except ValueError as exc:
        if "at least 3" not in str(exc):
            raise AssertionError(f"invalid minimum interval reason mismatch: {exc}") from exc
    else:
        raise AssertionError("interval <3h must be rejected")
    try:
        block.save_schedules({"schedule_policy": {"mode": "interval", "interval_hours": 5}, "schedules": []})
    except ValueError as exc:
        if "one of 3, 4, 6" not in str(exc):
            raise AssertionError(f"unsupported interval reason mismatch: {exc}") from exc
    else:
        raise AssertionError("unsupported interval must be rejected")
    due_block = SheetVitrinaV1AutoRefreshSchedulesBlock(
        runtime_dir=runtime_dir / "due",
        now_factory=lambda: datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc),
    )
    due_block.save_schedules({"schedule_policy": {"mode": "interval", "interval_hours": 4}, "schedules": []})
    due = due_block.due_schedules(now=datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc))
    due_times = [item[0]["local_time_hhmm"] for item in due]
    if due_times != ["10:00", "14:00", "18:00"]:
        raise AssertionError(f"interval due slots before 22:00 mismatch: {due}")
    missed_due, selected_due = _select_due_for_tick(due)
    if [item[0]["local_time_hhmm"] for item in missed_due] != ["10:00", "14:00"] or [item[0]["local_time_hhmm"] for item in selected_due] != ["18:00"]:
        raise AssertionError(f"tick must select only latest accumulated due slot, got missed={missed_due}, selected={selected_due}")
    _mark_missed_due_slots(due_block, missed_due)
    due_after_missed = due_block.due_schedules(now=datetime(2026, 5, 12, 14, 31, tzinfo=timezone.utc))
    if [item[0]["local_time_hhmm"] for item in due_after_missed] != ["18:00"]:
        raise AssertionError(f"missed interval slots must not stay due, got {due_after_missed}")
    selected_schedule, selected_due_at = selected_due[0]
    due_block.mark_run_started(
        str(selected_schedule["id"]),
        started_at="2026-05-12T14:31:00Z",
        due_at=selected_due_at,
        run_id="interval-job",
        trigger_source="scheduled",
    )
    if due_block.due_schedules(now=datetime(2026, 5, 12, 14, 32, tzinfo=timezone.utc)):
        raise AssertionError("started interval slot must not be launched twice")
    due_block.mark_run_finished(
        str(selected_schedule["id"]),
        finished_at="2026-05-12T14:35:00Z",
        result_payload={"semantic_status": "success", "semantic_reason": "ok"},
    )
    if due_block.due_schedules(now=datetime(2026, 5, 12, 14, 36, tzinfo=timezone.utc)):
        raise AssertionError("finished interval slot must not be launched twice")


def _assert_scheduled_parallel_block(entrypoint: RegistryUploadHttpEntrypoint) -> None:
    release = threading.Event()

    def hold_auto_update(log: object) -> dict[str, object]:
        if callable(log):
            log("fixture active auto update")
        release.wait(2)
        return {"status": "success", "semantic_status": "success"}

    active = entrypoint.operator_jobs.start(operation="auto_update", runner=hold_auto_update)
    try:
        skipped = entrypoint.start_sheet_scheduled_auto_update_job(
            schedule_id="custom_evening",
            due_at="2026-04-20T18:30:00Z",
            trigger_source="scheduled",
        )
        if (
            skipped.get("status") != "skipped"
            or not skipped.get("already_running_job_id")
            or skipped.get("retryable") is not True
            or skipped.get("due_preserved") is not True
        ):
            raise AssertionError(f"scheduled slot must be blocked while prior auto update is running, got {skipped}")
        schedule = entrypoint.sheet_auto_refresh_schedules_block.get_schedule("custom_evening")
        if schedule.get("last_due_at") == "2026-04-20T18:30:00Z" or schedule.get("last_status") == "skipped":
            raise AssertionError(f"blocked scheduled slot must remain due for retry, got {schedule}")
    finally:
        release.set()
        _wait_for_job(entrypoint, str(active["job_id"]))


def _assert_cross_process_schedule_writes(runtime_dir: Path) -> None:
    block = SheetVitrinaV1AutoRefreshSchedulesBlock(
        runtime_dir=runtime_dir,
        now_factory=lambda: datetime(2026, 5, 12, 14, 30, tzinfo=timezone.utc),
    )
    block.save_schedules({"schedule_policy": {"mode": "interval", "interval_hours": 4}, "schedules": []})
    worker = """
import sys
from pathlib import Path
from packages.application.sheet_vitrina_v1_auto_refresh import SheetVitrinaV1AutoRefreshSchedulesBlock

runtime_dir = Path(sys.argv[1])
schedule_id = sys.argv[2]
started_at = sys.argv[3]
finished_at = sys.argv[4]
due_at = sys.argv[5]
block = SheetVitrinaV1AutoRefreshSchedulesBlock(runtime_dir=runtime_dir)
block.mark_run_started(schedule_id, started_at=started_at, due_at=due_at, run_id=schedule_id + "-job", trigger_source="scheduled")
block.mark_run_finished(schedule_id, finished_at=finished_at, result_payload={"semantic_status": "success", "semantic_reason": "ok"})
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker,
                str(runtime_dir),
                "interval_4h_10_00_ekt",
                "2026-05-12T05:01:00Z",
                "2026-05-12T05:05:00Z",
                "2026-05-12T05:00:00Z",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker,
                str(runtime_dir),
                "interval_4h_14_00_ekt",
                "2026-05-12T09:01:00Z",
                "2026-05-12T09:05:00Z",
                "2026-05-12T09:00:00Z",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        if process.returncode != 0:
            raise AssertionError(f"cross-process schedule writer failed: stdout={stdout} stderr={stderr}")
    payload = block.build_payload()
    by_id = {item["id"]: item for item in payload["schedules"]}
    if by_id["interval_4h_10_00_ekt"]["last_success_at"] != "2026-05-12T05:05:00Z":
        raise AssertionError(f"first cross-process schedule write was lost: {payload}")
    if by_id["interval_4h_14_00_ekt"]["last_success_at"] != "2026-05-12T09:05:00Z":
        raise AssertionError(f"second cross-process schedule write was lost: {payload}")


def _assert_default_timezone_schedule(runtime_dir: Path) -> None:
    before_11 = SheetVitrinaV1AutoRefreshSchedulesBlock(
        runtime_dir=runtime_dir / "tz-before-11",
        now_factory=lambda: datetime(2026, 5, 12, 5, 59, tzinfo=timezone.utc),
    )
    if before_11.build_payload()["next_auto_run_at"] != "2026-05-12T06:00:00Z":
        raise AssertionError("11:00 Asia/Yekaterinburg must map to 06:00Z before the morning run")
    due_11 = before_11.due_schedules(now=datetime(2026, 5, 12, 6, 1, tzinfo=timezone.utc))
    if [item[0]["local_time_hhmm"] for item in due_11] != ["11:00"]:
        raise AssertionError(f"only the 11:00 EKT schedule should be due at 06:01Z, got {due_11}")
    before_11.mark_run_started(
        "daily_11_00_ekt",
        started_at="2026-05-12T06:01:00Z",
        due_at="2026-05-12T06:00:00Z",
        run_id="job-morning",
        trigger_source="scheduled",
    )
    if before_11.due_schedules(now=datetime(2026, 5, 12, 6, 2, tzinfo=timezone.utc)):
        raise AssertionError("a recorded due attempt must not be launched twice in the same due slot")

    before_20 = SheetVitrinaV1AutoRefreshSchedulesBlock(
        runtime_dir=runtime_dir / "tz-before-20",
        now_factory=lambda: datetime(2026, 5, 12, 14, 59, tzinfo=timezone.utc),
    )
    if before_20.build_payload()["next_auto_run_at"] != "2026-05-12T15:00:00Z":
        raise AssertionError("20:00 Asia/Yekaterinburg must map to 15:00Z before the evening run")
    due_20 = before_20.due_schedules(now=datetime(2026, 5, 12, 15, 1, tzinfo=timezone.utc))
    if [item[0]["local_time_hhmm"] for item in due_20] != ["11:00", "20:00"]:
        raise AssertionError(f"missed morning plus current evening schedules should be due after 20:00 EKT, got {due_20}")
    before_20.mark_due_skipped(
        "daily_11_00_ekt",
        due_at="2026-05-12T06:00:00Z",
        reason="fixture missed by later due slot",
        trigger_source="raw_auto_refresh_missed_due",
    )
    due_after_skip = before_20.due_schedules(now=datetime(2026, 5, 12, 15, 2, tzinfo=timezone.utc))
    if [item[0]["local_time_hhmm"] for item in due_after_skip] != ["20:00"]:
        raise AssertionError(f"skipped missed schedule must not stay due in the same slot, got {due_after_skip}")
    skipped = before_20.get_schedule("daily_11_00_ekt")
    if skipped["last_status"] != "skipped" or skipped["last_run_at"]:
        raise AssertionError(f"missed schedule skip must not masquerade as a run attempt, got {skipped}")
    after_20_payload = SheetVitrinaV1AutoRefreshSchedulesBlock(
        runtime_dir=runtime_dir / "tz-after-20",
        now_factory=lambda: datetime(2026, 5, 12, 15, 1, tzinfo=timezone.utc),
    ).build_payload()
    if after_20_payload["next_auto_run_at"] != "2026-05-13T06:00:00Z":
        raise AssertionError(f"after 20:00 EKT next run must be next-day 11:00 EKT, got {after_20_payload}")


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
