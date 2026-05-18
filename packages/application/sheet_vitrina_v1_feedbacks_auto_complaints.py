"""Runtime schedules for automatic guarded feedback complaint runs."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apps.seller_portal_automation_guard import (
    SellerPortalAutomationBusy,
    busy_response_payload,
    seller_portal_automation_lock,
    seller_portal_storage_state_path,
    validate_storage_state_path_for_runtime,
)
from packages.application.sheet_vitrina_v1_feedbacks import SheetVitrinaV1FeedbacksBlock
from packages.application.sheet_vitrina_v1_feedbacks_ai import MAX_ROWS_PER_RUN, SheetVitrinaV1FeedbacksAiBlock
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (
    SUBMIT_JOB_MAX_SELECTED_IDS,
    SUBMIT_JOB_MAX_SUBMIT_HARD_CAP,
    SheetVitrinaV1FeedbacksComplaintsBlock,
)
from packages.business_time import CANONICAL_BUSINESS_TIMEZONE_NAME


CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_auto_complaints"
CONTRACT_VERSION = "v1"
SCHEDULES_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_auto_complaints_schedules"
RUNS_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_auto_complaints_runs"
RUN_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_auto_complaints_run"
TICK_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_auto_complaints_tick"
DEFAULT_STATE_FILENAME = "sheet_vitrina_v1_feedbacks_auto_complaints.json"
DEFAULT_REPORT_DIRNAME = "feedbacks_auto_complaints"
DEFAULT_TIMEZONE = CANONICAL_BUSINESS_TIMEZONE_NAME
DEFAULT_LOCAL_TIME = "12:00"
DEFAULT_FIRST_LOOKBACK_HOURS = 24
DEFAULT_OVERLAP_HOURS = 24
DEFAULT_HARD_CAP_PER_RUN = min(SUBMIT_JOB_MAX_SUBMIT_HARD_CAP, 10)
ACTIVE_RUN_STATUSES = {"queued", "running"}
SUCCESS_RUN_STATUSES = {"completed", "no_new_feedbacks", "no_low_rating_feedbacks", "no_ai_candidates", "hard_cap_reached"}
NON_RETRYABLE_ATTEMPT_ACTIONS = {"submitted_confirmed"}
RETRYABLE_ATTEMPT_ACTIONS = {"submit_unconfirmed", "safety_rejected", "error"}
AI_CANDIDATE_VALUES = {"yes", "да", "review", "проверить"}
CONFIRMED_JOURNAL_STATUSES = {"waiting_response", "satisfied", "rejected"}
FINAL_JOURNAL_STATUSES = {"satisfied", "rejected"}
UNCONFIRMED_JOURNAL_STATUS = "journal_unconfirmed_requires_probe"
SCHEDULE_RUNTIME_FIELDS = {
    "created_at",
    "enabled_since_at",
    "last_run_at",
    "last_success_at",
    "last_due_at",
    "last_status",
    "last_run_id",
    "last_stats",
}


class SheetVitrinaV1FeedbacksAutoComplaintsError(RuntimeError):
    def __init__(self, message: str, *, http_status: int = 500, reason: str = "", status: str = "") -> None:
        self.http_status = http_status
        self.reason = reason
        self.status = status or reason
        super().__init__(message)


class JsonFileFeedbacksAutoComplaintsStore:
    def __init__(self, runtime_dir: Path, *, now_factory: Callable[[], datetime] | None = None) -> None:
        self.runtime_dir = runtime_dir
        self.path = runtime_dir / DEFAULT_STATE_FILENAME
        self.report_root = runtime_dir / DEFAULT_REPORT_DIRNAME
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._mark_interrupted_runs()

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def save_schedules(self, schedules: list[Mapping[str, Any]]) -> dict[str, Any]:
        now = _iso_now(self.now_factory)
        with self._lock:
            payload = self._read_unlocked()
            existing_by_id = {
                str(item.get("id") or ""): item
                for item in payload.get("schedules", [])
                if isinstance(item, Mapping) and str(item.get("id") or "")
            }
            normalized = [
                _normalize_schedule(
                    _merge_schedule_for_save(item, existing_by_id.get(str(item.get("id") or "").strip()), now=now),
                    now=now,
                    now_factory=self.now_factory,
                )
                for item in schedules
            ]
            ids = [str(item["id"]) for item in normalized]
            if len(ids) != len(set(ids)):
                raise ValueError("schedule ids must be unique")
            payload["schedules"] = normalized
            self._write_unlocked(payload)
            return self._read_unlocked()

    def add_run(self, run: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            payload = self._read_unlocked()
            normalized = _normalize_run(run)
            payload.setdefault("runs", []).append(normalized)
            self._write_unlocked(payload)
            return normalized

    def update_run(self, run_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        with self._lock:
            payload = self._read_unlocked()
            runs = payload.setdefault("runs", [])
            for index, run in enumerate(runs):
                if str(run.get("run_id") or "") != normalized_run_id:
                    continue
                patch_payload = dict(patch)
                if "events" in patch_payload:
                    existing_events = run.get("events") if isinstance(run.get("events"), list) else []
                    patch_events = patch_payload.get("events") if isinstance(patch_payload.get("events"), list) else []
                    patch_payload["events"] = [*existing_events, *patch_events]
                merged = _normalize_run({**dict(run), **patch_payload})
                runs[index] = merged
                self._write_unlocked(payload)
                self._write_run_report(merged)
                return merged
        raise SheetVitrinaV1FeedbacksAutoComplaintsError(f"auto complaint run not found: {normalized_run_id}", http_status=404)

    def update_schedule_after_run(self, schedule_id: str, run: Mapping[str, Any]) -> None:
        normalized_schedule_id = str(schedule_id or "").strip()
        if not normalized_schedule_id:
            return
        with self._lock:
            payload = self._read_unlocked()
            schedules = payload.setdefault("schedules", [])
            for index, schedule in enumerate(schedules):
                if str(schedule.get("id") or "") != normalized_schedule_id:
                    continue
                status = str(run.get("status") or "")
                next_run_at = _next_run_at(schedule, self.now_factory())
                patch = {
                    "last_run_at": _safe_text(run.get("finished_at") or run.get("started_at") or run.get("created_at"), 80),
                    "last_due_at": _safe_text(run.get("due_at"), 80),
                    "next_run_at": next_run_at,
                    "last_status": status,
                    "last_run_id": _safe_text(run.get("run_id"), 160),
                    "last_stats": _run_stats(run),
                }
                if status in SUCCESS_RUN_STATUSES:
                    patch["last_success_at"] = _safe_text(run.get("window_to"), 80)
                schedules[index] = _normalize_schedule({**dict(schedule), **patch}, now=_iso_now(self.now_factory), now_factory=self.now_factory)
                self._write_unlocked(payload)
                return

    def get_run(self, run_id: str) -> dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id query parameter is required")
        payload = self.read()
        for run in payload.get("runs", []):
            if isinstance(run, Mapping) and str(run.get("run_id") or "") == normalized_run_id:
                return _normalize_run(run)
        raise SheetVitrinaV1FeedbacksAutoComplaintsError(f"auto complaint run not found: {normalized_run_id}", http_status=404)

    def active_run(self) -> dict[str, Any] | None:
        payload = self.read()
        active = [
            _normalize_run(run)
            for run in payload.get("runs", [])
            if isinstance(run, Mapping) and str(run.get("status") or "") in ACTIVE_RUN_STATUSES
        ]
        if not active:
            return None
        return max(active, key=lambda item: (str(item.get("started_at") or ""), str(item.get("created_at") or "")))

    def non_retryable_attempt_feedback_ids(self) -> set[str]:
        return self._attempt_feedback_ids(NON_RETRYABLE_ATTEMPT_ACTIONS)

    def retryable_attempt_feedback_ids(self) -> set[str]:
        return self._attempt_feedback_ids(RETRYABLE_ATTEMPT_ACTIONS)

    def _attempt_feedback_ids(self, actions: set[str]) -> set[str]:
        payload = self.read()
        ids: set[str] = set()
        for run in payload.get("runs", []):
            if not isinstance(run, Mapping):
                continue
            attempts = run.get("attempts") if isinstance(run.get("attempts"), list) else []
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    continue
                action = str(attempt.get("action") or "")
                feedback_id = str(attempt.get("feedback_id") or "").strip()
                if feedback_id and action in actions:
                    ids.add(feedback_id)
        return ids

    def _mark_interrupted_runs(self) -> None:
        with self._lock:
            payload = self._read_unlocked()
            changed = False
            now = _iso_now(self.now_factory)
            for index, run in enumerate(payload.get("runs", [])):
                if not isinstance(run, Mapping) or str(run.get("status") or "") not in ACTIVE_RUN_STATUSES:
                    continue
                payload["runs"][index] = _normalize_run(
                    {
                        **dict(run),
                        "status": "error",
                        "blocker_reason": "runtime service restarted before auto complaints run finished",
                        "finished_at": now,
                    }
                )
                changed = True
            if changed:
                self._write_unlocked(payload)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "schedules": [],
                "runs": [],
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SheetVitrinaV1FeedbacksAutoComplaintsError("auto complaints state is not readable") from exc
        if not isinstance(payload, dict):
            raise SheetVitrinaV1FeedbacksAutoComplaintsError("auto complaints state has invalid shape")
        now = _iso_now(self.now_factory)
        schedules = payload.get("schedules") if isinstance(payload.get("schedules"), list) else []
        runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "updated_at": _safe_text(payload.get("updated_at"), 80),
            "schedules": [_normalize_schedule(item, now=now, now_factory=self.now_factory) for item in schedules if isinstance(item, Mapping)],
            "runs": [_normalize_run(item) for item in runs if isinstance(item, Mapping)][-200:],
        }

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = _iso_now(self.now_factory)
        schedules = [
            _normalize_schedule(item, now=now, now_factory=self.now_factory)
            for item in payload.get("schedules", [])
            if isinstance(item, Mapping)
        ]
        runs = [_normalize_run(item) for item in payload.get("runs", []) if isinstance(item, Mapping)][-200:]
        normalized = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "updated_at": now,
            "schedules": schedules,
            "runs": runs,
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.path)

    def _write_run_report(self, run: Mapping[str, Any]) -> None:
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            return
        run_dir = self.report_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        json_path = run_dir / "sheet_vitrina_v1_feedbacks_auto_complaints_run.json"
        md_path = run_dir / "sheet_vitrina_v1_feedbacks_auto_complaints_run.md"
        json_path.write_text(json.dumps(_normalize_run(run), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(_render_run_markdown(run), encoding="utf-8")


class SheetVitrinaV1FeedbacksAutoComplaintsBlock:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        feedbacks_block: SheetVitrinaV1FeedbacksBlock,
        feedbacks_ai_block: SheetVitrinaV1FeedbacksAiBlock,
        complaints_block: SheetVitrinaV1FeedbacksComplaintsBlock,
        store: JsonFileFeedbacksAutoComplaintsStore | None = None,
        now_factory: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.feedbacks_block = feedbacks_block
        self.feedbacks_ai_block = feedbacks_ai_block
        self.complaints_block = complaints_block
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.store = store or JsonFileFeedbacksAutoComplaintsStore(runtime_dir, now_factory=self.now_factory)
        self.sleep_fn = sleep_fn or (lambda seconds: None)

    def build_schedules(self) -> dict[str, Any]:
        payload = self.store.read()
        now = self.now_factory()
        journal_by_id = self._journal_by_feedback_id()
        schedules = [
            _public_schedule({**schedule, "next_run_at": schedule.get("next_run_at") or _next_run_at(schedule, now)})
            for schedule in payload.get("schedules", [])
        ]
        return {
            "contract_name": SCHEDULES_CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "meta": {
                "storage_path": str(self.store.path),
                "timezone_default": DEFAULT_TIMEZONE,
                "timezone_label_default": "Екатеринбург",
                "hard_cap_default": DEFAULT_HARD_CAP_PER_RUN,
                "generated_at": _iso_now(self.now_factory),
            },
            "schedules": schedules,
            "recent_runs": [
                _public_run(_reconcile_run_with_journal(run, journal_by_id))
                for run in reversed(payload.get("runs", [])[-10:])
                if isinstance(run, Mapping)
            ],
        }

    def save_schedules(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        schedules = payload.get("schedules")
        if not isinstance(schedules, list):
            raise ValueError("schedules must be a JSON array")
        self.store.save_schedules([item for item in schedules if isinstance(item, Mapping)])
        return self.build_schedules()

    def list_runs(self) -> dict[str, Any]:
        payload = self.store.read()
        journal_by_id = self._journal_by_feedback_id()
        return {
            "contract_name": RUNS_CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "runs": [
                _public_run(_reconcile_run_with_journal(run, journal_by_id))
                for run in reversed(payload.get("runs", [])[-50:])
                if isinstance(run, Mapping)
            ],
        }

    def get_run(self, run_id: str) -> dict[str, Any]:
        journal_by_id = self._journal_by_feedback_id()
        return {
            "contract_name": RUN_CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "run": _public_run(_reconcile_run_with_journal(self.store.get_run(run_id), journal_by_id), details=True),
        }

    def run_now(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        schedule_id = str(payload.get("schedule_id") or "").strip()
        schedule = self._schedule_by_id(schedule_id) if schedule_id else _normalize_schedule(
            {
                "id": "manual",
                "enabled": False,
                "local_time_hhmm": DEFAULT_LOCAL_TIME,
                "timezone": DEFAULT_TIMEZONE,
                "hard_cap_per_run": payload.get("hard_cap_per_run") or DEFAULT_HARD_CAP_PER_RUN,
            },
            now=_iso_now(self.now_factory),
            now_factory=self.now_factory,
        )
        max_submit_override = payload.get("max_submit")
        if max_submit_override is not None:
            schedule = _normalize_schedule({**schedule, "hard_cap_per_run": max_submit_override}, now=_iso_now(self.now_factory), now_factory=self.now_factory)
        run = self._start_run(schedule, trigger_source="manual", due_at=self.now_factory(), async_run=True)
        try:
            current_run = self.store.get_run(str(run.get("run_id") or ""))
        except Exception:
            current_run = run
        schedules_payload = self.build_schedules()
        public_run = _public_run(_reconcile_run_with_journal(current_run, self._journal_by_feedback_id()))
        return {
            "contract_name": RUN_CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "run_id": public_run.get("run_id") or "",
            "status": public_run.get("status") or "",
            "reason": public_run.get("blocker_reason") or "",
            "blocker_reason": public_run.get("blocker_reason") or "",
            "summary": _run_stats(public_run),
            "run": public_run,
            "schedules": schedules_payload.get("schedules", []),
            "recent_runs": schedules_payload.get("recent_runs", []),
        }

    def tick(self, payload: Mapping[str, Any] | None = None, *, async_run: bool = True) -> dict[str, Any]:
        del payload
        now = self.now_factory()
        active = self.store.active_run()
        if active is not None:
            return {
                "contract_name": TICK_CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "status": "already_running",
                "started_runs": [],
                "active_run": _public_run(_reconcile_run_with_journal(active, self._journal_by_feedback_id())),
            }
        due = self._due_schedules(now)
        if not due:
            return {
                "contract_name": TICK_CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "status": "no_due_schedules",
                "started_runs": [],
                "checked_at": _iso_datetime(now),
            }
        started: list[dict[str, Any]] = []
        schedule, due_at = due[0]
        started_run = self._start_run(schedule, trigger_source="scheduled", due_at=due_at, async_run=async_run)
        started.append(_public_run(_reconcile_run_with_journal(started_run, self._journal_by_feedback_id())))
        return {
            "contract_name": TICK_CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "started",
            "started_runs": started,
            "due_count": len(due),
        }

    def run_due_schedules_sync(self) -> dict[str, Any]:
        return self.tick({}, async_run=False)

    def _start_run(
        self,
        schedule: Mapping[str, Any],
        *,
        trigger_source: str,
        due_at: datetime,
        async_run: bool,
    ) -> dict[str, Any]:
        active = self.store.active_run()
        if active is not None:
            return active
        now = _iso_now(self.now_factory)
        run_id = _new_run_id(self.now_factory)
        window = _compute_window(schedule, due_at)
        run = self.store.add_run(
            {
                "run_id": run_id,
                "schedule_id": "" if trigger_source == "manual" and schedule.get("id") == "manual" else schedule.get("id"),
                "trigger_source": trigger_source,
                "status": "queued",
                "created_at": now,
                "started_at": "",
                "finished_at": "",
                "due_at": _iso_datetime(due_at),
                "timezone": schedule.get("timezone") or DEFAULT_TIMEZONE,
                **window,
                "hard_cap_per_run": schedule.get("hard_cap_per_run") or DEFAULT_HARD_CAP_PER_RUN,
                "reason_counts": {},
                "attempts": [],
                "events": [_event("run_queued", "Auto complaints run queued", status="queued", at=now)],
            }
        )
        self.store.update_schedule_after_run(str(schedule.get("id") or ""), run)
        if async_run:
            thread = threading.Thread(
                target=self._run_and_persist,
                args=(run_id, schedule),
                daemon=True,
                name=f"feedbacks-auto-complaints-{run_id}",
            )
            thread.start()
        else:
            self._run_and_persist(run_id, schedule)
        return run

    def _run_and_persist(self, run_id: str, schedule: Mapping[str, Any]) -> None:
        try:
            run = self._run(run_id, schedule)
        except Exception as exc:  # pragma: no cover - bounded fallback
            run = self.store.update_run(
                run_id,
                {
                    "status": "error",
                    "blocker_reason": _safe_text(str(exc), 1000),
                    "sanitized_error_message": _safe_text(str(exc), 1000),
                    "finished_at": _iso_now(self.now_factory),
                    "events": [_event("run_error", str(exc), status="error")],
                },
            )
        self.store.update_schedule_after_run(str(schedule.get("id") or ""), run)

    def _run(self, run_id: str, schedule: Mapping[str, Any]) -> dict[str, Any]:
        started_at = _iso_now(self.now_factory)
        run = self.store.update_run(run_id, {"status": "running", "started_at": started_at, "events": [_event("run_started", "Auto complaints run started", status="running", at=started_at)]})
        self.store.update_schedule_after_run(str(schedule.get("id") or ""), run)
        hard_cap = max(1, min(DEFAULT_HARD_CAP_PER_RUN, _safe_int(schedule.get("hard_cap_per_run")) or DEFAULT_HARD_CAP_PER_RUN))
        try:
            storage_state_path = seller_portal_storage_state_path()
            validate_storage_state_path_for_runtime(storage_state_path, self.runtime_dir)
            with seller_portal_automation_lock(
                runtime_dir=self.runtime_dir,
                owner=CONTRACT_NAME,
                purpose="auto_complaints",
                run_id=run_id,
                expected_max_seconds=max(600, hard_cap * 180 + 900),
            ) as lock:
                run = self.store.update_run(run_id, {"automation_lock": lock.public_payload()})
                return self._run_locked(run_id, schedule, run, hard_cap=hard_cap, storage_state_path=str(storage_state_path))
        except SellerPortalAutomationBusy as exc:
            return self.store.update_run(
                run_id,
                _finish_patch(
                    "seller_portal_automation_busy",
                    now_factory=self.now_factory,
                    blocker_reason="seller_portal_automation_busy",
                    reason_counts={"seller_portal_automation_busy": 1},
                    automation_lock=busy_response_payload(exc.lock_payload).get("lock") or {},
                    events=[_event("seller_portal_automation_busy", "Seller Portal automation already running", status="error")],
                ),
            )
        except Exception as exc:
            code = "seller_portal_session_invalid" if "storage_state" in str(exc) or "Seller Portal" in str(exc) else "error"
            return self.store.update_run(
                run_id,
                _finish_patch(
                    code,
                    now_factory=self.now_factory,
                    blocker_reason=code,
                    sanitized_error_message=_safe_text(str(exc), 1000),
                    reason_counts={code: 1},
                    events=[_event(code, str(exc), status="error")],
                ),
            )

    def _run_locked(
        self,
        run_id: str,
        schedule: Mapping[str, Any],
        run: Mapping[str, Any],
        *,
        hard_cap: int,
        storage_state_path: str,
    ) -> dict[str, Any]:
        window_fetch_from = _parse_iso_required(str(run.get("window_fetch_from") or ""))
        window_to = _parse_iso_required(str(run.get("window_to") or ""))
        date_from = window_fetch_from.astimezone(ZoneInfo(str(run.get("timezone") or DEFAULT_TIMEZONE))).date().isoformat()
        date_to = window_to.astimezone(ZoneInfo(str(run.get("timezone") or DEFAULT_TIMEZONE))).date().isoformat()
        try:
            feedbacks_payload = self.feedbacks_block.build(
                date_from=date_from,
                date_to=date_to,
                stars=[1, 2],
                is_answered="all",
            )
        except Exception as exc:
            return self.store.update_run(
                run_id,
                _finish_patch(
                    "window_fetch_failed",
                    now_factory=self.now_factory,
                    blocker_reason="window_fetch_failed",
                    sanitized_error_message=_safe_text(str(exc), 1000),
                    reason_counts={"window_fetch_failed": 1},
                    events=[_event("window_fetch_failed", str(exc), status="error")],
                ),
            )
        loaded_rows = feedbacks_payload.get("rows") if isinstance(feedbacks_payload.get("rows"), list) else []
        rows = [row for row in loaded_rows if isinstance(row, Mapping) and _row_in_window(row, window_fetch_from, window_to)]
        low_rating_rows = [row for row in rows if _safe_int(row.get("product_valuation")) in {1, 2}]
        if not rows:
            return self.store.update_run(
                run_id,
                _finish_patch(
                    "no_new_feedbacks",
                    now_factory=self.now_factory,
                    loaded_feedbacks_count=0,
                    low_rating_feedbacks_count=0,
                    events=[_event("no_new_feedbacks", "No feedbacks in computed window", status="success")],
                ),
            )
        if not low_rating_rows:
            return self.store.update_run(
                run_id,
                _finish_patch(
                    "no_low_rating_feedbacks",
                    now_factory=self.now_factory,
                    loaded_feedbacks_count=len(rows),
                    low_rating_feedbacks_count=0,
                    events=[_event("no_low_rating_feedbacks", "No 1-2 star feedbacks in computed window", status="success")],
                ),
            )
        try:
            ai_results = self._analyze_rows(low_rating_rows)
        except Exception as exc:
            return self.store.update_run(
                run_id,
                _finish_patch(
                    "ai_parser_failed",
                    now_factory=self.now_factory,
                    loaded_feedbacks_count=len(rows),
                    low_rating_feedbacks_count=len(low_rating_rows),
                    blocker_reason="ai_parser_failed",
                    sanitized_error_message=_safe_text(str(exc), 1000),
                    reason_counts={"ai_parser_failed": 1},
                    events=[_event("ai_parser_failed", str(exc), status="error")],
                ),
            )
        ai_by_id = {str(item.get("feedback_id") or ""): item for item in ai_results}
        candidate_ids = [
            str(row.get("feedback_id") or "")
            for row in low_rating_rows
            if _is_ai_candidate(ai_by_id.get(str(row.get("feedback_id") or "")) or {})
        ]
        reason_counts: Counter[str] = Counter()
        attempts: list[dict[str, Any]] = []
        journal_by_id = {
            str(record.get("feedback_id") or "").strip(): record
            for record in self.complaints_block.journal.list_records()
            if isinstance(record, Mapping) and str(record.get("feedback_id") or "").strip()
        }
        prior_non_retryable_ids = self.store.non_retryable_attempt_feedback_ids()
        prior_retryable_ids = self.store.retryable_attempt_feedback_ids()
        diagnostic_events: list[dict[str, Any]] = []
        selected_ids: list[str] = []
        retry_errors_for_selected = False
        unconfirmed_existing_count = 0
        for feedback_id in candidate_ids:
            if not feedback_id:
                continue
            journal_record = journal_by_id.get(feedback_id)
            if journal_record is not None:
                existing = _classify_journal_record(journal_record)
                if existing["terminal_confirmed"]:
                    attempts.append(
                        _attempt(
                            run_id,
                            feedback_id,
                            action=str(existing["action"]),
                            reason=str(existing["reason"]),
                            ai=ai_by_id.get(feedback_id),
                            complaint_journal_ref=str(existing["complaint_journal_ref"]),
                            evidence_refs=list(existing["evidence_refs"]),
                        )
                    )
                    diagnostic_events.append(
                        _event(
                            "candidate_already_confirmed",
                            f"Already confirmed feedback_id={feedback_id} reason={existing['reason']}",
                            status="skipped",
                        )
                    )
                    continue
                if existing["retryable"]:
                    retry_errors_for_selected = True
                    diagnostic_events.append(
                        _event(
                            "candidate_retry",
                            f"Retrying feedback_id={feedback_id} after non-terminal journal status={existing['journal_status']}",
                            status="running",
                        )
                    )
                else:
                    unconfirmed_existing_count += 1
                    attempts.append(
                        _attempt(
                            run_id,
                            feedback_id,
                            action=str(existing["action"]),
                            reason=str(existing["reason"]),
                            ai=ai_by_id.get(feedback_id),
                            complaint_journal_ref=str(existing["complaint_journal_ref"]),
                            evidence_refs=list(existing["evidence_refs"]),
                        )
                    )
                    diagnostic_events.append(
                        _event(
                            "candidate_requires_confirmation_probe",
                            f"Skipped feedback_id={feedback_id} reason={existing['reason']}",
                            status="error",
                        )
                    )
                    continue
            if feedback_id in prior_non_retryable_ids:
                attempts.append(_attempt(run_id, feedback_id, action="already_confirmed_in_prior_run", reason="already_confirmed_in_prior_run", ai=ai_by_id.get(feedback_id)))
                diagnostic_events.append(_event("candidate_already_confirmed", f"Skipped feedback_id={feedback_id} reason=already_confirmed_in_prior_run", status="skipped"))
                continue
            if feedback_id in prior_retryable_ids:
                diagnostic_events.append(_event("candidate_retry", f"Retrying feedback_id={feedback_id} after retryable prior attempt", status="running"))
            if len(selected_ids) >= hard_cap or len(selected_ids) >= SUBMIT_JOB_MAX_SELECTED_IDS:
                attempts.append(_attempt(run_id, feedback_id, action="skipped_due_to_hard_cap", reason="skipped_due_to_hard_cap", ai=ai_by_id.get(feedback_id)))
                diagnostic_events.append(_event("candidate_skipped", f"Skipped feedback_id={feedback_id} reason=skipped_due_to_hard_cap", status="skipped"))
                continue
            selected_ids.append(feedback_id)
            diagnostic_events.append(_event("candidate_selected", f"Selected feedback_id={feedback_id} for guarded submit", status="running"))
        if not candidate_ids:
            return self.store.update_run(
                run_id,
                _finish_patch(
                    "no_ai_candidates",
                    now_factory=self.now_factory,
                    loaded_feedbacks_count=len(rows),
                    low_rating_feedbacks_count=len(low_rating_rows),
                    ai_candidates_count=0,
                    attempts=attempts,
                    reason_counts=dict(reason_counts),
                    events=[_event("no_ai_candidates", "AI returned no yes/review candidates", status="success")],
                    session={"storage_state_path": storage_state_path, "route_specific_checks": "deferred_to_guarded_submit_if_candidates"},
                ),
            )
        pre_submit_attempt_count = len(attempts)
        submit_report: dict[str, Any] = {}
        if selected_ids:
            submit_report = self.complaints_block.run_submit_selected_inline(
                {
                    "run_id": f"{run_id}_submit",
                    "feedback_ids": selected_ids,
                    "date_from": date_from,
                    "date_to": date_to,
                    "stars": [1, 2],
                    "is_answered": "all",
                    "max_api_rows": max(100, len(low_rating_rows)),
                    "max_submit": min(hard_cap, len(selected_ids)),
                    "retry_errors": retry_errors_for_selected,
                    "requested_by": "auto_complaints",
                }
            )
            attempts.extend(_attempts_from_submit_report(run_id, submit_report, ai_by_id=ai_by_id))
        submitted_count = _safe_int(((submit_report.get("aggregate") or {}) if isinstance(submit_report.get("aggregate"), Mapping) else {}).get("submitted_count"))
        pre_submit_attempts = attempts[:pre_submit_attempt_count]
        skipped_count = _safe_int(((submit_report.get("aggregate") or {}) if isinstance(submit_report.get("aggregate"), Mapping) else {}).get("skipped_count")) + sum(1 for item in pre_submit_attempts if str(item.get("action") or "").startswith(("already_", "skipped_", "journal_")))
        error_count = _safe_int(((submit_report.get("aggregate") or {}) if isinstance(submit_report.get("aggregate"), Mapping) else {}).get("error_count"))
        for attempt in attempts:
            reason = str(attempt.get("reason") or "")
            if reason and reason != "submitted_confirmed":
                reason_counts[reason] += 1
        derived_counts = _attempt_counters(attempts, selected_count=len(selected_ids), submit_report=submit_report)
        status_sync_result = submit_report.get("status_sync") if isinstance(submit_report.get("status_sync"), Mapping) else {}
        status = "completed"
        blocker = ""
        if unconfirmed_existing_count:
            status = UNCONFIRMED_JOURNAL_STATUS
            blocker = UNCONFIRMED_JOURNAL_STATUS
        elif error_count:
            if any(str(item.get("action") or "") == "submitted_attempted_unconfirmed" for item in attempts):
                status = "submit_unconfirmed"
                blocker = "submit_unconfirmed"
            else:
                status = "error"
                blocker = str((submit_report.get("errors") or [{}])[0].get("message") if isinstance(submit_report.get("errors"), list) and submit_report.get("errors") else "submit error")
        elif reason_counts.get("skipped_due_to_hard_cap"):
            status = "hard_cap_reached"
        if isinstance(status_sync_result, Mapping) and submitted_count and (status_sync_result.get("error") or status_sync_result.get("errors")):
            status = "status_sync_failed"
            blocker = "status_sync_failed"
        return self.store.update_run(
            run_id,
            _finish_patch(
                status,
                now_factory=self.now_factory,
                blocker_reason=blocker,
                loaded_feedbacks_count=len(rows),
                low_rating_feedbacks_count=len(low_rating_rows),
                ai_candidates_count=len(candidate_ids),
                submitted_count=submitted_count,
                skipped_count=skipped_count,
                hard_cap_reached=bool(reason_counts.get("skipped_due_to_hard_cap")),
                status_sync_result=status_sync_result,
                reason_counts=dict(reason_counts),
                attempts=attempts,
                **derived_counts,
                session={"storage_state_path": storage_state_path, "route_specific_checks": "guarded_submit_and_status_sync"},
                events=[*diagnostic_events, _event("run_finished", f"Auto complaints run finished with status {status}", status="success" if status in SUCCESS_RUN_STATUSES else "error")],
            ),
        )

    def _analyze_rows(self, rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index in range(0, len(rows), MAX_ROWS_PER_RUN):
            if index:
                self.sleep_fn(3.0)
            batch = rows[index : index + MAX_ROWS_PER_RUN]
            payload = self.feedbacks_ai_block.analyze({"rows": batch})
            batch_results = payload.get("results") if isinstance(payload.get("results"), list) else []
            results.extend([dict(item) for item in batch_results if isinstance(item, Mapping)])
        return results

    def _journal_by_feedback_id(self) -> dict[str, Mapping[str, Any]]:
        return {
            str(record.get("feedback_id") or "").strip(): record
            for record in self.complaints_block.journal.list_records()
            if isinstance(record, Mapping) and str(record.get("feedback_id") or "").strip()
        }

    def _schedule_by_id(self, schedule_id: str) -> dict[str, Any]:
        for schedule in self.store.read().get("schedules", []):
            if str(schedule.get("id") or "") == schedule_id:
                return dict(schedule)
        raise SheetVitrinaV1FeedbacksAutoComplaintsError(
            f"auto complaints schedule not found: {schedule_id}",
            http_status=404,
            reason="schedule_not_found",
            status="schedule_not_found",
        )

    def _due_schedules(self, now: datetime) -> list[tuple[dict[str, Any], datetime]]:
        due: list[tuple[dict[str, Any], datetime]] = []
        active = self.store.active_run()
        if active is not None:
            return []
        for schedule in self.store.read().get("schedules", []):
            if not bool(schedule.get("enabled")):
                continue
            latest_due = _latest_due_at(schedule, now)
            if latest_due is None:
                continue
            due_cutoff = _parse_iso(str(schedule.get("enabled_since_at") or schedule.get("created_at") or ""))
            if due_cutoff is not None and latest_due < due_cutoff:
                continue
            last_success = _parse_iso(str(schedule.get("last_success_at") or ""))
            if last_success is not None and last_success >= latest_due:
                continue
            due.append((dict(schedule), latest_due))
        due.sort(key=lambda item: item[1])
        return due


def _normalize_schedule(schedule: Mapping[str, Any], *, now: str, now_factory: Callable[[], datetime]) -> dict[str, Any]:
    schedule_id = str(schedule.get("id") or "").strip() or uuid4().hex
    local_time = _normalize_hhmm(schedule.get("local_time_hhmm") or schedule.get("time") or DEFAULT_LOCAL_TIME)
    tz_name = _normalize_timezone(schedule.get("timezone") or DEFAULT_TIMEZONE)
    created_at = _safe_text(schedule.get("created_at") or now, 80)
    enabled = bool(schedule.get("enabled"))
    enabled_since_at = _safe_text(schedule.get("enabled_since_at"), 80)
    if enabled and not enabled_since_at:
        enabled_since_at = now
    if not enabled:
        enabled_since_at = ""
    normalized = {
        "id": _safe_text(schedule_id, 80),
        "enabled": enabled,
        "local_time_hhmm": local_time,
        "timezone": tz_name,
        "timezone_label": "Екатеринбург" if tz_name == DEFAULT_TIMEZONE else tz_name,
        "first_lookback_hours": _bounded_int(schedule.get("first_lookback_hours"), 1, 168, DEFAULT_FIRST_LOOKBACK_HOURS),
        "overlap_hours": _bounded_int(schedule.get("overlap_hours"), 0, 72, DEFAULT_OVERLAP_HOURS),
        "hard_cap_per_run": _bounded_int(schedule.get("hard_cap_per_run"), 1, DEFAULT_HARD_CAP_PER_RUN, DEFAULT_HARD_CAP_PER_RUN),
        "created_at": created_at,
        "updated_at": now,
        "enabled_since_at": enabled_since_at,
        "last_run_at": _safe_text(schedule.get("last_run_at"), 80),
        "last_success_at": _safe_text(schedule.get("last_success_at"), 80),
        "last_due_at": _safe_text(schedule.get("last_due_at"), 80),
        "next_run_at": "",
        "last_status": _safe_text(schedule.get("last_status"), 80),
        "last_run_id": _safe_text(schedule.get("last_run_id"), 160),
        "last_stats": dict(schedule.get("last_stats") or {}) if isinstance(schedule.get("last_stats"), Mapping) else {},
    }
    normalized["next_run_at"] = _next_run_at(normalized, now_factory())
    return normalized


def _merge_schedule_for_save(schedule: Mapping[str, Any], existing: Mapping[str, Any] | None, *, now: str) -> dict[str, Any]:
    """Treat run lifecycle fields as server-owned runtime state, not browser payload."""

    incoming = dict(schedule)
    for field in SCHEDULE_RUNTIME_FIELDS:
        incoming.pop(field, None)
    if not isinstance(existing, Mapping):
        return incoming

    previous = _normalize_schedule(existing, now=_safe_text(existing.get("updated_at") or now, 80), now_factory=lambda: _parse_iso(now) or datetime.now(timezone.utc))
    merged = dict(incoming)
    merged["created_at"] = previous.get("created_at") or now
    was_enabled = bool(previous.get("enabled"))
    will_be_enabled = bool(incoming.get("enabled"))
    if will_be_enabled:
        merged["enabled_since_at"] = previous.get("enabled_since_at") if was_enabled else now
    else:
        merged["enabled_since_at"] = ""
    for field in ("last_run_at", "last_success_at", "last_due_at", "last_status", "last_run_id", "last_stats"):
        merged[field] = previous.get(field)
    return merged


def _normalize_run(run: Mapping[str, Any]) -> dict[str, Any]:
    reason_counts = run.get("reason_counts") if isinstance(run.get("reason_counts"), Mapping) else {}
    attempts = run.get("attempts") if isinstance(run.get("attempts"), list) else []
    events = run.get("events") if isinstance(run.get("events"), list) else []
    status = str(run.get("status") or "queued").strip() or "queued"
    return {
        "run_id": _safe_text(run.get("run_id"), 160),
        "schedule_id": _safe_text(run.get("schedule_id"), 100),
        "trigger_source": _safe_text(run.get("trigger_source") or "manual", 40),
        "status": _safe_text(status, 80),
        "blocker_reason": _safe_text(run.get("blocker_reason"), 200),
        "created_at": _safe_text(run.get("created_at"), 80),
        "started_at": _safe_text(run.get("started_at"), 80),
        "finished_at": _safe_text(run.get("finished_at"), 80),
        "due_at": _safe_text(run.get("due_at"), 80),
        "timezone": _safe_text(run.get("timezone") or DEFAULT_TIMEZONE, 80),
        "window_base_from": _safe_text(run.get("window_base_from"), 80),
        "window_fetch_from": _safe_text(run.get("window_fetch_from"), 80),
        "window_to": _safe_text(run.get("window_to"), 80),
        "overlap_hours": _safe_int(run.get("overlap_hours")),
        "hard_cap_per_run": _safe_int(run.get("hard_cap_per_run")),
        "loaded_feedbacks_count": _safe_int(run.get("loaded_feedbacks_count")),
        "low_rating_feedbacks_count": _safe_int(run.get("low_rating_feedbacks_count")),
        "ai_candidates_count": _safe_int(run.get("ai_candidates_count")),
        "eligible_for_submit_count": _safe_int(run.get("eligible_for_submit_count")),
        "submit_attempted_count": _safe_int(run.get("submit_attempted_count")),
        "submit_confirmed_count": _safe_int(run.get("submit_confirmed_count")),
        "already_confirmed_count": _safe_int(run.get("already_confirmed_count")),
        "skipped_existing_confirmed_count": _safe_int(run.get("skipped_existing_confirmed_count")),
        "skipped_existing_unconfirmed_count": _safe_int(run.get("skipped_existing_unconfirmed_count")),
        "skipped_hard_cap_count": _safe_int(run.get("skipped_hard_cap_count")),
        "skipped_other_count": _safe_int(run.get("skipped_other_count")),
        "error_count": _safe_int(run.get("error_count")),
        "submitted_count": _safe_int(run.get("submitted_count")),
        "skipped_count": _safe_int(run.get("skipped_count")),
        "hard_cap_reached": bool(run.get("hard_cap_reached")),
        "status_sync_result": _sanitize_report_mapping(run.get("status_sync_result")),
        "reason_counts": {str(key): _safe_int(value) for key, value in reason_counts.items()},
        "sanitized_error_message": _safe_text(run.get("sanitized_error_message"), 1000),
        "evidence_refs": [_safe_text(item, 260) for item in run.get("evidence_refs", [])] if isinstance(run.get("evidence_refs"), list) else [],
        "automation_lock": _sanitize_report_mapping(run.get("automation_lock")),
        "session": _sanitize_report_mapping(run.get("session")),
        "attempts": [_normalize_attempt(item) for item in attempts if isinstance(item, Mapping)][-200:],
        "events": [_normalize_event(item) for item in events if isinstance(item, Mapping)][-200:],
    }


def _normalize_attempt(attempt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": _safe_text(attempt.get("run_id"), 160),
        "feedback_id": _safe_text(attempt.get("feedback_id"), 160),
        "rating": _safe_int(attempt.get("rating")),
        "ai_status": _safe_text(attempt.get("ai_status"), 40),
        "candidate": bool(attempt.get("candidate")),
        "action": _safe_text(attempt.get("action"), 80),
        "reason": _safe_text(attempt.get("reason"), 240),
        "reason_label": _safe_text(attempt.get("reason_label"), 240),
        "complaint_journal_ref": _safe_text(attempt.get("complaint_journal_ref"), 160),
        "created_at": _safe_text(attempt.get("created_at"), 80),
        "updated_at": _safe_text(attempt.get("updated_at"), 80),
        "evidence_refs": list(attempt.get("evidence_refs") or []) if isinstance(attempt.get("evidence_refs"), list) else [],
    }


def _normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": _safe_text(event.get("timestamp"), 80),
        "event": _safe_text(event.get("event"), 120),
        "message": _safe_text(event.get("message"), 1000),
        "status": _safe_text(event.get("status"), 40),
    }


def _public_schedule(schedule: Mapping[str, Any]) -> dict[str, Any]:
    return dict(_normalize_schedule(schedule, now=_safe_text(schedule.get("updated_at") or _iso_datetime(datetime.now(timezone.utc)), 80), now_factory=lambda: datetime.now(timezone.utc)))


def _public_run(run: Mapping[str, Any], *, details: bool = False) -> dict[str, Any]:
    normalized = _normalize_run(run)
    if details:
        return normalized
    compact = dict(normalized)
    compact["attempts"] = compact["attempts"][-20:]
    compact["events"] = compact["events"][-20:]
    return compact


def _reconcile_run_with_journal(run: Mapping[str, Any], journal_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Upgrade legacy skip reasons using current journal evidence without mutating runtime state."""

    normalized = _normalize_run(run)
    attempts: list[dict[str, Any]] = []
    changed = False
    for attempt in normalized["attempts"]:
        action = str(attempt.get("action") or "")
        reason = str(attempt.get("reason") or "")
        feedback_id = str(attempt.get("feedback_id") or "").strip()
        patch: dict[str, Any] = {}
        if feedback_id and (action == "already_journaled" or reason == "existing_journal_feedback_id"):
            record = journal_by_id.get(feedback_id)
            if isinstance(record, Mapping):
                existing = _classify_journal_record(record)
                if existing.get("terminal_confirmed"):
                    patch = {
                        "action": str(existing.get("action") or "already_journaled_confirmed"),
                        "reason": str(existing.get("reason") or "already_journaled_confirmed"),
                        "complaint_journal_ref": str(existing.get("complaint_journal_ref") or ""),
                        "evidence_refs": list(existing.get("evidence_refs") or []),
                    }
                else:
                    patch = {
                        "action": "skipped_existing_unconfirmed",
                        "reason": str(existing.get("reason") or "skipped_existing_unconfirmed"),
                        "complaint_journal_ref": str(existing.get("complaint_journal_ref") or ""),
                        "evidence_refs": list(existing.get("evidence_refs") or []),
                    }
            else:
                patch = {
                    "action": "skipped_existing_unconfirmed",
                    "reason": "journal_reference_missing",
                    "evidence_refs": [],
                }
        elif reason == "already_attempted_feedback_id":
            patch = {"action": "already_confirmed_in_prior_run", "reason": "already_confirmed_in_prior_run"}
        elif reason == "hard_cap_reached":
            patch = {"action": "skipped_due_to_hard_cap", "reason": "skipped_due_to_hard_cap"}
        if patch:
            changed = True
            attempts.append(_normalize_attempt({**attempt, **patch}))
        else:
            attempts.append(_normalize_attempt(attempt))
    if not changed:
        return normalized

    reason_counts: Counter[str] = Counter()
    for attempt in attempts:
        reason = str(attempt.get("reason") or "")
        if reason and reason != "submitted_confirmed":
            reason_counts[reason] += 1
    submitted_from_attempts = sum(1 for attempt in attempts if str(attempt.get("action") or "") == "submitted_confirmed")
    submit_attempted_from_attempts = submitted_from_attempts + sum(
        1 for attempt in attempts if str(attempt.get("action") or "") == "submitted_attempted_unconfirmed"
    )
    selected_count = max(_safe_int(normalized.get("eligible_for_submit_count")), submit_attempted_from_attempts)
    counters = _attempt_counters(
        attempts,
        selected_count=selected_count,
        submit_report={"aggregate": {"error_count": _safe_int(normalized.get("error_count"))}},
    )
    skipped_from_attempts = sum(
        1
        for attempt in attempts
        if str(attempt.get("action") or "").startswith(("already_", "skipped_", "safety_"))
    )
    normalized.update(
        {
            "attempts": attempts,
            "reason_counts": dict(reason_counts),
            "submitted_count": max(_safe_int(normalized.get("submitted_count")), submitted_from_attempts),
            "skipped_count": max(_safe_int(normalized.get("skipped_count")), skipped_from_attempts),
            "hard_cap_reached": bool(normalized.get("hard_cap_reached") or reason_counts.get("skipped_due_to_hard_cap")),
            **counters,
        }
    )
    if counters.get("skipped_existing_unconfirmed_count") and normalized.get("status") == "completed":
        normalized["status"] = UNCONFIRMED_JOURNAL_STATUS
        normalized["blocker_reason"] = UNCONFIRMED_JOURNAL_STATUS
    elif reason_counts.get("skipped_due_to_hard_cap") and normalized.get("status") == "completed":
        normalized["status"] = "hard_cap_reached"
    return normalized


