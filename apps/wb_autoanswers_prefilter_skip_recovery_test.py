#!/usr/bin/env python3
"""Regression checks for bounded prefilter-skip state restoration."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from apps.wb_autoanswers_prefilter_skip_recovery import (
    _open_ro,
    apply_latch_plan,
    apply_plan,
    build_latch_plan,
    build_plan,
    latch_readback,
    readback,
)
from packages.application.wb_autoanswers_runtime import AutoanswersRepository


ROOT = Path(__file__).resolve().parents[1]


def feedback(feedback_id: str) -> dict:
    return {
        "id": feedback_id,
        "createdDate": "2026-07-24T10:00:00Z",
        "text": "Содержательный отзыв",
        "pros": "",
        "cons": "",
        "productValuation": 5,
        "productDetails": {
            "nmId": 123,
            "supplierArticle": "SKU-1",
            "productName": "Товар",
        },
        "photoLinks": [],
        "answer": None,
    }


class PrefilterSkipRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.runtime_dir = Path(self.temp.name)
        self.now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        self.repo = AutoanswersRepository(
            runtime_dir=self.runtime_dir,
            now_factory=lambda: self.now,
            env={},
        )
        self.repo.update_settings(
            master_enabled=True,
            mode="draft_only",
            actor_id="test",
        )
        self.repo.upsert_feedback(
            feedback("skip-incident"),
            source_stream="unanswered",
            run_kind="steady",
        )
        job = self.repo.enqueue_processing(
            "skip-incident",
            trigger_source="steady_sync",
            actor_id="sync",
        )
        self.processing_key = str(job["processing_key"])
        self.repo.claim_processing_job(worker_id="worker")
        self.repo.mark_provider_call_started(
            self.processing_key,
            worker_id="worker",
        )
        self.repo.settle_budget(
            self.processing_key,
            actual_cost_usd="0",
        )
        self.repo.complete_skip(
            self.processing_key,
            reason="empty_five_star",
            worker_id="worker",
        )
        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state='terminal_error',attempts=attempts+1,
                    last_error_code='reservation_missing',
                    transition_run_id='incident-run',
                    completed_at='2026-07-24T12:05:00Z',
                    updated_at='2026-07-24T12:05:00Z'
                WHERE processing_key=?
                """,
                (self.processing_key,),
            )
            self.repo._audit(
                conn,
                aggregate_type="feedback",
                aggregate_id="skip-incident",
                event_type="policy_reconciled",
                actor_type="policy",
                actor_id="reconcile",
                details={
                    "outcome": "generation_queued",
                    "transition_run_id": "incident-run",
                },
                at=datetime(2026, 7, 24, 12, 4, tzinfo=timezone.utc),
            )
            self.repo._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=self.processing_key,
                event_type="processing_terminal_error",
                actor_type="worker",
                actor_id="worker",
                details={"error_code": "reservation_missing"},
                at=datetime(2026, 7, 24, 12, 5, tzinfo=timezone.utc),
                previous_state="processing",
                next_state="terminal_error",
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_apply_restores_projection_and_repeat_is_noop(self) -> None:
        with closing(_open_ro(self.runtime_dir)) as conn:
            plan = build_plan(
                conn,
                transition_run_id="incident-run",
                expected_rows=1,
            )
        self.assertTrue(plan["coverage_confirmed"])
        self.assertEqual(plan["candidate_count"], 1)
        self.assertEqual(
            plan["expected_affected_records"],
            {
                "processing_jobs_updated": 1,
                "audit_events_appended": 1,
                "reservations_updated": 0,
                "provider_calls_created": 0,
                "cost_events_created": 0,
                "wb_writes_created": 0,
            },
        )
        self.assertFalse(plan["reversibility"]["backup_required"])
        self.assertEqual(
            plan["non_target_snapshot"]["provider_boundaries"],
            1,
        )

        applied = apply_plan(
            self.runtime_dir,
            transition_run_id="incident-run",
            expected_rows=1,
            expected_fingerprint=plan["plan_fingerprint"],
            actor="test",
        )
        self.assertEqual(applied["status"], "reconciled")
        self.assertFalse(applied["idempotent"])
        self.assertTrue(applied["non_target_invariants_preserved"])
        self.assertEqual(
            applied["non_target_readback"]["before"],
            applied["non_target_readback"]["after"],
        )
        self.assertEqual(applied["restored_processing_keys"], [self.processing_key])

        with sqlite3.connect(self.repo.db_path) as conn:
            job = conn.execute(
                """
                SELECT state,last_error_code,attempts,completed_at
                FROM sheet_vitrina_v1_wb_autoanswer_jobs
                WHERE processing_key=?
                """,
                (self.processing_key,),
            ).fetchone()
            reservation = conn.execute(
                """
                SELECT status,actual_cost_usd,provider_call_started_at
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                WHERE processing_key=?
                """,
                (self.processing_key,),
            ).fetchone()
            counts_before_replay = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_audit_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts)
                """
            ).fetchone()
        self.assertEqual(job[0], "skipped")
        self.assertEqual(job[1], "empty_five_star")
        self.assertEqual(job[2], 2)
        self.assertEqual(job[3], "2026-07-24T12:00:00Z")
        self.assertEqual(reservation[0], "settled")
        self.assertEqual(float(reservation[1]), 0.0)
        self.assertIsNotNone(reservation[2])

        replay = apply_plan(
            self.runtime_dir,
            transition_run_id="incident-run",
            expected_rows=1,
            expected_fingerprint=plan["plan_fingerprint"],
            actor="test",
        )
        self.assertEqual(replay["status"], "already_reconciled")
        self.assertTrue(replay["idempotent"])
        self.assertEqual(sum(replay["affected_records"].values()), 0)
        with sqlite3.connect(self.repo.db_path) as conn:
            counts_after_replay = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_audit_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts)
                """
            ).fetchone()
        self.assertEqual(counts_after_replay, counts_before_replay)
        self.assertEqual(
            readback(
                self.runtime_dir,
                transition_run_id="incident-run",
                expected_rows=1,
            )["status"],
            "confirmed",
        )

    def test_changed_fingerprint_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "evidence changed"):
            apply_plan(
                self.runtime_dir,
                transition_run_id="incident-run",
                expected_rows=1,
                expected_fingerprint="sha256:" + "0" * 64,
                actor="test",
            )

    def test_apply_requires_positive_exact_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive expected rows"):
            apply_plan(
                self.runtime_dir,
                transition_run_id="incident-run",
                expected_rows=0,
                expected_fingerprint="sha256:" + "0" * 64,
                actor="test",
            )

    def test_repo_root_cli_bootstrap_supports_direct_execution(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "apps/wb_autoanswers_prefilter_skip_recovery.py",
                "dry-run",
                "--runtime-dir",
                str(self.runtime_dir),
                "--transition-run-id",
                "incident-run",
                "--expected-rows",
                "1",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertTrue(payload["coverage_confirmed"])

    def test_release_worker_latch_after_exact_projection_recovery(self) -> None:
        with closing(_open_ro(self.runtime_dir)) as conn:
            source_plan = build_plan(
                conn,
                transition_run_id="incident-run",
                expected_rows=1,
            )
        source_fingerprint = str(source_plan["plan_fingerprint"])
        apply_plan(
            self.runtime_dir,
            transition_run_id="incident-run",
            expected_rows=1,
            expected_fingerprint=source_fingerprint,
            actor="test",
        )
        with self.repo.transaction() as conn:
            self.repo._set_stop_reason(
                conn,
                "worker_error",
                details={"code": "reservation_missing"},
                at=self.now,
            )
        with closing(_open_ro(self.runtime_dir)) as conn:
            latch_plan = build_latch_plan(
                conn,
                transition_run_id="incident-run",
                expected_rows=1,
                source_fingerprint=source_fingerprint,
            )
        self.assertTrue(latch_plan["release_eligible"])
        self.assertEqual(latch_plan["unresolved_uncertainty"], 0)
        self.assertEqual(latch_plan["active_reservations"], 0)
        self.assertEqual(
            latch_plan["expected_affected_records"],
            {
                "runtime_state_rows_updated": 1,
                "audit_events_appended": 1,
                "reservations_updated": 0,
                "provider_calls_created": 0,
                "cost_events_created": 0,
                "wb_writes_created": 0,
            },
        )

        applied = apply_latch_plan(
            self.runtime_dir,
            transition_run_id="incident-run",
            expected_rows=1,
            source_fingerprint=source_fingerprint,
            expected_fingerprint=latch_plan["plan_fingerprint"],
            actor="test",
        )
        self.assertEqual(applied["status"], "reconciled")
        self.assertTrue(applied["non_target_invariants_preserved"])
        self.assertEqual(
            applied["non_target_readback"]["before"],
            applied["non_target_readback"]["after"],
        )

        replay = apply_latch_plan(
            self.runtime_dir,
            transition_run_id="incident-run",
            expected_rows=1,
            source_fingerprint=source_fingerprint,
            expected_fingerprint=latch_plan["plan_fingerprint"],
            actor="test",
        )
        self.assertEqual(replay["status"], "already_reconciled")
        self.assertTrue(replay["idempotent"])
        self.assertEqual(sum(replay["affected_records"].values()), 0)
        self.assertEqual(
            latch_readback(
                self.runtime_dir,
                transition_run_id="incident-run",
                expected_rows=1,
                source_fingerprint=source_fingerprint,
            )["status"],
            "confirmed",
        )
        self.assertEqual(self.repo.progress_status()["stop_reason"], "worker_unavailable")

    def test_queued_invalid_reclaim_is_restored_without_provider_mutation(self) -> None:
        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state='queued',last_error_code=NULL,
                    completed_at=NULL,updated_at='2026-07-24T12:04:00Z'
                WHERE processing_key=?
                """,
                (self.processing_key,),
            )
        with closing(_open_ro(self.runtime_dir)) as conn:
            plan = build_plan(
                conn,
                transition_run_id="incident-run",
                expected_rows=1,
            )
        self.assertEqual(plan["candidate_count"], 1)
        self.assertEqual(plan["candidates"][0]["current_state"], "queued")
        result = apply_plan(
            self.runtime_dir,
            transition_run_id="incident-run",
            expected_rows=1,
            expected_fingerprint=plan["plan_fingerprint"],
            actor="test",
        )
        self.assertEqual(result["status"], "reconciled")
        with sqlite3.connect(self.repo.db_path) as conn:
            state, error, reservation_status = conn.execute(
                """
                SELECT j.state,j.last_error_code,r.status
                FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                JOIN sheet_vitrina_v1_wb_autoanswers_budget_reservations r
                  ON r.processing_key=j.processing_key
                WHERE j.processing_key=?
                """,
                (self.processing_key,),
            ).fetchone()
        self.assertEqual((state, error, reservation_status), (
            "skipped",
            "empty_five_star",
            "settled",
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
