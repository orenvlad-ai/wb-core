#!/usr/bin/env python3
"""Free fake-transport tests for resumable WB feedback synchronization."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from packages.adapters.wb_autoanswers import FeedbackPage, WbAutoanswersHttpError
from packages.application.wb_autoanswers_sync import FeedbackSyncError, WbFeedbackSyncService
from packages.application.wb_autoanswers_runtime import AutoanswersRepository
from apps.wb_autoanswers_runtime_test import MutableClock, feedback


class FakeReadSource:
    def __init__(self) -> None:
        self.pages: list[list[dict]] = []
        self.archive: list[dict] = []
        self.remote_unanswered = 0
        self.error: Exception | None = None
        self.calls: list[dict] = []

    def fetch_feedbacks_page(self, **kwargs: object) -> FeedbackPage:
        self.calls.append(dict(kwargs))
        if self.error:
            raise self.error
        rows = self.pages.pop(0) if self.pages else []
        take = int(kwargs["take"])
        return FeedbackPage(rows=rows, take=take, skip=int(kwargs["skip"]), has_more=len(rows) == take)

    def fetch_archive_page(self, *, take: int, skip: int) -> FeedbackPage:
        rows = self.archive[skip : skip + take]
        return FeedbackPage(rows=rows, take=take, skip=skip, has_more=skip + take < len(self.archive))

    def fetch_detail(self, feedback_id: str) -> dict | None:
        return None

    def count_unanswered(self) -> int:
        return self.remote_unanswered


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.clock = MutableClock()
        self.repo = AutoanswersRepository(runtime_dir=Path(self.temp.name), now_factory=self.clock, env={})
        self.source = FakeReadSource()
        self.service = WbFeedbackSyncService(
            repository=self.repo,
            source=self.source,
            now_factory=self.clock,
            page_size=2,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_backfill_is_resumable_and_never_enqueues(self) -> None:
        self.repo.update_settings(master_enabled=True, actor_id="admin")
        self.source.pages = [[feedback("history")]]
        result = self.service.initial_backfill_tick(is_answered=False)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["enqueued"], 0)
        self.assertEqual(result["cursor"]["day"], "2026-01-02")
        detail = self.repo.get_feedback("history")
        self.assertEqual(detail["ai_jobs"], [])

    def test_steady_sync_persists_then_enqueues_exactly_once(self) -> None:
        self.repo.update_settings(master_enabled=True, actor_id="admin")
        self.source.pages = [[feedback("new")], [feedback("new")]]
        first = self.service.steady_sync_tick(is_answered=False)
        second = self.service.steady_sync_tick(is_answered=False)
        self.assertEqual(first["enqueued"], 1)
        self.assertEqual(second["enqueued"], 0)
        self.assertEqual(len(self.repo.get_feedback("new")["ai_jobs"]), 1)

    def test_semantic_edit_creates_one_new_processing_version_but_observation_does_not(self) -> None:
        self.repo.update_settings(master_enabled=True, actor_id="admin")
        changed = feedback("edited", text="Новый смысл отзыва")
        observed = {**changed, "wasViewed": True}
        self.source.pages = [[feedback("edited")], [changed], [observed]]
        self.service.steady_sync_tick(is_answered=False)
        self.service.steady_sync_tick(is_answered=False)
        self.service.steady_sync_tick(is_answered=False)
        jobs = self.repo.get_feedback("edited")["ai_jobs"]
        self.assertEqual(len(jobs), 2)
        self.assertEqual({int(item["content_version"]) for item in jobs}, {1, 2})

    def test_switch_off_race_blocks_enqueue_but_not_sync(self) -> None:
        self.source.pages = [[feedback("off")]]
        result = self.service.steady_sync_tick(is_answered=False)
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(result["enqueued"], 0)
        self.assertIsNotNone(self.repo.get_feedback("off"))

    def test_429_does_not_advance_cursor(self) -> None:
        self.source.error = WbAutoanswersHttpError(429, "limited", retry_after_seconds=2)
        with self.assertRaises(FeedbackSyncError) as raised:
            self.service.initial_backfill_tick(is_answered=False)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after_seconds, 2)
        self.assertIsNone(self.repo.sync_cursor("wb_feedback_backfill:unanswered"))

    def test_archive_and_unanswered_reconciliation(self) -> None:
        self.source.archive = [feedback("archived", answer="Уже отвечено")]
        result = self.service.reconcile_archive_tick(resume_cursor=True)
        self.assertEqual(result["upserted"], 1)
        self.assertTrue(self.repo.sync_cursor("wb_feedback_archive")["cursor"]["complete"])
        self.source.remote_unanswered = 0
        status = self.service.unanswered_reconciliation_status()
        self.assertTrue(status["matches"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
