#!/usr/bin/env python3
"""Safety checks for the bounded rolling Autoanswers recovery runner."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from apps.wb_autoanswers_rolling_recovery import (
    _open,
    apply_plan,
    build_plan,
)
from packages.application.wb_autoanswers_runtime import (
    AutoanswersRepository,
    SCHEMA_VERSION,
)


def feedback(
    feedback_id: str,
    *,
    rating: int,
    text: str = "",
    tags: list[str] | None = None,
) -> dict:
    return {
        "id": feedback_id,
        "createdDate": "2026-07-20T10:00:00Z",
        "text": text,
        "pros": "",
        "cons": "",
        "tags": list(tags or []),
        "productValuation": rating,
        "productDetails": {
            "nmId": 123,
            "supplierArticle": "SKU-1",
            "productName": "Товар",
        },
        "photoLinks": [],
        "answer": None,
    }


class RollingRecoveryTest(unittest.TestCase):
    def test_exact_recovery_requeues_without_cost_or_publication_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            now = lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
            repo = AutoanswersRepository(
                runtime_dir=runtime_dir,
                now_factory=now,
                env={},
            )
            repo.upsert_feedback(
                feedback("node-exit", rating=4, text="content"),
                source_stream="archive",
                run_kind="backfill",
            )
            repo.upsert_feedback(
                feedback("tagged-five", rating=5, tags=["Красивый цвет"]),
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
            run_id = applied["sweep"]["transition_run_id"]

            repo.reconcile_policy_sweep_once(worker_id="reconcile")
            node = repo.claim_processing_job(worker_id="worker")
            self.assertEqual(node["feedback_id"], "node-exit")
            repo.mark_provider_call_started(
                node["processing_key"],
                worker_id="worker",
            )
            repo.record_processing_terminal(
                node["processing_key"],
                error_code="node_process_exit_1",
                worker_id="worker",
            )
            budget_plan = repo.budget_reconciliation_plan()
            repo.apply_budget_reconciliation(
                expected_fingerprint=budget_plan["plan_fingerprint"],
                actor_id="admin",
            )

            repo.reconcile_policy_sweep_once(worker_id="reconcile")
            tagged = repo.claim_processing_job(worker_id="worker")
            self.assertEqual(tagged["feedback_id"], "tagged-five")
            repo.mark_provider_call_started(
                tagged["processing_key"],
                worker_id="worker",
            )
            repo.settle_budget(tagged["processing_key"], actual_cost_usd="0")
            repo.complete_skip(
                tagged["processing_key"],
                reason="empty_five_star",
                worker_id="worker",
            )

            backup_dir = (
                runtime_dir
                / "backups"
                / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
            )
            backup_dir.mkdir(parents=True)
            backup = backup_dir / "verified.sqlite3"
            with sqlite3.connect(repo.db_path) as source:
                with sqlite3.connect(backup) as target:
                    source.backup(target)

            with _open(runtime_dir, read_only=True) as conn:
                plan = build_plan(
                    conn,
                    runtime_dir=runtime_dir,
                    transition_run_id=run_id,
                    expected_empty=1,
                    expected_node=1,
                )
            self.assertTrue(plan["coverage_confirmed"])
            self.assertTrue(plan["schema_backup"]["verified"])
            before = dict(plan["non_target_snapshot"])
            applied_recovery = apply_plan(
                runtime_dir,
                transition_run_id=run_id,
                expected_empty=1,
                expected_node=1,
                expected_fingerprint=plan["plan_fingerprint"],
                actor="test",
            )
            self.assertEqual(applied_recovery["status"], "reconciled")
            self.assertEqual(len(applied_recovery["processing_keys"]), 2)

            with sqlite3.connect(repo.db_path) as conn:
                states = conn.execute(
                    """
                    SELECT feedback_id,state,last_error_code,attempts
                    FROM sheet_vitrina_v1_wb_autoanswer_jobs
                    ORDER BY feedback_id
                    """
                ).fetchall()
                revisions = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_autoanswer_job_revisions
                    """
                ).fetchone()[0]
                publications = conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs"
                ).fetchone()[0]
                legacy_holds = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
                    """
                ).fetchone()[0]
            self.assertEqual(
                states,
                [
                    ("node-exit", "queued", None, 1),
                    ("tagged-five", "queued", None, 1),
                ],
            )
            self.assertEqual(revisions, 2)
            self.assertEqual(publications, 0)
            self.assertEqual(legacy_holds, 1)

            with _open(runtime_dir, read_only=True) as conn:
                after_plan = build_plan(
                    conn,
                    runtime_dir=runtime_dir,
                    transition_run_id=run_id,
                    expected_empty=0,
                    expected_node=0,
                )
            self.assertFalse(any(after_plan["candidates"].values()))
            self.assertEqual(before, after_plan["non_target_snapshot"])
            replay = apply_plan(
                runtime_dir,
                transition_run_id=run_id,
                expected_empty=1,
                expected_node=1,
                expected_fingerprint=plan["plan_fingerprint"],
                actor="test",
            )
            self.assertEqual(replay["status"], "already_reconciled")
            self.assertTrue(replay["idempotent"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
