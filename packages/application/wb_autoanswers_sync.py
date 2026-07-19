"""Bounded, resumable WB feedback synchronization service."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from packages.adapters.wb_autoanswers import (
    WbAutoanswersHttpError,
    WbAutoanswersTransportError,
    WbFeedbackReadPort,
)
from packages.application.wb_autoanswers_runtime import (
    AutoanswersRepository,
    AutoanswersRuntimeError,
    iso_utc,
    parse_timestamp,
)
from packages.contracts.wb_autoanswers import BACKFILL_FROM_DATE


DEFAULT_PAGE_SIZE = 100
STEADY_OVERLAP_SECONDS = 48 * 60 * 60


class FeedbackSyncError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def _day_bounds(day: date) -> tuple[int, int]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone.utc) - timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp())


def _inside_history_window(row: Any) -> bool:
    if not isinstance(row, dict):
        try:
            value = row.get("createdDate")
        except AttributeError:
            return False
    else:
        value = row.get("createdDate")
    parsed = parse_timestamp(value)
    return parsed is None or parsed.date() >= date.fromisoformat(BACKFILL_FROM_DATE)


class WbFeedbackSyncService:
    """Persists all reads before enqueueing new steady-state reviews."""

    def __init__(
        self,
        *,
        repository: AutoanswersRepository,
        source: WbFeedbackReadPort,
        now_factory: Any,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.repository = repository
        self.source = source
        self.now_factory = now_factory
        self.page_size = min(5000, max(1, int(page_size)))

    def _now(self) -> datetime:
        value = self.now_factory()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def initial_backfill_tick(self, *, is_answered: bool) -> dict[str, Any]:
        """Fetch one page of one UTC day, then durably advance the cursor."""

        stream_name = "answered" if is_answered else "unanswered"
        stream_key = f"wb_feedback_backfill:{stream_name}"
        stored = self.repository.sync_cursor(stream_key)
        cursor = dict(stored["cursor"]) if stored else {
            "day": BACKFILL_FROM_DATE,
            "skip": 0,
            "complete": False,
        }
        if bool(cursor.get("complete")):
            return {"stream": stream_name, "complete": True, "rows": 0, "enqueued": 0}
        day = date.fromisoformat(str(cursor["day"]))
        today = self._now().date()
        if day > today:
            cursor["complete"] = True
            self.repository.save_sync_cursor(stream_key, cursor=cursor, successful=True)
            return {"stream": stream_name, "complete": True, "rows": 0, "enqueued": 0}
        run_id = self.repository.start_sync_run(run_kind="backfill", source_stream=stream_name, cursor=cursor)
        start_ts, end_ts = _day_bounds(day)
        try:
            page = self.source.fetch_feedbacks_page(
                date_from_ts=start_ts,
                date_to_ts=end_ts,
                is_answered=is_answered,
                take=self.page_size,
                skip=int(cursor.get("skip") or 0),
            )
            upserted = 0
            for row in page.rows:
                if not _inside_history_window(row):
                    continue
                outcome = self.repository.upsert_feedback(
                    row,
                    source_stream=stream_name,
                    run_kind="backfill",
                    sync_run_id=run_id,
                )
                upserted += int(outcome["is_new"] or outcome["content_changed"] or outcome["observation_changed"])
            if page.has_more:
                cursor["skip"] = int(cursor.get("skip") or 0) + page.take
            else:
                cursor["day"] = (day + timedelta(days=1)).isoformat()
                cursor["skip"] = 0
                cursor["complete"] = day >= today
            self.repository.save_sync_cursor(
                stream_key,
                cursor=cursor,
                watermark_at=datetime.combine(day, time.max, tzinfo=timezone.utc).isoformat(),
                successful=True,
            )
            self.repository.finish_sync_run(
                run_id,
                state="succeeded",
                discovered_count=len(page.rows),
                upserted_count=upserted,
                cursor=cursor,
            )
            return {
                "run_id": run_id,
                "stream": stream_name,
                "complete": bool(cursor["complete"]),
                "rows": len(page.rows),
                "upserted": upserted,
                "enqueued": 0,
                "cursor": cursor,
            }
        except Exception as exc:
            error = self._map_error(exc)
            self.repository.finish_sync_run(
                run_id,
                state="retryable_error" if error.retryable else "terminal_error",
                discovered_count=0,
                upserted_count=0,
                cursor=cursor,
                error_code=error.code,
            )
            raise error from exc

    def steady_sync_tick(self, *, is_answered: bool) -> dict[str, Any]:
        """Fetch one resumable page from an overlapping steady-state window."""

        now = self._now()
        stream_name = "answered" if is_answered else "unanswered"
        stream_key = f"wb_feedback_steady:{stream_name}"
        stored = self.repository.sync_cursor(stream_key)
        cursor = dict(stored["cursor"]) if stored else {}
        if not cursor.get("window_end"):
            previous = parse_timestamp(stored["watermark_at"]) if stored else None
            window_from = (previous or now) - timedelta(seconds=STEADY_OVERLAP_SECONDS)
            cursor = {
                "window_from": iso_utc(window_from),
                "window_end": iso_utc(now),
                "skip": 0,
            }
        window_from = parse_timestamp(cursor["window_from"])
        window_end = parse_timestamp(cursor["window_end"])
        if window_from is None or window_end is None:
            raise FeedbackSyncError("invalid steady cursor", code="invalid_sync_cursor", retryable=False)
        run_id = self.repository.start_sync_run(run_kind="steady", source_stream=stream_name, cursor=cursor)
        try:
            page = self.source.fetch_feedbacks_page(
                date_from_ts=int(window_from.timestamp()),
                date_to_ts=int(window_end.timestamp()),
                is_answered=is_answered,
                take=self.page_size,
                skip=int(cursor.get("skip") or 0),
            )
            upserted = 0
            enqueued = 0
            for row in page.rows:
                if not _inside_history_window(row):
                    continue
                outcome = self.repository.upsert_feedback(
                    row,
                    source_stream=stream_name,
                    run_kind="steady",
                    sync_run_id=run_id,
                )
                upserted += int(outcome["is_new"] or outcome["content_changed"] or outcome["observation_changed"])
                if outcome["auto_enqueue"]:
                    try:
                        self.repository.enqueue_processing(
                            outcome["feedback_id"],
                            content_version=outcome["content_version"],
                            trigger_source="steady_sync",
                            actor_id="wb-feedback-sync",
                        )
                        enqueued += 1
                    except AutoanswersRuntimeError as enqueue_error:
                        if enqueue_error.code not in {"master_switch_off", "emergency_force_off"}:
                            raise
            completed_window = not page.has_more
            if completed_window:
                next_cursor: dict[str, Any] = {}
                watermark = iso_utc(window_end)
            else:
                cursor["skip"] = int(cursor.get("skip") or 0) + page.take
                next_cursor = cursor
                watermark = stored["watermark_at"] if stored else None
            self.repository.save_sync_cursor(
                stream_key,
                cursor=next_cursor,
                watermark_at=watermark,
                successful=completed_window,
            )
            self.repository.finish_sync_run(
                run_id,
                state="succeeded",
                discovered_count=len(page.rows),
                upserted_count=upserted,
                cursor=next_cursor,
            )
            return {
                "run_id": run_id,
                "stream": stream_name,
                "window_complete": completed_window,
                "rows": len(page.rows),
                "upserted": upserted,
                "enqueued": enqueued,
                "cursor": next_cursor,
            }
        except Exception as exc:
            error = self._map_error(exc)
            self.repository.finish_sync_run(
                run_id,
                state="retryable_error" if error.retryable else "terminal_error",
                discovered_count=0,
                upserted_count=0,
                cursor=cursor,
                error_code=error.code,
            )
            raise error from exc

    def reconcile_archive_tick(
        self,
        *,
        skip: int | None = None,
        resume_cursor: bool = False,
    ) -> dict[str, Any]:
        stream_key = "wb_feedback_archive"
        stored = self.repository.sync_cursor(stream_key) if resume_cursor else None
        cursor = dict(stored["cursor"]) if stored else {"skip": 0, "complete": False}
        if skip is not None:
            cursor = {"skip": max(0, int(skip)), "complete": False}
        if bool(cursor.get("complete")):
            return {"rows": 0, "upserted": 0, "cursor": cursor}
        current_skip = max(0, int(cursor.get("skip") or 0))
        run_id = self.repository.start_sync_run(
            run_kind="reconciliation", source_stream="archive", cursor={"skip": current_skip}
        )
        try:
            page = self.source.fetch_archive_page(take=self.page_size, skip=current_skip)
            upserted = 0
            for row in page.rows:
                if not _inside_history_window(row):
                    continue
                outcome = self.repository.upsert_feedback(
                    row, source_stream="archive", run_kind="reconciliation", sync_run_id=run_id
                )
                upserted += int(outcome["is_new"] or outcome["content_changed"] or outcome["observation_changed"])
            cursor = {"skip": page.skip + page.take if page.has_more else 0, "complete": not page.has_more}
            if resume_cursor and skip is None:
                self.repository.save_sync_cursor(
                    stream_key,
                    cursor=cursor,
                    successful=not page.has_more,
                )
            self.repository.finish_sync_run(
                run_id,
                state="succeeded",
                discovered_count=len(page.rows),
                upserted_count=upserted,
                cursor=cursor,
            )
            return {"run_id": run_id, "rows": len(page.rows), "upserted": upserted, "cursor": cursor}
        except Exception as exc:
            error = self._map_error(exc)
            self.repository.finish_sync_run(
                run_id,
                state="retryable_error" if error.retryable else "terminal_error",
                discovered_count=0,
                upserted_count=0,
                cursor={"skip": current_skip},
                error_code=error.code,
            )
            raise error from exc

    def unanswered_reconciliation_status(self) -> dict[str, Any]:
        remote = self.source.count_unanswered()
        local = self.repository.local_unanswered_count()
        return {"remote_unanswered": remote, "local_unanswered": local, "matches": remote == local}

    @staticmethod
    def _map_error(exc: Exception) -> FeedbackSyncError:
        if isinstance(exc, FeedbackSyncError):
            return exc
        if isinstance(exc, WbAutoanswersHttpError):
            retryable = exc.status_code == 429 or exc.status_code >= 500
            return FeedbackSyncError(
                str(exc),
                code=f"wb_http_{exc.status_code}",
                retryable=retryable,
                retry_after_seconds=exc.retry_after_seconds,
            )
        if isinstance(exc, WbAutoanswersTransportError):
            return FeedbackSyncError(str(exc), code="wb_transport", retryable=True)
        if isinstance(exc, AutoanswersRuntimeError):
            return FeedbackSyncError(str(exc), code=exc.code, retryable=exc.retryable)
        return FeedbackSyncError(str(exc), code="sync_internal_error", retryable=False)
