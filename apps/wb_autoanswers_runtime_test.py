#!/usr/bin/env python3
"""Free local checks for the WB autoanswers durable runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest

from packages.application.wb_autoanswers_runtime import (
    AUTOANSWERS_DB_FILENAME,
    AUTOANSWERS_STORE_MANIFEST,
    LEGACY_RUNTIME_DB_FILENAME,
    AutoanswersRepository,
    AutoanswersRuntimeError,
    SCHEMA_VERSION,
    autoanswers_settings_revision,
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
        self.enable()
        self.assertTrue(self.repo.settings().effective_enabled)
        self.env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
        settings = self.repo.settings()
        self.assertTrue(settings.master_enabled)
        self.assertFalse(settings.effective_enabled)
        with self.assertRaisesRegex(AutoanswersRuntimeError, "OFF"):
            self.repo.assert_effective_on(operation="test")

    def test_applied_schema_startup_does_not_compete_for_writer_lock(self) -> None:
        blocker = sqlite3.connect(self.repo.db_path, timeout=1, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            reopened = AutoanswersRepository(
                runtime_dir=Path(self.temp.name),
                now_factory=self.clock,
                env=self.env,
            )
            self.assertEqual(reopened.settings().policy_epoch, 0)
        finally:
            blocker.rollback()
            blocker.close()

    def test_schema_v8_adds_acknowledgements_without_rewriting_execution_evidence(
        self,
    ) -> None:
        self.enable("auto_all")
        self.insert_new("schema-v8-evidence")
        job = self.repo.enqueue_processing(
            "schema-v8-evidence",
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
        immutable_tables = (
            "sheet_vitrina_v1_wb_autoanswer_jobs",
            "sheet_vitrina_v1_wb_publication_jobs",
            "sheet_vitrina_v1_wb_autoanswers_budget_reservations",
            "sheet_vitrina_v1_wb_autoanswers_cost_events",
        )
        with sqlite3.connect(self.repo.db_path) as conn:
            before = {
                table: conn.execute(
                    f"SELECT * FROM {table} ORDER BY 1"  # noqa: S608 - fixed allowlist
                ).fetchall()
                for table in immutable_tables
            }
            conn.execute(
                "DELETE FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations WHERE version=8"
            )
            conn.execute(
                "DROP TABLE sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements"
            )

        migrated = AutoanswersRepository(
            runtime_dir=Path(self.temp.name),
            now_factory=self.clock,
            env=self.env,
        )
        with sqlite3.connect(migrated.db_path) as conn:
            after = {
                table: conn.execute(
                    f"SELECT * FROM {table} ORDER BY 1"  # noqa: S608 - fixed allowlist
                ).fetchall()
                for table in immutable_tables
            }
            versions = {
                int(row[0])
                for row in conn.execute(
                    "SELECT version FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations"
                ).fetchall()
            }
            acknowledgement_table = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table'
                  AND name='sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements'
                """
            ).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(versions, set(range(1, SCHEMA_VERSION + 1)))
        self.assertIsNotNone(acknowledgement_table)
        self.assertEqual(
            migrated.verified_schema_backup_status()["integrity_check"],
            "ok",
        )

    def test_new_isolated_store_does_not_modify_unrelated_registry_database(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            db_path = runtime_dir / "registry_upload_runtime.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
                conn.execute("INSERT INTO legacy_marker(value) VALUES('preserved')")
            AutoanswersRepository(runtime_dir=runtime_dir, now_factory=self.clock, env={})
            backups = list(
                (
                    runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
                ).glob("*.sqlite3")
            )
            self.assertEqual(backups, [])
            self.assertTrue((runtime_dir / "wb_autoanswers_runtime.sqlite3").is_file())
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT value FROM legacy_marker").fetchone()[0], "preserved")
            AutoanswersRepository(runtime_dir=runtime_dir, now_factory=self.clock, env={})
            self.assertEqual(
                list(
                    (
                        runtime_dir
                        / "backups"
                        / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
                    ).glob("*.sqlite3")
                ),
                [],
            )

    def test_legacy_autoanswers_tables_migrate_to_reconciled_isolated_store(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            legacy_seed = AutoanswersRepository(
                runtime_dir=runtime_dir,
                now_factory=self.clock,
                env={},
            )
            legacy_seed.update_settings(
                master_enabled=True,
                mode="manual",
                actor_id="migration-test",
            )
            with sqlite3.connect(legacy_seed.db_path) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            os.replace(
                runtime_dir / AUTOANSWERS_DB_FILENAME,
                runtime_dir / LEGACY_RUNTIME_DB_FILENAME,
            )
            (runtime_dir / AUTOANSWERS_STORE_MANIFEST).unlink(missing_ok=True)

            migrated = AutoanswersRepository(
                runtime_dir=runtime_dir,
                now_factory=self.clock,
                env={},
            )
            self.assertEqual(migrated.store_status["status"], "migrated")
            self.assertEqual(migrated.store_status["integrity_check"], "ok")
            self.assertTrue(migrated.settings().master_enabled)
            manifest = json.loads(
                (runtime_dir / AUTOANSWERS_STORE_MANIFEST).read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(manifest["legacy_retained"])
            self.assertTrue(
                all(
                    item["matching"]
                    for item in manifest["table_evidence"].values()
                )
            )
            manifest["status"] = "prepared"
            (runtime_dir / AUTOANSWERS_STORE_MANIFEST).write_text(
                json.dumps(manifest, sort_keys=True),
                encoding="utf-8",
            )
            recovered = AutoanswersRepository(
                runtime_dir=runtime_dir,
                now_factory=self.clock,
                env={},
            )
            self.assertEqual(
                recovered.store_status["migration_status"],
                "migrated",
            )
            recovered_manifest = json.loads(
                (runtime_dir / AUTOANSWERS_STORE_MANIFEST).read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(recovered_manifest["recovered_after_interruption"])

    def test_schema_v1_settings_constraint_migrates_to_manual_without_data_loss(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            repo = AutoanswersRepository(runtime_dir=runtime_dir, now_factory=self.clock, env={})
            repo.update_settings(mode="auto_safe", actor_id="admin")
            with sqlite3.connect(repo.db_path) as conn:
                conn.executescript(
                    """
                    DELETE FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations WHERE version>=2;
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

    def test_transition_ordinals_materialization_and_processing_share_rating_priority(self) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="manual",
            max_materialized_processing_jobs=20,
            actor_id="admin",
        )
        rows = (
            self.classified_feedback(
                "content-5-newest",
                text="пять",
                rating=5,
                created_at="2026-07-24T12:00:00Z",
            ),
            self.classified_feedback(
                "content-1-old",
                text="один старый",
                rating=1,
                created_at="2026-07-20T10:00:00Z",
            ),
            self.classified_feedback(
                "content-2",
                text="два",
                rating=2,
                created_at="2026-07-23T10:00:00Z",
            ),
            self.classified_feedback(
                "content-1-new",
                text="один новый",
                rating=1,
                created_at="2026-07-21T10:00:00Z",
            ),
            self.classified_feedback(
                "content-3",
                text="три",
                rating=3,
                created_at="2026-07-22T10:00:00Z",
            ),
            self.classified_feedback(
                "content-4",
                text="четыре",
                rating=4,
                created_at="2026-07-24T11:00:00Z",
            ),
            self.classified_feedback(
                "rating-old",
                rating=1,
                created_at="2026-07-19T10:00:00Z",
            ),
            self.classified_feedback(
                "rating-new",
                rating=5,
                created_at="2026-07-25T10:00:00Z",
            ),
        )
        for row in rows:
            self.repo.upsert_feedback(
                row,
                source_stream="archive",
                run_kind="backfill",
            )
        expected = [
            "content-1-new",
            "content-1-old",
            "content-2",
            "content-3",
            "content-4",
            "content-5-newest",
            "rating-new",
            "rating-old",
        ]
        preview = self.repo.preview_mode_transition(
            "draft_only",
            actor_id="admin",
            run_max_usd="10.00",
        )
        applied = self.repo.apply_mode_transition(
            "draft_only",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        sweep_id = applied["sweep"]["sweep_id"]
        run_id = applied["sweep"]["transition_run_id"]
        with sqlite3.connect(self.repo.db_path) as conn:
            ordinal_order = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT feedback_id
                    FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope
                    WHERE sweep_id=?
                    ORDER BY ordinal
                    """,
                    (sweep_id,),
                ).fetchall()
            ]
        self.assertEqual(ordinal_order, expected)

        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=1)
        with sqlite3.connect(self.repo.db_path) as conn:
            first_materialized = conn.execute(
                """
                SELECT feedback_id
                FROM sheet_vitrina_v1_wb_autoanswer_jobs
                WHERE transition_run_id=?
                """,
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(first_materialized, expected[0])
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)

        claimed_order: list[str] = []
        for _feedback_id in expected[:6]:
            self.repo.reconcile_policy_sweep_once(
                worker_id="reconcile",
                batch_size=25,
            )
            claimed = self.repo.claim_processing_job(worker_id="worker")
            self.assertIsNotNone(claimed)
            claimed_order.append(str(claimed["feedback_id"]))
            self.repo.settle_budget(
                claimed["processing_key"],
                actual_cost_usd="0.01",
            )
            self.repo.complete_generation(
                claimed["processing_key"],
                result=successful_result(),
                worker_id="worker",
            )
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile", batch_size=25)
        with self.repo.transaction() as conn:
            content_key = str(
                conn.execute(
                    """
                    SELECT processing_key
                    FROM sheet_vitrina_v1_wb_autoanswer_jobs
                    WHERE feedback_id='content-5-newest'
                    """
                ).fetchone()[0]
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET regeneration_required=1, media_uncertain=1,
                    regeneration_reason='test_new_content_work'
                WHERE processing_key=?
                """,
                (content_key,),
            )
        self.repo.request_regeneration(
            content_key,
            actor_id="reconcile",
            trigger_source="policy_reconciliation",
            transition_run_id=run_id,
        )
        preempting_content = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(preempting_content["feedback_id"], "content-5-newest")
        self.repo.settle_budget(
            preempting_content["processing_key"],
            actual_cost_usd="0.01",
        )
        self.repo.complete_generation(
            preempting_content["processing_key"],
            result=successful_result(),
            worker_id="worker",
        )
        for _feedback_id in expected[6:]:
            claimed = self.repo.claim_processing_job(worker_id="worker")
            self.assertIsNotNone(claimed)
            claimed_order.append(str(claimed["feedback_id"]))
            self.repo.complete_rating_only_template(
                claimed["processing_key"],
                worker_id="worker",
            )
        self.assertEqual(claimed_order, expected)

    def test_rolling_admission_is_incremental_idempotent_and_version_safe(self) -> None:
        self.repo.upsert_feedback(
            self.classified_feedback(
                "initial-3",
                text="initial",
                rating=3,
            ),
            source_stream="archive",
            run_kind="backfill",
        )
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
        initial = self.repo.refresh_rolling_admissions(actor_id="scheduler")
        self.assertEqual(initial["admitted"], 0)

        self.clock.advance(1)
        self.repo.upsert_feedback(
            self.classified_feedback(
                "rolling-1",
                text="new urgent",
                rating=1,
                created_at="2026-07-20T12:00:01Z",
            ),
            source_stream="unanswered",
            run_kind="steady",
            sync_run_id="sync-1",
        )
        self.repo.upsert_feedback(
            self.classified_feedback(
                "rolling-rating",
                rating=5,
                created_at="2026-07-20T12:00:01Z",
            ),
            source_stream="unanswered",
            run_kind="steady",
            sync_run_id="sync-1",
        )
        admitted = self.repo.refresh_rolling_admissions(actor_id="scheduler")
        self.assertEqual(admitted["admitted"], 2)
        self.assertEqual(
            admitted["admitted_by_class"],
            {"content_bearing": 1, "rating_only": 1},
        )
        replay = self.repo.refresh_rolling_admissions(actor_id="scheduler")
        self.assertEqual(replay["admitted"], 0)

        restarted = AutoanswersRepository(
            runtime_dir=Path(self.temp.name),
            now_factory=self.clock,
            env=self.env,
        )
        self.assertEqual(
            restarted.refresh_rolling_admissions(actor_id="restarted")["admitted"],
            0,
        )
        runtime_progress = restarted.progress_status()
        progress = runtime_progress["rolling_admission"]
        self.assertEqual(progress["initial_membership"], 1)
        self.assertEqual(progress["admitted_since_start"], 2)
        self.assertEqual(progress["current_total"], 3)
        self.assertEqual(runtime_progress["scope_total"], 3)
        self.assertEqual(runtime_progress["content_bearing_total"], 2)
        self.assertEqual(runtime_progress["rating_only_total"], 1)
        self.assertEqual(
            progress["current_priority_bucket"],
            "content_bearing_1_star",
        )

        self.clock.advance(1)
        changed = self.classified_feedback(
            "rolling-rating",
            text="new content version",
            rating=2,
            created_at="2026-07-20T12:00:01Z",
        )
        outcome = restarted.upsert_feedback(
            changed,
            source_stream="unanswered",
            run_kind="steady",
            sync_run_id="sync-2",
        )
        self.assertEqual(outcome["content_version"], 2)
        changed_admission = restarted.refresh_rolling_admissions(
            actor_id="scheduler"
        )
        self.assertEqual(changed_admission["admitted"], 1)
        runtime_progress = restarted.progress_status()
        progress = runtime_progress["rolling_admission"]
        self.assertEqual(progress["admitted_since_start"], 3)
        self.assertEqual(progress["current_total"], 3)
        self.assertEqual(runtime_progress["scope_total"], 3)
        self.assertEqual(runtime_progress["content_bearing_total"], 3)
        self.assertEqual(runtime_progress["rating_only_total"], 0)
        with sqlite3.connect(restarted.db_path) as conn:
            rows = conn.execute(
                """
                SELECT feedback_id,content_version,content_version_hash
                FROM sheet_vitrina_v1_wb_autoanswers_rolling_admissions
                WHERE transition_run_id=?
                ORDER BY feedback_id,content_version
                """,
                (applied["sweep"]["transition_run_id"],),
            ).fetchall()
        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [("rolling-1", 1), ("rolling-rating", 1), ("rolling-rating", 2)],
        )

    def test_new_high_priority_blocks_ready_lower_publication(self) -> None:
        self.repo.upsert_feedback(
            self.classified_feedback("ready-5", text="low", rating=5),
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
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        low = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(low["feedback_id"], "ready-5")
        self.repo.settle_budget(low["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            low["processing_key"],
            result=successful_result(),
            worker_id="worker",
        )

        self.clock.advance(1)
        self.repo.upsert_feedback(
            self.classified_feedback(
                "urgent-1",
                text="urgent",
                rating=1,
                created_at="2026-07-20T12:00:01Z",
            ),
            source_stream="unanswered",
            run_kind="steady",
        )
        self.repo.refresh_rolling_admissions(actor_id="scheduler")
        self.assertIsNone(self.repo.claim_publication_job(worker_id="publisher"))
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        urgent = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(urgent["feedback_id"], "urgent-1")
        self.assertIsNone(self.repo.claim_publication_job(worker_id="publisher"))
        self.repo.settle_budget(urgent["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            urgent["processing_key"],
            result=successful_result(),
            worker_id="worker",
        )
        publication = self.repo.claim_publication_job(worker_id="publisher")
        self.assertEqual(publication["feedback_id"], "urgent-1")

    def test_inflight_lower_result_defers_publication_enqueue_until_bucket_opens(
        self,
    ) -> None:
        self.repo.upsert_feedback(
            self.classified_feedback("inflight-5", text="low", rating=5),
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
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        low = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(low["feedback_id"], "inflight-5")

        self.clock.advance(1)
        self.repo.upsert_feedback(
            self.classified_feedback(
                "urgent-before-completion",
                text="urgent",
                rating=1,
                created_at="2026-07-20T12:00:01Z",
            ),
            source_stream="unanswered",
            run_kind="steady",
        )
        self.repo.refresh_rolling_admissions(actor_id="scheduler")
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")

        self.repo.settle_budget(low["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            low["processing_key"],
            result=successful_result(),
            worker_id="worker",
        )
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_publication_jobs
                    WHERE feedback_id='inflight-5'
                    """
                ).fetchone()[0],
                0,
            )

        urgent = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(urgent["feedback_id"], "urgent-before-completion")
        self.repo.settle_budget(urgent["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            urgent["processing_key"],
            result=successful_result(),
            worker_id="worker",
        )
        first_publication = self.repo.claim_publication_job(
            worker_id="publisher"
        )
        self.assertEqual(first_publication["feedback_id"], "urgent-before-completion")

        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET state='published',updated_at=?
                WHERE publication_key=?
                """,
                (self.clock().isoformat(), first_publication["publication_key"]),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state='published',updated_at=?
                WHERE processing_key=?
                """,
                (self.clock().isoformat(), urgent["processing_key"]),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_feedbacks
                SET answer_text='Спасибо за отзыв!'
                WHERE feedback_id='urgent-before-completion'
                """
            )

        lower_publication = self.repo.claim_publication_job(
            worker_id="publisher"
        )
        self.assertEqual(lower_publication["feedback_id"], "inflight-5")
        self.assertEqual(lower_publication["action"], "write")

    def test_started_write_readback_is_not_preempted(self) -> None:
        self.repo.upsert_feedback(
            self.classified_feedback("writing-5", text="low", rating=5),
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
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        low = self.repo.claim_processing_job(worker_id="worker")
        self.repo.settle_budget(low["processing_key"], actual_cost_usd="0.01")
        self.repo.complete_generation(
            low["processing_key"],
            result=successful_result(),
            worker_id="worker",
        )
        write = self.repo.claim_publication_job(worker_id="publisher")
        begun = self.repo.begin_publication_write(
            write["publication_key"],
            worker_id="publisher",
        )

        self.clock.advance(1)
        self.repo.upsert_feedback(
            self.classified_feedback(
                "urgent-during-write",
                text="urgent",
                rating=1,
                created_at="2026-07-20T12:00:01Z",
            ),
            source_stream="unanswered",
            run_kind="steady",
        )
        self.repo.refresh_rolling_admissions(actor_id="scheduler")
        self.repo.record_publication_transport(
            write["publication_key"],
            attempt_id=begun["attempt_id"],
            outcome="http_200",
            http_status=200,
            worker_id="publisher",
        )
        readback = self.repo.claim_publication_job(worker_id="publisher")
        self.assertEqual(readback["publication_key"], write["publication_key"])
        self.assertEqual(readback["action"], "readback")

    def test_rolling_content_preempts_ready_rating_only(self) -> None:
        self.repo.upsert_feedback(
            self.classified_feedback("ready-rating", rating=5),
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
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        rating = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(rating["processing_kind"], "rating_only_template")
        self.repo.complete_rating_only_template(
            rating["processing_key"],
            worker_id="worker",
        )

        self.clock.advance(1)
        self.repo.upsert_feedback(
            self.classified_feedback(
                "rolling-content-5",
                tags=["важный тег"],
                rating=5,
                created_at="2026-07-20T12:00:01Z",
            ),
            source_stream="unanswered",
            run_kind="steady",
        )
        self.repo.refresh_rolling_admissions(actor_id="scheduler")
        self.assertIsNone(self.repo.claim_publication_job(worker_id="publisher"))
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        content = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(content["feedback_id"], "rolling-content-5")
        self.assertEqual(content["processing_kind"], "frozen_ai")

    def test_current_policy_media_review_does_not_hold_automatic_priority(self) -> None:
        for row in (
            self.classified_feedback(
                "media-review-1",
                text="urgent media",
                rating=1,
                created_at="2026-07-20T12:00:01Z",
            ),
            self.classified_feedback(
                "automatic-5",
                text="ordinary",
                rating=5,
                created_at="2026-07-20T12:00:00Z",
            ),
        ):
            self.repo.upsert_feedback(
                row,
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
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        urgent = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(urgent["feedback_id"], "media-review-1")
        self.repo.complete_media_uncertainty(
            urgent["processing_key"],
            uncertainty=["media_fetch_failed"],
            worker_id="worker",
        )

        progress = self.repo.progress_status()
        self.assertEqual(
            progress["rolling_admission"]["current_priority_bucket"],
            "content_bearing_5_star",
        )
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        ordinary = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(ordinary["feedback_id"], "automatic-5")

    def test_opaque_node_exit_retries_once_with_attempt_holds(self) -> None:
        self.enable("draft_only")
        self.insert_new("opaque-exit", photo_query="")
        queued = self.repo.enqueue_processing(
            "opaque-exit",
            trigger_source="steady_sync",
            actor_id="sync",
        )
        first = self.repo.claim_processing_job(worker_id="worker")
        self.repo.mark_provider_call_started(
            first["processing_key"],
            worker_id="worker",
        )
        retry = self.repo.record_processing_boundary_failure(
            first["processing_key"],
            error_code="node_process_exit_1",
            worker_id="worker",
            diagnostics={
                "returncode": 1,
                "stderr_bytes": 12,
                "stderr_sha256": "a" * 64,
                "raw_output_persisted": False,
                "unsafe_raw": "must-not-persist",
            },
        )
        self.assertEqual(retry["state"], "retryable_error")
        self.clock.advance(61)
        second = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(second["processing_key"], queued["processing_key"])
        self.assertEqual(second["attempts"], 2)
        self.repo.mark_provider_call_started(
            second["processing_key"],
            worker_id="worker",
        )
        isolated = self.repo.record_processing_boundary_failure(
            second["processing_key"],
            error_code="node_process_exit_1",
            worker_id="worker",
            diagnostics={"returncode": 1, "stderr_bytes": 8},
        )
        self.assertEqual(isolated["state"], "needs_review")
        self.assertEqual(
            isolated["last_error_code"],
            "node_process_exit_1_repeated_needs_review",
        )
        budget = self.repo.budget_status()
        self.assertEqual(budget["uncertainty_hold_count"], 2)
        self.assertAlmostEqual(budget["all_time_uncertainty_hold_usd"], 0.2)
        self.assertEqual(budget["unresolved_uncertainty_count"], 0)
        with sqlite3.connect(self.repo.db_path) as conn:
            evidence = conn.execute(
                """
                SELECT evidence_json
                FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts
                ORDER BY attempt_number
                """
            ).fetchall()
        self.assertNotIn("must-not-persist", "\n".join(row[0] for row in evidence))
        self.assertNotIn(
            self.repo.progress_status()["stop_reason"],
            {"budget_state_unknown", "worker_error"},
        )

    def test_opaque_node_exit_does_not_clear_unrelated_budget_pause(self) -> None:
        self.enable("draft_only")
        self.insert_new("opaque-budget-pause", photo_query="")
        queued = self.repo.enqueue_processing(
            "opaque-budget-pause",
            trigger_source="steady_sync",
            actor_id="sync",
        )
        first = self.repo.claim_processing_job(worker_id="worker")
        self.repo.mark_provider_call_started(
            first["processing_key"],
            worker_id="worker",
        )
        with self.repo.transaction() as conn:
            self.repo._set_stop_reason(
                conn,
                "hourly_budget_reached",
                details={"source": "test"},
                at=self.clock(),
            )
        retry = self.repo.record_processing_boundary_failure(
            queued["processing_key"],
            error_code="node_process_exit_1",
            worker_id="worker",
        )
        self.assertEqual(retry["state"], "retryable_error")
        self.assertEqual(
            self.repo.progress_status()["stop_reason"],
            "hourly_budget_reached",
        )

    def test_rolling_admissions_share_existing_run_cap(self) -> None:
        self.repo.upsert_feedback(
            self.classified_feedback("initial-cap", text="initial", rating=5),
            source_stream="archive",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "auto_all",
            actor_id="admin",
            run_max_usd="0.10",
        )
        applied = self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        first = self.repo.claim_processing_job(worker_id="worker")
        self.repo.settle_budget(first["processing_key"], actual_cost_usd="0.10")
        self.repo.complete_generation(
            first["processing_key"],
            result=successful_result(),
            worker_id="worker",
        )

        self.clock.advance(1)
        self.repo.upsert_feedback(
            self.classified_feedback(
                "rolling-after-cap",
                text="new",
                rating=1,
                created_at="2026-07-20T12:00:01Z",
            ),
            source_stream="unanswered",
            run_kind="steady",
        )
        admission = self.repo.refresh_rolling_admissions(actor_id="scheduler")
        self.assertEqual(admission["admitted"], 1)
        self.repo.reconcile_policy_sweep_once(worker_id="reconcile")
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))
        self.assertEqual(
            self.repo.progress_status()["stop_reason"],
            "run_budget_reached",
        )
        with sqlite3.connect(self.repo.db_path) as conn:
            run_cap = conn.execute(
                """
                SELECT run_max_usd
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
                WHERE transition_run_id=?
                """,
                (applied["sweep"]["transition_run_id"],),
            ).fetchone()[0]
        self.assertEqual(run_cap, "0.10000000")

    def test_manual_jobs_keep_owner_triggered_newest_first_order(self) -> None:
        self.enable("manual")
        for row in (
            self.classified_feedback(
                "manual-rating-1-old",
                text="старый ручной",
                rating=1,
                created_at="2026-07-20T10:00:00Z",
            ),
            self.classified_feedback(
                "manual-rating-5-new",
                text="новый ручной",
                rating=5,
                created_at="2026-07-21T10:00:00Z",
            ),
        ):
            outcome = self.repo.upsert_feedback(
                row,
                source_stream="unanswered",
                run_kind="steady",
            )
            self.repo.enqueue_manual_processing(
                row["id"],
                content_version=outcome["content_version"],
                actor_id="reviewer",
            )
        claimed = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(claimed["feedback_id"], "manual-rating-5-new")

    def test_policy_epoch_preserves_completed_prefilter_skip_without_reclaim(self) -> None:
        self.enable("draft_only")
        self.insert_new("prefilter-skip")
        queued = self.repo.enqueue_processing(
            "prefilter-skip",
            trigger_source="steady_sync",
            actor_id="sync",
        )
        claimed = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(claimed["processing_key"], queued["processing_key"])
        self.repo.mark_provider_call_started(
            queued["processing_key"],
            worker_id="worker",
        )
        self.repo.settle_budget(
            queued["processing_key"],
            actual_cost_usd="0",
        )
        skipped = self.repo.complete_skip(
            queued["processing_key"],
            reason="empty_five_star",
            worker_id="worker",
        )
        self.assertEqual(skipped["state"], "skipped")

        preview = self.repo.preview_mode_transition(
            "auto_all",
            actor_id="admin",
            run_max_usd="0.50",
        )
        applied = self.repo.apply_mode_transition(
            "auto_all",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        run_id = applied["sweep"]["transition_run_id"]
        status = self.repo.reconcile_policy_sweep_once(
            worker_id="reconcile",
            batch_size=25,
        )

        with sqlite3.connect(self.repo.db_path) as conn:
            conn.row_factory = sqlite3.Row
            job = conn.execute(
                """
                SELECT state,last_error_code,policy_epoch,transition_run_id,attempts
                FROM sheet_vitrina_v1_wb_autoanswer_jobs
                WHERE processing_key=?
                """,
                (queued["processing_key"],),
            ).fetchone()
            reservation = conn.execute(
                """
                SELECT status,actual_cost_usd,provider_call_started_at
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                WHERE processing_key=?
                """,
                (queued["processing_key"],),
            ).fetchone()
            boundary_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_audit_events
                WHERE aggregate_id=? AND event_type='provider_call_boundary_entered'
                """,
                (queued["processing_key"],),
            ).fetchone()[0]
        self.assertEqual(job["state"], "skipped")
        self.assertEqual(job["last_error_code"], "empty_five_star")
        self.assertIsNone(job["transition_run_id"])
        self.assertNotEqual(job["policy_epoch"], applied["settings"].policy_epoch)
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(reservation["status"], "settled")
        self.assertEqual(float(reservation["actual_cost_usd"]), 0.0)
        self.assertIsNotNone(reservation["provider_call_started_at"])
        self.assertEqual(boundary_count, 1)
        self.assertGreaterEqual(status["progress"]["skipped_preserved"], 1)
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements
                    WHERE sweep_id=? AND outcome='skipped_preserved'
                    """,
                    (applied["sweep"]["sweep_id"],),
                ).fetchone()[0],
                1,
            )
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))

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

    def test_reviews_seen_after_preview_wait_for_bounded_rolling_admission(
        self,
    ) -> None:
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
        self.assertEqual(stale_progress["rolling_admission"]["initial_membership"], 2)
        self.assertEqual(stale_progress["rolling_admission"]["current_total"], 1)
        self.assertEqual(stale_progress["all_preparation"]["total"], 1)
        self.assertEqual(stale_progress["outside_current_run"], 1)
        self.assertIsNone(
            self.repo.refresh_rolling_admissions(actor_id="scheduler")
        )

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

    def test_daily_limit_raise_resumes_same_run_and_persists_after_restart(self) -> None:
        for feedback_id in ("daily-first", "daily-second"):
            self.repo.upsert_feedback(
                feedback(feedback_id),
                source_stream="history",
                run_kind="backfill",
            )
        self.repo.update_settings(
            hourly_cap_usd="0.10",
            daily_cap_usd="0.10",
            monthly_cap_usd="1.00",
            actor_id="admin",
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
        run_id = applied["sweep"]["transition_run_id"]
        run_cap = applied["sweep"]["run_max_usd"]
        self.repo.reconcile_policy_sweep_once(worker_id="sweep", batch_size=25)
        first = self.repo.claim_processing_job(worker_id="worker")
        self.assertIsNotNone(first)
        self.repo.settle_budget(first["processing_key"], actual_cost_usd="0.10")
        self.repo.complete_generation(
            first["processing_key"],
            result=successful_result(),
            worker_id="worker",
        )
        self.clock.advance(3601)
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))
        self.assertEqual(
            self.repo.progress_status()["stop_reason"],
            "daily_budget_reached",
        )

        self.repo.update_settings(daily_cap_usd="0.50", actor_id="admin")
        resumed = self.repo.claim_processing_job(worker_id="worker")
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed["transition_run_id"], run_id)
        readback = self.repo.reconciliation_status()
        self.assertEqual(readback["transition_run_id"], run_id)
        self.assertEqual(readback["run_max_usd"], run_cap)
        self.assertEqual(self.repo.settings().daily_cap_usd, 0.5)

        restarted = AutoanswersRepository(
            runtime_dir=Path(self.temp.name),
            now_factory=self.clock,
            env=self.env,
        )
        self.assertEqual(restarted.settings().daily_cap_usd, 0.5)
        self.assertEqual(
            restarted.reconciliation_status()["transition_run_id"],
            run_id,
        )
        self.assertEqual(restarted.reconciliation_status()["run_max_usd"], run_cap)

    def test_lowering_below_usage_preserves_usage_and_pause(self) -> None:
        self.repo.update_settings(
            master_enabled=True,
            mode="draft_only",
            hourly_cap_usd="1.00",
            daily_cap_usd="1.00",
            monthly_cap_usd="1.00",
            actor_id="admin",
        )
        for feedback_id in ("lower-first", "lower-second"):
            self.insert_new(feedback_id)
            self.repo.enqueue_processing(
                feedback_id,
                trigger_source="automatic",
                actor_id="sync",
            )
        first = self.repo.claim_processing_job(worker_id="worker")
        self.repo.settle_budget(first["processing_key"], actual_cost_usd="0.20")
        self.repo.complete_generation(
            first["processing_key"],
            result=successful_result(),
            worker_id="worker",
        )
        self.clock.advance(3601)
        before = self.repo.budget_status()["daily_actual_usd"]
        self.repo.update_settings(
            hourly_cap_usd="0.10",
            daily_cap_usd="0.10",
            actor_id="admin",
        )
        self.assertEqual(self.repo.budget_status()["daily_actual_usd"], before)
        self.assertIsNone(self.repo.claim_processing_job(worker_id="worker"))
        self.assertEqual(
            self.repo.progress_status()["stop_reason"],
            "daily_budget_reached",
        )

    def test_limit_update_does_not_clear_stronger_gate_or_change_run_cap(self) -> None:
        self.repo.upsert_feedback(
            feedback("gate-run"),
            source_stream="history",
            run_kind="backfill",
        )
        preview = self.repo.preview_mode_transition(
            "draft_only",
            actor_id="admin",
            run_max_paid_reviews=3,
        )
        applied = self.repo.apply_mode_transition(
            "draft_only",
            actor_id="admin",
            preview_id=preview["preview_id"],
        )
        with self.repo.transaction() as conn:
            self.repo._set_stop_reason(  # noqa: SLF001 - exact safety regression
                conn,
                "budget_state_unknown",
                details={"evidence": "fixture"},
                at=self.clock(),
            )
        self.repo.update_settings(daily_cap_usd="6.00", actor_id="admin")
        self.assertEqual(
            self.repo.progress_status()["stop_reason"],
            "budget_state_unknown",
        )
        readback = self.repo.reconciliation_status()
        self.assertEqual(
            readback["transition_run_id"],
            applied["sweep"]["transition_run_id"],
        )
        self.assertEqual(readback["run_max_paid_reviews"], 3)

    def test_concurrent_limit_updates_accept_only_one_settings_revision(self) -> None:
        initial = self.repo.settings()
        revision = autoanswers_settings_revision(initial)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def update(value: str) -> None:
            barrier.wait()
            try:
                self.repo.update_settings(
                    daily_cap_usd=value,
                    expected_policy_epoch=initial.policy_epoch,
                    expected_settings_revision=revision,
                    actor_id=f"admin-{value}",
                )
                outcomes.append("saved")
            except AutoanswersRuntimeError as exc:
                outcomes.append(exc.code)

        threads = [
            threading.Thread(target=update, args=(value,))
            for value in ("6.00", "7.00")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertCountEqual(
            outcomes,
            ["saved", "settings_revision_stale"],
        )
        self.assertIn(self.repo.settings().daily_cap_usd, {6.0, 7.0})

    def test_operator_limit_validation_rejects_nonfinite_unsafe_and_conflicting_values(self) -> None:
        invalid_changes = (
            {"hourly_cap_usd": "NaN"},
            {"hourly_cap_usd": "0"},
            {"hourly_cap_usd": "10.01"},
            {"daily_cap_usd": "50.01"},
            {"monthly_cap_usd": "500.01"},
            {"max_paid_reviews_per_hour": 201},
            {"global_paid_review_concurrency": 5},
            {"max_inflight_role_calls": 9},
            {"max_materialized_processing_jobs": 101},
            {"hourly_cap_usd": "6.00", "daily_cap_usd": "5.00"},
            {
                "global_paid_review_concurrency": 4,
                "max_materialized_processing_jobs": 3,
            },
        )
        for change in invalid_changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.repo.update_settings(actor_id="admin", **change)

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

    def test_local_list_filters_use_only_latest_job_per_feedback(self) -> None:
        self.enable("manual")
        self.insert_new("current-published")
        current = self.repo.enqueue_manual_processing(
            "current-published",
            content_version=1,
            actor_id="reviewer",
        )

        self.insert_new("superseded-published")
        superseded = self.repo.enqueue_manual_processing(
            "superseded-published",
            content_version=1,
            actor_id="reviewer",
        )
        changed = feedback("superseded-published", text="Новая версия отзыва")
        self.repo.upsert_feedback(changed, source_stream="steady", run_kind="steady")
        latest = self.repo.enqueue_manual_processing(
            "superseded-published",
            content_version=2,
            actor_id="reviewer",
        )
        with self.repo.transaction() as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state='published' WHERE processing_key IN (?,?)",
                (current["processing_key"], superseded["processing_key"]),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state='needs_review' WHERE processing_key=?",
                (latest["processing_key"],),
            )

        published = self.repo.list_feedbacks(filters={"published": True})
        needs_review = self.repo.list_feedbacks(filters={"needs_review": True})
        self.assertEqual([item["id"] for item in published["items"]], ["current-published"])
        self.assertEqual([item["id"] for item in needs_review["items"]], ["superseded-published"])

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
        # A live automatic sweep remains queued while any admitted automatic
        # action is still pending; this is what keeps the cross-stage priority
        # barrier authoritative after materialization.
        self.assertEqual(restarted.reconciliation_status()["state"], "queued")
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

    def test_budget_uncertainty_reconciliation_appends_hold_without_fake_spend(self) -> None:
        self.enable("manual")
        self.insert_new("unknown-provider-cost")
        job = self.repo.enqueue_manual_processing(
            "unknown-provider-cost",
            content_version=1,
            actor_id="reviewer",
        )
        claimed = self.repo.claim_processing_job(worker_id="worker")
        self.assertEqual(claimed["processing_key"], job["processing_key"])
        self.repo.mark_provider_call_started(
            job["processing_key"], worker_id="worker"
        )
        self.repo.record_processing_retry(
            job["processing_key"],
            error_code="node_process_exit_1",
            retry_after_seconds=60,
            worker_id="worker",
        )

        before = self.repo.budget_status()
        self.assertEqual(before["budget_state"], "unknown")
        self.assertEqual(before["monthly_actual_usd"], 0)
        plan = self.repo.budget_reconciliation_plan()
        self.assertEqual(plan["candidate_count"], 1)
        self.assertEqual(plan["pre_change_digest"], plan["plan_fingerprint"])
        self.assertEqual(
            plan["expected_affected_records"],
            {
                "uncertainty_holds_inserted": 1,
                "audit_events_appended": 1,
                "runtime_state_rows_updated": 1,
                "provider_calls_created": 0,
                "cost_events_created": 0,
                "wb_writes_created": 0,
            },
        )
        self.assertEqual(
            plan["non_target_invariants"],
            {
                "provider_calls_unchanged": True,
                "cost_events_unchanged": True,
                "wb_writes_unchanged": True,
                "reservation_and_job_evidence_unchanged": True,
            },
        )
        self.assertFalse(plan["reversibility"]["backup_required"])
        applied = self.repo.apply_budget_reconciliation(
            expected_fingerprint=plan["plan_fingerprint"],
            actor_id="operator",
        )
        self.assertEqual(applied["status"], "reconciled")
        self.assertFalse(applied["idempotent"])
        self.assertEqual(applied["holds_appended"], 1)
        self.assertEqual(
            applied["affected_records"], plan["expected_affected_records"]
        )
        self.assertTrue(applied["non_target_invariants_preserved"])
        after = self.repo.budget_status()
        self.assertEqual(after["monthly_actual_usd"], 0)
        self.assertGreater(after["monthly_uncertainty_hold_usd"], 0)
        self.assertEqual(after["budget_state"], "conservative_unverified")
        self.assertTrue(self.repo.budget_reconciliation_status()["confirmed"])
        self.assertEqual(
            self.repo.budget_reconciliation_plan()["candidate_count"], 0
        )
        with sqlite3.connect(self.repo.db_path) as conn:
            before_replay = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_audit_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts)
                """
            ).fetchone()
        replay = self.repo.apply_budget_reconciliation(
            expected_fingerprint=plan["plan_fingerprint"],
            actor_id="operator",
        )
        self.assertEqual(replay["status"], "already_reconciled")
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["holds_appended"], 0)
        self.assertEqual(replay["previous_holds_appended"], 1)
        self.assertTrue(replay["non_target_invariants_preserved"])
        self.assertEqual(
            sum(replay["affected_records"].values()),
            0,
        )
        with sqlite3.connect(self.repo.db_path) as conn:
            after_replay = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_audit_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events),
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts)
                """
            ).fetchone()
        self.assertEqual(after_replay, before_replay)
        with self.assertRaisesRegex(
            AutoanswersRuntimeError, "evidence changed"
        ):
            self.repo.apply_budget_reconciliation(
                expected_fingerprint="sha256:" + "0" * 64,
                actor_id="operator",
            )

    def test_budget_reconciliation_does_not_clear_an_unrelated_stop_reason(self) -> None:
        self.enable("manual")
        self.insert_new("unknown-provider-cost-with-quota-stop")
        job = self.repo.enqueue_manual_processing(
            "unknown-provider-cost-with-quota-stop",
            content_version=1,
            actor_id="reviewer",
        )
        self.repo.claim_processing_job(worker_id="worker")
        self.repo.mark_provider_call_started(
            job["processing_key"], worker_id="worker"
        )
        self.repo.record_processing_retry(
            job["processing_key"],
            error_code="node_process_exit_1",
            retry_after_seconds=60,
            worker_id="worker",
        )
        with self.repo.transaction() as conn:
            self.repo._set_stop_reason(
                conn,
                "openai_quota_exhausted",
                details={"source": "test"},
                at=self.clock(),
            )
        plan = self.repo.budget_reconciliation_plan()
        self.assertEqual(plan["candidate_count"], 1)
        with self.assertRaisesRegex(
            AutoanswersRuntimeError, "different runtime stop reason"
        ):
            self.repo.apply_budget_reconciliation(
                expected_fingerprint=plan["plan_fingerprint"],
                actor_id="operator",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
