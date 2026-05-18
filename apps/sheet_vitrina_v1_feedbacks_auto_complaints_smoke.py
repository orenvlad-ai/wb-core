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
    SheetVitrinaV1FeedbacksAutoComplaintsError,
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
    _assert_schedule_persistence_duplicate_and_run_now_contract()
    _assert_new_future_schedule_is_not_backfilled()
    _assert_run_filters_ai_and_skips_existing_journal()
    _assert_retryable_prior_attempt_is_submitted()
    _assert_confirmed_prior_attempt_is_not_resubmitted()
    _assert_journal_only_unconfirmed_is_explicit_probe_blocker()
    _assert_error_journal_record_is_retryable()
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


def _assert_schedule_persistence_duplicate_and_run_now_contract() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    current_now = [now]
    with TemporaryDirectory(prefix="auto-complaints-run-now-") as tmp:
        runtime_dir = Path(tmp)
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({}),  # type: ignore[arg-type]
            complaints_block=SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime_dir),
            now_factory=lambda: current_now[0],
        )
        saved = block.save_schedules(
            {
                "schedules": [
                    {
                        "id": "schedule_client_1",
                        "enabled": False,
                        "local_time_hhmm": "12:00",
                        "timezone": "Asia/Yekaterinburg",
                        "last_success_at": "2026-05-08T07:59:00Z",
                        "last_status": "completed",
                    }
                ]
            }
        )
        schedule = saved["schedules"][0]
        if schedule["id"] != "schedule_client_1":
            raise AssertionError(f"backend must preserve valid client-generated schedule ids: {schedule}")
        if schedule.get("last_success_at") or schedule.get("last_status"):
            raise AssertionError(f"new schedule must ignore client-owned lifecycle fields: {schedule}")
        reloaded = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({}),  # type: ignore[arg-type]
            complaints_block=SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime_dir),
            now_factory=lambda: current_now[0],
        )
        if reloaded.build_schedules()["schedules"][0]["id"] != "schedule_client_1":
            raise AssertionError("persisted schedule id must survive reload/readback")
        try:
            reloaded.save_schedules(
                {
                    "schedules": [
                        {"id": "duplicate", "enabled": False, "local_time_hhmm": "12:00"},
                        {"id": "duplicate", "enabled": True, "local_time_hhmm": "13:00"},
                    ]
                }
            )
        except ValueError as exc:
            if "unique" not in str(exc):
                raise AssertionError(f"duplicate id validation must be explicit: {exc}") from exc
        else:
            raise AssertionError("duplicate schedule ids must be rejected")
        reloaded._run_and_persist = lambda *args, **kwargs: None  # type: ignore[method-assign]
        run_payload = reloaded.run_now({"schedule_id": "schedule_client_1"})
        run = run_payload["run"]
        if run_payload.get("run_id") != run["run_id"] or run_payload.get("status") != run["status"]:
            raise AssertionError(f"run-now response must expose observable run_id/status: {run_payload}")
        if not run_payload.get("summary") or "submitted_count" not in run_payload["summary"]:
            raise AssertionError(f"run-now response must expose stats summary: {run_payload}")
        if not run_payload.get("schedules") or run_payload["schedules"][0].get("last_run_id") != run["run_id"]:
            raise AssertionError(f"run-now response must refresh schedule summary with last_run_id: {run_payload}")
        if run_payload["schedules"][0].get("last_status") != "queued":
            raise AssertionError(f"new async run must be visible in schedule status immediately: {run_payload}")
        if not run_payload.get("recent_runs") or run_payload["recent_runs"][0].get("run_id") != run["run_id"]:
            raise AssertionError(f"run-now response must include recent runs: {run_payload}")
        if run["schedule_id"] != "schedule_client_1":
            raise AssertionError(f"run-now must use persisted canonical schedule id: {run}")
        if run["window_fetch_from"] != run["window_base_from"]:
            raise AssertionError(f"first run must not apply overlap to fetch window: {run}")
        if run["window_base_from"] != "2026-05-07T08:00:00Z" or run["window_to"] != "2026-05-08T08:00:00Z":
            raise AssertionError(f"first-run window must be the last 24h in schedule timezone: {run}")
        reloaded.store.update_run(
            run["run_id"],
            {
                "session": {"storage_state_path": "/opt/wb-web-bot/storage_state.json", "token": "must-not-leak"},
                "automation_lock": {"owner": "safe", "cookie": "must-not-leak"},
                "status_sync_result": {"status": "ok", "authorization": "must-not-leak"},
            },
        )
        detailed = reloaded.get_run(run["run_id"])["run"]
        serialized_detail = json.dumps(detailed, ensure_ascii=False)
        for forbidden in ("must-not-leak", "token", "cookie", "authorization"):
            if forbidden in serialized_detail:
                raise AssertionError(f"run details must be sanitized, leaked {forbidden}: {detailed}")
        if detailed["session"].get("storage_state_path") != "/opt/wb-web-bot/storage_state.json":
            raise AssertionError(f"safe storage_state path reference may remain visible: {detailed}")
        completed = reloaded.store.update_run(run["run_id"], {"status": "completed", "finished_at": "2026-05-08T08:00:01Z"})
        reloaded.store.update_schedule_after_run("schedule_client_1", completed)
        completed_schedule = reloaded.build_schedules()["schedules"][0]
        if completed_schedule.get("last_success_at") != "2026-05-08T08:00:00Z":
            raise AssertionError(f"successful run must set schedule last_success_at from run window_to: {completed_schedule}")
        recurring = reloaded.save_schedules(
            {
                "schedules": [
                    {
                        **completed_schedule,
                        "last_success_at": "2030-01-01T00:00:00Z",
                        "last_status": "client-stale-status",
                        "overlap_hours": 24,
                    }
                ]
            }
        )["schedules"][0]
        if recurring.get("last_success_at") != "2026-05-08T08:00:00Z" or recurring.get("last_status") != "completed":
            raise AssertionError(f"save must preserve server-owned lifecycle fields, not client stale values: {recurring}")
        current_now[0] = datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc)
        reloaded._run_and_persist = lambda *args, **kwargs: None  # type: ignore[method-assign]
        recurring_run = reloaded.run_now({"schedule_id": recurring["id"]})["run"]
        if recurring_run["window_base_from"] != "2026-05-08T08:00:00Z":
            raise AssertionError(f"recurring base window must start at last_success_at: {recurring_run}")
        if recurring_run["window_fetch_from"] != "2026-05-07T08:00:00Z":
            raise AssertionError(f"recurring fetch window must include 24h overlap: {recurring_run}")
        try:
            reloaded.run_now({"schedule_id": "schedule_missing"})
        except SheetVitrinaV1FeedbacksAutoComplaintsError as exc:
            if exc.http_status != 404 or exc.reason != "schedule_not_found" or exc.status != "schedule_not_found":
                raise AssertionError(f"unknown schedule must be structured schedule_not_found: {exc.__dict__}") from exc
        else:
            raise AssertionError("unknown schedule id must not start a run")


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
    current_now = [datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)]
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
        journal.create_or_update(
            {
                "feedback_id": "existing",
                "complaint_status": "waiting_response",
                "submitted_at": "2026-05-07T07:00:00Z",
                "submit_run_id": "previous-submit",
                "submit_result": "confirmed_success",
            }
        )
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
            now_factory=lambda: current_now[0],
        )
        block.save_schedules(
            {
                "schedules": [
                    {
                        "id": "daily-noon",
                        "enabled": True,
                        "local_time_hhmm": "12:00",
                        "timezone": "Asia/Yekaterinburg",
                        "hard_cap_per_run": 2,
                    }
                ]
            }
        )
        current_now[0] = now
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
        if reasons.get("already_journaled_confirmed") != 1 or reasons.get("skipped_due_to_hard_cap") != 1:
            raise AssertionError(f"existing journal and cap skips must be counted: {reasons}")
        if run["already_confirmed_count"] != 1 or run["skipped_hard_cap_count"] != 1:
            raise AssertionError(f"detailed counters must split already-confirmed and hard cap: {run}")
        if not submitted_payloads or submitted_payloads[0]["feedback_ids"] != ["new-yes", "new-review"]:
            raise AssertionError(f"auto job must submit only safe selected ids through guarded submit: {submitted_payloads}")
        schedules = block.build_schedules()["schedules"]
        if not schedules[0]["last_success_at"]:
            raise AssertionError(f"controlled hard-cap run must advance last_success_at: {schedules[0]}")
        second = block.run_due_schedules_sync()
        if second["status"] != "no_due_schedules":
            raise AssertionError(f"repeated tick must not duplicate completed due run: {second}")


