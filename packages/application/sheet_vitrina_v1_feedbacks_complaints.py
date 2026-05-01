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
CONTRACT_VERSION = "v1"
DEFAULT_JOURNAL_FILENAME = "sheet_vitrina_v1_feedbacks_complaints_journal.json"

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


class SheetVitrinaV1FeedbacksComplaintsBlock:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        journal: JsonFileFeedbacksComplaintJournal | None = None,
        status_sync_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        now_factory: Any | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.journal = journal or JsonFileFeedbacksComplaintJournal(runtime_dir)
        self.status_sync_runner = status_sync_runner
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

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
            )
        )


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


def _iso_now(now_factory: Any | None = None) -> str:
    now = now_factory() if now_factory else datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
