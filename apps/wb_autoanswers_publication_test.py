#!/usr/bin/env python3
"""Free fake-WB tests for publication idempotency and mandatory readback."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from packages.adapters.wb_autoanswers import WbAutoanswersHttpError, WbAutoanswersTransportError
from packages.application.wb_autoanswers_publication import AutoanswersPublicationWorker
from packages.application.wb_autoanswers_runtime import AutoanswersRepository, AutoanswersRuntimeError, final_reply_hash
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

    @staticmethod
    def empty_feedback(feedback_id: str, *, created_at: str) -> dict:
        row = feedback(feedback_id, text="")
        row["pros"] = ""
        row["cons"] = ""
        row["photoLinks"] = []
        row["productValuation"] = 5
        row["createdDate"] = created_at
        return row

    def manual_reviewed(self, feedback_id: str = "manual", *, route: str = "public_only") -> dict:
        self.env["WB_CORE_WEB_AUTH_USERNAME"] = "reviewer"
        self.repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
        self.repo.upsert_feedback(feedback(feedback_id), source_stream="unanswered", run_kind="steady")
        job = self.repo.enqueue_manual_processing(feedback_id, content_version=1, actor_id="reviewer")
        self.repo.claim_processing_job(worker_id="ai")
        result = successful_result(route)
        if route == "seller_chat":
            result["final_reply"] = "Здравствуйте. Напишите в чат продавца по коду WB-A123."
            result["case_code"] = "WB-A123"
        stored = self.repo.complete_generation(job["processing_key"], result=result, worker_id="ai")
        reviewed = self.repo.save_manual_reply_review(
            job["processing_key"],
            reply=result["final_reply"],
            guard_passed=True,
            guard_errors=[],
            actor_id="reviewer",
        )
        self.assertIn(stored["state"], {"generated", "needs_review"})
        return reviewed

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
        self.assertEqual(self.repo.get_feedback("publish")["answer"]["text"], reply)
        self.assertEqual(self.repo.local_unanswered_count(), 0)

        audit_types = {
            item["event_type"]
            for item in self.repo.get_feedback("publish")["audit"]
        }
        self.assertIn("feedback_publication_readback_observed", audit_types)

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

    def test_readback_429_reconciles_get_only_while_master_off(self) -> None:
        self.approved()
        reply = self.repo.get_feedback("publish")["generated_reply"]
        self.transport.readbacks = [WbAutoanswersHttpError(429, "limited", retry_after_seconds=1), {"answer": {"text": reply}}]
        self.worker.run_once()
        self.repo.update_settings(master_enabled=False, actor_id="admin")
        first_readback = self.worker.run_once()
        self.assertEqual(first_readback["state"], "retryable_error")
        self.clock.advance(2)
        second_readback = self.worker.run_once()
        self.assertEqual(second_readback["state"], "published")
        self.assertEqual(len(self.transport.readbacks), 0)
        self.assertEqual(len(self.transport.write_calls), 1)

    def test_legacy_rating_publication_waits_for_content_through_confirmed_readback(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        self.repo.upsert_feedback(
            self.empty_feedback("rating-ready", created_at="2026-07-21T11:00:00Z"),
            source_stream="unanswered",
            run_kind="steady",
        )
        rating_job = self.repo.enqueue_processing(
            "rating-ready", trigger_source="steady_sync", actor_id="sync"
        )
        self.repo.claim_processing_job(worker_id="ai")
        self.repo.complete_rating_only_template(rating_job["processing_key"], worker_id="ai")
        legacy_publication = self.repo.get_feedback("rating-ready")["publications"][0]
        self.assertEqual(legacy_publication["state"], "approved")

        content = feedback("content-first", text="Содержательный отзыв")
        content["createdDate"] = "2026-07-18T10:00:00Z"
        self.repo.upsert_feedback(content, source_stream="archive", run_kind="backfill")
        preview = self.repo.preview_mode_transition(
            "auto_all", actor_id="admin", run_max_usd="1.00"
        )
        self.repo.apply_mode_transition(
            "auto_all", actor_id="admin", preview_id=preview["preview_id"]
        )
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)

        # The already-approved empty review is intentionally not claimable in
        # the new run while content still has an automatic next step.
        self.assertIsNone(self.repo.claim_publication_job(worker_id="publication"))
        content_job = self.repo.claim_processing_job(worker_id="ai")
        self.assertEqual(content_job["feedback_id"], "content-first")
        self.repo.settle_budget(content_job["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            content_job["processing_key"], result=successful_result(), worker_id="ai"
        )
        content_publication = self.repo.claim_publication_job(worker_id="publication")
        self.assertEqual(content_publication["feedback_id"], "content-first")
        started = self.repo.begin_publication_write(
            content_publication["publication_key"], worker_id="publication"
        )
        self.repo.record_publication_transport(
            content_publication["publication_key"],
            attempt_id=started["attempt_id"],
            outcome="http_response",
            http_status=204,
            worker_id="publication",
        )
        readback = self.repo.claim_publication_job(worker_id="publication")
        self.assertEqual(readback["action"], "readback")
        self.assertEqual(readback["feedback_id"], "content-first")
        self.repo.record_publication_readback(
            readback["publication_key"],
            answer_text=readback["exact_reply"],
            worker_id="publication",
        )

        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        rating_publication = self.repo.claim_publication_job(worker_id="publication")
        self.assertEqual(rating_publication["feedback_id"], "rating-ready")
        self.assertEqual(rating_publication["publication_key"], legacy_publication["publication_key"])

    def test_publication_claim_uses_content_rating_buckets_before_rating_only(self) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="manual",
            max_materialized_processing_jobs=20,
            actor_id="admin",
        )
        content_specs = (
            ("content-5", 5, "2026-07-25T10:00:00Z"),
            ("content-2", 2, "2026-07-24T10:00:00Z"),
            ("content-1", 1, "2026-07-20T10:00:00Z"),
            ("content-4", 4, "2026-07-23T10:00:00Z"),
            ("content-3", 3, "2026-07-22T10:00:00Z"),
        )
        for feedback_id, rating, created_at in content_specs:
            row = feedback(feedback_id, text=f"Содержательный отзыв {rating}")
            row["productValuation"] = rating
            row["createdDate"] = created_at
            self.repo.upsert_feedback(
                row,
                source_stream="archive",
                run_kind="backfill",
            )
        self.repo.upsert_feedback(
            self.empty_feedback("rating-only", created_at="2026-07-26T10:00:00Z"),
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "auto_all",
            actor_id="admin",
            run_max_usd="10.00",
        )
        self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)

        expected_content = [f"content-{rating}" for rating in range(1, 6)]
        publication_order: list[str] = []
        for feedback_id in expected_content:
            self.repo.reconcile_policy_sweep_once(
                worker_id="reconcile",
                batch_size=25,
            )
            claimed = self.repo.claim_processing_job(worker_id="ai")
            self.assertEqual(claimed["feedback_id"], feedback_id)
            self.repo.settle_budget(
                claimed["processing_key"],
                actual_cost_usd="0.01",
            )
            self.repo.complete_generation(
                claimed["processing_key"],
                result=successful_result(),
                worker_id="ai",
            )
            publication = self.repo.claim_publication_job(worker_id="publication")
            self.assertEqual(publication["action"], "write")
            publication_order.append(str(publication["feedback_id"]))
            started = self.repo.begin_publication_write(
                publication["publication_key"],
                worker_id="publication",
            )
            self.repo.record_publication_transport(
                publication["publication_key"],
                attempt_id=started["attempt_id"],
                outcome="http_response",
                http_status=204,
                worker_id="publication",
            )
            readback = self.repo.claim_publication_job(worker_id="publication")
            self.assertEqual(readback["action"], "readback")
            self.assertEqual(readback["feedback_id"], feedback_id)
            self.repo.record_publication_readback(
                readback["publication_key"],
                answer_text=readback["exact_reply"],
                worker_id="publication",
            )
        self.assertEqual(publication_order, expected_content)

        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        rating_job = self.repo.claim_processing_job(worker_id="ai")
        self.assertEqual(rating_job["feedback_id"], "rating-only")
        self.repo.complete_rating_only_template(
            rating_job["processing_key"],
            worker_id="ai",
        )
        rating_publication = self.repo.claim_publication_job(worker_id="publication")
        self.assertEqual(rating_publication["feedback_id"], "rating-only")

    def test_off_and_emergency_force_off_block_new_write(self) -> None:
        self.approved()
        self.repo.update_settings(master_enabled=False, actor_id="admin")
        self.assertIsNone(self.worker.run_once())
        self.assertEqual(self.transport.write_calls, [])
        self.repo.update_settings(master_enabled=True, actor_id="admin")
        self.env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
        self.assertIsNone(self.worker.run_once())
        self.assertEqual(self.transport.write_calls, [])

    def test_manual_publication_requires_confirmation_and_is_idempotent(self) -> None:
        reviewed = self.manual_reviewed()
        with self.assertRaisesRegex(AutoanswersRuntimeError, "confirmation"):
            self.repo.approve_for_publication(reviewed["processing_key"], actor_id="reviewer")
        publication = self.repo.approve_for_publication(
            reviewed["processing_key"],
            actor_id="reviewer",
            confirmed=True,
            expected_reply_sha256=reviewed["manual_reply_sha256"],
        )
        self.assertEqual(publication["request_source"], "manual")
        with self.assertRaises((AutoanswersRuntimeError, ValueError)):
            self.repo.approve_for_publication(
                reviewed["processing_key"],
                actor_id="reviewer",
                confirmed=True,
                expected_reply_sha256=reviewed["manual_reply_sha256"],
            )
        self.assertEqual(len(self.repo.get_feedback("manual")["publications"]), 1)

    def test_manual_edit_must_be_guarded_again_and_exact_hash_is_bound(self) -> None:
        reviewed = self.manual_reviewed("edited")
        edited = "Здравствуйте. Благодарим за обратную связь."
        with self.assertRaisesRegex(AutoanswersRuntimeError, "hash"):
            self.repo.approve_for_publication(
                reviewed["processing_key"],
                actor_id="reviewer",
                confirmed=True,
                expected_reply_sha256=final_reply_hash(edited),
            )
        guarded = self.repo.save_manual_reply_review(
            reviewed["processing_key"],
            reply=edited,
            guard_passed=True,
            guard_errors=[],
            actor_id="reviewer",
        )
        publication = self.repo.approve_for_publication(
            reviewed["processing_key"],
            actor_id="reviewer",
            confirmed=True,
            expected_reply_sha256=guarded["manual_reply_sha256"],
        )
        self.assertEqual(publication["normalized_reply_sha256"], final_reply_hash(edited))

    def test_manual_existing_answer_mode_change_permission_and_off_block_write(self) -> None:
        reviewed = self.manual_reviewed("gated")
        publication = self.repo.approve_for_publication(
            reviewed["processing_key"],
            actor_id="reviewer",
            confirmed=True,
            expected_reply_sha256=reviewed["manual_reply_sha256"],
        )
        self.repo.update_settings(master_enabled=False, actor_id="admin")
        self.assertIsNone(self.worker.run_once())
        self.assertEqual(self.transport.write_calls, [])
        self.repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
        self.env["WB_CORE_WEB_AUTH_USERNAME"] = "another-user"
        self.assertIsNone(self.worker.run_once())
        detail = self.repo.get_feedback("gated")
        self.assertEqual(detail["publications"][0]["state"], "needs_review")
        self.assertEqual(publication["publication_key"], detail["publications"][0]["publication_key"])

    def test_manual_existing_wb_answer_blocks_approval(self) -> None:
        reviewed = self.manual_reviewed("answered")
        self.repo.upsert_feedback(
            feedback("answered", answer="Внешний ответ"), source_stream="detail", run_kind="reconciliation"
        )
        with self.assertRaisesRegex(AutoanswersRuntimeError, "already has an answer"):
            self.repo.approve_for_publication(
                reviewed["processing_key"],
                actor_id="reviewer",
                confirmed=True,
                expected_reply_sha256=reviewed["manual_reply_sha256"],
            )

    def test_official_processed_without_answer_blocks_publication_write(self) -> None:
        self.approved("processed-without-answer")
        self.repo.upsert_feedback(
            {**feedback("processed-without-answer"), "state": "wbRu"},
            source_stream="answered",
            run_kind="reconciliation",
        )

        self.assertIsNone(self.worker.run_once())
        self.assertEqual(self.transport.write_calls, [])
        self.assertEqual(self.repo.operational_status()["claimable_publication_writes"], 0)

    def test_manual_publication_is_quarantined_if_mode_changes_before_write(self) -> None:
        reviewed = self.manual_reviewed("mode-changed")
        self.repo.approve_for_publication(
            reviewed["processing_key"],
            actor_id="reviewer",
            confirmed=True,
            expected_reply_sha256=reviewed["manual_reply_sha256"],
        )
        self.repo.update_settings(mode="draft_only", actor_id="admin")
        self.assertIsNone(self.worker.run_once())
        self.assertEqual(self.transport.write_calls, [])
        self.assertEqual(
            self.repo.get_feedback("mode-changed")["publications"][0]["state"],
            "needs_review",
        )

    def test_manual_seller_chat_requires_review_and_exact_single_case_code(self) -> None:
        reviewed = self.manual_reviewed("manual-chat", route="seller_chat")
        publication = self.repo.approve_for_publication(
            reviewed["processing_key"],
            actor_id="reviewer",
            confirmed=True,
            expected_reply_sha256=reviewed["manual_reply_sha256"],
        )
        self.assertEqual(publication["state"], "approved")
        self.transport.readbacks = [{"answer": {"text": reviewed["manual_reply"]}}]
        self.worker.run_once()
        result = self.worker.run_once()
        self.assertEqual(result["state"], "published")
        self.assertEqual(len(self.transport.write_calls), 1)

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

    def test_seller_chat_is_transformed_to_safe_public_without_operator(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        outcome = self.repo.upsert_feedback(feedback("chat"), source_stream="unanswered", run_kind="steady")
        job = self.repo.enqueue_processing("chat", trigger_source="steady_sync", actor_id="sync")
        self.repo.claim_processing_job(worker_id="ai")
        result = successful_result("seller_chat")
        result["final_reply"] = "Здравствуйте. Напишите, пожалуйста, в чат продавца по коду А1234."
        result["case_code"] = "А1234"
        stored = self.repo.complete_generation(job["processing_key"], result=result, worker_id="ai")
        self.assertEqual(stored["state"], "approved")
        self.assertEqual(stored["final_route"], "public_only")
        self.assertIsNone(stored["case_code"])
        self.assertNotIn("чат продавца", stored["final_reply"].casefold())
        publications = self.repo.get_feedback("chat")["publications"]
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0]["state"], "approved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
