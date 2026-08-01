#!/usr/bin/env python3
"""Safety and idempotency checks for the exact T0 recovery runner."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from apps.wb_autoanswers_backlog_recovery import (
    _fingerprint,
    _open,
    RecoveryPacedReadPort,
    apply_plan,
    build_plan,
    capture_t0_manifest,
    fetch_remote_evidence,
    reconcile_readback,
)
from apps.wb_autoanswers_runtime_test import MutableClock, feedback, successful_result
from packages.adapters.wb_autoanswers import FeedbackPage, WbAutoanswersHttpError
from packages.application.wb_autoanswers_runtime import (
    DEFAULT_POLICY_VERSION,
    PREVIOUS_POLICY_VERSION,
    AutoanswersRepository,
    SCHEMA_VERSION,
    canonical_json,
    content_projection,
    sha256_text,
)


class FakeSource:
    def __init__(
        self,
        details: dict[str, dict],
        *,
        unanswered: list[str] | None = None,
        count_override: int | None = None,
    ) -> None:
        self.details = details
        self.unanswered = list(unanswered if unanswered is not None else details)
        self.count_override = count_override

    def fetch_feedbacks_page(self, **kwargs: object) -> FeedbackPage:
        skip = int(kwargs["skip"])
        take = int(kwargs["take"])
        rows = [self.details[key] for key in self.unanswered[skip : skip + take]]
        return FeedbackPage(rows=rows, take=take, skip=skip, has_more=skip + take < len(self.unanswered))

    def fetch_archive_page(self, *, take: int, skip: int) -> FeedbackPage:
        return FeedbackPage(rows=[], take=take, skip=skip, has_more=False)

    def fetch_detail(self, feedback_id: str) -> dict | None:
        return dict(self.details[feedback_id])

    def count_unanswered(self) -> int:
        return len(self.unanswered) if self.count_override is None else self.count_override


class BacklogRecoveryTest(unittest.TestCase):
    def test_recovery_gets_are_paced_and_retry_429_with_server_delay(self) -> None:
        class Clock:
            def __init__(self) -> None:
                self.now = 0.0
                self.sleeps: list[float] = []

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.sleeps.append(seconds)
                self.now += seconds

        class RateLimitedSource(FakeSource):
            def __init__(self) -> None:
                super().__init__({"paced": feedback("paced", text="Отзыв")})
                self.detail_calls = 0

            def fetch_detail(self, feedback_id: str) -> dict | None:
                self.detail_calls += 1
                if self.detail_calls == 1:
                    raise WbAutoanswersHttpError(
                        429,
                        "limited",
                        retry_after_seconds=2,
                    )
                return super().fetch_detail(feedback_id)

        clock = Clock()
        source = RateLimitedSource()
        paced = RecoveryPacedReadPort(
            source,
            request_interval_seconds=0.5,
            max_rate_limit_retries=2,
            retry_seconds=1.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        self.assertEqual(paced.fetch_detail("paced")["id"], "paced")
        self.assertEqual(source.detail_calls, 2)
        self.assertEqual(paced.count_unanswered(), 1)
        self.assertEqual(clock.sleeps, [2.0, 0.5])

    def test_recovery_get_429_retry_budget_is_bounded(self) -> None:
        class Clock:
            def __init__(self) -> None:
                self.now = 0.0
                self.sleeps: list[float] = []

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.sleeps.append(seconds)
                self.now += seconds

        class ExhaustedSource(FakeSource):
            def __init__(self) -> None:
                super().__init__({"limited": feedback("limited", text="Отзыв")})
                self.detail_calls = 0

            def fetch_detail(self, feedback_id: str) -> dict | None:
                self.detail_calls += 1
                raise WbAutoanswersHttpError(429, "limited", retry_after_seconds=1)

        clock = Clock()
        source = ExhaustedSource()
        paced = RecoveryPacedReadPort(
            source,
            max_rate_limit_retries=1,
            max_retry_after_seconds=30,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        with self.assertRaises(WbAutoanswersHttpError):
            paced.fetch_detail("limited")
        self.assertEqual(source.detail_calls, 2)
        self.assertEqual(clock.sleeps, [1.0])

    def test_planned_apply_resumes_after_its_own_detail_upsert_prefix(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            repo = AutoanswersRepository(runtime_dir=runtime_dir, env={})
            repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
            with repo.transaction() as conn:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_autoanswers_settings SET policy_version=?",
                    (PREVIOUS_POLICY_VERSION,),
                )
            backup_dir = runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
            backup_dir.mkdir(parents=True)
            with sqlite3.connect(repo.db_path) as source_db:
                with sqlite3.connect(backup_dir / "verified.sqlite3") as target_db:
                    source_db.backup(target_db)
            details = {
                name: feedback(name, text=f"Отзыв {name}")
                for name in ("resume-a", "resume-b")
            }
            manifest = {
                "contract": "wb_autoanswers_t0_manifest_v1",
                "captured_at": "2026-08-01T12:00:00Z",
                "items": [
                    {
                        "feedback_id": feedback_id,
                        "wb_detail_content_hash": sha256_text(
                            canonical_json(content_projection(detail))
                        ),
                    }
                    for feedback_id, detail in details.items()
                ],
            }
            manifest["manifest_sha256"] = _fingerprint(manifest)
            remote, fetched_details = fetch_remote_evidence(FakeSource(details), manifest)
            with _open(runtime_dir, read_only=True) as conn:
                plan = build_plan(
                    conn,
                    runtime_dir=runtime_dir,
                    manifest=manifest,
                    remote=remote,
                )

            original_upsert = AutoanswersRepository.upsert_feedback
            call_count = 0

            def fail_after_first_upsert(
                instance: AutoanswersRepository,
                raw: dict,
                **kwargs: object,
            ) -> dict:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("simulated process interruption")
                return original_upsert(instance, raw, **kwargs)

            with patch.object(
                AutoanswersRepository,
                "upsert_feedback",
                new=fail_after_first_upsert,
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated process interruption"):
                    apply_plan(
                        runtime_dir,
                        manifest=manifest,
                        remote=remote,
                        details=fetched_details,
                        expected_fingerprint=plan["plan_fingerprint"],
                        actor="test",
                        approval_reference="test-human-gate",
                    )
            with sqlite3.connect(repo.db_path) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT state FROM sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs"
                    ).fetchone()[0],
                    "planned",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_feedbacks"
                    ).fetchone()[0],
                    1,
                )
            resumed = apply_plan(
                runtime_dir,
                manifest=manifest,
                remote=remote,
                details=fetched_details,
                expected_fingerprint=plan["plan_fingerprint"],
                actor="test",
                approval_reference="test-human-gate",
            )
            self.assertEqual(resumed["status"], "applied")
            self.assertFalse(resumed["idempotent"])
            self.assertEqual(repo.settings().policy_version, DEFAULT_POLICY_VERSION)
            self.assertEqual(len(repo.get_feedback("resume-a")["ai_jobs"]), 1)
            self.assertEqual(len(repo.get_feedback("resume-b")["ai_jobs"]), 1)

    def test_exact_manifest_reuses_evidence_transforms_chat_and_is_resumable(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            clock = MutableClock()
            repo = AutoanswersRepository(runtime_dir=runtime_dir, now_factory=clock, env={})
            repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
            with repo.transaction() as conn:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_autoanswers_settings SET policy_version=?",
                    (PREVIOUS_POLICY_VERSION,),
                )

            details = {
                name: feedback(name, text=f"Отзыв {name}")
                for name in (
                    "publication",
                    "rating-publication",
                    "seller",
                    "audited",
                    "stale",
                    "missing",
                )
            }
            details["missing"]["text"] = ""
            details["missing"]["pros"] = ""
            details["missing"]["cons"] = ""
            details["missing"]["photoLinks"] = []
            details["missing"]["productValuation"] = 5

            repo.upsert_feedback(details["publication"], source_stream="unanswered", run_kind="steady")
            repo.enqueue_processing("publication", trigger_source="steady_sync", actor_id="sync")
            publication = repo.claim_processing_job(worker_id="worker")
            self.assertEqual(publication["feedback_id"], "publication")
            repo.settle_budget(publication["processing_key"], actual_cost_usd="0")
            repo.complete_generation(
                publication["processing_key"],
                result=successful_result("public_only"),
                worker_id="worker",
            )
            repo.upsert_feedback(
                details["rating-publication"],
                source_stream="unanswered",
                run_kind="steady",
            )
            repo.enqueue_processing(
                "rating-publication", trigger_source="steady_sync", actor_id="sync"
            )
            rating_publication = repo.claim_processing_job(worker_id="worker")
            self.assertEqual(rating_publication["feedback_id"], "rating-publication")
            repo.settle_budget(rating_publication["processing_key"], actual_cost_usd="0")
            repo.complete_generation(
                rating_publication["processing_key"],
                result=successful_result("rating_only_template"),
                worker_id="worker",
            )
            repo.upsert_feedback(details["seller"], source_stream="unanswered", run_kind="steady")
            repo.enqueue_processing("seller", trigger_source="steady_sync", actor_id="sync")
            seller = repo.claim_processing_job(worker_id="worker")
            self.assertEqual(seller["feedback_id"], "seller")
            repo.settle_budget(seller["processing_key"], actual_cost_usd="0")
            seller_result = successful_result("seller_chat")
            seller_result["final_reply"] = "Напишите в чат продавца по коду А1234."
            seller_result["case_code"] = "А1234"
            repo.complete_generation(
                seller["processing_key"],
                result=seller_result,
                worker_id="worker",
            )
            repo.upsert_feedback(details["audited"], source_stream="unanswered", run_kind="steady")
            repo.enqueue_processing("audited", trigger_source="steady_sync", actor_id="sync")
            audited = repo.claim_processing_job(worker_id="worker")
            self.assertEqual(audited["feedback_id"], "audited")
            repo.mark_provider_call_started(audited["processing_key"], worker_id="worker")
            audited_reply = "Спасибо за отзыв. Ваше замечание учтено."
            repo.append_node_audit(
                audited["processing_key"],
                [
                    {"type": "route_guard", "payload": {"final_route": "public_only"}},
                    {
                        "type": "job_complete",
                        "payload": {
                            "outcome": "ready",
                            "model_call_count": 3,
                            "cost": {"total_usd": 0.02},
                            "final_reply": audited_reply,
                        },
                    },
                ],
            )
            repo.settle_budget(audited["processing_key"], actual_cost_usd="0.02")
            repo.record_processing_terminal(
                audited["processing_key"],
                error_code="reservation_missing",
                worker_id="worker",
            )
            repo.upsert_feedback(details["stale"], source_stream="unanswered", run_kind="steady")
            repo.enqueue_processing("stale", trigger_source="steady_sync", actor_id="sync")
            stale = repo.claim_processing_job(worker_id="worker")
            self.assertEqual(stale["feedback_id"], "stale")
            repo.record_processing_terminal(
                stale["processing_key"],
                error_code="stale_content_version",
                worker_id="worker",
            )

            with repo.transaction() as conn:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state='needs_review',last_error_code='policy_epoch_stale' WHERE processing_key=?",
                    (publication["processing_key"],),
                )
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_publication_jobs SET state='needs_review',last_error_code='policy_epoch_stale' WHERE processing_key=?",
                    (publication["processing_key"],),
                )
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state='needs_review',last_error_code='policy_epoch_stale' WHERE processing_key=?",
                    (rating_publication["processing_key"],),
                )
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_publication_jobs SET state='needs_review',last_error_code='policy_epoch_stale' WHERE processing_key=?",
                    (rating_publication["processing_key"],),
                )

            backup_dir = runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
            backup_dir.mkdir(parents=True)
            with sqlite3.connect(repo.db_path) as source:
                with sqlite3.connect(backup_dir / "verified.sqlite3") as target:
                    source.backup(target)

            items = []
            for feedback_id, detail in details.items():
                items.append(
                    {
                        "feedback_id": feedback_id,
                        "wb_detail_content_hash": sha256_text(canonical_json(content_projection(detail))),
                    }
                )
            manifest = {
                "contract": "wb_autoanswers_t0_manifest_v1",
                "captured_at": "2026-08-01T12:00:00Z",
                "items": items,
            }
            manifest["manifest_sha256"] = _fingerprint(manifest)
            source = FakeSource(details)
            remote, fetched_details = fetch_remote_evidence(source, manifest)
            with _open(runtime_dir, read_only=True) as conn:
                plan = build_plan(
                    conn,
                    runtime_dir=runtime_dir,
                    manifest=manifest,
                    remote=remote,
                )
            self.assertTrue(plan["coverage_confirmed"])
            self.assertEqual(plan["expected_feedback_count"], 6)
            self.assertEqual(plan["action_counts"]["rebind_publication"], 2)
            self.assertEqual(plan["action_counts"]["safe_public_transform"], 1)
            self.assertEqual(plan["action_counts"]["recover_audited_generation"], 1)
            self.assertEqual(plan["action_counts"]["safe_public_recovery"], 1)
            self.assertEqual(plan["action_counts"]["ingest_and_generate"], 1)

            applied = apply_plan(
                runtime_dir,
                manifest=manifest,
                remote=remote,
                details=fetched_details,
                expected_fingerprint=plan["plan_fingerprint"],
                actor="test",
                approval_reference="test-human-gate",
            )
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(applied["wb_posts_by_runner"], 0)
            self.assertEqual(applied["provider_calls_by_runner"], 0)
            replay = apply_plan(
                runtime_dir,
                manifest=manifest,
                remote=remote,
                details=fetched_details,
                expected_fingerprint=plan["plan_fingerprint"],
                actor="test",
                approval_reference="test-human-gate",
            )
            self.assertTrue(replay["idempotent"])

            self.assertEqual(repo.settings().policy_version, DEFAULT_POLICY_VERSION)
            seller_stored = repo.get_feedback("seller")
            self.assertEqual(seller_stored["route"], "public_only")
            self.assertNotIn("чат продавца", seller_stored["generated_reply"].casefold())
            self.assertEqual(len(seller_stored["publications"]), 1)
            audited_stored = repo.get_feedback("audited")
            self.assertEqual(audited_stored["generated_reply"], audited_reply)
            self.assertEqual(len(audited_stored["publications"]), 1)
            rating_stored = repo.get_feedback("rating-publication")["ai_jobs"][0]
            self.assertEqual(rating_stored["final_route"], "public_only")
            rating_result = json.loads(rating_stored["result_json"])
            self.assertEqual(rating_result["final_route"], "public_only")
            self.assertEqual(
                rating_result["server_policy_rebind"]["source_route"],
                "rating_only_template",
            )
            stale_stored = repo.get_feedback("stale")["ai_jobs"][0]
            self.assertEqual(stale_stored["state"], "queued")
            self.assertEqual(stale_stored["processing_kind"], "safe_public_template")
            missing_stored = repo.get_feedback("missing")["ai_jobs"][0]
            self.assertEqual(missing_stored["processing_kind"], "rating_only_template")

            answered_details = {
                feedback_id: {**detail, "answer": {"text": f"Ответ {feedback_id}"}}
                for feedback_id, detail in details.items()
            }
            answered_source = FakeSource(answered_details, unanswered=[])
            answered_remote, answer_payloads = fetch_remote_evidence(answered_source, manifest)
            database_before = repo.db_path.read_bytes()
            pending = reconcile_readback(
                runtime_dir,
                manifest=manifest,
                remote=answered_remote,
                details=answer_payloads,
                actor="test",
            )
            self.assertEqual(pending["status"], "pending")
            self.assertTrue(pending["read_only"])
            self.assertEqual(repo.db_path.read_bytes(), database_before)

            # Ordinary publication detail GETs (or an explicitly authorized
            # target sync) persist external answers; query-only readback never
            # manufactures that DB/API reconciliation evidence.
            for detail in answer_payloads.values():
                repo.upsert_feedback(
                    detail,
                    source_stream="publication_detail_readback",
                    run_kind="detail_readback",
                )
            database_before = repo.db_path.read_bytes()
            readback = reconcile_readback(
                runtime_dir,
                manifest=manifest,
                remote=answered_remote,
                details=answer_payloads,
                actor="test",
            )
            self.assertEqual(readback["status"], "reconciled")
            self.assertTrue(readback["full_unanswered_zero"])
            self.assertEqual(readback["t0_answered_detail_get"], 6)
            self.assertFalse(any(readback["local_zero_tail"].values()))
            self.assertEqual(repo.db_path.read_bytes(), database_before)

    def test_capture_and_plan_fail_closed_on_remote_inventory_drift(self) -> None:
        details = {name: feedback(name, text=f"Отзыв {name}") for name in ("a", "b")}
        manifest = capture_t0_manifest(FakeSource(details))
        self.assertEqual(manifest["contract"], "wb_autoanswers_t0_manifest_v1")
        self.assertEqual(len(manifest["items"]), 2)
        self.assertEqual(
            manifest["manifest_sha256"],
            _fingerprint({key: value for key, value in manifest.items() if key != "manifest_sha256"}),
        )
        with self.assertRaisesRegex(RuntimeError, "count changed"):
            capture_t0_manifest(FakeSource(details, count_override=3))

        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            repo = AutoanswersRepository(runtime_dir=runtime_dir, env={})
            repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
            backup_dir = runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
            backup_dir.mkdir(parents=True)
            with sqlite3.connect(repo.db_path) as source:
                with sqlite3.connect(backup_dir / "verified.sqlite3") as target:
                    source.backup(target)
            remote, _ = fetch_remote_evidence(
                FakeSource(details, count_override=3), manifest
            )
            with _open(runtime_dir, read_only=True) as conn:
                plan = build_plan(
                    conn,
                    runtime_dir=runtime_dir,
                    manifest=manifest,
                    remote=remote,
                )
            self.assertFalse(plan["coverage_confirmed"])

    def test_completed_node_evidence_does_not_mix_invocations_and_recovery_is_atomic(self) -> None:
        with TemporaryDirectory() as directory:
            repo = AutoanswersRepository(runtime_dir=Path(directory), env={})
            repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
            detail = feedback("audited-atomic", text="Нужен ответ")
            repo.upsert_feedback(detail, source_stream="unanswered", run_kind="steady")
            job = repo.enqueue_processing(
                "audited-atomic", trigger_source="steady_sync", actor_id="sync"
            )
            repo.claim_processing_job(worker_id="worker")
            reply = "Спасибо за отзыв. Ваше замечание учтено."
            repo.append_node_audit(
                job["processing_key"],
                [
                    {"type": "route_guard", "payload": {"final_route": "public_only"}},
                    {
                        "type": "job_complete",
                        "payload": {
                            "outcome": "ready",
                            "model_call_count": 3,
                            "final_reply": reply,
                        },
                    },
                ],
            )
            repo.append_node_audit(
                job["processing_key"],
                [{"type": "route_guard", "payload": {"final_route": "seller_chat"}}],
            )
            repo.record_processing_terminal(
                job["processing_key"], error_code="reservation_missing", worker_id="worker"
            )
            recovered = repo.recover_completed_node_result(
                job["processing_key"], actor_id="recovery"
            )
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["state"], "approved")
            self.assertEqual(recovered["final_route"], "public_only")
            self.assertEqual(recovered["final_reply"], reply)
            self.assertEqual(len(repo.get_feedback("audited-atomic")["publications"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
