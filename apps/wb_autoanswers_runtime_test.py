#!/usr/bin/env python3
"""Free local checks for the WB autoanswers durable runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest

from packages.application.wb_autoanswers_runtime import (
    AutoanswersRepository,
    AutoanswersRuntimeError,
    classify_feedback_content,
    content_version_hash,
    wb_observation_hash,
)
from packages.contracts.wb_autoanswers import (
    CONTENT_CLASS_CONTENT_BEARING,
    CONTENT_CLASS_INDETERMINATE,
    CONTENT_CLASS_RATING_ONLY,
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

    @staticmethod
    def classified_feedback(
        feedback_id: str,
        *,
        text: str = "",
        pros: str = "",
        cons: str = "",
        tags: list[object] | None = None,
        photo: bool = False,
        video: bool = False,
        rating: int = 5,
        created_at: str = "2026-07-20T10:00:00Z",
    ) -> dict:
        row = feedback(feedback_id, text=text)
        row["pros"] = pros
        row["cons"] = cons
        row["tags"] = list(tags or [])
        row["photoLinks"] = (
            [{"fullSize": "https://cdn.example/photo.jpg", "miniSize": "https://cdn.example/photo-mini.jpg"}]
            if photo
            else []
        )
        row["video"] = (
            [{"link": "https://cdn.example/video.mp4", "previewImage": "https://cdn.example/video.jpg"}]
            if video
            else []
        )
        row["productValuation"] = rating
        row["createdDate"] = created_at
        return row

    def test_canonical_content_classification_covers_every_content_surface(self) -> None:
        empty = {"text": "  ", "pros": "\n", "cons": "", "tags": ["", "  "], "media": [], "rating": 5}
        self.assertEqual(classify_feedback_content(json.dumps(empty)), CONTENT_CLASS_RATING_ONLY)
        for field in ("text", "pros", "cons"):
            with self.subTest(field=field):
                value = dict(empty)
                value[field] = "содержимое"
                self.assertEqual(
                    classify_feedback_content(json.dumps(value)),
                    CONTENT_CLASS_CONTENT_BEARING,
                )
        for value in (["важный тег"], [{"name": "важный тег"}]):
            with self.subTest(tags=value):
                tagged = dict(empty)
                tagged["tags"] = value
                self.assertEqual(
                    classify_feedback_content(json.dumps(tagged)),
                    CONTENT_CLASS_CONTENT_BEARING,
                )
        self.assertEqual(
            classify_feedback_content(json.dumps(empty), has_photo=True),
            CONTENT_CLASS_CONTENT_BEARING,
        )
        self.assertEqual(
            classify_feedback_content(json.dumps(empty), has_video=True),
            CONTENT_CLASS_CONTENT_BEARING,
        )
        self.assertEqual(
            classify_feedback_content(json.dumps(empty), canonical_media_present=True),
            CONTENT_CLASS_CONTENT_BEARING,
        )
        contradictory = dict(empty)
        contradictory["tags"] = "not-an-array"
        self.assertEqual(
            classify_feedback_content(json.dumps(contradictory)),
            CONTENT_CLASS_INDETERMINATE,
        )
        self.assertEqual(classify_feedback_content("not-json"), CONTENT_CLASS_INDETERMINATE)

    def test_only_true_rating_only_uses_zero_cost_processing_kind(self) -> None:
        self.enable("manual")
        variants = {
            "text": self.classified_feedback("text", text="текст"),
            "pros": self.classified_feedback("pros", pros="плюс"),
            "cons": self.classified_feedback("cons", cons="минус"),
            "tag": self.classified_feedback("tag", tags=["тег"]),
            "photo": self.classified_feedback("photo", photo=True),
            "video": self.classified_feedback("video", video=True),
            "combined": self.classified_feedback("combined", text="текст", tags=["тег"], photo=True),
            "empty": self.classified_feedback("empty", text="  ", tags=["", "  "]),
        }
        for feedback_id, row in variants.items():
            outcome = self.repo.upsert_feedback(row, source_stream="unanswered", run_kind="steady")
            expected = CONTENT_CLASS_RATING_ONLY if feedback_id == "empty" else CONTENT_CLASS_CONTENT_BEARING
            self.assertEqual(outcome["content_classification"], expected)
            job = self.repo.enqueue_manual_processing(feedback_id, content_version=1, actor_id="reviewer")
            self.assertEqual(
                job["processing_kind"],
                "rating_only_template" if feedback_id == "empty" else "frozen_ai",
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

    def test_first_additive_schema_backs_up_existing_database_once(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            db_path = runtime_dir / "registry_upload_runtime.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
                conn.execute("INSERT INTO legacy_marker(value) VALUES('preserved')")
            AutoanswersRepository(runtime_dir=runtime_dir, now_factory=self.clock, env={})
            backups = list((runtime_dir / "backups" / "wb_autoanswers_schema_v5").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(f"file:{backups[0].resolve()}?mode=ro", uri=True) as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT value FROM legacy_marker").fetchone()[0], "preserved")
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT value FROM legacy_marker").fetchone()[0], "preserved")
            AutoanswersRepository(runtime_dir=runtime_dir, now_factory=self.clock, env={})
            self.assertEqual(
                len(list((runtime_dir / "backups" / "wb_autoanswers_schema_v5").glob("*.sqlite3"))),
                1,
            )
            evidence = AutoanswersRepository(
                runtime_dir=runtime_dir, now_factory=self.clock, env={}
            ).verified_schema_backup_status()
            self.assertEqual(evidence["integrity_check"], "ok")
            self.assertTrue(str(evidence["sha256"]).startswith("sha256:"))
        self.enable()
        self.assertTrue(self.repo.settings().effective_enabled)
        self.env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
        settings = self.repo.settings()
        self.assertTrue(settings.master_enabled)
        self.assertFalse(settings.effective_enabled)
        with self.assertRaisesRegex(AutoanswersRuntimeError, "OFF"):
            self.repo.assert_effective_on(operation="test")

    def test_schema_v1_settings_constraint_migrates_to_manual_without_data_loss(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            repo = AutoanswersRepository(runtime_dir=runtime_dir, now_factory=self.clock, env={})
            repo.update_settings(mode="auto_safe", actor_id="admin")
            with sqlite3.connect(repo.db_path) as conn:
                conn.executescript(
                    """
                    DELETE FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations WHERE version IN (2,3,4,5);
                    ALTER TABLE sheet_vitrina_v1_wb_autoanswers_settings RENAME TO sheet_vitrina_v1_wb_autoanswers_settings_v2;
                    CREATE TABLE sheet_vitrina_v1_wb_autoanswers_settings(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        master_enabled INTEGER NOT NULL DEFAULT 0 CHECK(master_enabled IN (0,1)),
                        mode TEXT NOT NULL DEFAULT 'draft_only' CHECK(mode IN ('draft_only','auto_safe','auto_all')),
                        enable_epoch INTEGER NOT NULL DEFAULT 0,
                        enabled_at TEXT,
                        daily_cap_usd TEXT NOT NULL,
                        monthly_cap_usd TEXT NOT NULL,
                        warning_ratio TEXT NOT NULL,
                        max_reservation_per_review_usd TEXT NOT NULL,
                        policy_version TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO sheet_vitrina_v1_wb_autoanswers_settings
                    SELECT singleton, master_enabled, mode, enable_epoch, enabled_at,
                           daily_cap_usd, monthly_cap_usd, warning_ratio,
                           max_reservation_per_review_usd, policy_version, updated_at
                    FROM sheet_vitrina_v1_wb_autoanswers_settings_v2;
                    DROP TABLE sheet_vitrina_v1_wb_autoanswers_settings_v2;
                    """
                )
            migrated = AutoanswersRepository(runtime_dir=runtime_dir, now_factory=self.clock, env={})
            self.assertEqual(migrated.settings().mode, "auto_safe")
            migrated.update_settings(mode="manual", actor_id="admin")
            self.assertEqual(migrated.settings().mode, "manual")
            self.assertEqual(migrated.verified_schema_backup_status()["integrity_check"], "ok")

    def test_schema_v3_invalidates_only_unanswered_unpublished_media_uncertain_results(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            repo = AutoanswersRepository(runtime_dir=runtime_dir, now_factory=self.clock, env={})
            repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
            processing_keys: dict[str, str] = {}
            for feedback_id in ("must-regenerate", "owner-published"):
                repo.upsert_feedback(
                    feedback(feedback_id), source_stream="unanswered", run_kind="steady"
                )
                job = repo.enqueue_manual_processing(
                    feedback_id, content_version=1, actor_id="reviewer"
                )
                repo.claim_processing_job(worker_id=f"worker-{feedback_id}")
                repo.complete_generation(
                    job["processing_key"],
                    result=successful_result(media_uncertain=True),
                    worker_id=f"worker-{feedback_id}",
                )
                processing_keys[feedback_id] = job["processing_key"]
            repo.upsert_feedback(
                feedback("owner-published", answer="Ответ владельца"),
                source_stream="answered",
                run_kind="reconciliation",
            )
            with sqlite3.connect(repo.db_path) as conn:
                conn.execute(
                    "DELETE FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations WHERE version=3"
                )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET regeneration_required=0, regeneration_reason=NULL,
                        state='generated', review_reasons_json='[]'
                    WHERE processing_key IN (?, ?)
                    """,
                    (
                        processing_keys["must-regenerate"],
                        processing_keys["owner-published"],
                    ),
                )
            migrated = AutoanswersRepository(
                runtime_dir=runtime_dir, now_factory=self.clock, env={}
            )
            must_regenerate = migrated.get_feedback("must-regenerate")["ai_jobs"][0]
            preserved = migrated.get_feedback("owner-published")["ai_jobs"][0]
            self.assertTrue(must_regenerate["regeneration_required"])
            self.assertEqual(must_regenerate["state"], "needs_review")
            self.assertFalse(preserved["regeneration_required"])
            self.assertEqual(preserved["state"], "generated")
            self.assertEqual(
                migrated.get_feedback("owner-published")["answer"]["text"],
                "Ответ владельца",
            )

    def test_emergency_force_off_prevents_persisting_master_on(self) -> None:
        self.env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
        with self.assertRaisesRegex(AutoanswersRuntimeError, "force-off"):
            self.repo.update_settings(master_enabled=True, actor_id="admin")
        self.assertFalse(self.repo.settings().master_enabled)

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
        with self.assertRaisesRegex(AutoanswersRuntimeError, "capped mode-transition"):
            self.repo.preview_backlog(actor_id="test-admin")
        preview = self.repo.preview_mode_transition(
            "auto_safe", actor_id="test-admin", run_max_paid_reviews=1
        )
        applied = self.repo.apply_mode_transition(
            "auto_safe", actor_id="test-admin", preview_id=preview["preview_id"]
        )
        self.assertIsNotNone(applied["sweep"])

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
            "queued",
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

    def test_transition_run_claims_all_content_newest_first_before_legacy_rating_jobs(self) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="draft_only",
            max_materialized_processing_jobs=1,
            actor_id="admin",
        )
        old_rating = self.classified_feedback(
            "rating-old", created_at="2026-07-19T10:00:00Z", rating=1
        )
        rating_outcome = self.repo.upsert_feedback(
            old_rating, source_stream="unanswered", run_kind="steady"
        )
        legacy_rating_job = self.repo.enqueue_processing(
            "rating-old", trigger_source="steady_sync", actor_id="sync"
        )
        self.assertEqual(legacy_rating_job["processing_kind"], "rating_only_template")

        self.repo.update_settings(mode="manual", actor_id="admin")
        for row in (
            self.classified_feedback(
                "content-old", text="старый содержательный", created_at="2026-07-18T10:00:00Z", rating=5
            ),
            self.classified_feedback(
                "content-new", text="свежий содержательный", created_at="2026-07-21T10:00:00Z", rating=1
            ),
            self.classified_feedback(
                "rating-new", created_at="2026-07-21T11:00:00Z", rating=5
            ),
        ):
            self.repo.upsert_feedback(row, source_stream="archive", run_kind="backfill")

        preview = self.repo.preview_mode_transition(
            "draft_only", actor_id="admin", run_max_usd="1.00"
        )
        self.assertEqual(preview["counts"]["content_bearing"], 2)
        self.assertEqual(preview["counts"]["rating_only"], 2)
        applied = self.repo.apply_mode_transition(
            "draft_only", actor_id="admin", preview_id=preview["preview_id"]
        )
        run_id = applied["sweep"]["transition_run_id"]

        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        first = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(first["feedback_id"], "content-new")
        self.repo.settle_budget(first["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            first["processing_key"], result=successful_result(), worker_id="worker"
        )

        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        second = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(second["feedback_id"], "content-old")
        self.repo.settle_budget(second["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            second["processing_key"], result=successful_result(), worker_id="worker"
        )

        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        third = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(third["feedback_id"], "rating-new")
        self.repo.complete_rating_only_template(third["processing_key"], worker_id="worker")
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        fourth = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(fourth["feedback_id"], "rating-old")
        self.assertEqual(fourth["processing_key"], legacy_rating_job["processing_key"])
        self.assertEqual(fourth["transition_run_id"], run_id)
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE feedback_id='rating-old'"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(rating_outcome["content_classification"], CONTENT_CLASS_RATING_ONLY)

    def test_budget_pause_and_human_only_content_do_not_open_or_deadlock_rating_gate(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
        self.repo.upsert_feedback(
            self.classified_feedback("content-blocked", text="содержимое"),
            source_stream="archive",
            run_kind="backfill",
        )
        self.repo.upsert_feedback(
            self.classified_feedback("rating-waits"),
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "draft_only", actor_id="admin", run_max_paid_reviews=1
        )
        self.repo.apply_mode_transition(
            "draft_only", actor_id="admin", preview_id=preview["preview_id"]
        )
        self.repo.update_settings(hourly_cap_usd="0.01", actor_id="admin")
        status = self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        self.assertEqual(status["pause_reason"], "hourly_budget_reached")
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))
        self.assertEqual(self.repo.get_feedback("rating-waits")["ai_jobs"], [])

        # A hard-gate/manual-review content result is not automatic work and
        # therefore cannot hold the empty-review barrier forever.
        with sqlite3.connect(self.repo.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswer_jobs(
                    processing_key,feedback_id,content_version,content_version_hash,
                    state,trigger_source,bundle_version,evaluation_signature,
                    policy_version,enable_epoch,policy_epoch,processing_kind,
                    transition_run_id,available_at,attempts,created_at,updated_at,
                    hard_gates_passed,node_contract_valid
                )
                SELECT 'manual-review-evidence',f.feedback_id,f.content_version,f.content_version_hash,
                       'needs_review','policy_reconciliation','1.4.2','wb-autoanswers-evaluation-v1',
                       'owner-policy-2026-07-21-v3',s.enable_epoch,s.policy_epoch,'frozen_ai',
                       r.transition_run_id,datetime('now'),0,datetime('now'),datetime('now'),0,1
                FROM sheet_vitrina_v1_wb_feedbacks f
                CROSS JOIN sheet_vitrina_v1_wb_autoanswers_settings s
                JOIN sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps r
                  ON r.policy_epoch=s.policy_epoch
                WHERE f.feedback_id='content-blocked'
                """
            )
        self.repo.update_settings(hourly_cap_usd="0.50", actor_id="admin")
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        rating = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(rating["feedback_id"], "rating-waits")

    def test_reviews_seen_after_preview_remain_outside_current_run(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
        self.repo.upsert_feedback(
            self.classified_feedback("in-scope", text="scope"),
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "draft_only", actor_id="admin", run_max_usd="0.50"
        )
        self.repo.apply_mode_transition(
            "draft_only", actor_id="admin", preview_id=preview["preview_id"]
        )
        outside = self.repo.upsert_feedback(
            self.classified_feedback("outside", text="new"),
            source_stream="unanswered",
            run_kind="steady",
        )
        self.assertFalse(outside["auto_enqueue"])
        with self.assertRaisesRegex(AutoanswersRuntimeError, "outside the immutable"):
            self.repo.enqueue_processing(
                "outside", trigger_source="steady_sync", actor_id="sync"
            )
        self.assertEqual(self.repo.progress_status()["outside_current_run"], 1)
        self.repo.upsert_feedback(
            self.classified_feedback("in-scope", text="changed after apply"),
            source_stream="detail",
            run_kind="reconciliation",
        )
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        self.assertEqual(self.repo.get_feedback("in-scope")["ai_jobs"], [])
        self.assertEqual(self.repo.progress_status()["outside_current_run"], 2)

    def test_four_progress_stages_share_scope_and_exclude_rating_from_content_card(self) -> None:
        empty_progress = self.repo.progress_status()
        self.assertIsNone(empty_progress["all_preparation"]["percent"])
        self.assertIsNone(empty_progress["content_bearing_preparation"]["percent"])

        self.repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
        self.repo.upsert_feedback(
            self.classified_feedback("content", text="содержимое"),
            source_stream="archive",
            run_kind="backfill",
        )
        self.repo.upsert_feedback(
            self.classified_feedback("rating"),
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "draft_only", actor_id="admin", run_max_usd="0.50"
        )
        self.repo.apply_mode_transition(
            "draft_only", actor_id="admin", preview_id=preview["preview_id"]
        )
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        content = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(content["feedback_id"], "content")
        self.repo.settle_budget(content["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            content["processing_key"], result=successful_result(), worker_id="worker"
        )
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        rating = self.repo.claim_processing_job(worker_id="worker")
        self.repo.complete_rating_only_template(rating["processing_key"], worker_id="worker")
        self.repo.update_settings(mode="manual", actor_id="admin")

        progress = self.repo.progress_status()
        self.assertEqual(progress["all_preparation"], {
            "done": 2,
            "total": 2,
            "remaining": 0,
            "percent": 100.0,
            "status": "paused_manual",
            "pause_reason": "manual_pause",
        })
        self.assertEqual(progress["all_publication"]["done"], 0)
        self.assertEqual(progress["all_publication"]["total"], 2)
        self.assertEqual(progress["all_publication"]["percent"], 0.0)
        self.assertEqual(progress["content_bearing_preparation"]["done"], 1)
        self.assertEqual(progress["content_bearing_preparation"]["total"], 1)
        self.assertEqual(progress["content_bearing_preparation"]["percent"], 100.0)
        self.assertEqual(progress["content_bearing_publication"]["done"], 0)
        self.assertEqual(progress["content_bearing_publication"]["total"], 1)
        self.assertEqual(progress["rating_only_total"], 1)
        self.assertEqual(progress["content_bearing_total"], 1)
        restarted = AutoanswersRepository(
            runtime_dir=Path(self.temp.name), now_factory=self.clock, env=self.env
        )
        self.assertEqual(
            restarted.progress_status()["content_bearing_preparation"],
            progress["content_bearing_preparation"],
        )
        changed = self.classified_feedback("content", text="новая версия содержимого")
        self.repo.upsert_feedback(changed, source_stream="detail", run_kind="reconciliation")
        stale_progress = self.repo.progress_status()
        self.assertEqual(stale_progress["all_preparation"]["total"], 2)
        self.assertEqual(stale_progress["content_bearing_preparation"]["total"], 1)
        self.assertEqual(stale_progress["content_bearing_preparation"]["done"], 0)
        self.assertEqual(stale_progress["content_bearing_stale_or_regeneration"], 1)
        self.assertEqual(stale_progress["outside_current_run"], 1)

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

    def test_five_selector_states_and_force_off_precedence(self) -> None:
        self.assertFalse(self.repo.settings().master_enabled)
        for mode in ("manual", "draft_only", "auto_safe", "auto_all"):
            with self.subTest(mode=mode):
                settings = self.repo.update_settings(master_enabled=True, mode=mode, actor_id="admin")
                self.assertTrue(settings.effective_enabled)
                self.assertEqual(settings.mode, mode)
                settings = self.repo.update_settings(master_enabled=False, actor_id="admin")
                self.assertFalse(settings.effective_enabled)
        self.repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
        self.env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
        settings = self.repo.settings()
        self.assertTrue(settings.master_enabled)
        self.assertEqual(settings.mode, "manual")
        self.assertFalse(settings.effective_enabled)
        with self.assertRaisesRegex(AutoanswersRuntimeError, "OFF"):
            self.repo.enqueue_manual_processing("missing", content_version=1, actor_id="reviewer")

    def test_manual_sync_never_enqueues_and_click_is_idempotent(self) -> None:
        self.enable("manual")
        outcome = self.insert_new("manual-review")
        self.assertFalse(outcome["auto_enqueue"])
        self.assertIsNone(outcome["auto_eligible_epoch"])
        first = self.repo.enqueue_manual_processing(
            "manual-review", content_version=1, actor_id="reviewer"
        )
        second = self.repo.enqueue_manual_processing(
            "manual-review", content_version=1, actor_id="reviewer"
        )
        self.assertEqual(first["processing_key"], second["processing_key"])
        self.assertEqual(len(self.repo.get_feedback("manual-review")["ai_jobs"]), 1)
        claimed = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(claimed["trigger_source"], "manual_generate")

    def test_manual_result_is_stale_after_content_change(self) -> None:
        self.enable("manual")
        self.insert_new("manual-stale")
        job = self.repo.enqueue_manual_processing(
            "manual-stale", content_version=1, actor_id="reviewer"
        )
        self.repo.claim_processing_job(worker_id="worker")
        self.repo.complete_generation(
            job["processing_key"], result=successful_result(), worker_id="worker"
        )
        self.repo.upsert_feedback(
            feedback("manual-stale", text="Смысл отзыва изменился"),
            source_stream="detail",
            run_kind="reconciliation",
        )
        with self.assertRaisesRegex(AutoanswersRuntimeError, "stale"):
            self.repo.manual_guard_context(job["processing_key"])

    def test_manual_mode_disables_backlog_preview(self) -> None:
        self.enable("manual")
        self.repo.upsert_feedback(feedback("history"), source_stream="archive", run_kind="backfill")
        with self.assertRaisesRegex(AutoanswersRuntimeError, "capped mode-transition"):
            self.repo.preview_backlog(actor_id="admin")

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
            hourly_cap_usd="0.15",
            daily_cap_usd="1.40",
            monthly_cap_usd="5.00",
            global_paid_review_concurrency=2,
            max_inflight_role_calls=2,
            warning_ratio="0.60",
            actor_id="admin",
        )
        for feedback_id in ("one", "two"):
            self.insert_new(feedback_id)
            self.repo.enqueue_processing(feedback_id, trigger_source="automatic", actor_id="sync")
        self.assertIsNotNone(self.repo.claim_processing_job(worker_id="w1"))
        self.assertTrue(self.repo.budget_status()["warning"])
        self.assertIsNone(self.repo.claim_processing_job(worker_id="w2"))
        self.assertEqual(self.repo.progress_status()["stop_reason"], "hourly_budget_reached")

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

    def test_transition_preview_policy_epoch_and_resumable_sweep(self) -> None:
        self.enable("manual")
        for feedback_id in ("ready", "media-failed", "untouched"):
            self.insert_new(feedback_id)

        ready = self.repo.enqueue_manual_processing("ready", content_version=1, actor_id="reviewer")
        self.repo.claim_processing_job(worker_id="worker")
        self.repo.settle_budget(ready["processing_key"], actual_cost_usd="0.02")
        self.repo.complete_generation(
            ready["processing_key"], result=successful_result("public_only"), worker_id="worker"
        )

        failed = self.repo.enqueue_manual_processing("media-failed", content_version=1, actor_id="reviewer")
        self.repo.claim_processing_job(worker_id="worker")
        self.repo.settle_budget(failed["processing_key"], actual_cost_usd="0.03")
        self.repo.complete_generation(
            failed["processing_key"],
            result=successful_result("public_only", media_uncertain=True),
            worker_id="worker",
        )

        before = self.repo.settings().policy_epoch
        preview = self.repo.preview_mode_transition("auto_safe", actor_id="admin", run_max_usd="0.50")
        self.assertEqual(preview["counts"]["unanswered_total"], 3)
        self.assertEqual(preview["counts"]["current_ready"], 1)
        self.assertEqual(preview["counts"]["needs_generation"], 1)
        self.assertEqual(preview["counts"]["needs_regeneration"], 1)
        self.assertEqual(preview["counts"]["automatic_publication"], 1)
        applied = self.repo.apply_mode_transition(
            "auto_safe", actor_id="admin", preview_id=preview["preview_id"]
        )
        self.assertEqual(applied["settings"].policy_epoch, before + 1)
        self.assertEqual(applied["sweep"]["state"], "queued")

        # A restart uses a new repository object over the same durable DB.
        restarted = AutoanswersRepository(
            runtime_dir=Path(self.temp.name), now_factory=self.clock, env=self.env
        )
        for _ in range(6):
            status = restarted.reconcile_policy_sweep_once(worker_id="restarted", batch_size=1)
            if status and status["state"] == "succeeded":
                break
        self.assertEqual(restarted.reconciliation_status()["state"], "succeeded")
        ready_detail = restarted.get_feedback("ready")
        self.assertEqual(ready_detail["generated_reply"], "Спасибо за отзыв!")
        self.assertEqual(len(ready_detail["publications"]), 1)
        self.assertEqual(restarted.get_feedback("media-failed")["ai_jobs"][0]["state"], "queued")
        self.assertEqual(len(restarted.get_feedback("media-failed")["ai_revisions"]), 1)
        self.assertEqual(restarted.get_feedback("untouched")["ai_jobs"][0]["state"], "queued")

    def test_mode_transition_preview_is_snapshot_bound_and_idempotent(self) -> None:
        self.enable("manual")
        self.insert_new("scope")
        preview = self.repo.preview_mode_transition("draft_only", actor_id="admin", run_max_usd="0.50")
        self.insert_new("scope-changed")
        with self.assertRaisesRegex(AutoanswersRuntimeError, "scope changed"):
            self.repo.apply_mode_transition(
                "draft_only", actor_id="admin", preview_id=preview["preview_id"]
            )
        fresh = self.repo.preview_mode_transition("draft_only", actor_id="admin", run_max_usd="0.50")
        first = self.repo.apply_mode_transition(
            "draft_only", actor_id="admin", preview_id=fresh["preview_id"]
        )
        second = self.repo.apply_mode_transition(
            "draft_only", actor_id="admin", preview_id=fresh["preview_id"]
        )
        self.assertEqual(first["sweep"]["sweep_id"], second["sweep"]["sweep_id"])

        same_mode_preview = self.repo.preview_mode_transition("draft_only", actor_id="admin", run_max_usd="0.50")
        policy_before = self.repo.settings().policy_epoch
        same_mode = self.repo.apply_mode_transition(
            "draft_only", actor_id="admin", preview_id=same_mode_preview["preview_id"]
        )
        self.assertEqual(same_mode["settings"].policy_epoch, policy_before + 1)
        self.assertNotEqual(same_mode["sweep"]["sweep_id"], first["sweep"]["sweep_id"])

    def test_all_five_state_transitions_and_force_off_are_fail_closed(self) -> None:
        states = ("off", "manual", "draft_only", "auto_safe", "auto_all")

        def apply(repo: AutoanswersRepository, target: str, actor: str = "admin") -> None:
            if target in {"off", "manual"}:
                repo.apply_mode_transition(target, actor_id=actor)
                return
            preview = repo.preview_mode_transition(target, actor_id=actor, run_max_usd="0.50")
            repo.apply_mode_transition(target, actor_id=actor, preview_id=preview["preview_id"])

        for source in states:
            for target in states:
                with self.subTest(source=source, target=target), TemporaryDirectory() as directory:
                    env: dict[str, str] = {}
                    repo = AutoanswersRepository(
                        runtime_dir=Path(directory), now_factory=self.clock, env=env
                    )
                    apply(repo, source)
                    apply(repo, target)
                    settings = repo.settings()
                    self.assertEqual(settings.master_enabled, target != "off")
                    if target != "off":
                        self.assertEqual(settings.mode, target)

        with TemporaryDirectory() as directory:
            env = {}
            repo = AutoanswersRepository(runtime_dir=Path(directory), now_factory=self.clock, env=env)
            preview = repo.preview_mode_transition("auto_safe", actor_id="admin", run_max_usd="0.50")
            env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
            with self.assertRaisesRegex(AutoanswersRuntimeError, "forced OFF"):
                repo.apply_mode_transition(
                    "auto_safe", actor_id="admin", preview_id=preview["preview_id"]
                )

    def test_transition_preview_is_actor_bound(self) -> None:
        self.enable("manual")
        preview = self.repo.preview_mode_transition("auto_safe", actor_id="first-admin", run_max_usd="0.50")
        with self.assertRaisesRegex(AutoanswersRuntimeError, "another actor"):
            self.repo.apply_mode_transition(
                "auto_safe", actor_id="second-admin", preview_id=preview["preview_id"]
            )

    def test_regeneration_archives_old_result_and_is_click_idempotent(self) -> None:
        self.enable("manual")
        self.insert_new("regen")
        job = self.repo.enqueue_manual_processing("regen", content_version=1, actor_id="reviewer")
        self.repo.claim_processing_job(worker_id="worker")
        self.repo.settle_budget(job["processing_key"], actual_cost_usd="0.04")
        stored = self.repo.complete_generation(
            job["processing_key"],
            result=successful_result(media_uncertain=True),
            worker_id="worker",
        )
        self.assertTrue(stored["regeneration_required"])
        with self.assertRaisesRegex(AutoanswersRuntimeError, "uncertain"):
            self.repo.manual_guard_context(job["processing_key"])
        first = self.repo.request_regeneration(job["processing_key"], actor_id="reviewer")
        second = self.repo.request_regeneration(job["processing_key"], actor_id="reviewer")
        self.assertEqual(first["media_processing_version"], 2)
        self.assertEqual(second["media_processing_version"], 2)
        detail = self.repo.get_feedback("regen")
        self.assertEqual(len(detail["ai_revisions"]), 1)
        self.assertEqual(detail["ai_revisions"][0]["actual_cost_usd"], "0.04000000")
        self.assertEqual(detail["ai_jobs"][0]["state"], "queued")

    def test_downgrade_changes_policy_epoch_and_blocks_old_processing_claim(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        self.insert_new("old-policy")
        self.repo.enqueue_processing("old-policy", trigger_source="steady_sync", actor_id="sync")
        previous = self.repo.settings().policy_epoch
        self.repo.update_settings(mode="manual", actor_id="admin")
        self.assertGreater(self.repo.settings().policy_epoch, previous)
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))
        self.assertEqual(self.repo.get_feedback("old-policy")["ai_jobs"][0]["state"], "queued")


if __name__ == "__main__":
    unittest.main(verbosity=2)
