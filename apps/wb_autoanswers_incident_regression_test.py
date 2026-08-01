#!/usr/bin/env python3
"""Incident regressions for bounded spend, lazy queues and zero-cost reviews."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from apps.wb_autoanswers_runtime_test import MutableClock, feedback, successful_result
from packages.application.wb_autoanswers_coordinator import (
    AutoanswersCoordinator,
    _error_evidence,
)
from packages.application.wb_autoanswers_runtime import (
    AutoanswersRepository,
    AutoanswersRuntimeError,
    autoanswers_settings_revision,
    rating_only_template,
)
from packages.application.sqlite_contention import SQLiteContentionExhausted
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

    def test_exact_five_prefilter_skips_remain_terminal_across_policy_epoch(self) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="draft_only",
            max_materialized_processing_jobs=10,
            actor_id="admin",
        )
        processing_keys: list[str] = []
        for index in range(5):
            feedback_id = f"five-row-skip-{index}"
            self.repo.upsert_feedback(
                feedback(feedback_id),
                source_stream="unanswered",
                run_kind="steady",
            )
            job = self.repo.enqueue_processing(
                feedback_id,
                trigger_source="steady_sync",
                actor_id="sync",
            )
            processing_keys.append(str(job["processing_key"]))
            claimed = self.repo.claim_processing_job(worker_id="worker")
            self.repo.mark_provider_call_started(
                claimed["processing_key"],
                worker_id="worker",
            )
            self.repo.settle_budget(
                claimed["processing_key"],
                actual_cost_usd="0",
            )
            self.repo.complete_skip(
                claimed["processing_key"],
                reason="empty_five_star",
                worker_id="worker",
            )
        with sqlite3.connect(self.repo.db_path) as conn:
            before = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs)
                """
            ).fetchone()

        preview = self.repo.preview_mode_transition(
            "auto_all",
            actor_id="admin",
            run_max_usd="10.00",
        )
        applied = self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        status = self.repo.reconcile_policy_sweep_once(
            worker_id="reconcile",
            batch_size=25,
        )
        self.assertEqual(status["progress"]["skipped_preserved"], 5)
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))
        with sqlite3.connect(self.repo.db_path) as conn:
            rows = conn.execute(
                """
                SELECT j.processing_key,j.state,j.last_error_code,j.policy_epoch,
                       j.transition_run_id,r.status,r.actual_cost_usd,
                       r.provider_call_started_at
                FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                JOIN sheet_vitrina_v1_wb_autoanswers_budget_reservations r
                  ON r.processing_key=j.processing_key
                ORDER BY j.processing_key
                """
            ).fetchall()
            after = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs)
                """
            ).fetchone()
            acknowledgements = conn.execute(
                """
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements
                WHERE sweep_id=? AND outcome='skipped_preserved'
                """,
                (applied["sweep"]["sweep_id"],),
            ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(acknowledgements, 5)
        self.assertEqual([row[0] for row in rows], sorted(processing_keys))
        for row in rows:
            self.assertEqual(row[1], "skipped")
            self.assertEqual(row[2], "empty_five_star")
            self.assertNotEqual(row[3], applied["settings"].policy_epoch)
            self.assertIsNone(row[4])
            self.assertEqual(row[5], "settled")
            self.assertEqual(float(row[6]), 0.0)
            self.assertIsNotNone(row[7])
        self.clock.advance(60)
        restarted = AutoanswersRepository(
            runtime_dir=Path(self.temp.name),
            now_factory=self.clock,
            env={},
        )
        replay = restarted.reconcile_policy_sweep_once(
            worker_id="reconcile-after-restart",
            batch_size=25,
        )
        self.assertIsNone(replay)
        status_after_restart = restarted.reconciliation_status(
            applied["sweep"]["sweep_id"]
        )
        self.assertEqual(
            status_after_restart["cursor"]["acknowledged_total"],
            5,
        )
        self.assertEqual(status_after_restart["cursor"]["rate_per_minute"], 0.0)

    def test_40k_scope_progress_is_bounded_indexed_and_restart_idempotent(
        self,
    ) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="manual",
            max_materialized_processing_jobs=100,
            actor_id="admin",
        )
        self.repo.upsert_feedback(
            feedback("bulk-seed", text="Содержательный отзыв"),
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "draft_only",
            actor_id="admin",
            run_max_usd="500.00",
        )
        applied = self.repo.apply_mode_transition(
            "draft_only",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        sweep_id = str(applied["sweep"]["sweep_id"])
        with self.repo.transaction() as conn:
            conn.execute(
                """
                WITH RECURSIVE
                left_digit(value) AS (
                    VALUES(0) UNION ALL
                    SELECT value+1 FROM left_digit WHERE value<199
                ),
                right_digit(value) AS (
                    VALUES(0) UNION ALL
                    SELECT value+1 FROM right_digit WHERE value<199
                ),
                numbered(value) AS (
                    SELECT left_digit.value*200+right_digit.value+1
                    FROM left_digit CROSS JOIN right_digit
                )
                INSERT INTO sheet_vitrina_v1_wb_feedbacks(
                    feedback_id,created_at_wb,updated_at_wb,content_version,
                    content_version_hash,wb_observation_hash,content_json,
                    observation_json,raw_json,answer_text,rating,nm_id,
                    supplier_article,product_name,brand_name,has_photo,has_video,
                    source_stream,first_seen_at,last_seen_at,sync_status,
                    auto_eligible_epoch,last_sync_run_id,content_classification
                )
                SELECT
                    printf('bulk-%05d',numbered.value),
                    seed.created_at_wb,seed.updated_at_wb,1,
                    printf('bulk-hash-%05d',numbered.value),
                    seed.wb_observation_hash,seed.content_json,
                    seed.observation_json,seed.raw_json,'',2,seed.nm_id,
                    seed.supplier_article,seed.product_name,seed.brand_name,0,0,
                    seed.source_stream,seed.first_seen_at,seed.last_seen_at,
                    seed.sync_status,NULL,NULL,'content_bearing'
                FROM numbered
                CROSS JOIN sheet_vitrina_v1_wb_feedbacks seed
                WHERE seed.feedback_id='bulk-seed'
                """
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_feedback_versions(
                    feedback_id,content_version,content_version_hash,
                    content_json,source_raw_json,created_at
                )
                SELECT feedback_id,content_version,content_version_hash,
                       content_json,raw_json,first_seen_at
                FROM sheet_vitrina_v1_wb_feedbacks
                WHERE feedback_id GLOB 'bulk-[0-9]*'
                """
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_reconciliation_scope(
                    sweep_id,feedback_id,content_version_at_preview,
                    content_version_hash_at_preview,ordinal,
                    content_classification_at_preview
                )
                SELECT ?,feedback_id,content_version,content_version_hash,
                       1+CAST(substr(feedback_id,6) AS INTEGER),'content_bearing'
                FROM sheet_vitrina_v1_wb_feedbacks
                WHERE feedback_id GLOB 'bulk-[0-9]*'
                """,
                (sweep_id,),
            )

        started = time.monotonic()
        first = self.repo.reconcile_policy_sweep_once(
            worker_id="bulk-reconcile-1",
            batch_size=25,
        )
        first_elapsed = time.monotonic() - started
        self.assertLess(first_elapsed, 5.0)
        self.assertEqual(first["cursor"]["membership_total"], 40_001)
        self.assertEqual(first["cursor"]["acknowledged_total"], 25)
        self.assertEqual(first["cursor"]["reconciliation_remaining"], 39_976)
        self.assertEqual(first["cursor"]["action_total"], 25)

        self.clock.advance(60)
        restarted = AutoanswersRepository(
            runtime_dir=Path(self.temp.name),
            now_factory=self.clock,
            env={},
        )
        second = restarted.reconcile_policy_sweep_once(
            worker_id="bulk-reconcile-2",
            batch_size=25,
        )
        self.assertEqual(second["cursor"]["acknowledged_total"], 50)
        self.assertEqual(second["cursor"]["reconciliation_remaining"], 39_951)
        self.assertEqual(second["cursor"]["rate_per_minute"], 25.0)
        with sqlite3.connect(restarted.db_path) as conn:
            acknowledgement_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements
                WHERE sweep_id=?
                """,
                (sweep_id,),
            ).fetchone()[0]
            job_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswer_jobs
                WHERE policy_epoch=?
                """,
                (applied["settings"].policy_epoch,),
            ).fetchone()[0]
            lookup_plan = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT 1
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements
                WHERE sweep_id=? AND feedback_id=?
                  AND content_version=? AND content_version_hash=?
                """,
                (sweep_id, "bulk-00001", 1, "bulk-hash-00001"),
            ).fetchall()
            scope_plan = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT 1
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope
                WHERE sweep_id=? AND feedback_id=?
                  AND content_version_at_preview=?
                  AND content_version_hash_at_preview=?
                """,
                (sweep_id, "bulk-00001", 1, "bulk-hash-00001"),
            ).fetchall()
        self.assertEqual(acknowledgement_count, 50)
        self.assertEqual(job_count, 50)
        self.assertTrue(
            any("INDEX" in str(row[3]).upper() for row in lookup_plan),
            lookup_plan,
        )
        self.assertTrue(
            any("INDEX" in str(row[3]).upper() for row in scope_plan),
            scope_plan,
        )

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

    def test_readonly_process_uses_background_contention_and_reports_exhaustion(
        self,
    ) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="draft_only",
            actor_id="contention-test",
        )
        before_revision = autoanswers_settings_revision(self.repo.settings())
        with patch.dict(
            os.environ,
            {"WB_CORE_SQLITE_OWNER": "wb_autoanswers_readonly.py"},
        ), patch(
            "packages.application.sqlite_contention.DEFAULT_BACKGROUND_TIMEOUT_MS",
            200,
        ):
            probe = self.repo._connect()
            try:
                self.assertEqual(probe._contention_priority, "background")
                self.assertEqual(probe._contention_timeout_ms, 200)
            finally:
                probe.close()
            blocker = sqlite3.connect(
                self.repo.db_path,
                timeout=1,
                isolation_level=None,
            )
            blocker.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaises(SQLiteContentionExhausted) as raised:
                    self.repo.update_settings(
                        hourly_cap_usd="0.60",
                        actor_id="contention-test",
                    )
            finally:
                blocker.rollback()
                blocker.close()
        failure = raised.exception
        self.assertGreaterEqual(failure.wait_ms, 150)
        self.assertGreaterEqual(failure.retries, 1)
        self.assertEqual(
            autoanswers_settings_revision(self.repo.settings()),
            before_revision,
        )
        evidence = _error_evidence("reconciliation", failure)
        self.repo.record_scheduler_tick(errors=[evidence])
        progress = self.repo.progress_status()
        self.assertEqual(progress["stop_reason"], "retry_backoff")
        self.assertEqual(
            progress["stop_details"],
            {
                "code": "sqlite_contention_exhausted",
                "stage": "reconciliation",
                "wait_ms": failure.wait_ms,
                "retry_count": failure.retries,
                "contention_phase": failure.phase,
            },
        )
        self.assertIn(
            "sqlite_contention_exhausted",
            {alert["code"] for alert in progress["stall_alerts"]},
        )

    def test_coordinator_contains_stage_contention_and_keeps_bounded_tick_alive(
        self,
    ) -> None:
        stage_failure = SQLiteContentionExhausted(
            wait_ms=321,
            retries=4,
            phase="begin",
        )
        tick_failure = SQLiteContentionExhausted(
            wait_ms=654,
            retries=5,
            phase="commit",
        )

        class Repository:
            def sync_cursor(_self, _name: str) -> dict:
                return {"cursor": {"tick": 7}}

            def claim_sync_command(_self, *, worker_id: str) -> None:
                self.assertEqual(worker_id, "coordinator-test")
                return None

            def save_sync_cursor(_self, *_args: object, **_kwargs: object) -> None:
                return None

            def refresh_rolling_admissions(
                _self,
                *,
                actor_id: str,
                batch_size: int,
            ) -> None:
                self.assertEqual((actor_id, batch_size), ("coordinator-test", 250))
                raise stage_failure

            def reconcile_stale_reservations(_self) -> int:
                return 2

            def reconcile_policy_sweep_once(
                _self,
                *,
                worker_id: str,
                batch_size: int,
            ) -> dict:
                self.assertEqual((worker_id, batch_size), ("coordinator-test", 25))
                return {"state": "queued"}

            def record_scheduler_tick(
                _self,
                *,
                errors: list[dict],
            ) -> None:
                self.assertEqual(errors[0]["stage"], "rolling_admission")
                raise tick_failure

        class Sync:
            def steady_sync_tick(_self, *, is_answered: bool) -> dict:
                return {"answered": is_answered}

            def initial_backfill_tick(_self, *, is_answered: bool) -> dict:
                return {"backfill_answered": is_answered}

        class Worker:
            def __init__(self, result: str) -> None:
                self.result = result

            def run_once(self) -> dict:
                return {"result": self.result}

        report = AutoanswersCoordinator(
            repository=Repository(),  # type: ignore[arg-type]
            sync_service=Sync(),  # type: ignore[arg-type]
            processing_worker=Worker("processed"),  # type: ignore[arg-type]
            publication_worker=Worker("published"),  # type: ignore[arg-type]
            worker_id="coordinator-test",
        ).run_once()
        self.assertEqual(report["tick"], 8)
        self.assertEqual(report["stale_reservations_released"], 2)
        self.assertEqual(report["processing"], {"result": "processed"})
        self.assertEqual(report["publication"], {"result": "published"})
        self.assertEqual(
            [(item["stage"], item["contention_phase"]) for item in report["errors"]],
            [
                ("rolling_admission", "begin"),
                ("scheduler_tick_write", "commit"),
            ],
        )

    def test_business_throughput_keeps_published_ai_completion_visible(self) -> None:
        item = feedback("throughput-published", text="Нужен ответ")
        item["productValuation"] = 1
        self.repo.upsert_feedback(
            item,
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "auto_all",
            actor_id="admin",
            run_max_usd="1.00",
        )
        self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        job = self.repo.claim_processing_job(worker_id="ai")
        self.repo.settle_budget(job["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            job["processing_key"],
            result=successful_result(),
            worker_id="ai",
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

        throughput = self.repo.progress_status()["business_throughput"]
        self.assertEqual(throughput["new_ai_completions_last_hour"], 1)
        self.assertEqual(throughput["confirmed_wb_publications_last_hour"], 1)

    def test_actionable_regeneration_keeps_literal_priority_but_terminal_does_not(
        self,
    ) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="manual",
            actor_id="admin",
        )
        one_star = feedback("actionable-regeneration-1star", text="Нужен ответ")
        one_star["productValuation"] = 1
        two_star = feedback("fresh-action-2star", text="Нужен ответ")
        two_star["productValuation"] = 2
        self.repo.upsert_feedback(
            one_star,
            source_stream="archive",
            run_kind="backfill",
        )
        regeneration = self.repo.enqueue_manual_processing(
            "actionable-regeneration-1star",
            content_version=1,
            actor_id="admin",
        )
        self.repo.claim_processing_job(worker_id="ai")
        self.repo.settle_budget(
            regeneration["processing_key"],
            actual_cost_usd="0.01",
        )
        self.repo.complete_generation(
            regeneration["processing_key"],
            result=successful_result(media_uncertain=True),
            worker_id="ai",
        )
        self.repo.upsert_feedback(
            two_star,
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "auto_all",
            actor_id="admin",
            run_max_usd="1.00",
        )
        self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )

        self.repo.reconcile_policy_sweep_once(
            worker_id="reconciliation",
            batch_size=1,
        )
        claimed = self.repo.claim_processing_job(worker_id="ai")
        self.assertEqual(
            claimed["feedback_id"],
            "actionable-regeneration-1star",
        )
        self.assertFalse(self.repo.get_feedback("fresh-action-2star")["ai_jobs"])

    def test_external_answer_after_preview_is_acknowledged_without_new_work(
        self,
    ) -> None:
        self.repo.upsert_feedback(
            feedback("answered-after-preview", text="Нужен ответ"),
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "auto_all",
            actor_id="admin",
            run_max_usd="1.00",
        )
        applied = self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        self.repo.upsert_feedback(
            feedback(
                "answered-after-preview",
                text="Нужен ответ",
                answer="Уже отвечено продавцом",
            ),
            source_stream="answered",
            run_kind="steady",
        )

        status = self.repo.reconcile_policy_sweep_once(
            worker_id="reconciliation",
        )
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["progress"]["external_answer_skipped"], 1)
        self.assertEqual(status["cursor"]["acknowledged_total"], 1)
        self.assertFalse(
            self.repo.get_feedback("answered-after-preview")["ai_jobs"]
        )
        self.assertIsNone(
            self.repo.reconcile_policy_sweep_once(
                worker_id="reconciliation-restart",
            )
        )
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements
                    WHERE sweep_id=?
                    """,
                    (applied["sweep"]["sweep_id"],),
                ).fetchone()[0],
                1,
            )

    def test_restart_acknowledges_action_that_committed_before_member_ack(
        self,
    ) -> None:
        self.repo.upsert_feedback(
            feedback("action-before-ack", text="Нужен ответ"),
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "draft_only",
            actor_id="admin",
            run_max_usd="1.00",
        )
        applied = self.repo.apply_mode_transition(
            "draft_only",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        self.repo.reconcile_policy_sweep_once(
            worker_id="interrupted-reconciliation",
            batch_size=1,
        )
        job = self.repo.get_feedback("action-before-ack")["ai_jobs"][0]
        with self.repo.transaction() as conn:
            conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements
                WHERE sweep_id=?
                """,
                (applied["sweep"]["sweep_id"],),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
                SET state='queued',cursor_json='{}',progress_json='{}',
                    completed_at=NULL
                WHERE sweep_id=?
                """,
                (applied["sweep"]["sweep_id"],),
            )
        self.repo.claim_processing_job(worker_id="ai")
        self.repo.settle_budget(job["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            job["processing_key"],
            result=successful_result(),
            worker_id="ai",
        )

        restarted = AutoanswersRepository(
            runtime_dir=Path(self.temp.name),
            now_factory=self.clock,
            env={},
        )
        status = restarted.reconcile_policy_sweep_once(
            worker_id="restart",
        )
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["progress"]["already_reconciled"], 1)
        self.assertEqual(status["cursor"]["acknowledged_total"], 1)
        self.assertEqual(status["cursor"]["unchanged_total"], 1)
        self.assertEqual(
            restarted.get_feedback("action-before-ack")["ai_jobs"][0]["state"],
            "generated",
        )
        self.assertEqual(
            restarted.reconciliation_status(applied["sweep"]["sweep_id"])[
                "cursor"
            ]["reconciliation_remaining"],
            0,
        )

    def test_zero_output_automatic_sweep_surfaces_truthful_stall(self) -> None:
        content = feedback("stalled-action", text="Нужен ответ")
        content["productValuation"] = 1
        self.repo.upsert_feedback(
            content,
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "auto_all",
            actor_id="admin",
            run_max_usd="1.00",
        )
        self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        self.clock.advance(15 * 60 + 1)
        self.repo.record_scheduler_tick(errors=[])
        progress = self.repo.progress_status()
        self.assertEqual(progress["stop_reason"], "automatic_pipeline_stalled")
        alerts = {item["code"]: item for item in progress["stall_alerts"]}
        self.assertEqual(alerts["automatic_pipeline_stalled"]["severity"], "error")
        self.assertEqual(
            alerts["automatic_pipeline_stalled"]["priority_bucket"],
            "content_bearing_1_star",
        )
        self.assertEqual(progress["claimable_ai_jobs"], 0)
        self.assertEqual(progress["business_throughput"]["new_ai_completions_last_hour"], 0)

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

    def test_publication_bound_review_rebinds_without_new_cost_or_write_and_releases_latch(self) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="auto_all",
            max_materialized_processing_jobs=10,
            actor_id="admin",
        )
        processing_keys: list[str] = []
        for index in range(5):
            feedback_id = f"publication-bound-{index}"
            self.repo.upsert_feedback(
                feedback(feedback_id),
                source_stream="unanswered",
                run_kind="steady",
            )
            job = self.repo.enqueue_processing(
                feedback_id,
                trigger_source="steady_sync",
                actor_id="sync",
            )
            processing_keys.append(str(job["processing_key"]))
            claimed = self.repo.claim_processing_job(worker_id="worker")
            self.repo.settle_budget(
                claimed["processing_key"],
                actual_cost_usd="0.01",
            )
            self.repo.complete_generation(
                claimed["processing_key"],
                result=successful_result(),
                worker_id="worker",
            )
        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state='needs_review', regeneration_required=1,
                    regeneration_reason='policy_epoch_stale',
                    last_error_code='policy_epoch_stale'
                """
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state='generated'
                WHERE processing_key=?
                """,
                (processing_keys[0],),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET state='needs_review', last_error_code='policy_epoch_stale'
                """
            )
            self.repo._set_stop_reason(
                conn,
                "worker_error",
                details={
                    "code": "publication_already_exists",
                    "stage": "reconciliation",
                },
                at=self.clock(),
            )
            before = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts)
                """
            ).fetchone()
            identity_before = [
                tuple(row)
                for row in conn.execute(
                """
                SELECT j.state,j.policy_epoch,j.transition_run_id,
                       p.state,p.policy_epoch,p.transition_run_id
                FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                JOIN sheet_vitrina_v1_wb_publication_jobs p
                  ON p.processing_key=j.processing_key
                ORDER BY j.processing_key
                """
                ).fetchall()
            ]

        preview = self.repo.preview_mode_transition(
            "auto_all",
            actor_id="admin",
            run_max_usd="10.00",
        )
        applied = self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        status = self.repo.reconcile_policy_sweep_once(
            worker_id="reconcile",
            batch_size=25,
        )
        self.assertEqual(status["progress"]["publication_rebound"], 5)
        self.repo.record_scheduler_tick(errors=[])
        progress = self.repo.progress_status()
        self.assertNotEqual(progress["stop_reason"], "worker_error")
        with sqlite3.connect(self.repo.db_path) as conn:
            rows = conn.execute(
                """
                SELECT j.state,j.policy_epoch,j.transition_run_id,
                       p.state,p.policy_epoch,p.transition_run_id
                FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                JOIN sheet_vitrina_v1_wb_publication_jobs p
                  ON p.processing_key=j.processing_key
                ORDER BY j.processing_key
                """
            ).fetchall()
            after = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts)
                """
            ).fetchone()
            latch_audits = conn.execute(
                """
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_audit_events
                WHERE event_type='publication_conflict_worker_latch_reconciled'
                """
            ).fetchone()[0]
            acknowledgements = conn.execute(
                """
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements
                    WHERE sweep_id=? AND outcome='publication_rebound'
                """,
                (applied["sweep"]["sweep_id"],),
            ).fetchone()[0]
        self.assertEqual(after, tuple(before))
        self.assertEqual(latch_audits, 1)
        self.assertTrue(all(row[0] == "approved" for row in rows))
        self.assertTrue(all(row[3] == "approved" for row in rows))
        self.assertTrue(all(row[1] == applied["settings"].policy_epoch for row in rows))
        self.assertTrue(all(row[4] == applied["settings"].policy_epoch for row in rows))
        self.assertNotEqual(rows, identity_before)
        self.assertEqual(acknowledgements, 5)
        self.repo.record_scheduler_tick(errors=[])
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_autoanswers_audit_events
                    WHERE event_type='publication_conflict_worker_latch_reconciled'
                    """
                ).fetchone()[0],
                1,
            )
        for processing_key in processing_keys:
            with self.assertRaisesRegex(
                AutoanswersRuntimeError,
                "publication aggregate already exists",
            ):
                self.repo.request_regeneration(
                    processing_key,
                    actor_id="reconcile",
                    trigger_source="policy_reconciliation",
                    transition_run_id=applied["sweep"]["transition_run_id"],
                )

        with self.repo.transaction() as conn:
            self.repo._set_stop_reason(
                conn,
                "worker_error",
                details={
                    "code": "publication_already_exists",
                    "stage": "processing",
                },
                at=self.clock(),
            )
        self.repo.record_scheduler_tick(errors=[])
        self.assertEqual(
            self.repo.progress_status()["stop_reason"],
            "worker_error",
        )
        with self.repo.transaction() as conn:
            self.repo._set_stop_reason(
                conn,
                "worker_error",
                details={"code": "reservation_missing", "stage": "processing"},
                at=self.clock(),
            )
        self.repo.record_scheduler_tick(errors=[])
        self.assertEqual(
            self.repo.progress_status()["stop_reason"],
            "worker_error",
        )

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