def _sanitize_report_mapping(value: Any, *, depth: int = 0) -> dict[str, Any]:
    if not isinstance(value, Mapping) or depth > 3:
        return {}
    blocked = ("token", "cookie", "secret", "password", "authorization", "header")
    sanitized: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = _safe_text(raw_key, 120)
        lowered = key.lower()
        if not key:
            continue
        if lowered == "storage_state" or any(marker in lowered for marker in blocked):
            continue
        if isinstance(raw_value, Mapping):
            sanitized[key] = _sanitize_report_mapping(raw_value, depth=depth + 1)
        elif isinstance(raw_value, list):
            sanitized[key] = [
                _sanitize_report_mapping(item, depth=depth + 1) if isinstance(item, Mapping) else _safe_text(item, 260)
                for item in raw_value[:50]
            ]
        elif isinstance(raw_value, (int, float, bool)) or raw_value is None:
            sanitized[key] = raw_value
        else:
            sanitized[key] = _safe_text(raw_value, 1000)
    return sanitized


def _compute_window(schedule: Mapping[str, Any], due_at: datetime) -> dict[str, Any]:
    tz_name = str(schedule.get("timezone") or DEFAULT_TIMEZONE)
    tz = ZoneInfo(tz_name)
    window_to = due_at.astimezone(tz)
    last_success = _parse_iso(str(schedule.get("last_success_at") or ""))
    overlap_hours = _bounded_int(schedule.get("overlap_hours"), 0, 72, DEFAULT_OVERLAP_HOURS)
    if last_success is not None:
        base_from = last_success.astimezone(tz)
        fetch_from = base_from - timedelta(hours=overlap_hours)
    else:
        first_hours = _bounded_int(schedule.get("first_lookback_hours"), 1, 168, DEFAULT_FIRST_LOOKBACK_HOURS)
        base_from = window_to - timedelta(hours=first_hours)
        fetch_from = base_from
    return {
        "window_base_from": _iso_datetime(base_from),
        "window_fetch_from": _iso_datetime(fetch_from),
        "window_to": _iso_datetime(window_to),
        "overlap_hours": overlap_hours,
    }


