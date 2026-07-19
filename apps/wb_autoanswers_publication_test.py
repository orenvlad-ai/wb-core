#!/usr/bin/env python3
"""Free fake-WB tests for publication idempotency and mandatory readback."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from packages.adapters.wb_autoanswers import WbAutoanswersHttpError, WbAutoanswersTransportError
from packages.application.wb_autoanswers_publication import AutoanswersPublicationWorker
from packages.application.wb_autoanswers_runtime import AutoanswersRepository
from apps.wb_autoanswers_runtime_test import MutableClock, feedback, successful_result


class FakeWbTransport:
    def __init__(self) -> None:
        self.write_calls: list[tuple[str, str]] = []
        self.status: int | Exception = 204
        self.readbacks: list[dict | None | Exception] = []

    def create_answer(self, *, feedback_id: str, text: str) -> int:
        self.write_calls.append((feedback_id, text))
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def fetch_detail(self, feedback_id: str) -> dict | None:
        value = self.readbacks.pop(0) if self.readbacks else None
        if isinstance(value, Exception):
            raise value
        return value


class PublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.clock = MutableClock()
        self.env: dict[str, str] = {}
        self.repo = AutoanswersRepository(runtime_dir=Path(self.temp.name), now_factory=self.clock, env=self.env)
        self.transport = FakeWbTransport()
        self.worker = AutoanswersPublicationWorker(
            repository=self.repo, transport=self.transport, worker_id="publication-test"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def approved(self, feedback_id: str = "publish", *, route: str = "public_only") -> tuple[str, str]:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        outcome = self.repo.upsert_feedback(
            feedback(feedback_id), source_stream="unanswered", run_kind="steady"
        )
        job = self.repo.enqueue_processing(
            feedback_id, trigger_source="steady_sync", actor_id="sync"
        )
        self.repo.claim_processing_job(worker_id="ai")
        stored = self.repo.complete_generation(
            job["processing_key"], result=successful_result(route), worker_id="ai"
        )
        detail = self.repo.get_feedback(feedback_id)
        return stored["processing_key"], detail["publications"][0]["publication_key"]

    def test_204_requires_matching_detail_readback(self) -> None:
        _processing, publication = self.approved()
        reply = self.repo.get_feedback("publish")["generated_reply"]
        self.transport.readbacks = [{"id": "publish", "answer": {"text": reply}}]
        first = self.worker.run_once()
        second = self.worker.run_once()
        self.assertEqual(first["state"], "publish_pending_readback")
        self.assertEqual(second["state"], "published")
        self.assertEqual(len(self.transport.write_calls), 1)
        self.assertEqual(self.repo.get_feedback("publish")["publications"][0]["state"], "published")

    def test_204_missing_readback_goes_to_review_without_second_write(self) -> None:
        self.approved()
        self.transport.readbacks = [None]
        self.worker.run_once()
        result = self.worker.run_once()
        self.assertEqual(result["state"], "needs_review")
        self.assertIsNone(self.worker.run_once())
        self.assertEqual(len(self.transport.write_calls), 1)

    def test_different_or_external_readback_goes_to_review(self) -> None:
        self.approved()
        self.transport.readbacks = [{"answer": {"text": "Ответ другого оператора"}}]
        self.worker.run_once()
        result = self.worker.run_once()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(len(self.transport.write_calls), 1)

    def test_ambiguous_timeout_only_reads_back_and_never_repeats_write(self) -> None:
        self.approved()
        reply = self.repo.get_feedback("publish")["generated_reply"]
        self.transport.status = WbAutoanswersTransportError("timeout")
        self.transport.readbacks = [{"answer": {"text": reply}}]
        self.worker.run_once()
        self.worker.run_once()
        self.assertEqual(len(self.transport.write_calls), 1)
        self.assertEqual(self.repo.get_feedback("publish")["processing_status"], "published")

    def test_readback_429_is_not_claimed_while_master_off(self) -> None:
        self.approved()
        reply = self.repo.get_feedback("publish")["generated_reply"]
        self.transport.readbacks = [WbAutoanswersHttpError(429, "limited", retry_after_seconds=1), {"answer": {"text": reply}}]
        self.worker.run_once()
        self.repo.update_settings(master_enabled=False, actor_id="admin")
        first_readback = self.worker.run_once()
        self.assertIsNone(first_readback)
        self.clock.advance(2)
        second_readback = self.worker.run_once()
        self.assertIsNone(second_readback)
        self.assertEqual(len(self.transport.readbacks), 2)
        self.assertEqual(len(self.transport.write_calls), 1)
        self.repo.update_settings(master_enabled=True, actor_id="admin")
        retryable = self.worker.run_once()
        self.assertEqual(retryable["state"], "retryable_error")
        self.clock.advance(2)
        published = self.worker.run_once()
        self.assertEqual(published["state"], "published")
        self.assertEqual(len(self.transport.write_calls), 1)

    def test_off_and_emergency_force_off_block_new_write(self) -> None:
        self.approved()
        self.repo.update_settings(master_enabled=False, actor_id="admin")
        self.assertIsNone(self.worker.run_once())
        self.assertEqual(self.transport.write_calls, [])
        self.repo.update_settings(master_enabled=True, actor_id="admin")
        self.env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
        self.assertIsNone(self.worker.run_once())
        self.assertEqual(self.transport.write_calls, [])

    def test_duplicate_approval_and_worker_ticks_do_not_duplicate_publication(self) -> None:
        processing, publication = self.approved()
        detail = self.repo.get_feedback("publish")
        self.assertEqual(len(detail["publications"]), 1)
        self.transport.readbacks = [{"answer": {"text": detail["generated_reply"]}}]
        self.worker.run_once()
        self.worker.run_once()
        self.assertIsNone(self.worker.run_once())
        self.assertEqual(len(self.transport.write_calls), 1)

    def test_stale_version_or_external_answer_is_quarantined_before_write(self) -> None:
        self.approved()
        self.repo.upsert_feedback(
            feedback("publish", text="Изменённый отзыв"), source_stream="detail", run_kind="reconciliation"
        )
        self.assertIsNone(self.worker.run_once())
        self.assertEqual(self.transport.write_calls, [])
        self.assertEqual(self.repo.get_feedback("publish")["ai_jobs"][0]["state"], "needs_review")

    def test_seller_chat_is_review_only_until_explicit_approval(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        outcome = self.repo.upsert_feedback(feedback("chat"), source_stream="unanswered", run_kind="steady")
        job = self.repo.enqueue_processing("chat", trigger_source="steady_sync", actor_id="sync")
        self.repo.claim_processing_job(worker_id="ai")
        result = successful_result("seller_chat")
        result["final_reply"] = "Здравствуйте. Напишите, пожалуйста, в чат продавца по коду А1234."
        result["case_code"] = "А1234"
        stored = self.repo.complete_generation(job["processing_key"], result=result, worker_id="ai")
        self.assertEqual(stored["state"], "needs_review")
        self.assertEqual(self.repo.get_feedback("chat")["publications"], [])
        publication = self.repo.approve_for_publication(job["processing_key"], actor_id="reviewer")
        self.assertEqual(publication["state"], "approved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