def _assert_retryable_prior_attempt_is_submitted() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    current_now = [datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)]
    with TemporaryDirectory(prefix="auto-complaints-retryable-prior-") as tmp:
        runtime_dir = Path(tmp)
        store = JsonFileFeedbacksAutoComplaintsStore(runtime_dir, now_factory=lambda: current_now[0])
        submitted_payloads: list[dict[str, object]] = []

        def fake_submit(payload: object) -> dict[str, object]:
            data = dict(payload or {})
            submitted_payloads.append(data)
            return {
                "contract_name": "sheet_vitrina_v1_feedbacks_complaints_submit_job",
                "aggregate": {"submitted_count": 1, "skipped_count": 0, "error_count": 0},
                "rows": [{"feedback_id": "retryable", "submitted": True, "submit_clicked": True}],
                "status_sync": {"aggregate": {"statuses_updated": 0}},
            }

        complaints = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime_dir, submit_runner=fake_submit)
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([_row("retryable", "2026-05-08T06:30:00Z", 1)]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({"retryable": "review"}),  # type: ignore[arg-type]
            complaints_block=complaints,
            store=store,
            now_factory=lambda: current_now[0],
        )
        block.save_schedules({"schedules": [{"id": "daily-noon", "enabled": True, "local_time_hhmm": "12:00"}]})
        store.add_run(
            {
                "run_id": "previous_retryable",
                "status": "completed",
                "created_at": "2026-05-07T07:00:00Z",
                "finished_at": "2026-05-07T07:05:00Z",
                "attempts": [
                    {
                        "run_id": "previous_retryable",
                        "feedback_id": "retryable",
                        "rating": 1,
                        "ai_status": "review",
                        "candidate": True,
                        "action": "safety_rejected",
                        "reason": "exact actionable DOM row was not found after target-probe filter/materialization path",
                        "created_at": "2026-05-07T07:05:00Z",
                        "updated_at": "2026-05-07T07:05:00Z",
                        "evidence_refs": [],
                    }
                ],
            }
        )
        current_now[0] = now
        block.run_due_schedules_sync()
        run = block.list_runs()["runs"][0]
        if run["submitted_count"] != 1 or run["skipped_count"] != 0:
            raise AssertionError(f"retryable prior safety rejection must be submitted again: {run}")
        if run["reason_counts"].get("already_attempted_feedback_id"):
            raise AssertionError(f"retryable prior attempt must not become already_attempted skip: {run}")
        if not submitted_payloads or submitted_payloads[0].get("feedback_ids") != ["retryable"]:
            raise AssertionError(f"retryable prior attempt must reach guarded submit: {submitted_payloads}")


