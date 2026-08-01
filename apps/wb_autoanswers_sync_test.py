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

    def test_force_off_sync_first_is_materialized_by_active_sync_without_content_change(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        readonly_repo = AutoanswersRepository(
            runtime_dir=Path(self.temp.name),
            now_factory=self.clock,
            env={"WB_AUTOANSWERS_FORCE_OFF": "true"},
        )
        active_repo = AutoanswersRepository(
            runtime_dir=Path(self.temp.name),
            now_factory=self.clock,
            env={"WB_AUTOANSWERS_FORCE_OFF": "false"},
        )
        readonly_source = FakeReadSource()
        readonly_source.pages = [[feedback("readonly-won-race")]]
        active_source = FakeReadSource()
        active_source.pages = [
            [feedback("readonly-won-race")],
            [feedback("readonly-won-race")],
        ]
        readonly_service = WbFeedbackSyncService(
            repository=readonly_repo,
            source=readonly_source,
            now_factory=self.clock,
            page_size=2,
        )
        active_service = WbFeedbackSyncService(
            repository=active_repo,
            source=active_source,
            now_factory=self.clock,
            page_size=2,
        )

        first = readonly_service.steady_sync_tick(is_answered=False)
        second = active_service.steady_sync_tick(is_answered=False)
        replay = active_service.steady_sync_tick(is_answered=False)

        self.assertEqual(first["enqueued"], 0)
        self.assertEqual(second["upserted"], 0)
        self.assertEqual(second["enqueued"], 1)
        self.assertEqual(replay["enqueued"], 0)
        self.assertEqual(
            len(active_repo.get_feedback("readonly-won-race")["ai_jobs"]),
            1,
        )

    def test_active_sync_adopts_pre_fix_null_epoch_observed_under_current_settings(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        self.clock.advance(1)
        observed = self.repo.upsert_feedback(
            feedback("pre-fix-null-epoch"),
            source_stream="unanswered",
            run_kind="steady",
        )
        self.assertIsNotNone(observed["auto_eligible_epoch"])
        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_feedbacks
                SET auto_eligible_epoch=NULL
                WHERE feedback_id='pre-fix-null-epoch'
                """
            )

        self.source.pages = [[feedback("pre-fix-null-epoch")]]
        replay = self.service.steady_sync_tick(is_answered=False)

        self.assertEqual(replay["upserted"], 0)
        self.assertEqual(replay["enqueued"], 1)
        detail = self.repo.get_feedback("pre-fix-null-epoch")
        with self.repo.transaction() as conn:
            eligible_epoch = conn.execute(
                """
                SELECT auto_eligible_epoch
                FROM sheet_vitrina_v1_wb_feedbacks
                WHERE feedback_id='pre-fix-null-epoch'
                """
            ).fetchone()[0]
        self.assertEqual(eligible_epoch, self.repo.settings().enable_epoch)
        self.assertEqual(len(detail["ai_jobs"]), 1)

    def test_active_sync_does_not_adopt_null_epoch_seen_before_current_settings(self) -> None:
        self.source.pages = [[feedback("owner-off-null-epoch")]]
        first = self.service.steady_sync_tick(is_answered=False)
        self.assertEqual(first["enqueued"], 0)
        self.clock.advance(1)
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        self.source.pages = [[feedback("owner-off-null-epoch")]]

        replay = self.service.steady_sync_tick(is_answered=False)

        self.assertEqual(replay["enqueued"], 0)
        detail = self.repo.get_feedback("owner-off-null-epoch")
        with self.repo.transaction() as conn:
            eligible_epoch = conn.execute(
                """
                SELECT auto_eligible_epoch
                FROM sheet_vitrina_v1_wb_feedbacks
                WHERE feedback_id='owner-off-null-epoch'
                """
            ).fetchone()[0]
        self.assertIsNone(eligible_epoch)
        self.assertEqual(detail["ai_jobs"], [])

    def test_manual_mode_steady_sync_never_creates_ai_jobs(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
        self.source.pages = [[feedback("manual-new")], [feedback("manual-new")]]
        first = self.service.steady_sync_tick(is_answered=False)
        second = self.service.steady_sync_tick(is_answered=False)
        self.assertEqual(first["enqueued"], 0)
        self.assertEqual(second["enqueued"], 0)
        self.assertEqual(self.repo.get_feedback("manual-new")["ai_jobs"], [])

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

    def test_full_unanswered_inventory_has_no_history_floor_and_materializes_old_rows(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        old = feedback("old-unanswered")
        old["createdDate"] = "2025-08-04T10:00:00Z"
        self.source.remote_unanswered = 1
        self.source.pages = [[old]]
        result = self.service.full_unanswered_inventory_tick()
        self.assertTrue(result["window_complete"])
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(self.source.calls[-1]["date_from_ts"], 0)
        self.assertEqual(self.source.calls[-1]["take"], 5000)
        self.assertEqual(len(self.repo.get_feedback("old-unanswered")["ai_jobs"]), 1)
        cursor = self.repo.sync_cursor("wb_feedback_full_unanswered_inventory")
        self.assertFalse(cursor["cursor"]["active"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
