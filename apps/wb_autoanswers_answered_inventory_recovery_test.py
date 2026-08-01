#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from apps.wb_autoanswers_answered_inventory_recovery import (
    apply_plan,
    build_plan,
    capture_manifest,
    fetch_remote_evidence,
    reconcile_readback,
    _open,
)
from apps.wb_autoanswers_runtime_test import MutableClock, feedback
from packages.adapters.wb_autoanswers import FeedbackPage
from packages.application.wb_autoanswers_runtime import AutoanswersRepository


class FullInventorySource:
    def __init__(self, *, answered: list[dict], unanswered: list[dict]) -> None:
        self.answered = answered
        self.unanswered = unanswered
        self.calls: list[dict] = []

    def fetch_feedbacks_page(self, **kwargs: object) -> FeedbackPage:
        self.calls.append(dict(kwargs))
        rows = self.answered if bool(kwargs["is_answered"]) else self.unanswered
        skip = int(kwargs["skip"])
        take = int(kwargs["take"])
        page = rows[skip : skip + take]
        return FeedbackPage(
            rows=page,
            take=take,
            skip=skip,
            has_more=skip + take < len(rows),
        )

    def fetch_archive_page(self, *, take: int, skip: int) -> FeedbackPage:
        return FeedbackPage(rows=[], take=take, skip=skip, has_more=False)

    def fetch_detail(self, feedback_id: str) -> dict | None:
        return None

    def count_unanswered(self) -> int:
        return len(self.unanswered)


class AnsweredInventoryRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.runtime_dir = Path(self.temp.name)
        self.clock = MutableClock()
        self.repo = AutoanswersRepository(
            runtime_dir=self.runtime_dir,
            now_factory=self.clock,
            env={},
        )
        self.stale = feedback("stale-local")
        self.current = feedback("current-unanswered")
        self.already = feedback("already-local", answer="Уже подтверждено")
        self.remote_stale = feedback("stale-local", answer="Ответ из WB")
        self.remote_missing = feedback("missing-local", answer="Найдено в полном inventory")
        self.local_processed = feedback("processed-local")
        self.remote_processed = {**self.local_processed, "state": "wbRu"}
        self.repo.upsert_feedback(
            self.stale,
            source_stream="fixture",
            run_kind="reconciliation",
        )
        self.repo.upsert_feedback(
            self.current,
            source_stream="fixture",
            run_kind="reconciliation",
        )
        self.repo.upsert_feedback(
            self.already,
            source_stream="fixture",
            run_kind="reconciliation",
        )
        self.repo.upsert_feedback(
            self.local_processed,
            source_stream="fixture",
            run_kind="reconciliation",
        )
        self.source = FullInventorySource(
            answered=[
                self.already,
                self.remote_missing,
                self.remote_processed,
                self.remote_stale,
            ],
            unanswered=[self.current],
        )
        self.deployed = {"runtime_sha": "a" * 40}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_capture_uses_full_answered_inventory_without_history_floor(self) -> None:
        manifest = capture_manifest(self.source)
        self.assertEqual(len(manifest["items"]), 4)
        self.assertEqual(
            [item["feedback_id"] for item in manifest["items"]],
            ["already-local", "missing-local", "processed-local", "stale-local"],
        )
        processed = next(
            item for item in manifest["items"]
            if item["feedback_id"] == "processed-local"
        )
        self.assertEqual(processed["resolution_kind"], "processed_without_answer")
        self.assertEqual(processed["answer_sha256"], "")
        self.assertTrue(all(call["date_from_ts"] == 0 for call in self.source.calls))
        self.assertTrue(all(call["is_answered"] for call in self.source.calls))

    def test_plan_apply_replay_and_readback_reconcile_local_inventory(self) -> None:
        manifest = capture_manifest(self.source)
        remote, rows = fetch_remote_evidence(self.source, manifest)
        verified = {
            "verified": True,
            "kind": "compressed",
            "manifest": "fixture.manifest.json",
            "sha256": "sha256:" + "b" * 64,
        }
        paused = {
            "confirmed": True,
            "lifecycle_state": "suspended_by_master",
            "drift_status": "matched",
            "actual": False,
            "service_in_progress": False,
            "policy_epoch": 24,
            "transition_run_id": "fixture-run",
            "components": {
                "readonly_sync": {
                    "desired": False,
                    "actual": False,
                    "drift_status": "matched",
                    "service_active": "inactive",
                    "timer_active": "inactive",
                    "timer_enabled": "disabled",
                },
                "worker": {
                    "desired": False,
                    "actual": False,
                    "drift_status": "matched",
                    "service_active": "inactive",
                    "timer_active": "inactive",
                    "timer_enabled": "disabled",
                },
            },
        }
        with patch(
            "apps.wb_autoanswers_answered_inventory_recovery._verified_backup",
            return_value=verified,
        ), patch(
            "apps.wb_autoanswers_answered_inventory_recovery._lifecycle_pause_snapshot",
            return_value=paused,
        ):
            with _open(self.runtime_dir, read_only=True) as conn:
                plan = build_plan(
                    conn,
                    runtime_dir=self.runtime_dir,
                    manifest=manifest,
                    remote=remote,
                    deployed_runtime=self.deployed,
                )
            self.assertTrue(plan["coverage_confirmed"])
            self.assertEqual(plan["expected_local_updates"], 3)
            self.assertEqual(plan["expected_local_inserts"], 1)
            self.assertEqual(plan["manifest_processed_without_answer_count"], 1)
            applied = apply_plan(
                self.runtime_dir,
                manifest=manifest,
                remote=remote,
                remote_rows=rows,
                deployed_runtime=self.deployed,
                expected_fingerprint=plan["plan_fingerprint"],
                actor="test",
                approval_reference="test-gate",
            )
        self.assertEqual(applied["status"], "applied")
        self.assertFalse(applied["idempotent"])
        self.assertEqual(
            self.repo.get_feedback("stale-local")["answer"]["text"],
            "Ответ из WB",
        )
        self.assertEqual(
            self.repo.get_feedback("missing-local")["answer"]["text"],
            "Найдено в полном inventory",
        )
        processed = self.repo.get_feedback("processed-local")
        self.assertEqual(processed["answer"]["text"], "")
        with _open(self.runtime_dir, read_only=True) as conn:
            processed_raw = conn.execute(
                "SELECT json_extract(raw_json,'$.state') FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                ("processed-local",),
            ).fetchone()[0]
        self.assertEqual(processed_raw, "wbRu")
        replay = apply_plan(
            self.runtime_dir,
            manifest=manifest,
            remote=remote,
            remote_rows=rows,
            deployed_runtime=self.deployed,
            expected_fingerprint=plan["plan_fingerprint"],
            actor="test",
            approval_reference="test-gate",
        )
        self.assertTrue(replay["idempotent"])
        readback = reconcile_readback(
            self.runtime_dir,
            manifest=manifest,
            remote=remote,
            source=self.source,
        )
        self.assertEqual(readback["status"], "reconciled")
        self.assertTrue(readback["local_official_match"])
        self.assertEqual(readback["official_unanswered_count"], 1)
        self.assertEqual(readback["local_unanswered_count"], 1)

    def test_processed_inventory_rejects_unanswered_row_without_processed_state(self) -> None:
        self.source.answered = [feedback("not-processed")]
        with self.assertRaisesRegex(RuntimeError, "canonical processed state"):
            capture_manifest(self.source)

    def test_manifest_row_change_fails_closed(self) -> None:
        manifest = capture_manifest(self.source)
        self.source.answered = [
            self.already,
            self.remote_missing,
            feedback("stale-local", answer="Другой ответ"),
        ]
        remote, _ = fetch_remote_evidence(self.source, manifest)
        self.assertFalse(remote["manifest_subset_confirmed"])
        self.assertEqual(remote["changed_manifest_count"], 1)

    def test_plan_requires_confirmed_autoanswers_only_suspend(self) -> None:
        manifest = capture_manifest(self.source)
        remote, _ = fetch_remote_evidence(self.source, manifest)
        with patch(
            "apps.wb_autoanswers_answered_inventory_recovery._verified_backup",
            return_value={"verified": True},
        ), patch(
            "apps.wb_autoanswers_answered_inventory_recovery._lifecycle_pause_snapshot",
            return_value={"confirmed": False},
        ):
            with _open(self.runtime_dir, read_only=True) as conn:
                plan = build_plan(
                    conn,
                    runtime_dir=self.runtime_dir,
                    manifest=manifest,
                    remote=remote,
                    deployed_runtime=self.deployed,
                )
        self.assertFalse(plan["coverage_confirmed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