def _assert_confirmed_prior_attempt_is_not_resubmitted() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    current_now = [datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)]
    with TemporaryDirectory(prefix="auto-complaints-confirmed-prior-") as tmp:
        runtime_dir = Path(tmp)
        store = JsonFileFeedbacksAutoComplaintsStore(runtime_dir, now_factory=lambda: current_now[0])
        submitted_payloads: list[dict[str, object]] = []
        complaints = SheetVitrinaV1FeedbacksComplaintsBlock(
            runtime_dir=runtime_dir,
            submit_runner=lambda payload: submitted_payloads.append(dict(payload or {})) or {},
        )
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([_row("confirmed", "2026-05-08T06:30:00Z", 1)]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({"confirmed": "review"}),  # type: ignore[arg-type]
            complaints_block=complaints,
            store=store,
            now_factory=lambda: current_now[0],
        )
        block.save_schedules({"schedules": [{"id": "daily-noon", "enabled": True, "local_time_hhmm": "12:00"}]})
        store.add_run(
            {
                "run_id": "previous_confirmed",
                "status": "completed",
                "created_at": "2026-05-07T07:00:00Z",
                "finished_at": "2026-05-07T07:05:00Z",
                "attempts": [
                    {
                        "run_id": "previous_confirmed",
                        "feedback_id": "confirmed",
                        "rating": 1,
                        "ai_status": "review",
                        "candidate": True,
                        "action": "submitted_confirmed",
                        "reason": "submitted_confirmed",
                        "created_at": "2026-05-07T07:05:00Z",
                        "updated_at": "2026-05-07T07:05:00Z",
                        "evidence_refs": [],
                    }
                ],
            }
        )
        current_now[0] = now
        block.run_due_schedules_sync()
        run = block.list_runs()["runs"][0]
        if run["submitted_count"] != 0 or run["skipped_count"] != 1:
            raise AssertionError(f"confirmed prior submit must remain idempotent: {run}")
        if run["reason_counts"].get("already_confirmed_in_prior_run") != 1:
            raise AssertionError(f"confirmed prior submit must be counted as already-confirmed skip: {run}")
        if submitted_payloads:
            raise AssertionError(f"confirmed prior submit must not reach guarded submit again: {submitted_payloads}")