def _latest_due_at(schedule: Mapping[str, Any], now: datetime) -> datetime | None:
    tz_name = str(schedule.get("timezone") or DEFAULT_TIMEZONE)
    local_time = str(schedule.get("local_time_hhmm") or DEFAULT_LOCAL_TIME)
    hour, minute = (int(part) for part in local_time.split(":", 1))
    local_now = now.astimezone(ZoneInfo(tz_name))
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > local_now:
        candidate -= timedelta(days=1)
    return candidate


def _next_run_at(schedule: Mapping[str, Any], now: datetime) -> str:
    tz_name = str(schedule.get("timezone") or DEFAULT_TIMEZONE)
    local_time = str(schedule.get("local_time_hhmm") or DEFAULT_LOCAL_TIME)
    hour, minute = (int(part) for part in local_time.split(":", 1))
    local_now = now.astimezone(ZoneInfo(tz_name))
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return _iso_datetime(candidate)


def _finish_patch(status: str, *, now_factory: Callable[[], datetime], **kwargs: Any) -> dict[str, Any]:
    patch = {
        "status": status,
        "finished_at": _iso_now(now_factory),
        "blocker_reason": _safe_text(kwargs.pop("blocker_reason", ""), 200),
        "loaded_feedbacks_count": _safe_int(kwargs.pop("loaded_feedbacks_count", 0)),
        "low_rating_feedbacks_count": _safe_int(kwargs.pop("low_rating_feedbacks_count", 0)),
        "ai_candidates_count": _safe_int(kwargs.pop("ai_candidates_count", 0)),
        "eligible_for_submit_count": _safe_int(kwargs.pop("eligible_for_submit_count", 0)),
        "submit_attempted_count": _safe_int(kwargs.pop("submit_attempted_count", 0)),
        "submit_confirmed_count": _safe_int(kwargs.pop("submit_confirmed_count", 0)),
        "already_confirmed_count": _safe_int(kwargs.pop("already_confirmed_count", 0)),
        "skipped_existing_confirmed_count": _safe_int(kwargs.pop("skipped_existing_confirmed_count", 0)),
        "skipped_existing_unconfirmed_count": _safe_int(kwargs.pop("skipped_existing_unconfirmed_count", 0)),
        "skipped_hard_cap_count": _safe_int(kwargs.pop("skipped_hard_cap_count", 0)),
        "skipped_other_count": _safe_int(kwargs.pop("skipped_other_count", 0)),
        "error_count": _safe_int(kwargs.pop("error_count", 0)),
        "submitted_count": _safe_int(kwargs.pop("submitted_count", 0)),
        "skipped_count": _safe_int(kwargs.pop("skipped_count", 0)),
        "hard_cap_reached": bool(kwargs.pop("hard_cap_reached", False)),
        "status_sync_result": dict(kwargs.pop("status_sync_result", {}) or {}),
        "reason_counts": dict(kwargs.pop("reason_counts", {}) or {}),
        "sanitized_error_message": _safe_text(kwargs.pop("sanitized_error_message", ""), 1000),
        "events": list(kwargs.pop("events", []) or []),
        "attempts": list(kwargs.pop("attempts", []) or []),
    }
    patch.update(kwargs)
    return patch


