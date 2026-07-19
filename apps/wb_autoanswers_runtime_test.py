#!/usr/bin/env python3
"""Free local checks for the WB autoanswers durable runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from packages.application.wb_autoanswers_runtime import (
    AutoanswersRepository,
    AutoanswersRuntimeError,
    content_version_hash,
    wb_observation_hash,
)


def feedback(
    feedback_id: str,
    *,
    text: str = "Хороший товар",
    answer: str = "",
    photo_query: str = "a=1",
) -> dict:
    return {
        "id": feedback_id,
        "createdDate": "2026-07-20T10:00:00Z",
        "text": text,
        "pros": "удобный",
        "cons": "",
        "productValuation": 5,
        "productDetails": {"nmId": 123, "supplierArticle": "SKU-1", "productName": "Товар"},
        "photoLinks": [
            {
                "fullSize": f"https://cdn.example/photo.jpg?{photo_query}",
                "miniSize": f"https://cdn.example/photo-mini.jpg?{photo_query}",
            }
        ],
        "answer": {"text": answer, "state": "wbRu"} if answer else None,
        "wasViewed": False,
        "orderStatus": "buyout",
    }


def successful_result(route: str = "public_only", **overrides: object) -> dict:
    result = {
        "final_route": route,
        "final_reply": "Спасибо за отзыв!",
        "hard_gates_passed": True,
        "fallback_used": False,
        "media_uncertain": False,
        "node_contract_valid": True,
    }
    result.update(overrides)
    return result


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class RuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.clock = MutableClock()
        self.env: dict[str, str] = {}
        self.repo = AutoanswersRepository(
            runtime_dir=Path(self.temp.name), now_factory=self.clock, env=self.env
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def enable(self, mode: str = "draft_only") -> None:
        self.repo.update_settings(master_enabled=True, mode=mode, actor_id="test-admin")

    def insert_new(self, feedback_id: str = "f-1", **kwargs: object) -> dict:
        return self.repo.upsert_feedback(
            feedback(feedback_id, **kwargs), source_stream="unanswered", run_kind="steady"
        )

    def test_content_and_observation_hashes_are_independent(self) -> None:
        original = feedback("f-1", photo_query="token=old")
        media_token_changed = feedback("f-1", photo_query="token=new")
        viewed_changed = {**original, "wasViewed": True}
        self.assertEqual(content_version_hash(original), content_version_hash(media_token_changed))
        self.assertNotEqual(wb_observation_hash(original), wb_observation_hash(viewed_changed))
        self.assertEqual(content_version_hash(original), content_version_hash(viewed_changed))

    def test_master_default_off_and_emergency_force_off(self) -> None:
        self.assertFalse(self.repo.settings().effective_enabled)
        with self.assertRaisesRegex(AutoanswersRuntimeError, "OFF"):
            self.repo.assert_effective_on(operation="test")
        self.enable()
        self.assertTrue(self.repo.settings().effective_enabled)
        self.env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
        settings = self.repo.settings()
        self.assertTrue(settings.master_enabled)
        self.assertFalse(settings.effective_enabled)
        with self.assertRaisesRegex(AutoanswersRuntimeError, "OFF"):
            self.repo.assert_effective_on(operation="test")

    def test_reenable_does_not_auto_enqueue_reviews_seen_while_off(self) -> None:
        first = self.insert_new("off-review")
        self.assertFalse(first["auto_enqueue"])
        self.enable()
        with self.assertRaisesRegex(AutoanswersRuntimeError, "automatic enable epoch"):
            self.repo.enqueue_processing(
                "off-review", trigger_source="automatic", actor_id="sync"
            )
        self.repo.update_settings(master_enabled=False, actor_id="test-admin")
        self.repo.update_settings(master_enabled=True, actor_id="test-admin")
        with self.assertRaisesRegex(AutoanswersRuntimeError, "automatic enable epoch"):
            self.repo.enqueue_processing(
                "off-review", trigger_source="automatic", actor_id="sync"
            )
        preview = self.repo.preview_backlog(actor_id="test-admin")
        self.assertEqual(preview["count"], 1)
        self.assertEqual(
            self.repo.enqueue_backlog_from_preview(preview["preview_id"], actor_id="test-admin")["enqueued"],
            1,
        )

    def test_reenable_does_not_resume_old_epoch_queue_automatically(self) -> None:
        self.enable()
        self.insert_new("queued-before-off")
        self.repo.enqueue_processing(
            "queued-before-off", trigger_source="automatic", actor_id="sync"
        )
        self.repo.update_settings(master_enabled=False, actor_id="admin")
        self.repo.update_settings(master_enabled=True, actor_id="admin")
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))
        self.assertEqual(
            self.repo.get_feedback("queued-before-off")["ai_jobs"][0]["state"],
            "needs_review",
        )

    def test_backfill_never_auto_enqueues(self) -> None:
        self.enable()
        outcome = self.repo.upsert_feedback(
            feedback("history"), source_stream="archive", run_kind="backfill"
        )
        self.assertFalse(outcome["auto_enqueue"])
        self.assertIsNone(outcome["auto_eligible_epoch"])

    def test_duplicate_sync_and_duplicate_job_are_idempotent(self) -> None:
        self.enable()
        first = self.insert_new()
        second = self.insert_new()
        self.assertTrue(first["is_new"])
        self.assertFalse(second["is_new"])
        self.assertFalse(second["content_changed"])
        job1 = self.repo.enqueue_processing("f-1", trigger_source="automatic", actor_id="sync")
        job2 = self.repo.enqueue_processing("f-1", trigger_source="automatic", actor_id="sync")
        self.assertEqual(job1["processing_key"], job2["processing_key"])

    def test_observation_only_update_does_not_create_new_version(self) -> None:
        self.enable()
        first = self.insert_new()
        changed = feedback("f-1")
        changed["wasViewed"] = True
        second = self.repo.upsert_feedback(changed, source_stream="detail", run_kind="reconciliation")
        self.assertEqual(first["content_version"], second["content_version"])
        self.assertFalse(second["content_changed"])
        self.assertTrue(second["observation_changed"])

    def test_query_only_media_change_updates_source_not_version(self) -> None:
        self.enable()
        first = self.insert_new(photo_query="sig=one")
        second = self.insert_new(photo_query="sig=two")
        self.assertEqual(first["content_version"], second["content_version"])
        detail = self.repo.get_feedback("f-1")
        self.assertIn("sig=two", detail["media"][0]["source_full_url"])

    def _claim_and_complete(self, mode: str, route: str, **overrides: object) -> dict:
        self.enable(mode)
        outcome = self.insert_new(f"{mode}-{route}")
        self.assertTrue(outcome["auto_enqueue"])
        job = self.repo.enqueue_processing(
            outcome["feedback_id"], trigger_source="automatic", actor_id="sync"
        )
        claimed = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(claimed["processing_key"], job["processing_key"])
        return self.repo.complete_generation(
            job["processing_key"], result=successful_result(route, **overrides), worker_id="worker"
        )

    def test_three_modes_and_seller_chat_review_only(self) -> None:
        with self.subTest("draft_only"):
            row = self._claim_and_complete("draft_only", "public_only")
            self.assertEqual(row["state"], "generated")

        for mode, route, expected in (
            ("auto_safe", "public_only", "approved"),
            ("auto_safe", "wb_return", "approved"),
            ("auto_safe", "wb_support", "approved"),
            ("auto_safe", "unknown_route", "needs_review"),
            ("auto_all", "unknown_route", "approved"),
            ("auto_all", "seller_chat", "needs_review"),
        ):
            with self.subTest(mode=mode, route=route):
                with TemporaryDirectory() as directory:
                    clock = MutableClock()
                    repo = AutoanswersRepository(runtime_dir=Path(directory), now_factory=clock, env={})
                    repo.update_settings(master_enabled=True, mode=mode, actor_id="admin")
                    outcome = repo.upsert_feedback(
                        feedback(f"{mode}-{route}"), source_stream="unanswered", run_kind="steady"
                    )
                    job = repo.enqueue_processing(
                        outcome["feedback_id"], trigger_source="automatic", actor_id="sync"
                    )
                    repo.claim_processing_job(worker_id="worker")
                    stored = repo.complete_generation(
                        job["processing_key"], result=successful_result(route), worker_id="worker"
                    )
                    self.assertEqual(stored["state"], expected)

    def test_fallback_media_uncertainty_and_invalid_contract_need_review(self) -> None:
        for field in ("fallback_used", "media_uncertain"):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                repo = AutoanswersRepository(runtime_dir=Path(directory), now_factory=MutableClock(), env={})
                repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
                outcome = repo.upsert_feedback(feedback(field), source_stream="unanswered", run_kind="steady")
                job = repo.enqueue_processing(outcome["feedback_id"], trigger_source="automatic", actor_id="sync")
                repo.claim_processing_job(worker_id="worker")
                stored = repo.complete_generation(
                    job["processing_key"], result=successful_result(**{field: True}), worker_id="worker"
                )
                self.assertEqual(stored["state"], "needs_review")

    def test_stale_version_and_external_answer_are_not_claimed(self) -> None:
        self.enable()
        self.insert_new()
        self.repo.enqueue_processing("f-1", trigger_source="automatic", actor_id="sync")
        self.repo.upsert_feedback(
            feedback("f-1", text="Изменённый текст"), source_stream="detail", run_kind="reconciliation"
        )
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))
        detail = self.repo.get_feedback("f-1")
        self.assertEqual(detail["ai_jobs"][0]["state"], "needs_review")

        self.insert_new("answered-later")
        self.repo.enqueue_processing("answered-later", trigger_source="automatic", actor_id="sync")
        self.repo.upsert_feedback(
            feedback("answered-later", answer="Внешний ответ"),
            source_stream="detail",
            run_kind="reconciliation",
        )
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))

    def test_expired_lease_is_reclaimed_once_under_concurrency(self) -> None:
        self.enable()
        self.insert_new()
        self.repo.enqueue_processing("f-1", trigger_source="automatic", actor_id="sync")
        first = self.repo.claim_processing_job(worker_id="crashed", lease_seconds=2)
        self.assertIsNotNone(first)
        self.clock.advance(3)
        claims: list[dict | None] = []

        def claim(worker: str) -> None:
            claims.append(self.repo.claim_processing_job(worker_id=worker, lease_seconds=10))

        threads = [threading.Thread(target=claim, args=(f"w-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_budget_hard_cap_includes_reservations(self) -> None:
        self.repo.update_settings(
            master_enabled=True,
            daily_cap_usd="1.40",
            monthly_cap_usd="5.00",
            actor_id="admin",
        )
        for feedback_id in ("one", "two"):
            self.insert_new(feedback_id)
            self.repo.enqueue_processing(feedback_id, trigger_source="automatic", actor_id="sync")
        self.assertIsNotNone(self.repo.claim_processing_job(worker_id="w1"))
        self.assertTrue(self.repo.budget_status()["warning"])
        with self.assertRaisesRegex(AutoanswersRuntimeError, "daily"):
            self.repo.claim_processing_job(worker_id="w2")

    def test_local_list_defaults_to_50_and_supports_server_pagination_filters(self) -> None:
        for index in range(55):
            row = feedback(f"row-{index:02d}")
            row["createdDate"] = f"2026-07-{(index % 20) + 1:02d}T10:00:00Z"
            row["productValuation"] = 1 if index % 2 else 5
            self.repo.upsert_feedback(row, source_stream="backfill", run_kind="backfill")
        first = self.repo.list_feedbacks()
        second = self.repo.list_feedbacks(page=2)
        filtered = self.repo.list_feedbacks(filters={"rating": 1})
        self.assertEqual(len(first["items"]), 50)
        self.assertEqual(len(second["items"]), 5)
        self.assertTrue(first["has_more"])
        self.assertTrue(all(item["productValuation"] == 1 for item in filtered["items"]))
        same_day = self.repo.list_feedbacks(
            filters={"date_from": "2026-07-20", "date_to": "2026-07-20"}
        )
        self.assertEqual({item["createdDate"][:10] for item in same_day["items"]}, {"2026-07-20"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