def _assert_journal_only_unconfirmed_is_explicit_probe_blocker() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    current_now = [datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)]
    with TemporaryDirectory(prefix="auto-complaints-journal-only-") as tmp:
        runtime_dir = Path(tmp)
        journal = JsonFileFeedbacksComplaintJournal(runtime_dir)
        journal.create_or_update({"feedback_id": "journal-only", "complaint_status": "waiting_response"})
        submitted_payloads: list[dict[str, object]] = []
        complaints = SheetVitrinaV1FeedbacksComplaintsBlock(
            runtime_dir=runtime_dir,
            journal=journal,
            submit_runner=lambda payload: submitted_payloads.append(dict(payload or {})) or {},
        )
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([_row("journal-only", "2026-05-08T06:30:00Z", 1)]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({"journal-only": "yes"}),  # type: ignore[arg-type]
            complaints_block=complaints,
            now_factory=lambda: current_now[0],
        )
        block.save_schedules({"schedules": [{"id": "daily-noon", "enabled": True, "local_time_hhmm": "12:00"}]})
        current_now[0] = now
        block.run_due_schedules_sync()
        run = block.list_runs()["runs"][0]
        if run["status"] != "journal_unconfirmed_requires_probe":
            raise AssertionError(f"journal-only record must require probe, not silent completed skip: {run}")
        if run["reason_counts"].get("journal_only_unconfirmed") != 1:
            raise AssertionError(f"journal-only reason must be explicit: {run}")
        if run["skipped_existing_unconfirmed_count"] != 1 or submitted_payloads:
            raise AssertionError(f"journal-only record must not be submitted without probe: {run} {submitted_payloads}")


def _assert_error_journal_record_is_retryable() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    current_now = [datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)]
    with TemporaryDirectory(prefix="auto-complaints-error-retry-") as tmp:
        runtime_dir = Path(tmp)
        journal = JsonFileFeedbacksComplaintJournal(runtime_dir)
        journal.create_or_update({"feedback_id": "retry-error", "complaint_status": "error", "last_error": "submit unconfirmed"})
        submitted_payloads: list[dict[str, object]] = []

        def fake_submit(payload: object) -> dict[str, object]:
            data = dict(payload or {})
            submitted_payloads.append(data)
            return {
                "contract_name": "sheet_vitrina_v1_feedbacks_complaints_submit_job",
                "aggregate": {"submitted_count": 1, "skipped_count": 0, "error_count": 0},
                "rows": [{"feedback_id": "retry-error", "submitted": True, "submit_clicked": True}],
                "status_sync": {"aggregate": {"statuses_updated": 0}},
            }

        complaints = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime_dir, journal=journal, submit_runner=fake_submit)
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([_row("retry-error", "2026-05-08T06:30:00Z", 1)]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({"retry-error": "yes"}),  # type: ignore[arg-type]
            complaints_block=complaints,
            now_factory=lambda: current_now[0],
        )
        block.save_schedules({"schedules": [{"id": "daily-noon", "enabled": True, "local_time_hhmm": "12:00"}]})
        current_now[0] = now
        block.run_due_schedules_sync()
        run = block.list_runs()["runs"][0]
        if run["submitted_count"] != 1 or run["skipped_existing_unconfirmed_count"]:
            raise AssertionError(f"error journal record must retry through guarded submit: {run}")
        if not submitted_payloads or submitted_payloads[0].get("retry_errors") is not True:
            raise AssertionError(f"retry submit must carry retry_errors: {submitted_payloads}")


def _assert_noop_advances_last_success() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    current_now = [datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)]
    with TemporaryDirectory(prefix="auto-complaints-noop-") as tmp:
        runtime_dir = Path(tmp)
        complaints = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime_dir)
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({}),  # type: ignore[arg-type]
            complaints_block=complaints,
            now_factory=lambda: current_now[0],
        )
        block.save_schedules({"schedules": [{"id": "daily-noon", "enabled": True, "local_time_hhmm": "12:00"}]})
        current_now[0] = now
        block.run_due_schedules_sync()
        schedule = block.build_schedules()["schedules"][0]
        if schedule["last_status"] != "no_new_feedbacks" or not schedule["last_success_at"]:
            raise AssertionError(f"no-op run must be successful and advance last_success_at: {schedule}")


def _assert_busy_lock_is_controlled() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    current_now = [datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)]
    with TemporaryDirectory(prefix="auto-complaints-busy-") as tmp:
        runtime_dir = Path(tmp)
        complaints = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime_dir)
        block = SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=runtime_dir,
            feedbacks_block=FakeFeedbacksBlock([_row("busy-row", "2026-05-08T06:00:00Z", 1)]),  # type: ignore[arg-type]
            feedbacks_ai_block=FakeAiBlock({"busy-row": "yes"}),  # type: ignore[arg-type]
            complaints_block=complaints,
            now_factory=lambda: current_now[0],
        )
        block.save_schedules(
            {
                "schedules": [
                    {
                        "id": "daily-noon",
                        "enabled": True,
                        "local_time_hhmm": "12:00",
                    }
                ]
            }
        )
        current_now[0] = now
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
