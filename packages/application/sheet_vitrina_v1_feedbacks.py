"""Read-only feedback discovery block for sheet_vitrina_v1 operator UI."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping, Protocol

from packages.adapters.wb_feedbacks import (
    HttpBackedWbFeedbacksSource,
    WbFeedbacksHttpStatusError,
    WbFeedbacksTransportError,
)
from packages.business_time import CANONICAL_BUSINESS_TIMEZONE, current_business_date_iso


CONTRACT_NAME = "sheet_vitrina_v1_feedbacks"
CONTRACT_VERSION = "v1"
DEFAULT_STARS = (1, 2, 3, 4, 5)
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 31


class WbFeedbacksSource(Protocol):
    def fetch_feedbacks(
        self,
        *,
        date_from_ts: int,
        date_to_ts: int,
        is_answered: bool,
        request_timestamps: list[float],
    ) -> list[Mapping[str, Any]]:
        raise NotImplementedError


class SheetVitrinaV1FeedbacksError(RuntimeError):
    def __init__(self, message: str, *, http_status: int = 502) -> None:
        self.http_status = http_status
        super().__init__(message)


class SheetVitrinaV1FeedbacksBlock:
    """Builds normalized read-only feedback payloads for the public operator UI."""

    def __init__(
        self,
        *,
        source: WbFeedbacksSource | None = None,
        now_factory: Any | None = None,
    ) -> None:
        self.source = source or HttpBackedWbFeedbacksSource()
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        stars: list[int] | None = None,
        is_answered: str = "all",
    ) -> dict[str, Any]:
        normalized_from, normalized_to = self._resolve_date_window(date_from, date_to)
        normalized_stars = self._normalize_stars(stars)
        normalized_answered = self._normalize_is_answered(is_answered)
        date_from_ts, date_to_ts = _date_window_to_unix_bounds(normalized_from, normalized_to)
        requested_streams = [False, True] if normalized_answered == "all" else [normalized_answered == "true"]
        request_timestamps: list[float] = []
        raw_rows: list[Mapping[str, Any]] = []
        try:
            for stream_is_answered in requested_streams:
                raw_rows.extend(
                    self.source.fetch_feedbacks(
                        date_from_ts=date_from_ts,
                        date_to_ts=date_to_ts,
                        is_answered=stream_is_answered,
                        request_timestamps=request_timestamps,
                    )
                )
        except WbFeedbacksHttpStatusError as exc:
            raise SheetVitrinaV1FeedbacksError(
                _friendly_http_error_message(exc.status_code),
                http_status=_mapped_http_status(exc.status_code),
            ) from exc
        except WbFeedbacksTransportError as exc:
            raise SheetVitrinaV1FeedbacksError(str(exc), http_status=502) from exc
        except RuntimeError as exc:
            raise SheetVitrinaV1FeedbacksError(str(exc), http_status=502) from exc

        rows = [
            row
            for row in (_normalize_feedback_row(item) for item in raw_rows)
            if row["product_valuation"] in normalized_stars
        ]
        rows = _dedupe_feedback_rows(rows)
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        summary = _build_summary(rows)
        fetched_at = self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "meta": {
                "date_from": normalized_from,
                "date_to": normalized_to,
                "date_from_unix": date_from_ts,
                "date_to_unix": date_to_ts,
                "stars": normalized_stars,
                "is_answered": normalized_answered,
                "requested_streams": ["answered" if value else "unanswered" for value in requested_streams],
                "fetched_at": fetched_at,
                "source": "WB API / feedbacks",
                "pagination": "bounded",
            },
            "summary": summary,
            "schema": {
                "columns": [
                    {"key": "created_date", "label": "Дата"},
                    {"key": "product_valuation", "label": "Звезды"},
                    {"key": "answer_status", "label": "Статус"},
                    {"key": "nm_id", "label": "Артикул WB"},
                    {"key": "supplier_article", "label": "Артикул продавца"},
                    {"key": "product_name", "label": "Товар"},
                    {"key": "brand_name", "label": "Бренд"},
                    {"key": "text", "label": "Отзыв"},
                    {"key": "pros", "label": "Плюсы"},
                    {"key": "cons", "label": "Минусы"},
                    {"key": "answer_text", "label": "Ответ"},
                ]
            },
            "rows": rows,
        }

    def _resolve_date_window(self, date_from: str | None, date_to: str | None) -> tuple[str, str]:
        normalized_from = _normalize_date(date_from or "")
        normalized_to = _normalize_date(date_to or "")
        if bool(normalized_from) != bool(normalized_to):
            raise ValueError("date_from and date_to must be provided together")
        if not normalized_from and not normalized_to:
            normalized_to = current_business_date_iso(self.now_factory())
            normalized_from = (datetime.fromisoformat(normalized_to) - timedelta(days=DEFAULT_WINDOW_DAYS - 1)).date().isoformat()
        start = datetime.fromisoformat(normalized_from).date()
        end = datetime.fromisoformat(normalized_to).date()
        if end < start:
            raise ValueError("date_to must be greater than or equal to date_from")
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise ValueError(f"feedbacks date window must be at most {MAX_WINDOW_DAYS} days")
        return normalized_from, normalized_to

    def _normalize_stars(self, stars: list[int] | None) -> list[int]:
        if not stars:
            return list(DEFAULT_STARS)
        normalized = sorted({int(value) for value in stars})
        invalid = [value for value in normalized if value < 1 or value > 5]
        if invalid:
            raise ValueError("stars must contain values from 1 to 5")
        return normalized

    def _normalize_is_answered(self, value: str) -> str:
        normalized = str(value or "all").strip().lower()
        if normalized in {"", "all"}:
            return "all"
        if normalized in {"true", "false"}:
            return normalized
        raise ValueError("is_answered must be true, false, or all")


def _normalize_date(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError as exc:
        raise ValueError("date_from/date_to must use YYYY-MM-DD") from exc


def _date_window_to_unix_bounds(date_from: str, date_to: str) -> tuple[int, int]:
    start_date = datetime.fromisoformat(date_from).date()
    end_date = datetime.fromisoformat(date_to).date()
    start_dt = datetime.combine(start_date, time.min, tzinfo=CANONICAL_BUSINESS_TIMEZONE)
    end_dt = datetime.combine(end_date, time.max.replace(microsecond=0), tzinfo=CANONICAL_BUSINESS_TIMEZONE)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def _normalize_feedback_row(item: Mapping[str, Any]) -> dict[str, Any]:
    product = _mapping(item.get("productDetails")) or _mapping(item.get("product")) or {}
    answer = item.get("answer")
    answer_text = ""
    if isinstance(answer, Mapping):
        answer_text = str(answer.get("text") or answer.get("answer") or "").strip()
    elif answer is not None:
        answer_text = str(answer).strip()
    created_at = str(item.get("createdDate") or item.get("created_at") or item.get("date") or "").strip()
    is_answered = _bool_or_none(item.get("isAnswered"))
    if is_answered is None:
        is_answered = _bool_or_none(item.get("_wb_is_answered"))
    if is_answered is None:
        is_answered = bool(answer_text)
    product_valuation = _normalize_star(
        item.get("productValuation")
        if item.get("productValuation") is not None
        else item.get("valuation", item.get("rating"))
    )
    return {
        "feedback_id": str(item.get("id") or item.get("feedbackId") or "").strip(),
        "created_at": created_at,
        "created_date": _date_part(created_at),
        "product_valuation": product_valuation,
        "answer_status": "С ответом" if is_answered else "Без ответа",
        "is_answered": bool(is_answered),
        "nm_id": _safe_int(product.get("nmId") if product else item.get("nmId")),
        "supplier_article": str(product.get("supplierArticle") or item.get("supplierArticle") or "").strip(),
        "product_name": str(product.get("productName") or item.get("productName") or "").strip(),
        "brand_name": str(product.get("brandName") or item.get("brandName") or "").strip(),
        "text": str(item.get("text") or "").strip(),
        "pros": str(item.get("pros") or "").strip(),
        "cons": str(item.get("cons") or "").strip(),
        "answer_text": answer_text,
    }


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _normalize_star(value: Any) -> int:
    try:
        star = int(value)
    except (TypeError, ValueError):
        return 0
    return star if 1 <= star <= 5 else 0


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _date_part(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if len(normalized) >= 10 and normalized[4:5] == "-" and normalized[7:8] == "-":
        return normalized[:10]
    return normalized


def _dedupe_feedback_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        identity = str(row.get("feedback_id") or "") or "|".join(
            [
                str(row.get("created_at") or ""),
                str(row.get("nm_id") or ""),
                str(row.get("text") or ""),
            ]
        )
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(row)
    return deduped


def _build_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_star = {str(star): 0 for star in DEFAULT_STARS}
    answered = 0
    for row in rows:
        star = str(row.get("product_valuation") or "")
        if star in by_star:
            by_star[star] += 1
        if bool(row.get("is_answered")):
            answered += 1
    return {
        "total": len(rows),
        "by_star": by_star,
        "answered": answered,
        "unanswered": len(rows) - answered,
    }


def _friendly_http_error_message(status_code: int) -> str:
    if status_code == 401:
        return "WB API rejected WB_API_TOKEN for feedbacks: unauthorized"
    if status_code == 403:
        return "WB API rejected WB_API_TOKEN for feedbacks: access is forbidden"
    if status_code == 429:
        return "WB API feedbacks rate limit returned 429; retry later"
    if status_code >= 500:
        return f"WB API feedbacks upstream is unavailable: status {status_code}"
    return f"WB API feedbacks request failed with status {status_code}"


def _mapped_http_status(status_code: int) -> int:
    if status_code in {401, 403, 429}:
        return status_code
    if status_code >= 500:
        return 502
    return 502