def _attempt(
    run_id: str,
    feedback_id: str,
    *,
    action: str,
    reason: str,
    ai: Mapping[str, Any] | None = None,
    reason_label: str = "",
    complaint_journal_ref: str = "",
    evidence_refs: list[Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    ai = ai if isinstance(ai, Mapping) else {}
    return {
        "run_id": run_id,
        "feedback_id": feedback_id,
        "rating": _safe_int(ai.get("rating")),
        "ai_status": _safe_text(ai.get("complaint_fit"), 40),
        "candidate": True,
        "action": action,
        "reason": reason,
        "reason_label": reason_label,
        "complaint_journal_ref": complaint_journal_ref,
        "created_at": now,
        "updated_at": now,
        "evidence_refs": [_safe_text(item, 260) for item in (evidence_refs or [])],
    }


def _attempts_from_submit_report(run_id: str, submit_report: Mapping[str, Any], *, ai_by_id: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    rows = submit_report.get("rows") if isinstance(submit_report.get("rows"), list) else []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        feedback_id = str(row.get("feedback_id") or "").strip()
        if not feedback_id:
            continue
        if row.get("submitted"):
            action = "submitted_confirmed"
            reason = "submitted_confirmed"
        elif row.get("submit_clicked"):
            action = "submitted_attempted_unconfirmed"
            reason = "submitted_attempted_unconfirmed"
        else:
            raw_reason = _safe_text(row.get("skip_reason") or row.get("block_reason") or "safety_rejected", 240)
            action, reason = _classify_submit_skip(raw_reason)
        attempts.append(_attempt(run_id, feedback_id, action=action, reason=reason, ai=ai_by_id.get(feedback_id)))
    return attempts


def _classify_submit_skip(raw_reason: str) -> tuple[str, str]:
    normalized = raw_reason.lower()
    if "already_complained_in_wb" in normalized or "уже пожал" in normalized or "жалоба уже" in normalized:
        return "already_confirmed_in_wb", "already_confirmed_in_wb"
    if "hard_cap" in normalized:
        return "skipped_due_to_hard_cap", "skipped_due_to_hard_cap"
    if "session_invalid" in normalized or "automation_busy" in normalized or "storage_state" in normalized:
        return "skipped_due_to_session_or_capability_blocker", raw_reason or "skipped_due_to_session_or_capability_blocker"
    if (
        "not actionable" in normalized
        or "not found" in normalized
        or "safe row menu" in normalized
        or "complaint action is disabled" in normalized
        or "complaint_action_disabled" in normalized
    ):
        return "skipped_due_to_wb_ui_unavailable_or_not_actionable", raw_reason
    return "safety_rejected", raw_reason or "safety_rejected"


def _classify_journal_record(record: Mapping[str, Any]) -> dict[str, Any]:
    status = str(record.get("complaint_status") or "").strip()
    feedback_id = _safe_text(record.get("feedback_id"), 160)
    evidence_refs = _journal_evidence_refs(record)
    confirmed = _journal_record_has_confirmation(record)
    if confirmed:
        return {
            "feedback_id": feedback_id,
            "terminal_confirmed": True,
            "retryable": False,
            "journal_status": status,
            "action": "already_journaled_confirmed",
            "reason": "already_journaled_confirmed",
            "complaint_journal_ref": _safe_text(record.get("complaint_id"), 160),
            "evidence_refs": evidence_refs,
        }
    if status == "error":
        return {
            "feedback_id": feedback_id,
            "terminal_confirmed": False,
            "retryable": True,
            "journal_status": status,
            "action": "journal_error_retry_allowed",
            "reason": "journal_error_retry_allowed",
            "complaint_journal_ref": _safe_text(record.get("complaint_id"), 160),
            "evidence_refs": evidence_refs,
        }
    reason = "journal_only_unconfirmed" if not str(record.get("submit_run_id") or record.get("status_sync_run_id") or "").strip() else "skipped_existing_unconfirmed"
    return {
        "feedback_id": feedback_id,
        "terminal_confirmed": False,
        "retryable": False,
        "journal_status": status,
        "action": "skipped_existing_unconfirmed",
        "reason": reason,
        "complaint_journal_ref": _safe_text(record.get("complaint_id"), 160),
        "evidence_refs": evidence_refs,
    }


def _journal_record_has_confirmation(record: Mapping[str, Any]) -> bool:
    status = str(record.get("complaint_status") or "").strip()
    if status not in CONFIRMED_JOURNAL_STATUSES:
        return False
    submit_result = str(record.get("submit_result") or "").strip()
    if submit_result == "confirmed_success":
        return True
    if status in FINAL_JOURNAL_STATUSES and str(record.get("status_sync_run_id") or "").strip() and str(record.get("last_status_checked_at") or "").strip():
        return True
    if status == "waiting_response" and str(record.get("submitted_at") or "").strip() and str(record.get("submit_run_id") or "").strip():
        return True
    return False


def _journal_evidence_refs(record: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for key, prefix in (
        ("complaint_id", "journal"),
        ("submit_run_id", "submit"),
        ("status_sync_run_id", "status_sync"),
    ):
        value = _safe_text(record.get(key), 160)
        if value:
            refs.append(f"{prefix}:{value}")
    return refs


def _attempt_counters(attempts: list[Mapping[str, Any]], *, selected_count: int, submit_report: Mapping[str, Any]) -> dict[str, int]:
    actions = Counter(str(item.get("action") or "") for item in attempts if isinstance(item, Mapping))
    aggregate = submit_report.get("aggregate") if isinstance(submit_report.get("aggregate"), Mapping) else {}
    error_count = _safe_int(aggregate.get("error_count"))
    submit_attempted = actions.get("submitted_confirmed", 0) + actions.get("submitted_attempted_unconfirmed", 0)
    already_confirmed = (
        actions.get("already_journaled_confirmed", 0)
        + actions.get("already_confirmed_in_wb", 0)
        + actions.get("already_confirmed_in_prior_run", 0)
    )
    skipped_existing_confirmed = (
        actions.get("already_journaled_confirmed", 0)
        + actions.get("already_confirmed_in_prior_run", 0)
        + actions.get("already_confirmed_in_wb", 0)
    )
    skipped_existing_unconfirmed = actions.get("skipped_existing_unconfirmed", 0)
    skipped_hard_cap = actions.get("skipped_due_to_hard_cap", 0)
    skipped_known = skipped_existing_confirmed + skipped_existing_unconfirmed + skipped_hard_cap + already_confirmed
    skipped_other = max(0, sum(1 for item in attempts if str(item.get("action") or "").startswith(("skipped_", "safety_"))) - skipped_known)
    return {
        "eligible_for_submit_count": selected_count,
        "submit_attempted_count": submit_attempted,
        "submit_confirmed_count": actions.get("submitted_confirmed", 0),
        "already_confirmed_count": already_confirmed,
        "skipped_existing_confirmed_count": skipped_existing_confirmed,
        "skipped_existing_unconfirmed_count": skipped_existing_unconfirmed,
        "skipped_hard_cap_count": skipped_hard_cap,
        "skipped_other_count": skipped_other,
        "error_count": error_count,
    }


def _run_stats(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "loaded_feedbacks_count": _safe_int(run.get("loaded_feedbacks_count")),
        "low_rating_feedbacks_count": _safe_int(run.get("low_rating_feedbacks_count")),
        "ai_candidates_count": _safe_int(run.get("ai_candidates_count")),
        "eligible_for_submit_count": _safe_int(run.get("eligible_for_submit_count")),
        "submit_attempted_count": _safe_int(run.get("submit_attempted_count")),
        "submit_confirmed_count": _safe_int(run.get("submit_confirmed_count")),
        "already_confirmed_count": _safe_int(run.get("already_confirmed_count")),
        "skipped_existing_confirmed_count": _safe_int(run.get("skipped_existing_confirmed_count")),
        "skipped_existing_unconfirmed_count": _safe_int(run.get("skipped_existing_unconfirmed_count")),
        "skipped_hard_cap_count": _safe_int(run.get("skipped_hard_cap_count")),
        "skipped_other_count": _safe_int(run.get("skipped_other_count")),
        "error_count": _safe_int(run.get("error_count")),
        "submitted_count": _safe_int(run.get("submitted_count")),
        "skipped_count": _safe_int(run.get("skipped_count")),
        "hard_cap_reached": bool(run.get("hard_cap_reached")),
        "blocker_reason": _safe_text(run.get("blocker_reason"), 200),
    }


def _row_in_window(row: Mapping[str, Any], start: datetime, end: datetime) -> bool:
    value = str(row.get("created_at") or "").strip()
    parsed = _parse_iso(value)
    if parsed is None:
        date_value = str(row.get("created_date") or "")
        return bool(date_value and start.date().isoformat() <= date_value <= end.date().isoformat())
    return start <= parsed <= end


def _is_ai_candidate(result: Mapping[str, Any]) -> bool:
    value = str(result.get("complaint_fit") or result.get("complaint_fit_label") or "").strip().lower()
    return value in AI_CANDIDATE_VALUES


def _normalize_hhmm(value: Any) -> str:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("local_time_hhmm must use HH:mm format")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError("local_time_hhmm must use HH:mm format") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("local_time_hhmm must be a valid 24h time")
    return f"{hour:02d}:{minute:02d}"


def _normalize_timezone(value: Any) -> str:
    text = str(value or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(text)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unsupported timezone: {text}") from exc
    return text


def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    parsed = _safe_int(value)
    if parsed <= 0:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _event(event: str, message: str, *, status: str, at: str | None = None) -> dict[str, Any]:
    return {
        "timestamp": at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event": _safe_text(event, 120),
        "message": _safe_text(message, 1000),
        "status": _safe_text(status, 40),
    }


def _new_run_id(now_factory: Callable[[], datetime]) -> str:
    return "auto_complaints_" + now_factory().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _iso_now(now_factory: Callable[[], datetime]) -> str:
    return _iso_datetime(now_factory())


def _iso_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_iso_required(value: str) -> datetime:
    parsed = _parse_iso(value)
    if parsed is None:
        raise ValueError(f"invalid ISO datetime: {value}")
    return parsed


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_text(value: Any, limit: int) -> str:
    return str(value or "").replace("\n", " ").replace("\r", " ").strip()[:limit]


def _render_run_markdown(run: Mapping[str, Any]) -> str:
    normalized = _normalize_run(run)
    return "\n".join(
        [
            "# Feedbacks Auto Complaints Run",
            "",
            f"- Run ID: `{normalized['run_id']}`",
            f"- Status: `{normalized['status']}`",
            f"- Window: `{normalized['window_fetch_from']}`..`{normalized['window_to']}`",
            f"- Loaded feedbacks: `{normalized['loaded_feedbacks_count']}`",
            f"- Low-rating feedbacks: `{normalized['low_rating_feedbacks_count']}`",
            f"- AI candidates: `{normalized['ai_candidates_count']}`",
            f"- Submitted: `{normalized['submitted_count']}`",
            f"- Skipped: `{normalized['skipped_count']}`",
            f"- Reason counts: `{json.dumps(normalized['reason_counts'], ensure_ascii=False)}`",
        ]
    ) + "\n"
