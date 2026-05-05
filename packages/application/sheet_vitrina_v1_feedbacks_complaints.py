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
CONTRACT_VERSION = "v1"
DEFAULT_JOURNAL_FILENAME = "sheet_vitrina_v1_feedbacks_complaints_journal.json"
DEFAULT_SYNC_JOB_DIRNAME = "feedbacks_complaints_status_sync_jobs"
DEFAULT_SYNC_REPORT_DIRNAME = "feedbacks_complaints_status_sync"
SYNC_JOB_ACTIVE_STATUSES = {"queued", "running"}

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
            if isinstance(job, Mapping) and str(job.get("status") or "") in SYNC_JOB_ACTIVE_STATUSES
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
                if not isinstance(job, Mapping) or str(job.get("status") or "") not in SYNC_JOB_ACTIVE_STATUSES:
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


class SheetVitrinaV1FeedbacksComplaintsBlock:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        journal: JsonFileFeedbacksComplaintJournal | None = None,
        status_sync_jobs: JsonFileFeedbacksComplaintsStatusSyncJobStore | None = None,
        status_sync_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        now_factory: Any | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.journal = journal or JsonFileFeedbacksComplaintJournal(runtime_dir)
        self.status_sync_runner = status_sync_runner
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.status_sync_jobs = status_sync_jobs or JsonFileFeedbacksComplaintsStatusSyncJobStore(
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
