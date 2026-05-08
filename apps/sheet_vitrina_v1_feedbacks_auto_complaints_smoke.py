"""Smoke checks for sheet_vitrina_v1 feedback auto-complaints scheduler."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1_feedbacks_auto_complaints import (  # noqa: E402
    JsonFileFeedbacksAutoComplaintsStore,
    SheetVitrinaV1FeedbacksAutoComplaintsBlock,
)
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (  # noqa: E402
    JsonFileFeedbacksComplaintJournal,
    SheetVitrinaV1FeedbacksComplaintsBlock,
)


class FakeFeedbacksBlock:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, object]] = []

    def build(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return {"contract_name": "sheet_vitrina_v1_feedbacks", "rows": list(self.rows)}


class FakeAiBlock:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses
        self.calls: list[list[str]] = []

    def analyze(self, payload: dict[str, object]) -> dict[str, object]:
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        ids = [str((row or {}).get("feedback_id") or "") for row in rows if isinstance(row, dict)]
        self.calls.append(ids)
        return {
            "contract_name": "sheet_vitrina_v1_feedbacks_ai_analysis",
            "results": [
                {
                    "feedback_id": feedback_id,
                    "complaint_fit": self.statuses.get(feedback_id, "no"),
                    "reason": "test",
                    "category": "other",
                }
                for feedback_id in ids
            ],
        }


def main() -> None:
    _assert_schedule_validation_and_due_window()
    _assert_new_future_schedule_is_not_backfilled()
    _assert_run_filters_ai_and_skips_existing_journal()
    _assert_noop_advances_last_success()
    _assert_busy_lock_is_controlled()
    print("sheet_vitrina_v1_feedbacks_auto_complaints_smoke: OK")


def _assert_schedule_validation_and_due_window() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)  # 13:00 Asia/Yekaterinburg
    with TemporaryDirectory(prefix="auto-complaints-schedule-") as tmp:
        store = JsonFileFeedbacksAutoComplaintsStore(Path(tmp), now_factory=lambda: now)
        state = store.save_schedules(
            [
                {
                    "id": "daily-noon",
                    "enabled": True,
                    "local_time_hhmm": "12:00",
                    "timezone": "Asia/Yekaterinburg",
                }
            ]
        )
        schedule = state["schedules"][0]
        if schedule["local_time_hhmm"] != "12:00" or schedule["timezone_label"] != "Екатеринбург":
            raise AssertionError(f"schedule must normalize time/timezone: {schedule}")
        if not schedule["next_run_at"]:
            raise AssertionError(f"schedule must expose next_run_at: {schedule}")
        resaved = store.save_schedules(
            [
                {
                    **schedule,
                    "local_time_hhmm": "14:30",
                    "next_run_at": "2030-01-01T00:00:00Z",
                }
            ]
        )
        if resaved["schedules"][0]["next_run_at"] != "2026-05-08T09:30:00Z":
            raise AssertionError(f"next_run_at must be recomputed after schedule edits: {resaved['schedules'][0]}")
        try:
            store.save_schedules([{"id": "bad", "local_time_hhmm": "25:00"}])
        except ValueError:
            pass
        else:
            raise AssertionError("invalid HH:mm must be rejected")


def _assert_new_future_schedule_is_not_backfilled() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)  # 13:00 Asia/Yekaterinburg
    with TemporaryDirectory(prefix="auto-complaints-future-") as tmp:
        runtime_dir = Path(tmp)
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([_row("future-row", "2026-05-08T06:00:00Z", 1)]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({"future-row": "yes"}),  # type: ignore[arg-type]
            complaints_block=SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime_dir),
            now_factory=lambda: now,
        )
        block.save_schedules(
            {
                "schedules": [
                    {
                        "id": "future-today",
                        "enabled": True,
                        "local_time_hhmm": "14:30",
                        "timezone": "Asia/Yekaterinburg",
                    }
                ]
            }
        )
        tick = block.run_due_schedules_sync()
        if tick["status"] != "no_due_schedules":
            raise AssertionError(f"new future schedule must not backfill yesterday's due time: {tick}")


def _assert_run_filters_ai_and_skips_existing_journal() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    rows = [
        _row("new-yes", "2026-05-08T06:00:00Z", 1),
        _row("new-review", "2026-05-08T06:01:00Z", 2),
        _row("new-no", "2026-05-08T06:02:00Z", 1),
        _row("existing", "2026-05-08T06:03:00Z", 1),
        _row("cap-skip", "2026-05-08T06:04:00Z", 1),
    ]
    with TemporaryDirectory(prefix="auto-complaints-run-") as tmp:
        runtime_dir = Path(tmp)
        journal = JsonFileFeedbacksComplaintJournal(runtime_dir)
        journal.create_or_update({"feedback_id": "existing", "complaint_status": "waiting_response"})
        submitted_payloads: list[dict[str, object]] = []

        def fake_submit(payload: object) -> dict[str, object]:
            data = dict(payload or {})
            submitted_payloads.append(data)
            return {
                "contract_name": "sheet_vitrina_v1_feedbacks_complaints_submit_job",
                "aggregate": {"submitted_count": 2, "skipped_count": 0, "error_count": 0},
                "rows": [
                    {"feedback_id": "new-yes", "submitted": True, "submit_clicked": True},
                    {"feedback_id": "new-review", "submitted": True, "submit_clicked": True},
                ],
                "status_sync": {"aggregate": {"statuses_updated": 0}},
            }

        complaints = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime_dir, journal=journal, submit_runner=fake_submit)
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock(rows),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({"new-yes": "yes", "new-review": "review", "new-no": "no", "existing": "yes", "cap-skip": "yes"}),  # type: ignore[arg-type]
            complaints_block=complaints,
            now_factory=lambda: now,
        )
        block.save_schedules(
            {
                "schedules": [
                    {
                        "id": "daily-noon",
                        "enabled": True,
                        "local_time_hhmm": "12:00",
                        "timezone": "Asia/Yekaterinburg",
                        "enabled_since_at": "2026-05-08T06:00:00Z",
                        "hard_cap_per_run": 2,
                    }
                ]
            }
        )
        tick = block.run_due_schedules_sync()
        if tick["status"] != "started":
            raise AssertionError(f"due tick must start one run: {tick}")
        runs = block.list_runs()["runs"]
        run = runs[0]
        if run["status"] != "hard_cap_reached":
            raise AssertionError(f"cap overflow must be explicit controlled status: {run}")
        if run["loaded_feedbacks_count"] != 5 or run["low_rating_feedbacks_count"] != 5 or run["ai_candidates_count"] != 4:
            raise AssertionError(f"run counters mismatch: {run}")
        if run["submitted_count"] != 2:
            raise AssertionError(f"fake submit must count submitted rows: {run}")
        reasons = run["reason_counts"]
        if reasons.get("existing_journal_feedback_id") != 1 or reasons.get("hard_cap_reached") != 1:
            raise AssertionError(f"existing journal and cap skips must be counted: {reasons}")
        if not submitted_payloads or submitted_payloads[0]["feedback_ids"] != ["new-yes", "new-review"]:
            raise AssertionError(f"auto job must submit only safe selected ids through guarded submit: {submitted_payloads}")
        schedules = block.build_schedules()["schedules"]
        if not schedules[0]["last_success_at"]:
            raise AssertionError(f"controlled hard-cap run must advance last_success_at: {schedules[0]}")
        second = block.run_due_schedules_sync()
        if second["status"] != "no_due_schedules":
            raise AssertionError(f"repeated tick must not duplicate completed due run: {second}")


def _assert_noop_advances_last_success() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    with TemporaryDirectory(prefix="auto-complaints-noop-") as tmp:
        runtime_dir = Path(tmp)
        complaints = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime_dir)
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({}),  # type: ignore[arg-type]
            complaints_block=complaints,
            now_factory=lambda: now,
        )
        block.save_schedules({"schedules": [{"id": "daily-noon", "enabled": True, "local_time_hhmm": "12:00"}]})
        schedule = block.build_schedules()["schedules"][0]
        schedule["enabled_since_at"] = "2026-05-08T06:00:00Z"
        block.save_schedules({"schedules": [schedule]})
        block.run_due_schedules_sync()
        schedule = block.build_schedules()["schedules"][0]
        if schedule["last_status"] != "no_new_feedbacks" or not schedule["last_success_at"]:
            raise AssertionError(f"no-op run must be successful and advance last_success_at: {schedule}")


def _assert_busy_lock_is_controlled() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    with TemporaryDirectory(prefix="auto-complaints-busy-") as tmp:
        runtime_dir = Path(tmp)
        complaints = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime_dir)
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([_row("busy-row", "2026-05-08T06:00:00Z", 1)]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({"busy-row": "yes"}),  # type: ignore[arg-type]
            complaints_block=complaints,
            now_factory=lambda: now,
        )
        block.save_schedules(
            {
                "schedules": [
                    {
                        "id": "daily-noon",
                        "enabled": True,
                        "local_time_hhmm": "12:00",
                        "enabled_since_at": "2026-05-08T06:00:00Z",
                    }
                ]
            }
        )
        (runtime_dir / "seller_portal_automation.lock.json").write_text(
            json.dumps(
                {
                    "contract_name": "seller_portal_automation_lock",
                    "contract_version": "v1",
                    "owner": "smoke",
                    "purpose": "busy",
                    "run_id": "busy-lock",
                    "started_at": "2026-05-08T07:59:00Z",
                    "heartbeat_at": "2026-05-08T07:59:00Z",
                    "pid": 1,
                    "host": socket.gethostname(),
                    "command": "smoke",
                    "expected_max_seconds": 3600,
                    "lock_id": "busy-lock-id",
                }
            ),
            encoding="utf-8",
        )
        block.run_due_schedules_sync()
        run = block.list_runs()["runs"][0]
        if run["status"] != "seller_portal_automation_busy" or run["submitted_count"] != 0:
            raise AssertionError(f"busy lock must stop before submit and expose status: {run}")
        schedule = block.build_schedules()["schedules"][0]
        if schedule["last_success_at"]:
            raise AssertionError(f"busy blocker must not advance last_success_at: {schedule}")


def _row(feedback_id: str, created_at: str, rating: int) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "created_at": created_at,
        "created_date": created_at[:10],
        "product_valuation": rating,
        "text": "test feedback",
        "review_tags": [],
    }


if __name__ == "__main__":
    main()
