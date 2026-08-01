#!/usr/bin/env python3
"""Safety checks for exact Autoanswers reconciliation-stall recovery."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from apps.wb_autoanswers_reconciliation_recovery import (
    _open,
    apply_plan,
    build_plan,
    readback,
)
from apps.wb_autoanswers_runtime_test import MutableClock, feedback, successful_result
from packages.application.wb_autoanswers_runtime import (
    AutoanswersRepository,
    SCHEMA_VERSION,
    canonical_json,
    observation_projection,
    sha256_text,
)


IMMUTABLE_TABLES = (
    "sheet_vitrina_v1_wb_autoanswer_jobs",
    "sheet_vitrina_v1_wb_publication_jobs",
    "sheet_vitrina_v1_wb_publication_attempts",
    "sheet_vitrina_v1_wb_autoanswers_budget_reservations",
    "sheet_vitrina_v1_wb_autoanswers_cost_events",
    "sheet_vitrina_v1_wb_autoanswers_failed_cost_events",
    "sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds",
    "sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts",
    "sheet_vitrina_v1_wb_autoanswer_job_revisions",
)


def content_feedback(feedback_id: str, *, rating: int) -> dict:
    row = feedback(feedback_id, text=f"Содержательный отзыв {feedback_id}")
    row["productValuation"] = rating
    row["photoLinks"] = []
    return row


def immutable_snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path) as conn:
        return {
            table: [
                tuple(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY 1"  # noqa: S608 - fixed allowlist
                ).fetchall()
            ]
            for table in IMMUTABLE_TABLES
        }


class ReconciliationRecoveryTest(unittest.TestCase):
    def test_exact_recovery_preserves_execution_identity_and_is_restart_safe(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            clock = MutableClock()
            repo = AutoanswersRepository(
                runtime_dir=runtime_dir,
                now_factory=clock,
                env={},
            )
            repo.update_settings(
                master_enabled=True,
                mode="auto_all",
                max_materialized_processing_jobs=20,
                actor_id="admin",
            )

            for index in range(5):
                feedback_id = f"stale-published-1star-{index}"
                repo.upsert_feedback(
                    content_feedback(feedback_id, rating=1),
                    source_stream="unanswered",
                    run_kind="steady",
                )
                job = repo.enqueue_processing(
                    feedback_id,
                    trigger_source="steady_sync",
                    actor_id="sync",
                )
                repo.claim_processing_job(worker_id="ai")
                repo.settle_budget(job["processing_key"], actual_cost_usd="0.01")
                repo.complete_generation(
                    job["processing_key"],
                    result=successful_result(),
                    worker_id="ai",
                )
                publication = repo.claim_publication_job(worker_id="publisher")
                started = repo.begin_publication_write(
                    publication["publication_key"],
                    worker_id="publisher",
                )
                repo.record_publication_transport(
                    publication["publication_key"],
                    attempt_id=started["attempt_id"],
                    outcome="http_response",
                    http_status=204,
                    worker_id="publisher",
                )
                readback_job = repo.claim_publication_job(worker_id="publisher")
                repo.record_publication_readback(
                    readback_job["publication_key"],
                    answer_text=readback_job["exact_reply"],
                    worker_id="publisher",
                )

                # This recovery fixture represents the deployed pre-fix
                # incident where publication GET truth lived only on the
                # publication aggregate.  Current record_publication_readback
                # deliberately closes that gap, so recreate only the legacy
                # feedback projection directly without weakening production
                # semantics or immutable publication evidence.
                legacy_raw = content_feedback(feedback_id, rating=1)
                legacy_observation = observation_projection(legacy_raw)
                with repo.transaction() as conn:
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_feedbacks
                        SET answer_text='', raw_json=?, observation_json=?,
                            wb_observation_hash=?, source_stream='legacy_pre_fix_fixture'
                        WHERE feedback_id=?
                        """,
                        (
                            canonical_json(legacy_raw),
                            canonical_json(legacy_observation),
                            sha256_text(canonical_json(legacy_observation)),
                            feedback_id,
                        ),
                    )

            repo.upsert_feedback(
                content_feedback("terminal-human-1star", rating=1),
                source_stream="unanswered",
                run_kind="steady",
            )
            terminal = repo.enqueue_processing(
                "terminal-human-1star",
                trigger_source="steady_sync",
                actor_id="sync",
            )
            repo.claim_processing_job(worker_id="ai")
            repo.record_processing_terminal(
                terminal["processing_key"],
                error_code="media_fetch_failed",
                worker_id="ai",
            )
            with repo.transaction() as conn:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET regeneration_required=1,
                        regeneration_reason='media_fetch_failed'
                    WHERE processing_key=?
                    """,
                    (terminal["processing_key"],),
                )

            repo.upsert_feedback(
                content_feedback("real-action-2star", rating=2),
                source_stream="archive",
                run_kind="backfill",
            )
            rating_only = feedback("rating-only-last", text="")
            rating_only["pros"] = ""
            rating_only["cons"] = ""
            rating_only["photoLinks"] = []
            rating_only["productValuation"] = 5
            repo.upsert_feedback(
                rating_only,
                source_stream="archive",
                run_kind="backfill",
            )

            preview = repo.preview_mode_transition(
                "auto_all",
                actor_id="admin",
                run_max_usd="10.00",
            )
            applied = repo.apply_mode_transition(
                "auto_all",
                actor_id="admin",
                preview_id=preview["preview_id"],
            )
            sweep_id = str(applied["sweep"]["sweep_id"])
            run_id = str(applied["sweep"]["transition_run_id"])
            policy_epoch = int(applied["settings"].policy_epoch)

            backup_dir = (
                runtime_dir
                / "backups"
                / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
            )
            backup_dir.mkdir(parents=True)
            backup_path = backup_dir / "verified-pre-v8.sqlite3"
            with sqlite3.connect(repo.db_path) as source:
                with sqlite3.connect(backup_path) as target:
                    source.backup(target)

            before = immutable_snapshot(repo.db_path)
            with _open(runtime_dir, read_only=True) as conn:
                plan = build_plan(
                    conn,
                    runtime_dir=runtime_dir,
                    sweep_id=sweep_id,
                    expected_policy_epoch=policy_epoch,
                    transition_run_id=run_id,
                    expected_candidates=6,
                )
                self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertTrue(plan["coverage_confirmed"])
            self.assertEqual(
                plan["candidate_counts"],
                {
                    "published_preserved": 5,
                    "terminal_error_preserved": 1,
                },
            )
            self.assertNotIn("result_json", plan["candidates"][0])
            self.assertTrue(
                plan["candidates"][0]["result_json_fingerprint"].startswith(
                    "sha256:"
                )
            )

            result = apply_plan(
                runtime_dir,
                sweep_id=sweep_id,
                expected_policy_epoch=policy_epoch,
                transition_run_id=run_id,
                expected_candidates=6,
                expected_fingerprint=plan["plan_fingerprint"],
                actor="test",
            )
            self.assertEqual(result["status"], "reconciled")
            self.assertEqual(result["acknowledgement_count"], 6)
            self.assertEqual(result["cursor"]["state"], "queued")
            self.assertEqual(result["cursor"]["cursor"]["priority_bucket"], 2)
            self.assertEqual(immutable_snapshot(repo.db_path), before)

            with _open(runtime_dir, read_only=True) as conn:
                confirmed = readback(
                    conn,
                    expected_fingerprint=plan["plan_fingerprint"],
                )
            self.assertEqual(confirmed["status"], "confirmed")
            self.assertTrue(confirmed["member_fingerprints_match"])
            self.assertTrue(confirmed["target_execution_evidence_match"])
            self.assertTrue(confirmed["run_identity_and_caps_match"])
            self.assertTrue(confirmed["non_target_invariants_preserved"])

            restarted = AutoanswersRepository(
                runtime_dir=runtime_dir,
                now_factory=clock,
                env={},
            )
            replay = apply_plan(
                runtime_dir,
                sweep_id=sweep_id,
                expected_policy_epoch=policy_epoch,
                transition_run_id=run_id,
                expected_candidates=6,
                expected_fingerprint=plan["plan_fingerprint"],
                actor="test",
            )
            self.assertEqual(replay["status"], "already_reconciled")
            self.assertTrue(replay["idempotent"])
            self.assertEqual(immutable_snapshot(repo.db_path), before)

            restarted.reconcile_policy_sweep_once(
                worker_id="reconcile",
                batch_size=25,
            )
            with _open(runtime_dir, read_only=True) as conn:
                after_unrelated_progress = readback(
                    conn,
                    expected_fingerprint=plan["plan_fingerprint"],
                )
            self.assertEqual(after_unrelated_progress["status"], "confirmed")
            replay_after_progress = apply_plan(
                runtime_dir,
                sweep_id=sweep_id,
                expected_policy_epoch=policy_epoch,
                transition_run_id=run_id,
                expected_candidates=6,
                expected_fingerprint=plan["plan_fingerprint"],
                actor="test",
            )
            self.assertEqual(
                replay_after_progress["status"],
                "already_reconciled",
            )
            claimed = restarted.claim_processing_job(worker_id="ai")
            self.assertEqual(claimed["feedback_id"], "real-action-2star")
            with sqlite3.connect(repo.db_path) as conn:
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements
                        WHERE sweep_id=?
                        """,
                        (sweep_id,),
                    ).fetchone()[0],
                    7,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
