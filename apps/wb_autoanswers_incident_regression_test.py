#!/usr/bin/env python3
"""Incident regressions for bounded spend, lazy queues and zero-cost reviews."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from apps.wb_autoanswers_incident_evidence import collect_evidence
from apps.wb_autoanswers_runtime_test import MutableClock, feedback, successful_result
from packages.application.wb_autoanswers_runtime import (
    AutoanswersRepository,
    AutoanswersRuntimeError,
    rating_only_template,
)
from packages.application.wb_autoanswers_worker import AutoanswersProcessingWorker


class NeverCalled:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"external dependency called for zero-cost template: {name}")


class DowngradeDuringMedia:
    def __init__(self, repository: AutoanswersRepository) -> None:
        self.repository = repository

    def process(self, **_kwargs: object) -> dict:
        self.repository.update_settings(mode="manual", actor_id="admin")
        return {"media_uncertain": False}


def empty_feedback(feedback_id: str, rating: int) -> dict:
    row = feedback(feedback_id, text="")
    row["pros"] = ""
    row["cons"] = ""
    row["productValuation"] = rating
    row["photoLinks"] = []
    return row


class IncidentRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.clock = MutableClock()
        self.repo = AutoanswersRepository(
            runtime_dir=Path(self.temp.name), now_factory=self.clock, env={}
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_owner_templates_are_stable_for_ratings_one_to_five(self) -> None:
        policy_path = (
            Path(__file__).resolve().parents[1]
            / "packages/contracts/wb_autoanswers_rating_only_policy_v2.json"
        )
        self.assertEqual(
            hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            "0b2636321f68524fc0515a3f45ab20daeb3d8276cc07889345dd0a18191e5f00",
        )
        expected_counts = {1: 5, 2: 5, 3: 5, 4: 5, 5: 8}
        for rating, count in expected_counts.items():
            selected = rating_only_template(f"stable-{rating}", rating)
            self.assertEqual(selected, rating_only_template(f"stable-{rating}", rating))
            self.assertEqual(selected["route"], "rating_only_template")
            self.assertEqual(selected["subcategory"], f"rating_{rating}_empty")
            self.assertTrue(selected["reply"].startswith("Здравствуйте"))
            self.assertIn(int(selected["template_id"].rsplit("v", 1)[1]), range(1, count + 1))

    def test_rating_only_worker_never_touches_media_or_openai(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
        for rating in range(1, 6):
            feedback_id = f"empty-{rating}"
            self.repo.upsert_feedback(
                empty_feedback(feedback_id, rating), source_stream="unanswered", run_kind="steady"
            )
            self.repo.enqueue_manual_processing(
                feedback_id, content_version=1, actor_id="reviewer"
            )
        worker = AutoanswersProcessingWorker(
            repository=self.repo,
            bridge=NeverCalled(),
            media_processor=NeverCalled(),
            worker_id="zero-cost-worker",
        )
        for _rating in range(1, 6):
            result = worker.run_once()
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["cost_usd"], 0)
            self.assertEqual(result["route"], "rating_only_template")
        self.assertEqual(self.repo.budget_status()["daily_actual_usd"], 0)
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations").fetchone()[0], 0)

    def test_five_reclassified_publications_remain_review_only_across_policy_epoch(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        publication_keys: list[str] = []
        for index in range(5):
            feedback_id = f"incident-review-{index}"
            self.repo.upsert_feedback(
                empty_feedback(feedback_id, 5),
                source_stream="unanswered",
                run_kind="steady",
            )
            job = self.repo.enqueue_processing(
                feedback_id,
                trigger_source="steady_sync",
                actor_id="sync",
            )
            self.repo.claim_processing_job(worker_id="worker")
            self.repo.complete_rating_only_template(
                job["processing_key"],
                worker_id="worker",
            )
            publication_keys.append(
                self.repo.get_feedback(feedback_id)["publications"][0]["publication_key"]
            )

        with sqlite3.connect(self.repo.db_path) as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_feedbacks
                SET content_classification='content_bearing'
                WHERE feedback_id LIKE 'incident-review-%'
                """
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state='needs_review',
                    regeneration_required=1,
                    regeneration_reason='content_classification_v3_changed',
                    review_reasons_json='["content_classification_v3_changed"]'
                WHERE feedback_id LIKE 'incident-review-%'
                """
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET state='needs_review', last_error_code=NULL
                WHERE feedback_id LIKE 'incident-review-%'
                """
            )

        preview = self.repo.preview_mode_transition(
            "auto_all",
            actor_id="admin",
            run_max_usd="1.00",
        )
        self.assertEqual(preview["counts"]["content_bearing"], 5)
        self.assertEqual(preview["counts"]["needs_review"], 5)
        self.assertEqual(preview["counts"]["requires_openai"], 0)
        applied = self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        reconciled = self.repo.reconcile_policy_sweep_once(
            worker_id="reconcile",
            batch_size=25,
        )
        self.assertEqual(
            reconciled["progress"]["regeneration_review_preserved"],
            5,
        )
        self.assertEqual(self.repo.progress_status()["automatic_content_pending"], 0)

        with sqlite3.connect(self.repo.db_path) as conn:
            jobs = conn.execute(
                """
                SELECT state,policy_epoch,transition_run_id,regeneration_required
                FROM sheet_vitrina_v1_wb_autoanswer_jobs
                WHERE feedback_id LIKE 'incident-review-%'
                """
            ).fetchall()
            publications = conn.execute(
                """
                SELECT publication_key,state,policy_epoch,transition_run_id,
                       write_started_at,attempts,last_error_code
                FROM sheet_vitrina_v1_wb_publication_jobs
                WHERE feedback_id LIKE 'incident-review-%'
                """
            ).fetchall()
        self.assertEqual(len(jobs), 5)
        self.assertEqual(len(publications), 5)
        self.assertEqual(
            {row[0] for row in jobs},
            {"needs_review"},
        )
        self.assertEqual(
            {row[1] for row in jobs},
            {applied["settings"].policy_epoch},
        )
        self.assertEqual(
            {row[2] for row in jobs},
            {applied["sweep"]["transition_run_id"]},
        )
        self.assertEqual({row[3] for row in jobs}, {1})
        self.assertEqual({row[0] for row in publications}, set(publication_keys))
        self.assertEqual({row[1] for row in publications}, {"needs_review"})
        self.assertEqual(
            {row[2] for row in publications},
            {applied["settings"].policy_epoch},
        )
        self.assertEqual(
            {row[3] for row in publications},
            {applied["sweep"]["transition_run_id"]},
        )
        self.assertEqual({row[4] for row in publications}, {None})
        self.assertEqual({row[5] for row in publications}, {0})
        self.assertEqual(
            {row[6] for row in publications},
            {"regeneration_requires_publication_review"},
        )

        replay = self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        self.assertEqual(
            replay["sweep"]["transition_run_id"],
            applied["sweep"]["transition_run_id"],
        )
        replayed_reconciliation = self.repo.reconcile_policy_sweep_once(
            worker_id="reconcile",
            batch_size=25,
        )
        self.assertEqual(replayed_reconciliation["state"], "succeeded")
        self.assertEqual(
            replayed_reconciliation["progress"],
            {"regeneration_review_preserved": 5},
        )
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_publication_jobs
                    WHERE feedback_id LIKE 'incident-review-%'
                    """
                ).fetchone()[0],
                5,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                    WHERE processing_key LIKE 'incident-review-%'
                    """
                ).fetchone()[0],
                0,
            )

    def test_incident_evidence_counts_exact_confirmed_readback_outcome(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        self.repo.upsert_feedback(
            feedback("confirmed-evidence"),
            source_stream="unanswered",
            run_kind="steady",
        )
        job = self.repo.enqueue_processing(
            "confirmed-evidence",
            trigger_source="steady_sync",
            actor_id="sync",
        )
        self.repo.claim_processing_job(worker_id="worker")
        self.repo.settle_budget(job["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            job["processing_key"],
            result=successful_result(),
            worker_id="worker",
        )
        publication = self.repo.claim_publication_job(worker_id="publication")
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
        self.repo.record_publication_readback(
            readback["publication_key"],
            answer_text=readback["exact_reply"],
            worker_id="publication",
        )

        evidence = collect_evidence(Path(self.temp.name), now=self.clock())
        self.assertEqual(evidence["wb_writes_after_run_created"]["attempts"], 1)
        self.assertEqual(evidence["wb_writes_after_run_created"]["confirmed"], 1)
        self.assertEqual(evidence["wb_writes_after_run_created"]["ambiguous"], 0)

    def test_retry_terminal_and_lease_loss_release_reservations(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        for feedback_id in ("retry", "terminal", "lease"):
            self.repo.upsert_feedback(
                feedback(feedback_id), source_stream="unanswered", run_kind="steady"
            )
            self.repo.enqueue_processing(feedback_id, trigger_source="automatic", actor_id="sync")
        retry = self.repo.claim_processing_job(worker_id="w1", lease_seconds=2)
        self.repo.record_processing_retry(
            retry["processing_key"], error_code="OPENAI_HTTP_500", retry_after_seconds=30, worker_id="w1"
        )
        terminal = self.repo.claim_processing_job(worker_id="w2", lease_seconds=2)
        self.repo.record_processing_terminal(
            terminal["processing_key"], error_code="contract_invalid", worker_id="w2"
        )
        lease = self.repo.claim_processing_job(worker_id="w3", lease_seconds=2)
        self.repo.mark_provider_call_started(lease["processing_key"], worker_id="w3")
        self.clock.advance(3)
        released = self.repo.reconcile_stale_reservations()
        self.assertEqual(released, 1)
        with sqlite3.connect(self.repo.db_path) as conn:
            rows = conn.execute(
                "SELECT processing_key,status,actual_cost_usd,released_reason FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations ORDER BY processing_key"
            ).fetchall()
        self.assertEqual({row[1] for row in rows}, {"released"})
        self.assertEqual(sum(float(row[2]) for row in rows), 0)
        self.assertIn("terminal_error_without_usage", {row[3] for row in rows})
        self.assertIn("stale_or_orphaned", {row[3] for row in rows})
        self.assertEqual(lease["state"], "processing")
        self.assertEqual(self.repo.progress_status()["stop_reason"], "budget_state_unknown")
        self.assertIsNone(self.repo.claim_processing_job(worker_id="blocked-after-lease-loss"))

    def test_transition_requires_run_cap_and_materializes_only_bounded_batch(self) -> None:
        for index in range(12):
            self.repo.upsert_feedback(
                feedback(f"lazy-{index}"), source_stream="history", run_kind="backfill"
            )
        with self.assertRaisesRegex(AutoanswersRuntimeError, "requires max USD"):
            self.repo.preview_mode_transition("auto_all", actor_id="admin")
        preview = self.repo.preview_mode_transition(
            "auto_all", actor_id="admin", run_max_usd="0.50"
        )
        self.assertEqual(preview["counts"]["requires_openai"], 12)
        applied = self.repo.apply_mode_transition(
            "auto_all", actor_id="admin", preview_id=preview["preview_id"]
        )
        self.assertEqual(applied["sweep"]["run_max_usd"], "0.50000000")
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope WHERE sweep_id=?",
                    (applied["sweep"]["sweep_id"],),
                ).fetchone()[0],
                12,
            )
        self.assertTrue(self.repo.progress_status()["scope_membership_exact"])
        self.repo.upsert_feedback(
            empty_feedback("outside-preview-scope", 5),
            source_stream="unanswered",
            run_kind="backfill",
        )
        first = self.repo.reconcile_policy_sweep_once(worker_id="sweep", batch_size=25)
        self.assertEqual(first["progress"]["generation_queued"], 5)
        self.assertEqual(self.repo.get_feedback("outside-preview-scope")["ai_jobs"], [])
        second = self.repo.reconcile_policy_sweep_once(worker_id="sweep", batch_size=25)
        self.assertEqual(second["pause_reason"], "processing_queue_depth_limit")
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_jobs").fetchone()[0], 5)

    def test_zero_cost_template_materialization_is_also_queue_bounded(self) -> None:
        for index in range(12):
            self.repo.upsert_feedback(
                empty_feedback(f"empty-lazy-{index}", (index % 5) + 1),
                source_stream="history",
                run_kind="backfill",
            )
        preview = self.repo.preview_mode_transition(
            "auto_safe", actor_id="admin", run_max_paid_reviews=1
        )
        applied = self.repo.apply_mode_transition(
            "auto_safe", actor_id="admin", preview_id=preview["preview_id"]
        )
        first = self.repo.reconcile_policy_sweep_once(worker_id="sweep", batch_size=25)
        self.assertEqual(first["progress"]["generation_queued"], 5)
        second = self.repo.reconcile_policy_sweep_once(worker_id="sweep", batch_size=25)
        self.assertEqual(second["pause_reason"], "processing_queue_depth_limit")
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE policy_epoch=?",
                    (applied["settings"].policy_epoch,),
                ).fetchone()[0],
                5,
            )

    def test_fresh_capped_preview_starts_new_run_in_same_automatic_mode(self) -> None:
        self.repo.upsert_feedback(
            feedback("same-mode-run"), source_stream="history", run_kind="backfill"
        )
        first_preview = self.repo.preview_mode_transition(
            "draft_only", actor_id="admin", run_max_paid_reviews=1
        )
        first = self.repo.apply_mode_transition(
            "draft_only", actor_id="admin", preview_id=first_preview["preview_id"]
        )
        second_preview = self.repo.preview_mode_transition(
            "draft_only", actor_id="admin", run_max_paid_reviews=1
        )
        second = self.repo.apply_mode_transition(
            "draft_only", actor_id="admin", preview_id=second_preview["preview_id"]
        )
        self.assertGreater(second["settings"].policy_epoch, first["settings"].policy_epoch)
        self.assertNotEqual(second["sweep"]["transition_run_id"], first["sweep"]["transition_run_id"])
        replay = self.repo.apply_mode_transition(
            "draft_only", actor_id="admin", preview_id=second_preview["preview_id"]
        )
        self.assertEqual(replay["sweep"]["transition_run_id"], second["sweep"]["transition_run_id"])

    def test_progress_filters_and_cost_reservation_are_observable(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        self.repo.upsert_feedback(feedback("visible"), source_stream="unanswered", run_kind="steady")
        self.repo.enqueue_processing("visible", trigger_source="automatic", actor_id="sync")
        self.repo.claim_processing_job(worker_id="worker")
        progress = self.repo.progress_status()
        budget = self.repo.budget_status()
        filtered = self.repo.list_feedbacks(filters={"system_answer": "processing", "unanswered": True})
        self.assertEqual(progress["processing_now"], 1)
        self.assertEqual(budget["active_reserved_usd"], 0.1)
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(len(filtered["filter_hash"]), 64)

    def test_manual_click_adopts_preserved_old_epoch_job_once(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        self.repo.upsert_feedback(feedback("adopt"), source_stream="unanswered", run_kind="steady")
        original = self.repo.enqueue_processing("adopt", trigger_source="steady_sync", actor_id="sync")
        self.repo.update_settings(mode="manual", actor_id="admin")
        first = self.repo.enqueue_manual_processing("adopt", content_version=1, actor_id="reviewer")
        second = self.repo.enqueue_manual_processing("adopt", content_version=1, actor_id="reviewer")
        self.assertEqual(first["processing_key"], original["processing_key"])
        self.assertEqual(second["processing_key"], original["processing_key"])
        self.assertEqual(first["trigger_source"], "manual_generate")
        self.assertEqual(first["policy_epoch"], self.repo.settings().policy_epoch)
        self.assertEqual(len(self.repo.get_feedback("adopt")["ai_jobs"]), 1)
        self.assertIsNotNone(self.repo.claim_processing_job(worker_id="manual-worker"))

    def test_mode_downgrade_blocks_already_claimed_job_before_paid_boundary(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        self.repo.upsert_feedback(feedback("claimed"), source_stream="unanswered", run_kind="steady")
        job = self.repo.enqueue_processing("claimed", trigger_source="steady_sync", actor_id="sync")
        self.repo.claim_processing_job(worker_id="worker")
        self.repo.update_settings(mode="manual", actor_id="admin")
        with self.assertRaisesRegex(AutoanswersRuntimeError, "epoch is stale"):
            self.repo.assert_processing_execution_allowed(job["processing_key"])

    def test_worker_mode_downgrade_preserves_claim_as_retryable_not_terminal(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        self.repo.upsert_feedback(feedback("race"), source_stream="unanswered", run_kind="steady")
        job = self.repo.enqueue_processing("race", trigger_source="steady_sync", actor_id="sync")
        worker = AutoanswersProcessingWorker(
            repository=self.repo,
            bridge=NeverCalled(),
            media_processor=DowngradeDuringMedia(self.repo),
            worker_id="race-worker",
        )
        with self.assertRaisesRegex(AutoanswersRuntimeError, "epoch is stale"):
            worker.run_once(execution_mode="live")
        stored = self.repo.get_feedback("race")["ai_jobs"][0]
        self.assertEqual(stored["processing_key"], job["processing_key"])
        self.assertEqual(stored["state"], "retryable_error")
        self.assertEqual(stored["last_error_code"], "policy_epoch_stale")
        self.assertEqual(self.repo.budget_status()["active_reserved_usd"], 0)

    def test_failed_role_usage_is_actual_and_reservation_is_released(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        self.repo.upsert_feedback(feedback("partial"), source_stream="unanswered", run_kind="steady")
        job = self.repo.enqueue_processing("partial", trigger_source="steady_sync", actor_id="sync")
        self.repo.claim_processing_job(worker_id="worker")
        self.repo.record_failed_processing_usage(
            job["processing_key"],
            actual_cost_usd="0.03125",
            usage={"classifier": {"input_tokens": 100}},
            role_calls=1,
            error_code="OPENAI_OUTPUT_NOT_JSON",
            worker_id="worker",
        )
        self.repo.record_processing_retry(
            job["processing_key"],
            error_code="OPENAI_OUTPUT_NOT_JSON",
            retry_after_seconds=30,
            worker_id="worker",
        )
        budget = self.repo.budget_status()
        self.assertEqual(budget["daily_actual_usd"], 0.03125)
        self.assertEqual(budget["active_reserved_usd"], 0)
        with sqlite3.connect(self.repo.db_path) as conn:
            event = conn.execute(
                "SELECT actual_cost_usd,role_calls FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events"
            ).fetchone()
            reservation = conn.execute(
                "SELECT status,released_reason FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations"
            ).fetchone()
        self.assertEqual(event, ("0.03125000", 1))
        self.assertEqual(reservation, ("released", "processing_failed_after_usage"))

    def test_incident_adjustment_corrects_only_fake_terminal_reservation(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        for feedback_id in ("fake", "real"):
            self.repo.upsert_feedback(feedback(feedback_id), source_stream="unanswered", run_kind="steady")
            self.repo.enqueue_processing(feedback_id, trigger_source="steady_sync", actor_id="sync")
        with sqlite3.connect(self.repo.db_path) as conn:
            conn.execute("DELETE FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations WHERE version=4")
            for feedback_id, actual, job_actual in (("fake", "1.0", "0"), ("real", "0.2", "0.2")):
                key = f"{feedback_id}|1|1.4.2"
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state='terminal_error',actual_cost_usd=?,last_error_code='node_invalid_json' WHERE processing_key=?",
                    (job_actual, key),
                )
                conn.execute(
                    "INSERT INTO sheet_vitrina_v1_wb_autoanswers_budget_reservations(processing_key,reserved_usd,actual_cost_usd,status,created_at,updated_at) VALUES(?,0,?,'settled',?,?)",
                    (key, actual, "2026-07-20T12:00:00Z", "2026-07-20T12:00:00Z"),
                )
        migrated = AutoanswersRepository(
            runtime_dir=Path(self.temp.name), now_factory=self.clock, env={}
        )
        with sqlite3.connect(migrated.db_path) as conn:
            adjustments = conn.execute(
                "SELECT processing_key,amount_usd FROM sheet_vitrina_v1_wb_autoanswers_budget_adjustments"
            ).fetchall()
        self.assertEqual(adjustments, [("fake|1|1.4.2", "-1.00000000")])
        budget = migrated.budget_status()
        self.assertEqual(budget["daily_actual_usd"], 0.2)
        self.assertEqual(budget["daily_unverified_legacy_usd"], 1.0)
        self.assertEqual(budget["daily_used_and_reserved_usd"], 1.2)

    def test_timeout_and_quota_are_visible_global_paid_processing_latches(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        for feedback_id in ("timeout", "quota", "remaining"):
            self.repo.upsert_feedback(feedback(feedback_id), source_stream="unanswered", run_kind="steady")
            self.repo.enqueue_processing(feedback_id, trigger_source="steady_sync", actor_id="sync")
        timeout = self.repo.claim_processing_job(worker_id="worker")
        self.repo.record_processing_retry(
            timeout["processing_key"], error_code="node_timeout", retry_after_seconds=60, worker_id="worker"
        )
        self.assertEqual(self.repo.progress_status()["stop_reason"], "budget_state_unknown")
        self.repo.record_scheduler_tick(
            errors=[{"stage": "sync", "code": "temporary_sync_failure", "retryable": True}]
        )
        self.assertEqual(self.repo.progress_status()["stop_reason"], "budget_state_unknown")
        self.assertIsNone(self.repo.claim_processing_job(worker_id="blocked"))

        with self.repo.transaction() as conn:
            self.repo._set_stop_reason(conn, None, at=self.clock())
        quota = self.repo.claim_processing_job(worker_id="worker")
        self.repo.record_processing_terminal(
            quota["processing_key"], error_code="OPENAI_INSUFFICIENT_QUOTA", worker_id="worker"
        )
        self.assertEqual(self.repo.progress_status()["stop_reason"], "openai_quota_exhausted")
        self.assertIsNone(self.repo.claim_processing_job(worker_id="blocked"))

    def test_concurrent_success_cannot_clear_unknown_budget_latch(self) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="draft_only",
            global_paid_review_concurrency=2,
            max_inflight_role_calls=2,
            actor_id="admin",
        )
        for feedback_id in ("ambiguous", "successful"):
            self.repo.upsert_feedback(
                feedback(feedback_id), source_stream="unanswered", run_kind="steady"
            )
            self.repo.enqueue_processing(
                feedback_id, trigger_source="steady_sync", actor_id="sync"
            )
        ambiguous = self.repo.claim_processing_job(worker_id="worker-1")
        successful = self.repo.claim_processing_job(worker_id="worker-2")
        self.repo.mark_provider_call_started(
            ambiguous["processing_key"], worker_id="worker-1"
        )
        self.repo.record_processing_retry(
            ambiguous["processing_key"],
            error_code="node_timeout",
            retry_after_seconds=60,
            worker_id="worker-1",
        )
        self.repo.settle_budget(successful["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            successful["processing_key"],
            result={
                "final_route": "public_only",
                "final_reply": "Здравствуйте. Спасибо.",
                "hard_gates_passed": True,
                "fallback_used": False,
                "media_uncertain": False,
                "node_contract_valid": True,
            },
            worker_id="worker-2",
        )
        self.assertEqual(
            self.repo.progress_status()["stop_reason"], "budget_state_unknown"
        )
        self.assertIsNone(self.repo.claim_processing_job(worker_id="blocked"))

    def test_budget_stop_keeps_zero_cost_waiting_behind_automatic_content(self) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="draft_only",
            hourly_cap_usd="0.50",
            actor_id="admin",
        )
        self.repo.upsert_feedback(
            feedback("already-ready"), source_stream="unanswered", run_kind="steady"
        )
        ready = self.repo.enqueue_processing(
            "already-ready", trigger_source="steady_sync", actor_id="sync"
        )
        self.repo.claim_processing_job(worker_id="worker")
        self.repo.settle_budget(ready["processing_key"], actual_cost_usd="0.50")
        self.repo.complete_generation(
            ready["processing_key"],
            result={
                "final_route": "public_only",
                "final_reply": "Здравствуйте. Спасибо.",
                "hard_gates_passed": True,
                "fallback_used": False,
                "media_uncertain": False,
                "node_contract_valid": True,
            },
            worker_id="worker",
        )
        self.repo.upsert_feedback(
            feedback("untouched-paid"), source_stream="history", run_kind="backfill"
        )
        self.repo.upsert_feedback(
            empty_feedback("zero-cost", 5), source_stream="history", run_kind="backfill"
        )
        preview = self.repo.preview_mode_transition(
            "auto_safe", actor_id="admin", run_max_paid_reviews=10
        )
        self.repo.apply_mode_transition(
            "auto_safe", actor_id="admin", preview_id=preview["preview_id"]
        )
        sweep = self.repo.reconcile_policy_sweep_once(worker_id="sweep", batch_size=25)
        self.assertEqual(sweep["pause_reason"], "hourly_budget_reached")
        self.assertTrue(self.repo.get_feedback("already-ready")["publications"])
        self.assertFalse(self.repo.get_feedback("zero-cost")["ai_jobs"])
        self.assertFalse(self.repo.get_feedback("untouched-paid")["ai_jobs"])

    def test_daily_monthly_hourly_review_and_run_caps_have_exact_stop_reasons(self) -> None:
        cases = (
            ("hourly", 0, "hourly_budget_reached"),
            ("daily", 3601, "daily_budget_reached"),
            ("monthly", 86401, "monthly_budget_reached"),
        )
        for name, advance, reason in cases:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                clock = MutableClock()
                repo = AutoanswersRepository(runtime_dir=Path(directory), now_factory=clock, env={})
                repo.update_settings(
                    master_enabled=True,
                    mode="draft_only",
                    hourly_cap_usd="0.10",
                    daily_cap_usd="0.10",
                    monthly_cap_usd="0.10",
                    actor_id="admin",
                )
                for feedback_id in ("first", "second"):
                    repo.upsert_feedback(feedback(feedback_id), source_stream="unanswered", run_kind="steady")
                    repo.enqueue_processing(feedback_id, trigger_source="steady_sync", actor_id="sync")
                first = repo.claim_processing_job(worker_id="worker")
                repo.settle_budget(first["processing_key"], actual_cost_usd="0.10")
                repo.complete_generation(
                    first["processing_key"],
                    result={
                        "final_route": "public_only",
                        "final_reply": "Здравствуйте. Спасибо за отзыв.",
                        "hard_gates_passed": True,
                        "fallback_used": False,
                        "media_uncertain": False,
                        "node_contract_valid": True,
                    },
                    worker_id="worker",
                )
                clock.advance(advance)
                self.assertIsNone(repo.claim_processing_job(worker_id="worker"))
                self.assertEqual(repo.progress_status()["stop_reason"], reason)

        with TemporaryDirectory() as directory:
            clock = MutableClock()
            repo = AutoanswersRepository(runtime_dir=Path(directory), now_factory=clock, env={})
            repo.update_settings(
                master_enabled=True,
                mode="draft_only",
                hourly_cap_usd="5",
                daily_cap_usd="5",
                monthly_cap_usd="50",
                max_paid_reviews_per_hour=1,
                actor_id="admin",
            )
            for feedback_id in ("first", "second"):
                repo.upsert_feedback(feedback(feedback_id), source_stream="unanswered", run_kind="steady")
                repo.enqueue_processing(feedback_id, trigger_source="steady_sync", actor_id="sync")
            first = repo.claim_processing_job(worker_id="worker")
            repo.settle_budget(first["processing_key"], actual_cost_usd="0.01")
            repo.complete_generation(first["processing_key"], result={
                "final_route": "public_only", "final_reply": "Здравствуйте. Спасибо.",
                "hard_gates_passed": True, "fallback_used": False,
                "media_uncertain": False, "node_contract_valid": True,
            }, worker_id="worker")
            self.assertIsNone(repo.claim_processing_job(worker_id="worker"))
            self.assertEqual(repo.progress_status()["stop_reason"], "paid_reviews_hourly_limit")

        with TemporaryDirectory() as directory:
            clock = MutableClock()
            repo = AutoanswersRepository(runtime_dir=Path(directory), now_factory=clock, env={})
            for feedback_id in ("first", "second"):
                repo.upsert_feedback(feedback(feedback_id), source_stream="history", run_kind="backfill")
            preview = repo.preview_mode_transition("auto_all", actor_id="admin", run_max_usd="0.10")
            repo.apply_mode_transition("auto_all", actor_id="admin", preview_id=preview["preview_id"])
            repo.reconcile_policy_sweep_once(worker_id="sweep")
            first = repo.claim_processing_job(worker_id="worker")
            repo.settle_budget(first["processing_key"], actual_cost_usd="0.01")
            repo.complete_generation(first["processing_key"], result={
                "final_route": "public_only", "final_reply": "Здравствуйте. Спасибо.",
                "hard_gates_passed": True, "fallback_used": False,
                "media_uncertain": False, "node_contract_valid": True,
            }, worker_id="worker")
            self.assertIsNone(repo.claim_processing_job(worker_id="worker"))
            self.assertEqual(repo.progress_status()["stop_reason"], "run_budget_reached")


if __name__ == "__main__":
    unittest.main(verbosity=2)
