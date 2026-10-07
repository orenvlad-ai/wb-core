"""Bounded, resumable WB feedback synchronization service."""

from __future__ import annotations

from contextlib import closing
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

    def full_unanswered_inventory_tick(self) -> dict[str, Any]:
        """Reconcile one full official unanswered-list page without a date floor.

        The ordinary overlapping stream is optimized for new work.  This
        independent inventory sweep prevents an old unanswered row from being
        permanently invisible because of a historical backfill boundary.
        """

        now = self._now()
        stream_key = "wb_feedback_full_unanswered_inventory"
        stored = self.repository.sync_cursor(stream_key)
        cursor = dict(stored["cursor"]) if stored else {}
        if not cursor.get("active"):
            cursor = {
                "active": True,
                "skip": 0,
                "window_end": iso_utc(now),
                "started_at": iso_utc(now),
                "remote_count_at_start": int(self.source.count_unanswered()),
                "observed_ids": [],
            }
        window_end = parse_timestamp(cursor.get("window_end"))
        if window_end is None:
            raise FeedbackSyncError(
                "invalid full-inventory cursor",
                code="invalid_full_inventory_cursor",
                retryable=False,
            )
        run_id = self.repository.start_sync_run(
            run_kind="reconciliation",
            source_stream="unanswered_full_inventory",
            cursor=cursor,
        )
        try:
            page = self.source.fetch_feedbacks_page(
                date_from_ts=0,
                date_to_ts=int(window_end.timestamp()),
                is_answered=False,
                take=5000,
                skip=int(cursor.get("skip") or 0),
            )
            upserted = 0
            enqueued = 0
            observed_ids = list(cursor.get("observed_ids") or [])
            for row in page.rows:
                outcome = self.repository.upsert_feedback(
                    row,
                    source_stream="unanswered_full_inventory",
                    run_kind="steady",
                    sync_run_id=run_id,
                )
                observed_ids.append(str(outcome["feedback_id"]))
                upserted += int(
                    outcome["is_new"]
                    or outcome["content_changed"]
                    or outcome["observation_changed"]
                )
                if outcome["auto_enqueue"]:
                    self.repository.enqueue_processing(
                        outcome["feedback_id"],
                        content_version=outcome["content_version"],
                        trigger_source="full_unanswered_inventory",
                        actor_id="wb-full-unanswered-inventory",
                    )
                    enqueued += 1
            if page.has_more:
                next_cursor = {
                    **cursor,
                    "skip": int(cursor.get("skip") or 0) + page.take,
                    "observed_ids": observed_ids,
                }
                successful = False
            else:
                remote_count_at_end = int(self.source.count_unanswered())
                next_cursor = {
                    "active": False,
                    "skip": 0,
                    "completed_at": iso_utc(now),
                    "feedback_ids": sorted(set(observed_ids)),
                    "remote_count_at_end": remote_count_at_end,
                    "coverage_confirmed": len(observed_ids) == len(set(observed_ids))
                    == int(cursor.get("remote_count_at_start") or 0) == remote_count_at_end,
                    "remote_count_at_start": int(
                        cursor.get("remote_count_at_start") or len(page.rows)
                    ),
                    "local_unanswered_after": self.repository.local_unanswered_count(),
                }
                successful = True
            self.repository.save_sync_cursor(
                stream_key,
                cursor=next_cursor,
                watermark_at=iso_utc(window_end),
                successful=successful,
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
                "rows": len(page.rows),
                "upserted": upserted,
                "enqueued": enqueued,
                "window_complete": successful,
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

    def reconcile_absent_unanswered_tick(self, *, batch_size: int = 2) -> dict[str, Any]:
        """Resolve a bounded local tail by detail GET, never by list absence."""
        inventory = self.repository.sync_cursor("wb_feedback_full_unanswered_inventory")
        observed = dict((inventory or {}).get("cursor") or {})
        completed = parse_timestamp(observed.get("completed_at"))
        if not observed.get("coverage_confirmed") or not completed or completed < self._now() - timedelta(minutes=15):
            return {"checked": 0, "resolved": 0, "inventory_pending": True}
        stream = "wb_feedback_absent_unanswered_details"
        saved = self.repository.sync_cursor(stream)
        cursor = dict((saved or {}).get("cursor") or {})
        # The rotating pass survives repeated inventories, so an unresolved
        # early row cannot starve the rest of the local tail. Missing details
        # remain unresolved and are revisited after the pass ends.
        after = str(cursor.get("after") or "")
        official = set(observed.get("feedback_ids") or [])
        with closing(self.repository._connect()) as conn:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute("""SELECT feedback_id FROM sheet_vitrina_v1_wb_feedbacks
                WHERE COALESCE(answer_text,'')='' AND COALESCE(json_extract(raw_json,'$.state'),'')<>'wbRu'
                  AND feedback_id>? ORDER BY feedback_id LIMIT ?""", (after, min(100, max(1, batch_size)) + len(official))).fetchall()
        selected = [str(row["feedback_id"]) for row in rows if str(row["feedback_id"]) not in official][:max(1, min(100, batch_size))]
        resolved = 0
        for feedback_id in selected:
            try:
                detail = self.source.fetch_detail(feedback_id)
                # Identity and official answer/state are required. An empty or
                # missing detail does not manufacture a resolved local row.
                answer = (detail or {}).get("answer")
                answer_text = str(answer.get("text") or "") if isinstance(answer, dict) else str(answer or "")
                if detail and str(detail.get("id") or "") == feedback_id and (answer_text.strip() or detail.get("state") == "wbRu"):
                    self.repository.upsert_feedback(detail, source_stream="absent_inventory_detail", run_kind="detail_readback")
                    resolved += 1
            except Exception as exc:
                raise self._map_error(exc) from exc
        self.repository.save_sync_cursor(stream, cursor={"inventory_at": observed["completed_at"], "after": selected[-1] if selected else ""}, successful=True)
        return {"checked": len(selected), "resolved": resolved}

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
