"""Runtime complaint journal for sheet_vitrina_v1 feedbacks.

The journal is operational state for the Seller Portal complaint workflow. It is
not accepted truth, not the canonical EBD layer and not a Google Sheets/GAS
path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4


CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_complaints"
SYNC_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_complaints_status_sync"
SYNC_JOB_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_complaints_status_sync_job"
SYNC_JOB_STORE_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_complaints_status_sync_jobs"
SYNC_JOB_KIND = "feedbacks_complaints_status_sync"
SUBMIT_JOB_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_complaints_submit_job"
SUBMIT_JOB_STORE_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_complaints_submit_jobs"
SUBMIT_JOB_KIND = "feedbacks_complaints_submit_selected"
CONTRACT_VERSION = "v1"
DEFAULT_JOURNAL_FILENAME = "sheet_vitrina_v1_feedbacks_complaints_journal.json"
DEFAULT_SYNC_JOB_DIRNAME = "feedbacks_complaints_status_sync_jobs"
DEFAULT_SYNC_REPORT_DIRNAME = "feedbacks_complaints_status_sync"
DEFAULT_SUBMIT_JOB_DIRNAME = "feedbacks_complaints_submit_jobs"
DEFAULT_SUBMIT_REPORT_DIRNAME = "feedbacks_complaint_submit"
JOB_ACTIVE_STATUSES = {"queued", "running"}
SUBMIT_JOB_MAX_SELECTED_IDS = 20
SUBMIT_JOB_MAX_SUBMIT_HARD_CAP = 5
SUBMIT_JOB_EVENTS_LIMIT = 200

COMPLAINT_STATUS_LABELS = {
    "waiting_response": "Ждёт ответа",
    "satisfied": "Удовлетворена",
    "rejected": "Отклонена",
    "error": "Ошибка",
}
COMPLAINT_STATUS_IDS = tuple(COMPLAINT_STATUS_LABELS.keys())

COMPLAINT_TABLE_COLUMNS = [
    ("complaint_status_label", "Статус жалобы"),
    ("wb_category_label", "Категория WB"),
    ("complaint_text", "Текст жалобы"),
    ("submitted_at", "Дата подачи"),
    ("last_status_checked_at", "Дата обновления статуса"),
    ("wb_decision_text", "Результат WB"),
    ("review_created_at", "Дата отзыва"),
    ("rating", "Оценка"),
    ("nm_id", "nmId"),
    ("supplier_article", "Артикул"),
    ("product_name", "Товар"),
    ("review_text", "Текст отзыва"),
    ("pros", "Плюсы"),
    ("cons", "Минусы"),
    ("is_answered_label", "Есть ответ"),
    ("answer_text", "Ответ продавца"),
    ("photo_count", "Фото"),
    ("video_count", "Видео"),
    ("ai_complaint_fit_label", "Подходит для жалобы"),
    ("ai_category_label", "Категория AI"),
    ("ai_reason", "Причина AI / текст ситуации"),
    ("ai_confidence_label", "Уверенность AI"),
    ("feedback_id", "ID отзыва"),
    ("match_status", "Match status"),
    ("match_score", "Match score"),
    ("last_error", "Ошибка"),
]


class SheetVitrinaV1FeedbacksComplaintsError(RuntimeError):
    def __init__(self, message: str, *, http_status: int = 500) -> None:
        self.http_status = http_status
        super().__init__(message)


@dataclass(frozen=True)
class ComplaintJournalResult:
    record: dict[str, Any]
    created: bool
    duplicate: bool


class JsonFileFeedbacksComplaintJournal:
    """Small atomic JSON journal keyed by feedback_id."""

    def __init__(self, runtime_dir: Path, *, filename: str = DEFAULT_JOURNAL_FILENAME) -> None:
        self.path = runtime_dir / filename
        self._lock = threading.Lock()

    def list_records(self) -> list[dict[str, Any]]:
        return self._read_payload().get("records", [])

    def find_by_feedback_id(self, feedback_id: str) -> dict[str, Any] | None:
        normalized = str(feedback_id or "").strip()
        if not normalized:
            return None
        for record in self.list_records():
            if str(record.get("feedback_id") or "").strip() == normalized:
                return record
        return None

    def create_or_update(self, record: Mapping[str, Any], *, retry_errors: bool = False) -> ComplaintJournalResult:
        feedback_id = str(record.get("feedback_id") or "").strip()
        if not feedback_id:
            raise ValueError("feedback_id is required for complaint journal record")
        now = _iso_now()
        with self._lock:
            payload = self._read_payload()
            records = payload.setdefault("records", [])
            for index, existing in enumerate(records):
                if str(existing.get("feedback_id") or "").strip() != feedback_id:
                    continue
                existing_status = str(existing.get("complaint_status") or "")
                if existing_status != "error" or not retry_errors:
                    return ComplaintJournalResult(record=dict(existing), created=False, duplicate=True)
                merged = _normalize_record({**existing, **dict(record), "updated_at": now})
                records[index] = merged
                self._write_payload(payload)
                return ComplaintJournalResult(record=merged, created=False, duplicate=False)
            normalized = _normalize_record(
                {
                    "complaint_id": str(record.get("complaint_id") or uuid4()),
                    "created_at": now,
                    "updated_at": now,
                    **dict(record),
                }
            )
            records.append(normalized)
            self._write_payload(payload)
            return ComplaintJournalResult(record=normalized, created=True, duplicate=False)

    def update_status(
        self,
        feedback_id: str,
        *,
        status: str,
        raw_status_text: str = "",
        wb_decision_text: str = "",
        status_sync_run_id: str = "",
        last_error: str | None = None,
        checked_at: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_id = str(feedback_id or "").strip()
        normalized_status = _normalize_status(status)
        now = checked_at or _iso_now()
        with self._lock:
            payload = self._read_payload()
            records = payload.setdefault("records", [])
            for index, record in enumerate(records):
                if str(record.get("feedback_id") or "").strip() != normalized_id:
                    continue
                updated = dict(record)
                updated.update(
                    {
                        "updated_at": now,
                        "last_status_checked_at": now,
                        "complaint_status": normalized_status,
                        "complaint_status_label": COMPLAINT_STATUS_LABELS[normalized_status],
                        "raw_status_text": _safe_text(raw_status_text, 600),
                        "wb_decision_text": _safe_text(wb_decision_text, 600),
                    }
                )
                if normalized_status != "error":
                    updated["last_error"] = ""
                elif last_error is not None:
                    updated["last_error"] = _safe_text(last_error, 800)
                if status_sync_run_id:
                    updated["status_sync_run_id"] = status_sync_run_id
                records[index] = _normalize_record(updated)
                self._write_payload(payload)
                return dict(records[index])
        return None

    def update_metadata(self, feedback_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None:
        normalized_id = str(feedback_id or "").strip()
        if not normalized_id:
            return None
        blocked_keys = {"feedback_id", "complaint_id", "created_at"}
        metadata = {str(key): value for key, value in fields.items() if str(key) not in blocked_keys}
        if not metadata:
            return None
        with self._lock:
            payload = self._read_payload()
            records = payload.setdefault("records", [])
            for index, record in enumerate(records):
                if str(record.get("feedback_id") or "").strip() != normalized_id:
                    continue
                updated = {**dict(record), **metadata, "updated_at": _iso_now()}
                records[index] = _normalize_record(updated)
                self._write_payload(payload)
                return dict(records[index])
        return None

    def _read_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "records": [],
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SheetVitrinaV1FeedbacksComplaintsError("complaint journal is not readable") from exc
        if not isinstance(payload, dict):
            raise SheetVitrinaV1FeedbacksComplaintsError("complaint journal has invalid shape")
        records = payload.get("records")
        if not isinstance(records, list):
            payload["records"] = []
        payload["records"] = [dict(item) for item in payload["records"] if isinstance(item, Mapping)]
        return payload

    def _write_payload(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        normalized = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "updated_at": _iso_now(),
            "records": [dict(item) for item in payload.get("records", []) if isinstance(item, Mapping)],
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.path)


class JsonFileFeedbacksComplaintsStatusSyncJobStore:
    """Persistent operational state for read-only complaints status sync jobs."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        journal: JsonFileFeedbacksComplaintJournal,
        dirname: str = DEFAULT_SYNC_JOB_DIRNAME,
        now_factory: Any | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.path = runtime_dir / dirname / "jobs.json"
        self.journal = journal
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._mark_interrupted_active_jobs()

    def start(
        self,
        payload: Mapping[str, Any] | None,
        *,
        runner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        requested_by: str = "public_route",
    ) -> dict[str, Any]:
        request_payload = dict(payload or {})
        normalized_requested_by = str(requested_by or "public_route").strip() or "public_route"
        with self._lock:
            store_payload = self._read_payload_unlocked()
            active = self._active_job(store_payload)
            if active is not None:
                return _public_sync_job(active, already_running=True)

            run_id = _new_status_sync_run_id(self.now_factory)
            now = _iso_now(self.now_factory)
            job = _normalize_sync_job(
                {
                    "run_id": run_id,
                    "kind": SYNC_JOB_KIND,
                    "status": "queued",
                    "created_at": now,
                    "started_at": "",
                    "finished_at": "",
                    "requested_by": normalized_requested_by,
                    "report_dir": "",
                    "report_json_path": "",
                    "report_markdown_path": "",
                    "summary": {},
                    "error": "",
                    "journal_record_count_before": len(self.journal.list_records()),
                    "journal_record_count_after": 0,
                    "matched_local_complaints": 0,
                    "statuses_updated": 0,
                    "weak_rejected": 0,
                    "direct_matches": 0,
                    "strong_composite_matches": 0,
                }
            )
            store_payload.setdefault("jobs", []).append(job)
            self._write_payload_unlocked(store_payload)
            started_snapshot = _public_sync_job(job, already_running=False)

        thread = threading.Thread(
            target=self._run,
            args=(run_id, request_payload, runner),
            daemon=True,
            name=f"feedbacks-complaints-status-sync-{run_id}",
        )
        thread.start()
        return started_snapshot

    def get(self, run_id: str) -> dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id query parameter is required")
        with self._lock:
            job = self._find_job_unlocked(normalized_run_id)
            if job is None:
                raise SheetVitrinaV1FeedbacksComplaintsError(
                    f"complaints status sync job not found: {normalized_run_id}",
                    http_status=404,
                )
            return _public_sync_job(job, already_running=False)

    def _run(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        runner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        self._update_job(
            run_id,
            {
                "status": "running",
                "started_at": _iso_now(self.now_factory),
            },
        )
        try:
            with self._lock:
                current_job = self._find_job_unlocked(run_id) or {}
                journal_record_count_before = _safe_int(current_job.get("journal_record_count_before"))
            report = dict(runner({**dict(payload), "run_id": run_id}))
            after_count = len(self.journal.list_records())
            patch = _sync_job_patch_from_report(
                report,
                journal_record_count_before=journal_record_count_before,
                journal_record_count_after=after_count,
            )
        except Exception as exc:  # pragma: no cover - live fallback
            patch = _sync_job_error_patch(
                str(exc),
                finished_at=_iso_now(self.now_factory),
                journal_record_count_after=len(self.journal.list_records()),
            )
        self._update_job(run_id, patch)

    def _update_job(self, run_id: str, patch: Mapping[str, Any]) -> None:
        with self._lock:
            store_payload = self._read_payload_unlocked()
            jobs = store_payload.setdefault("jobs", [])
            for index, job in enumerate(jobs):
                if str(job.get("run_id") or "").strip() != run_id:
                    continue
                jobs[index] = _normalize_sync_job({**dict(job), **dict(patch)})
                self._write_payload_unlocked(store_payload)
                return

    def _active_job(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        active = [
            _normalize_sync_job(job)
            for job in payload.get("jobs", [])
            if isinstance(job, Mapping) and str(job.get("status") or "") in JOB_ACTIVE_STATUSES
        ]
        if not active:
            return None
        return max(
            active,
            key=lambda item: (
                str(item.get("started_at") or ""),
                str(item.get("created_at") or ""),
                str(item.get("run_id") or ""),
            ),
        )

    def _find_job_unlocked(self, run_id: str) -> dict[str, Any] | None:
        payload = self._read_payload_unlocked()
        for job in payload.get("jobs", []):
            if isinstance(job, Mapping) and str(job.get("run_id") or "").strip() == run_id:
                return _normalize_sync_job(job)
        return None

    def _mark_interrupted_active_jobs(self) -> None:
        with self._lock:
            payload = self._read_payload_unlocked()
            changed = False
            now = _iso_now(self.now_factory)
            for index, job in enumerate(payload.get("jobs", [])):
                if not isinstance(job, Mapping) or str(job.get("status") or "") not in JOB_ACTIVE_STATUSES:
                    continue
                payload["jobs"][index] = _normalize_sync_job(
                    {
                        **dict(job),
                        **_sync_job_error_patch(
                            "runtime service restarted before complaints status sync job finished",
                            finished_at=now,
                            journal_record_count_after=len(self.journal.list_records()),
                        ),
                    }
                )
                changed = True
            if changed:
                self._write_payload_unlocked(payload)

    def _read_payload_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "contract_name": SYNC_JOB_STORE_CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "jobs": [],
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SheetVitrinaV1FeedbacksComplaintsError("complaints status sync job store is not readable") from exc
        if not isinstance(payload, dict):
            raise SheetVitrinaV1FeedbacksComplaintsError("complaints status sync job store has invalid shape")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            payload["jobs"] = []
        payload["jobs"] = [_normalize_sync_job(item) for item in payload["jobs"] if isinstance(item, Mapping)]
        return payload

    def _write_payload_unlocked(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        jobs = [_normalize_sync_job(item) for item in payload.get("jobs", []) if isinstance(item, Mapping)]
        normalized = {
            "contract_name": SYNC_JOB_STORE_CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "updated_at": _iso_now(self.now_factory),
            "jobs": jobs[-100:],
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.path)


class JsonFileFeedbacksComplaintsSubmitJobStore:
    """Persistent operational state for operator-triggered guarded complaint submit jobs."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        journal: JsonFileFeedbacksComplaintJournal,
        dirname: str = DEFAULT_SUBMIT_JOB_DIRNAME,
        now_factory: Any | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.path = runtime_dir / dirname / "jobs.json"
        self.journal = journal
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._mark_interrupted_active_jobs()

    def start(
        self,
        payload: Mapping[str, Any] | None,
        *,
        runner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        requested_by: str = "operator_ui",
    ) -> dict[str, Any]:
        request_payload = _validate_submit_selected_payload(payload or {})
        normalized_requested_by = str(requested_by or "operator_ui").strip() or "operator_ui"
        with self._lock:
            store_payload = self._read_payload_unlocked()
            active = self._active_job(store_payload)
            if active is not None:
                return _public_submit_job(active, already_running=True)

            run_id = _new_submit_run_id(self.now_factory)
            now = _iso_now(self.now_factory)
            selected_ids = list(request_payload.get("feedback_ids") or [])
            job = _normalize_submit_job(
                {
                    "run_id": run_id,
                    "kind": SUBMIT_JOB_KIND,
                    "status": "queued",
                    "created_at": now,
                    "started_at": "",
                    "finished_at": "",
                    "requested_by": normalized_requested_by,
                    "selected_count": len(selected_ids),
                    "tested_count": 0,
                    "submitted_count": 0,
                    "skipped_count": 0,
                    "error_count": 0,
                    "submitted_feedback_ids": [],
                    "skipped": [],
                    "events": [_submit_event("job_started", message="Submit job queued", status="queued", at=now)],
                    "report_dir": "",
                    "report_json_path": "",
                    "report_markdown_path": "",
                    "summary": {},
                    "status_sync_pending": False,
                    "status_sync_run_id": "",
                    "status_sync_report_path": "",
                    "error": "",
                }
            )
            store_payload.setdefault("jobs", []).append(job)
            self._write_payload_unlocked(store_payload)
            started_snapshot = _public_submit_job(job, already_running=False)

        thread = threading.Thread(
            target=self._run,
            args=(run_id, request_payload, runner),
            daemon=True,
            name=f"feedbacks-complaints-submit-{run_id}",
        )
        thread.start()
        return started_snapshot

    def get(self, run_id: str) -> dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id query parameter is required")
        with self._lock:
            job = self._find_job_unlocked(normalized_run_id)
            if job is None:
                raise SheetVitrinaV1FeedbacksComplaintsError(
                    f"complaints submit job not found: {normalized_run_id}",
                    http_status=404,
                )
            return _public_submit_job(job, already_running=False)

    def patch(self, run_id: str, patch: Mapping[str, Any]) -> None:
        self._update_job(run_id, patch)

    def _run(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        runner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        self._update_job(
            run_id,
            {
                "status": "running",
                "started_at": _iso_now(self.now_factory),
            },
        )
        try:
            report = dict(runner({**dict(payload), "run_id": run_id}))
            patch = _submit_job_patch_from_report(report)
        except Exception as exc:  # pragma: no cover - live fallback
            patch = _submit_job_error_patch(
                str(exc),
                finished_at=_iso_now(self.now_factory),
            )
        self._update_job(run_id, patch)

    def _update_job(self, run_id: str, patch: Mapping[str, Any]) -> None:
        with self._lock:
            store_payload = self._read_payload_unlocked()
            jobs = store_payload.setdefault("jobs", [])
            for index, job in enumerate(jobs):
                if str(job.get("run_id") or "").strip() != run_id:
                    continue
                jobs[index] = _normalize_submit_job({**dict(job), **dict(patch)})
                self._write_payload_unlocked(store_payload)
                return

    def _active_job(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        active = [
            _normalize_submit_job(job)
            for job in payload.get("jobs", [])
            if isinstance(job, Mapping) and str(job.get("status") or "") in JOB_ACTIVE_STATUSES
        ]
        if not active:
            return None
        return max(
            active,
            key=lambda item: (
                str(item.get("started_at") or ""),
                str(item.get("created_at") or ""),
                str(item.get("run_id") or ""),
            ),
        )

    def _find_job_unlocked(self, run_id: str) -> dict[str, Any] | None:
        payload = self._read_payload_unlocked()
        for job in payload.get("jobs", []):
            if isinstance(job, Mapping) and str(job.get("run_id") or "").strip() == run_id:
                return _normalize_submit_job(job)
        return None

    def _mark_interrupted_active_jobs(self) -> None:
        with self._lock:
            payload = self._read_payload_unlocked()
            changed = False
            now = _iso_now(self.now_factory)
            for index, job in enumerate(payload.get("jobs", [])):
                if not isinstance(job, Mapping) or str(job.get("status") or "") not in JOB_ACTIVE_STATUSES:
                    continue
                payload["jobs"][index] = _normalize_submit_job(
                    {
                        **dict(job),
                        **_submit_job_error_patch(
                            "runtime service restarted before complaints submit job finished",
                            finished_at=now,
                        ),
                    }
                )
                changed = True
            if changed:
                self._write_payload_unlocked(payload)

    def _read_payload_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "contract_name": SUBMIT_JOB_STORE_CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "jobs": [],
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SheetVitrinaV1FeedbacksComplaintsError("complaints submit job store is not readable") from exc
        if not isinstance(payload, dict):
            raise SheetVitrinaV1FeedbacksComplaintsError("complaints submit job store has invalid shape")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            payload["jobs"] = []
        payload["jobs"] = [_normalize_submit_job(item) for item in payload["jobs"] if isinstance(item, Mapping)]
        return payload

    def _write_payload_unlocked(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        jobs = [_normalize_submit_job(item) for item in payload.get("jobs", []) if isinstance(item, Mapping)]
        normalized = {
            "contract_name": SUBMIT_JOB_STORE_CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "updated_at": _iso_now(self.now_factory),
            "jobs": jobs[-100:],
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.path)


class SheetVitrinaV1FeedbacksComplaintsBlock:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        journal: JsonFileFeedbacksComplaintJournal | None = None,
        status_sync_jobs: JsonFileFeedbacksComplaintsStatusSyncJobStore | None = None,
        submit_jobs: JsonFileFeedbacksComplaintsSubmitJobStore | None = None,
        status_sync_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        submit_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        now_factory: Any | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.journal = journal or JsonFileFeedbacksComplaintJournal(runtime_dir)
        self.status_sync_runner = status_sync_runner
        self.submit_runner = submit_runner
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.status_sync_jobs = status_sync_jobs or JsonFileFeedbacksComplaintsStatusSyncJobStore(
            runtime_dir,
            journal=self.journal,
            now_factory=self.now_factory,
        )
        self.submit_jobs = submit_jobs or JsonFileFeedbacksComplaintsSubmitJobStore(
            runtime_dir,
            journal=self.journal,
            now_factory=self.now_factory,
        )

    def build_table(self) -> dict[str, Any]:
        rows = sorted(
            [_public_record(row) for row in self.journal.list_records()],
            key=lambda item: str(item.get("submitted_at") or item.get("created_at") or ""),
            reverse=True,
        )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "meta": {
                "storage_path": str(self.journal.path),
                "generated_at": _iso_now(self.now_factory),
                "record_count": len(rows),
                "dedupe_key": "feedback_id",
                "status_ids": list(COMPLAINT_STATUS_IDS),
                "status_labels": dict(COMPLAINT_STATUS_LABELS),
                "auto_sync_on_page_load": False,
            },
            "summary": _summary(rows),
            "schema": {"columns": [{"key": key, "label": label} for key, label in COMPLAINT_TABLE_COLUMNS]},
            "rows": rows,
        }

    def sync_status(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        requested_by = str(payload.get("requested_by") or "public_route").strip() or "public_route"
        return self.status_sync_jobs.start(payload, runner=self._run_status_sync, requested_by=requested_by)

    def get_sync_status_job(self, run_id: str) -> dict[str, Any]:
        return self.status_sync_jobs.get(run_id)

    def submit_selected(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        requested_by = str(payload.get("requested_by") or "operator_ui").strip() or "operator_ui"
        return self.submit_jobs.start(payload, runner=self._run_submit_selected, requested_by=requested_by)

    def get_submit_job(self, run_id: str) -> dict[str, Any]:
        return self.submit_jobs.get(run_id)

    def _run_status_sync(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.status_sync_runner is not None:
            result = dict(self.status_sync_runner(payload))
            return result
        try:
            from apps.seller_portal_feedbacks_complaints_status_sync import run_status_sync_for_runtime
        except Exception as exc:  # pragma: no cover - import fallback
            raise SheetVitrinaV1FeedbacksComplaintsError(f"status sync runner unavailable: {exc}") from exc
        return dict(
            run_status_sync_for_runtime(
                runtime_dir=self.runtime_dir,
                max_complaint_rows=int(payload.get("max_complaint_rows") or 80),
                headless=not bool(payload.get("headed")),
                timeout_ms=max(5000, int(payload.get("timeout_ms") or 20000)),
                output_dir=self.runtime_dir / DEFAULT_SYNC_REPORT_DIRNAME,
                run_id=str(payload.get("run_id") or "").strip() or None,
            )
        )

    def _run_submit_selected(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.submit_runner is not None:
            return dict(self.submit_runner(payload))
        return _run_guarded_submit_selected_for_runtime(
            runtime_dir=self.runtime_dir,
            payload=payload,
            journal=self.journal,
            status_sync_runner=self._run_status_sync,
            now_factory=self.now_factory,
            job_patch=self.submit_jobs.patch,
        )


def _normalize_sync_job(job: Mapping[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "queued").strip()
    if status not in {"queued", "running", "success", "error"}:
        status = "error"
    summary = job.get("summary") if isinstance(job.get("summary"), Mapping) else {}
    return {
        "run_id": _safe_text(job.get("run_id"), 160),
        "kind": _safe_text(job.get("kind") or SYNC_JOB_KIND, 120),
        "status": status,
        "created_at": _safe_text(job.get("created_at"), 80),
        "started_at": _safe_text(job.get("started_at"), 80),
        "finished_at": _safe_text(job.get("finished_at"), 80),
        "requested_by": _safe_text(job.get("requested_by") or "public_route", 80),
        "report_dir": _safe_text(job.get("report_dir"), 600),
        "report_json_path": _safe_text(job.get("report_json_path"), 600),
        "report_markdown_path": _safe_text(job.get("report_markdown_path"), 600),
        "summary": dict(summary),
        "error": _safe_text(job.get("error"), 1000),
        "journal_record_count_before": _safe_int(job.get("journal_record_count_before")),
        "journal_record_count_after": _safe_int(job.get("journal_record_count_after")),
        "matched_local_complaints": _safe_int(job.get("matched_local_complaints")),
        "statuses_updated": _safe_int(job.get("statuses_updated")),
        "weak_rejected": _safe_int(job.get("weak_rejected")),
        "direct_matches": _safe_int(job.get("direct_matches")),
        "strong_composite_matches": _safe_int(job.get("strong_composite_matches")),
    }


def _public_sync_job(job: Mapping[str, Any], *, already_running: bool) -> dict[str, Any]:
    normalized = _normalize_sync_job(job)
    return {
        "contract_name": SYNC_JOB_CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "run_id": normalized["run_id"],
        "kind": normalized["kind"],
        "status": normalized["status"],
        "already_running": bool(already_running),
        "created_at": normalized["created_at"],
        "started_at": normalized["started_at"],
        "finished_at": normalized["finished_at"],
        "requested_by": normalized["requested_by"],
        "report_dir": normalized["report_dir"],
        "report_json_path": normalized["report_json_path"],
        "report_markdown_path": normalized["report_markdown_path"],
        "summary": dict(normalized["summary"]),
        "error": normalized["error"],
        "journal_record_count_before": normalized["journal_record_count_before"],
        "journal_record_count_after": normalized["journal_record_count_after"],
        "matched_local_complaints": normalized["matched_local_complaints"],
        "statuses_updated": normalized["statuses_updated"],
        "weak_rejected": normalized["weak_rejected"],
        "direct_matches": normalized["direct_matches"],
        "strong_composite_matches": normalized["strong_composite_matches"],
    }


def _normalize_submit_job(job: Mapping[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "queued").strip()
    if status not in {"queued", "running", "success", "error"}:
        status = "error"
    summary = job.get("summary") if isinstance(job.get("summary"), Mapping) else {}
    skipped = job.get("skipped") if isinstance(job.get("skipped"), list) else []
    events = job.get("events") if isinstance(job.get("events"), list) else []
    submitted_ids = job.get("submitted_feedback_ids") if isinstance(job.get("submitted_feedback_ids"), list) else []
    return {
        "run_id": _safe_text(job.get("run_id"), 160),
        "kind": _safe_text(job.get("kind") or SUBMIT_JOB_KIND, 120),
        "status": status,
        "created_at": _safe_text(job.get("created_at"), 80),
        "started_at": _safe_text(job.get("started_at"), 80),
        "finished_at": _safe_text(job.get("finished_at"), 80),
        "requested_by": _safe_text(job.get("requested_by") or "operator_ui", 80),
        "selected_count": _safe_int(job.get("selected_count")),
        "tested_count": _safe_int(job.get("tested_count")),
        "submitted_count": _safe_int(job.get("submitted_count")),
        "skipped_count": _safe_int(job.get("skipped_count")),
        "error_count": _safe_int(job.get("error_count")),
        "submitted_feedback_ids": [_safe_text(item, 160) for item in submitted_ids if _safe_text(item, 160)],
        "skipped": [_normalize_submit_skip(item) for item in skipped if isinstance(item, Mapping)][:SUBMIT_JOB_EVENTS_LIMIT],
        "events": [_normalize_submit_event(item) for item in events if isinstance(item, Mapping)][-SUBMIT_JOB_EVENTS_LIMIT:],
        "report_dir": _safe_text(job.get("report_dir"), 600),
        "report_json_path": _safe_text(job.get("report_json_path"), 600),
        "report_markdown_path": _safe_text(job.get("report_markdown_path"), 600),
        "summary": dict(summary),
        "status_sync_pending": bool(job.get("status_sync_pending")),
        "status_sync_run_id": _safe_text(job.get("status_sync_run_id"), 160),
        "status_sync_report_path": _safe_text(job.get("status_sync_report_path"), 600),
        "error": _safe_text(job.get("error"), 1000),
    }


def _public_submit_job(job: Mapping[str, Any], *, already_running: bool) -> dict[str, Any]:
    normalized = _normalize_submit_job(job)
    return {
        "contract_name": SUBMIT_JOB_CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "run_id": normalized["run_id"],
        "kind": normalized["kind"],
        "status": normalized["status"],
        "already_running": bool(already_running),
        "created_at": normalized["created_at"],
        "started_at": normalized["started_at"],
        "finished_at": normalized["finished_at"],
        "requested_by": normalized["requested_by"],
        "selected_count": normalized["selected_count"],
        "tested_count": normalized["tested_count"],
        "submitted_count": normalized["submitted_count"],
        "skipped_count": normalized["skipped_count"],
        "error_count": normalized["error_count"],
        "submitted_feedback_ids": list(normalized["submitted_feedback_ids"]),
        "skipped": list(normalized["skipped"]),
        "events": list(normalized["events"]),
        "report_dir": normalized["report_dir"],
        "report_json_path": normalized["report_json_path"],
        "report_markdown_path": normalized["report_markdown_path"],
        "summary": dict(normalized["summary"]),
        "status_sync_pending": normalized["status_sync_pending"],
        "status_sync_run_id": normalized["status_sync_run_id"],
        "status_sync_report_path": normalized["status_sync_report_path"],
        "error": normalized["error"],
    }


def _validate_submit_selected_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_ids = payload.get("feedback_ids")
    if not isinstance(raw_ids, list):
        raise ValueError("feedback_ids must be a JSON array")
    feedback_ids: list[str] = []
    for raw_id in raw_ids:
        feedback_id = str(raw_id or "").strip()
        if feedback_id and feedback_id not in feedback_ids:
            feedback_ids.append(feedback_id)
    if not feedback_ids:
        raise ValueError("feedback_ids must contain at least one non-empty feedback_id")
    if len(feedback_ids) > SUBMIT_JOB_MAX_SELECTED_IDS:
        raise ValueError(f"feedback_ids hard cap is {SUBMIT_JOB_MAX_SELECTED_IDS}")
    max_submit = _safe_int(payload.get("max_submit") or 1)
    if max_submit < 1:
        raise ValueError("max_submit must be positive")
    if max_submit > SUBMIT_JOB_MAX_SUBMIT_HARD_CAP:
        raise ValueError(f"max_submit hard cap is {SUBMIT_JOB_MAX_SUBMIT_HARD_CAP}")
    date_from = _safe_text(payload.get("date_from"), 40)
    date_to = _safe_text(payload.get("date_to"), 40)
    stars = payload.get("stars")
    if not date_from or not date_to:
        raise ValueError("date_from and date_to are required for guarded submit-selected execution")
    if not stars:
        raise ValueError("stars are required for guarded submit-selected execution")
    return {
        **dict(payload),
        "feedback_ids": feedback_ids,
        "max_submit": max_submit,
        "date_from": date_from,
        "date_to": date_to,
        "is_answered": _safe_text(payload.get("is_answered") or "all", 20) or "all",
        "max_api_rows": max(1, _safe_int(payload.get("max_api_rows") or 100)),
    }


def _run_guarded_submit_selected_for_runtime(
    *,
    runtime_dir: Path,
    payload: Mapping[str, Any],
    journal: JsonFileFeedbacksComplaintJournal,
    status_sync_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    now_factory: Any,
    job_patch: Callable[[str, Mapping[str, Any]], None],
) -> dict[str, Any]:
    try:
        from apps.seller_portal_feedbacks_complaint_submit import (
            DEFAULT_OUTPUT_ROOT,
            DEFAULT_START_URL,
            DEFAULT_STORAGE_STATE_PATH,
            DEFAULT_WB_BOT_PYTHON,
            LOCAL_OUTPUT_ROOT,
            SubmitConfig,
            normalize_deny_feedback_ids,
            parse_stars,
            run_submit,
            write_report_artifacts,
        )
    except Exception as exc:  # pragma: no cover - import fallback
        raise SheetVitrinaV1FeedbacksComplaintsError(f"submit runner unavailable: {exc}") from exc

    request_payload = _validate_submit_selected_payload(payload)
    run_id = str(request_payload.get("run_id") or _new_submit_run_id(now_factory)).strip()
    selected_ids = list(request_payload.get("feedback_ids") or [])
    max_submit = min(SUBMIT_JOB_MAX_SUBMIT_HARD_CAP, _safe_int(request_payload.get("max_submit") or 1))
    output_root = DEFAULT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_OUTPUT_ROOT
    report: dict[str, Any] = {
        "contract_name": SUBMIT_JOB_CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "started_at": _iso_now(now_factory),
        "finished_at": "",
        "parameters": {
            "feedback_ids": selected_ids,
            "date_from": request_payload.get("date_from"),
            "date_to": request_payload.get("date_to"),
            "stars": request_payload.get("stars"),
            "is_answered": request_payload.get("is_answered"),
            "max_api_rows": request_payload.get("max_api_rows"),
            "max_submit": max_submit,
            "hard_max_submit": SUBMIT_JOB_MAX_SUBMIT_HARD_CAP,
        },
        "events": [_submit_event("job_started", message="Submit job started", status="running")],
        "rows": [],
        "status_sync": {},
        "aggregate": {
            "selected_count": len(selected_ids),
            "tested_count": 0,
            "submitted_count": 0,
            "skipped_count": 0,
            "error_count": 0,
        },
        "errors": [],
    }
    submitted_ids: list[str] = []
    skipped: list[dict[str, Any]] = []
    current_journal_ids = [str(item.get("feedback_id") or "") for item in journal.list_records()]
    deny_ids = normalize_deny_feedback_ids([*current_journal_ids, "QhDufSkeSBzUCxaDchAn"])

    def publish(status: str = "running", error: str = "") -> None:
        aggregate = dict(report["aggregate"])
        job_patch(
            run_id,
            {
                "status": status,
                "tested_count": aggregate.get("tested_count", 0),
                "submitted_count": aggregate.get("submitted_count", 0),
                "skipped_count": aggregate.get("skipped_count", 0),
                "error_count": aggregate.get("error_count", 0),
                "submitted_feedback_ids": submitted_ids,
                "skipped": skipped,
                "events": report["events"],
                "summary": aggregate,
                "error": error,
            },
        )

    for feedback_id in selected_ids:
        if report["aggregate"]["submitted_count"] >= max_submit:
            break
        report["aggregate"]["tested_count"] += 1
        report["events"].append(_submit_event("row_selected", feedback_id=feedback_id, message="Row selected for guarded submit", status="running"))
        existing = journal.find_by_feedback_id(feedback_id)
        if existing is not None:
            reason = f"complaint already exists for feedback_id with status={existing.get('complaint_status') or ''}"
            skipped.append(_submit_skip(feedback_id, "row_skipped_existing_complaint", reason))
            report["events"].append(_submit_event("row_skipped_existing_complaint", feedback_id=feedback_id, message=reason, status="skipped"))
            report["rows"].append({"feedback_id": feedback_id, "status": "skipped", "skip_reason": reason})
            report["aggregate"]["skipped_count"] += 1
            publish()
            continue
        config = SubmitConfig(
            date_from=str(request_payload.get("date_from") or ""),
            date_to=str(request_payload.get("date_to") or ""),
            stars=parse_stars(",".join(str(item) for item in request_payload.get("stars")) if isinstance(request_payload.get("stars"), list) else str(request_payload.get("stars") or "")),
            is_answered=str(request_payload.get("is_answered") or "all"),
            max_api_rows=max(_safe_int(request_payload.get("max_api_rows") or 100), len(selected_ids)),
            max_submit=1,
            include_review=True,
            dry_run=False,
            require_exact=True,
            retry_errors=False,
            submit_confirmation=True,
            runtime_dir=runtime_dir,
            storage_state_path=DEFAULT_STORAGE_STATE_PATH,
            wb_bot_python=DEFAULT_WB_BOT_PYTHON,
            output_dir=output_root,
            start_url=DEFAULT_START_URL,
            headless=True,
            timeout_ms=max(5000, _safe_int(request_payload.get("timeout_ms") or 20000)),
            write_artifacts=False,
            deny_feedback_ids=deny_ids,
            target_feedback_id=feedback_id,
        )
        try:
            submit_report = dict(run_submit(config))
            artifact_paths = write_report_artifacts(submit_report, output_root)
            submit_report["artifact_paths"] = {key: str(path) for key, path in artifact_paths.items()}
        except Exception as exc:  # pragma: no cover - live fallback
            error = _safe_text(str(exc), 1000)
            row_result = {"feedback_id": feedback_id, "status": "error", "error": error}
            report["rows"].append(row_result)
            report["errors"].append({"feedback_id": feedback_id, "stage": "submit_runner", "message": error})
            report["events"].append(_submit_event("row_error", feedback_id=feedback_id, message=error, status="error"))
            report["aggregate"]["error_count"] += 1
            publish(status="error", error=error)
            break
        row_result = _submit_selected_row_result(feedback_id, submit_report)
        report["rows"].append(row_result)
        report["events"].extend(_submit_events_from_submit_report(feedback_id, submit_report, row_result))
        if row_result.get("submitted"):
            submitted_ids.append(feedback_id)
            report["aggregate"]["submitted_count"] += 1
        elif row_result.get("submit_clicked"):
            report["aggregate"]["error_count"] += 1
            error = str(row_result.get("block_reason") or row_result.get("submit_result") or "submit unconfirmed")
            report["errors"].append({"feedback_id": feedback_id, "stage": "submit", "message": error})
            publish(status="error", error=error)
            break
        else:
            reason = str(row_result.get("skip_reason") or row_result.get("block_reason") or "not submitted")
            event_code = "row_skipped_ai_no_not_submit_ready" if "complaint_fit=no" in reason or "Жалобу не подавать" in reason else "row_error"
            skipped.append(_submit_skip(feedback_id, event_code, reason))
            report["events"].append(_submit_event(event_code, feedback_id=feedback_id, message=reason, status="skipped"))
            report["aggregate"]["skipped_count"] += 1
        publish()

    if submitted_ids and not report["aggregate"]["error_count"]:
        try:
            status_sync_report = dict(
                status_sync_runner(
                    {
                        "run_id": f"{run_id}_status_sync",
                        "requested_by": "submit_selected_job",
                        "max_complaint_rows": request_payload.get("max_complaint_rows") or 80,
                        "timeout_ms": request_payload.get("status_sync_timeout_ms") or 20000,
                    }
                )
            )
            report["status_sync"] = status_sync_report
            report["events"].append(
                _submit_event(
                    "row_status_synced",
                    message="Read-only status sync completed after submit job",
                    status="success",
                )
            )
        except Exception as exc:  # pragma: no cover - live fallback
            report["status_sync"] = {"error": _safe_text(str(exc), 1000)}
            report["events"].append(
                _submit_event(
                    "row_error",
                    message=f"Read-only status sync failed after submit job: {exc}",
                    status="error",
                )
            )
    report["finished_at"] = _iso_now(now_factory)
    report["events"].append(
        _submit_event(
            "job_finished",
            message="Submit job finished",
            status="error" if report["aggregate"]["error_count"] else "success",
        )
    )
    paths = _write_submit_selected_report(report, runtime_dir / "feedbacks_complaint_submit_selected")
    report["artifact_paths"] = {key: str(path) for key, path in paths.items()}
    return report


def _submit_selected_row_result(feedback_id: str, submit_report: Mapping[str, Any]) -> dict[str, Any]:
    candidates = submit_report.get("candidates") if isinstance(submit_report.get("candidates"), list) else []
    candidate = next((item for item in candidates if isinstance(item, Mapping) and str(item.get("feedback_id") or "") == feedback_id), {})
    modal = candidate.get("modal") if isinstance(candidate.get("modal"), Mapping) else {}
    aggregate = submit_report.get("aggregate") if isinstance(submit_report.get("aggregate"), Mapping) else {}
    return {
        "feedback_id": feedback_id,
        "status": "submitted" if modal.get("submit_success") else "skipped",
        "submitted": bool(modal.get("submit_success")),
        "submit_clicked": bool(modal.get("submit_clicked")),
        "submit_result": _safe_text(modal.get("submit_result"), 120),
        "skip_reason": _safe_text(candidate.get("skip_reason"), 600),
        "block_reason": _safe_text(modal.get("blocker") or submit_report.get("final_conclusion"), 600),
        "complaint_action_found": bool(modal.get("complaint_action_found") or modal.get("modal_opened")),
        "description_value_match": bool(modal.get("description_value_match")),
        "selected_category": _safe_text(modal.get("selected_category"), 180),
        "submit_payload_has_description": _safe_json_value((modal.get("submit_network_capture") or {}).get("submit_payload_has_description"), 80)
        if isinstance(modal.get("submit_network_capture"), Mapping)
        else "unknown",
        "submit_payload_description_length": _safe_int((modal.get("submit_network_capture") or {}).get("submit_payload_description_length"))
        if isinstance(modal.get("submit_network_capture"), Mapping)
        else 0,
        "runner_run_id": _safe_text(submit_report.get("run_id"), 160),
        "runner_final_conclusion": _safe_text(submit_report.get("final_conclusion"), 160),
        "runner_artifacts": dict(submit_report.get("artifact_paths") or {}) if isinstance(submit_report.get("artifact_paths"), Mapping) else {},
        "runner_aggregate": dict(aggregate),
    }


def _submit_events_from_submit_report(feedback_id: str, submit_report: Mapping[str, Any], row_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    candidates = submit_report.get("candidates") if isinstance(submit_report.get("candidates"), list) else []
    candidate = next((item for item in candidates if isinstance(item, Mapping) and str(item.get("feedback_id") or "") == feedback_id), {})
    match = candidate.get("match") if isinstance(candidate.get("match"), Mapping) else {}
    modal = candidate.get("modal") if isinstance(candidate.get("modal"), Mapping) else {}
    if str(match.get("match_status") or "") == "exact" or row_result.get("complaint_action_found"):
        events.append(_submit_event("row_exact_match_success", feedback_id=feedback_id, message="Exact/actionable match passed guarded runner", status="success"))
    if row_result.get("complaint_action_found"):
        events.append(_submit_event("row_actionable_found", feedback_id=feedback_id, message="Complaint action found in Seller Portal UI", status="success"))
    if row_result.get("description_value_match"):
        events.append(_submit_event("row_description_validated", feedback_id=feedback_id, message="Description field matched after fill/blur", status="success"))
    if row_result.get("submit_clicked"):
        events.append(_submit_event("row_submit_clicked", feedback_id=feedback_id, message="Final submit clicked once by guarded runner", status="running"))
    if row_result.get("submitted"):
        events.append(_submit_event("row_submit_confirmed_success", feedback_id=feedback_id, message="WB submit success confirmed by guarded runner", status="success"))
    elif modal.get("blocker"):
        events.append(_submit_event("row_error", feedback_id=feedback_id, message=str(modal.get("blocker") or ""), status="skipped"))
    return events


def _submit_job_patch_from_report(report: Mapping[str, Any]) -> dict[str, Any]:
    aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), Mapping) else {}
    artifacts = report.get("artifact_paths") if isinstance(report.get("artifact_paths"), Mapping) else {}
    json_path = _safe_text(artifacts.get("json"), 600)
    markdown_path = _safe_text(artifacts.get("markdown"), 600)
    status_sync = report.get("status_sync") if isinstance(report.get("status_sync"), Mapping) else {}
    status_sync_artifacts = status_sync.get("artifact_paths") if isinstance(status_sync.get("artifact_paths"), Mapping) else {}
    events = report.get("events") if isinstance(report.get("events"), list) else []
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    skipped = [
        _submit_skip(str(row.get("feedback_id") or ""), "row_skipped", str(row.get("skip_reason") or row.get("block_reason") or "not submitted"))
        for row in rows
        if isinstance(row, Mapping) and not row.get("submitted") and not row.get("submit_clicked")
    ]
    submitted_ids = [
        str(row.get("feedback_id") or "")
        for row in rows
        if isinstance(row, Mapping) and row.get("submitted") and str(row.get("feedback_id") or "")
    ]
    error_count = _safe_int(aggregate.get("error_count"))
    return {
        "status": "error" if error_count else "success",
        "finished_at": str(report.get("finished_at") or _iso_now()),
        "selected_count": _safe_int(aggregate.get("selected_count")),
        "tested_count": _safe_int(aggregate.get("tested_count")),
        "submitted_count": _safe_int(aggregate.get("submitted_count")),
        "skipped_count": _safe_int(aggregate.get("skipped_count")),
        "error_count": error_count,
        "submitted_feedback_ids": submitted_ids,
        "skipped": skipped,
        "events": events,
        "report_dir": str(Path(json_path).parent) if json_path else "",
        "report_json_path": json_path,
        "report_markdown_path": markdown_path,
        "summary": dict(aggregate),
        "status_sync_pending": bool(status_sync.get("error")),
        "status_sync_run_id": _safe_text(status_sync.get("run_id"), 160),
        "status_sync_report_path": _safe_text(status_sync_artifacts.get("json") or status_sync.get("report_json_path"), 600),
        "error": _submit_job_error_text(report.get("errors")),
    }


def _submit_job_error_patch(error: str, *, finished_at: str) -> dict[str, Any]:
    return {
        "status": "error",
        "finished_at": finished_at,
        "error_count": 1,
        "summary": {"error_count": 1},
        "error": _safe_text(error, 1000),
        "events": [_submit_event("row_error", message=error, status="error", at=finished_at)],
    }


def _write_submit_selected_report(report: Mapping[str, Any], output_root: Path) -> dict[str, Path]:
    run_dir = output_root / str(report.get("run_id") or _new_submit_run_id())
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "sheet_vitrina_v1_feedbacks_complaints_submit_selected.json"
    md_path = run_dir / "sheet_vitrina_v1_feedbacks_complaints_submit_selected.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render_submit_selected_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def _render_submit_selected_markdown(report: Mapping[str, Any]) -> str:
    aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), Mapping) else {}
    lines = [
        "# Feedbacks Complaints Submit Selected",
        "",
        f"- Run: `{report.get('run_id')}`",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- Selected: `{aggregate.get('selected_count', 0)}`",
        f"- Tested: `{aggregate.get('tested_count', 0)}`",
        f"- Submitted: `{aggregate.get('submitted_count', 0)}`",
        f"- Skipped: `{aggregate.get('skipped_count', 0)}`",
        f"- Errors: `{aggregate.get('error_count', 0)}`",
        "",
        "## Rows",
        "",
    ]
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- `{row.get('feedback_id')}` submitted `{row.get('submitted')}` clicked `{row.get('submit_clicked')}` "
            f"result `{row.get('submit_result')}` reason `{row.get('skip_reason') or row.get('block_reason') or ''}`"
        )
    return "\n".join(lines) + "\n"


def _new_submit_run_id(now_factory: Any | None = None) -> str:
    now = now_factory() if now_factory is not None else datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]


def _submit_event(code: str, *, feedback_id: str = "", message: str = "", status: str = "", at: str | None = None) -> dict[str, Any]:
    return _normalize_submit_event(
        {
            "timestamp": at or _iso_now(),
            "event": code,
            "feedback_id": feedback_id,
            "message": message,
            "status": status,
        }
    )


def _normalize_submit_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": _safe_text(event.get("timestamp"), 80),
        "event": _safe_text(event.get("event"), 80),
        "feedback_id": _safe_text(event.get("feedback_id"), 160),
        "message": _safe_text(event.get("message"), 600),
        "status": _safe_text(event.get("status"), 80),
    }


def _submit_skip(feedback_id: str, code: str, reason: str) -> dict[str, Any]:
    return _normalize_submit_skip({"feedback_id": feedback_id, "code": code, "reason": reason})


def _normalize_submit_skip(skip: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "feedback_id": _safe_text(skip.get("feedback_id"), 160),
        "code": _safe_text(skip.get("code"), 100),
        "reason": _safe_text(skip.get("reason"), 600),
    }


def _submit_job_error_text(errors: Any) -> str:
    if not isinstance(errors, list) or not errors:
        return ""
    chunks: list[str] = []
    for error in errors[:5]:
        if not isinstance(error, Mapping):
            continue
        feedback_id = _safe_text(error.get("feedback_id"), 160)
        stage = _safe_text(error.get("stage"), 80)
        message = _safe_text(error.get("message"), 500)
        chunks.append(" / ".join(part for part in (feedback_id, stage, message) if part))
    return _safe_text("; ".join(chunks), 1000)


def _sync_job_patch_from_report(
    report: Mapping[str, Any],
    *,
    journal_record_count_before: int,
    journal_record_count_after: int,
) -> dict[str, Any]:
    summary = _sync_job_summary_from_report(
        report,
        journal_record_count_before=journal_record_count_before,
        journal_record_count_after=journal_record_count_after,
    )
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    artifact_paths = report.get("artifact_paths") if isinstance(report.get("artifact_paths"), Mapping) else {}
    json_path = str(artifact_paths.get("json") or "").strip()
    markdown_path = str(artifact_paths.get("markdown") or "").strip()
    status = "error" if errors else "success"
    error_text = _sync_job_error_text(errors)
    return {
        "status": status,
        "finished_at": str(report.get("finished_at") or _iso_now()),
        "report_dir": str(Path(json_path).parent) if json_path else "",
        "report_json_path": json_path,
        "report_markdown_path": markdown_path,
        "summary": summary,
        "error": error_text,
        "journal_record_count_before": _safe_int(summary.get("journal_record_count_before")),
        "journal_record_count_after": journal_record_count_after,
        "matched_local_complaints": _safe_int(summary.get("matched_local_complaints")),
        "statuses_updated": _safe_int(summary.get("statuses_updated")),
        "weak_rejected": _safe_int(summary.get("weak_rejected")),
        "direct_matches": _safe_int(summary.get("direct_matches")),
        "strong_composite_matches": _safe_int(summary.get("strong_composite_matches")),
    }


def _sync_job_error_patch(
    error: str,
    *,
    finished_at: str,
    journal_record_count_after: int,
) -> dict[str, Any]:
    return {
        "status": "error",
        "finished_at": finished_at,
        "summary": {
            "pending_rows_read": 0,
            "answered_rows_read": 0,
            "journal_record_count_after": journal_record_count_after,
            "matched_local_complaints": 0,
            "statuses_updated": 0,
            "weak_rejected": 0,
            "direct_matches": 0,
            "strong_composite_matches": 0,
            "error_count": 1,
        },
        "error": _safe_text(error, 1000),
        "journal_record_count_after": journal_record_count_after,
        "matched_local_complaints": 0,
        "statuses_updated": 0,
        "weak_rejected": 0,
        "direct_matches": 0,
        "strong_composite_matches": 0,
    }


def _sync_job_summary_from_report(
    report: Mapping[str, Any],
    *,
    journal_record_count_before: int,
    journal_record_count_after: int,
) -> dict[str, Any]:
    aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), Mapping) else {}
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    return {
        "runner_contract_name": str(report.get("contract_name") or ""),
        "runner_contract_version": str(report.get("contract_version") or ""),
        "runner_run_id": str(report.get("run_id") or ""),
        "pending_rows_read": _safe_int(aggregate.get("pending_rows_read")),
        "answered_rows_read": _safe_int(aggregate.get("answered_rows_read")),
        "journal_record_count_before": _safe_int(aggregate.get("local_records_before") or journal_record_count_before),
        "journal_record_count_after": journal_record_count_after,
        "matched_local_complaints": _safe_int(aggregate.get("matched_local_complaints")),
        "statuses_updated": _safe_int(aggregate.get("statuses_updated")),
        "weak_rejected": _safe_int(aggregate.get("weak_matches_rejected")),
        "direct_matches": _safe_int(aggregate.get("direct_matches")),
        "strong_composite_matches": _safe_int(aggregate.get("strong_composite_matches")),
        "unmatched_rows": _safe_int(aggregate.get("unmatched_rows")),
        "duplicate_row_matches_skipped": _safe_int(aggregate.get("duplicate_row_matches_skipped")),
        "error_count": len(errors),
    }


def _sync_job_error_text(errors: Any) -> str:
    if not isinstance(errors, list) or not errors:
        return ""
    chunks: list[str] = []
    for error in errors[:5]:
        if not isinstance(error, Mapping):
            continue
        stage = _safe_text(error.get("stage"), 80)
        code = _safe_text(error.get("code"), 80)
        message = _safe_text(error.get("message"), 500)
        chunks.append(" / ".join(part for part in (stage, code, message) if part))
    return _safe_text("; ".join(chunks), 1000)


def _normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    status = _normalize_status(record.get("complaint_status") or "waiting_response")
    normalized = {
        "complaint_id": str(record.get("complaint_id") or uuid4()),
        "feedback_id": str(record.get("feedback_id") or "").strip(),
        "created_at": str(record.get("created_at") or _iso_now()),
        "updated_at": str(record.get("updated_at") or _iso_now()),
        "submitted_at": str(record.get("submitted_at") or ""),
        "last_status_checked_at": str(record.get("last_status_checked_at") or ""),
        "complaint_status": status,
        "complaint_status_label": COMPLAINT_STATUS_LABELS[status],
        "wb_category_label": _safe_text(record.get("wb_category_label"), 180),
        "complaint_text": _safe_text(record.get("complaint_text"), 1000),
        "wb_complaint_row_fingerprint": _safe_text(record.get("wb_complaint_row_fingerprint"), 120),
        "seller_portal_feedback_id": _safe_text(record.get("seller_portal_feedback_id"), 120),
        "match_status": _safe_text(record.get("match_status"), 40),
        "match_score": _safe_text(record.get("match_score"), 40),
        "rating": _safe_text(record.get("rating"), 20),
        "review_created_at": _safe_text(record.get("review_created_at"), 80),
        "nm_id": _safe_text(record.get("nm_id"), 80),
        "supplier_article": _safe_text(record.get("supplier_article"), 180),
        "product_name": _safe_text(record.get("product_name"), 300),
        "review_text": _safe_text(record.get("review_text"), 1200),
        "pros": _safe_text(record.get("pros"), 600),
        "cons": _safe_text(record.get("cons"), 600),
        "is_answered": bool(record.get("is_answered")),
        "is_answered_label": "Да" if record.get("is_answered") else "Нет",
        "answer_text": _safe_text(record.get("answer_text"), 800),
        "photo_count": int(record.get("photo_count") or 0),
        "video_count": int(record.get("video_count") or 0),
        "ai_complaint_fit": _safe_text(record.get("ai_complaint_fit"), 30),
        "ai_complaint_fit_label": _safe_text(record.get("ai_complaint_fit_label"), 30),
        "ai_category_label": _safe_text(record.get("ai_category_label"), 180),
        "ai_reason": _safe_text(record.get("ai_reason"), 1000),
        "ai_confidence": _safe_text(record.get("ai_confidence"), 30),
        "ai_confidence_label": _safe_text(record.get("ai_confidence_label"), 40),
        "submit_run_id": _safe_text(record.get("submit_run_id"), 120),
        "submit_clicked_count": _safe_int(record.get("submit_clicked_count")),
        "submit_result": _safe_text(record.get("submit_result"), 80),
        "submit_network_evidence_summary": _safe_json_value(record.get("submit_network_evidence_summary"), 2500),
        "submit_ui_evidence_summary": _safe_json_value(record.get("submit_ui_evidence_summary"), 2500),
        "post_submit_row_state": _safe_json_value(record.get("post_submit_row_state"), 1600),
        "modal_description_value_before_submit": _safe_text(record.get("modal_description_value_before_submit"), 1000),
        "submit_payload_has_description": _normalize_bool_unknown(record.get("submit_payload_has_description")),
        "submit_payload_description_length": _safe_int(record.get("submit_payload_description_length")),
        "submit_payload_description_snippet": _safe_text(record.get("submit_payload_description_snippet"), 260),
        "post_submit_wb_description_text": _safe_text(record.get("post_submit_wb_description_text"), 1000),
        "description_persisted": _normalize_bool_unknown(record.get("description_persisted")),
        "confirmation_probe_path": _safe_text(record.get("confirmation_probe_path"), 600),
        "status_sync_report_path": _safe_text(record.get("status_sync_report_path"), 600),
        "status_sync_run_id": _safe_text(record.get("status_sync_run_id"), 120),
        "last_error": _safe_text(record.get("last_error"), 800),
        "raw_status_text": _safe_text(record.get("raw_status_text"), 800),
        "wb_decision_text": _safe_text(record.get("wb_decision_text"), 800),
    }
    return normalized


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return _normalize_record(record)


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip()
    if status in COMPLAINT_STATUS_LABELS:
        return status
    return "error"


def _normalize_bool_unknown(value: Any) -> bool | str:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1", "да"}:
        return True
    if text in {"false", "no", "0", "нет"}:
        return False
    return "unknown"


def _summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {status: 0 for status in COMPLAINT_STATUS_IDS}
    for row in rows:
        status = str(row.get("complaint_status") or "")
        if status in counts:
            counts[status] += 1
    return {
        "total": len(rows),
        "waiting_response": counts["waiting_response"],
        "satisfied": counts["satisfied"],
        "rejected": counts["rejected"],
        "error": counts["error"],
    }


def _safe_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[: max(0, int(limit))]


def _safe_json_value(value: Any, limit: int) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        used = 0
        for key, item in value.items():
            safe_key = _safe_text(key, 80)
            safe_item = _safe_json_value(item, max(160, int(limit) // 3))
            preview = json.dumps({safe_key: safe_item}, ensure_ascii=False, sort_keys=True)
            used += len(preview)
            if used > int(limit):
                result["_truncated"] = True
                break
            result[safe_key] = safe_item
        return result
    if isinstance(value, list):
        items: list[Any] = []
        used = 0
        for item in value[:20]:
            safe_item = _safe_json_value(item, max(120, int(limit) // 4))
            preview = json.dumps(safe_item, ensure_ascii=False, sort_keys=True)
            used += len(preview)
            if used > int(limit):
                items.append({"_truncated": True})
                break
            items.append(safe_item)
        return items
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _safe_text(value, min(int(limit), 800))


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _new_status_sync_run_id(now_factory: Any | None = None) -> str:
    now = _iso_now(now_factory)
    compact = now.replace("-", "").replace(":", "").replace("+00:00", "Z")
    compact = compact.replace(".", "").replace("Z", "Z")
    return f"{compact}_{uuid4().hex[:8]}"


def _iso_now(now_factory: Any | None = None) -> str:
    now = now_factory() if now_factory else datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
