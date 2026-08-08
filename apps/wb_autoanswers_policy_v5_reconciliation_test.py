#!/usr/bin/env python3
"""Regression coverage for owner-policy v5 and zero-write reconciliation."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from apps.wb_autoanswers_policy_v5_reconciliation import (
    _non_target_invariants,
    _open,
    apply_plan,
    build_plan,
    public_plan,
    readback,
)
from apps.wb_autoanswers_runtime_test import feedback, successful_result
from packages.application.wb_autoanswers_owner_policy import (
    OWNER_POLICY_VERSION,
    apply_owner_policy,
    classify_return_guard,
    normalize_unfortunately,
)
from packages.application.wb_autoanswers_runtime import (
    PREVIOUS_POLICY_VERSION,
    AutoanswersRepository,
    SCHEMA_VERSION,
    canonical_json,
    final_reply_hash,
    iso_utc,
)


def content(text: str) -> str:
    return json.dumps(
        {"text": text, "pros": "", "cons": "", "tags": [], "rating": 1},
        ensure_ascii=False,
    )


def policy_result(feedback_id: str, text: str, *, reply: str = "Исходный ответ") -> dict:
    return apply_owner_policy(
        feedback_id=feedback_id,
        rating=1,
        content_json=content(text),
        result=successful_result("wb_return", final_reply=reply),
    )


class OwnerPolicyV5Test(unittest.TestCase):
    def test_ordinary_post_use_breakage_is_public_for_timing_and_word_variants(self) -> None:
        fixtures = (
            "Треснуло через день, телефон не падал",
            "Через неделю защитное стекло лопнуло само по себе",
            "Через несколько дней стекло посыпалось по краям",
            "Края скалываются буквально об воздух",
            "Защитное стекло крошится в кармане",
            "Неделю проходил и трусгуло без падений",
            "Через неделю телефон получил трещину",
        )
        replies: set[str] = set()
        for index, review in enumerate(fixtures):
            with self.subTest(review=review):
                result = policy_result(f"post-use-{index}", review)
                self.assertEqual(result["final_route"], "public_only")
                evidence = result["server_owner_policy"]
                self.assertEqual(evidence["reason"], "ordinary_post_use_breakage")
                self.assertFalse(evidence["hard_return_reasons"])
                self.assertNotIn("экран остался цел", result["final_reply"].casefold())
                self.assertNotIn("телефон не пострадал", result["final_reply"].casefold())
                replies.add(result["final_reply"])
        self.assertGreaterEqual(len(replies), 3)

    def test_impact_formula_requires_an_explicit_positive_impact(self) -> None:
        negative = policy_result("no-impact", "Стекло треснуло, телефон не падал и ударов не было")
        positive = policy_result("impact", "После небольшого падения стекло треснуло")
        self.assertNotIn("сила, угол и точка контакта", negative["final_reply"].casefold())
        self.assertIn("сил", positive["final_reply"].casefold())
        self.assertIn("уг", positive["final_reply"].casefold())
        self.assertIn("точк", positive["final_reply"].casefold())

    def test_large_dangerous_chip_and_independent_hard_reasons_stay_return(self) -> None:
        fixtures = {
            "large_chip": "На следующий день скол на четверть экрана",
            "sharp_edge": "Стекло скололось, острый режущий край может травмировать человека",
            "arrived_cracked": "Пришло уже треснутое стекло",
            "received_cracked": "Получил товар с треснутым стеклом",
            "opened_incomplete": "Упаковка расклеена, не было салфеток и липкой ленты",
            "wrong_variant": "Заказывала матовое, пришло глянцевое стекло",
            "fit": "Стекло меньше экрана и не подошло к телефону",
            "stripe": "Появилась полоса во весь экран, которая не стирается",
            "sensor": "После установки экран не реагирует на касания",
            "camera": "С этим стеклом фронтальная камера мутная",
            "privacy_absent": "Совсем не антишпион, никакого затемнения",
            "device_damage": "Защитное стекло поцарапало основной экран",
            "injury": "Острый скол, есть риск травмы",
        }
        for name, review in fixtures.items():
            with self.subTest(name=name):
                result = policy_result(name, review)
                self.assertEqual(result["final_route"], "wb_return")
                self.assertTrue(result["server_owner_policy"]["hard_return_reasons"])

    def test_mixed_review_uses_the_independent_hard_reason(self) -> None:
        result = policy_result(
            "mixed-sensor",
            "Через неделю стекло треснуло, а экран после установки не реагирует на касания",
        )
        self.assertEqual(result["final_route"], "wb_return")
        self.assertIn(
            "persistent_sensor_or_camera_failure",
            result["server_owner_policy"]["hard_return_reasons"],
        )
        self.assertNotIn(
            "ordinary_post_use_breakage",
            result["server_owner_policy"]["hard_return_reasons"],
        )

    def test_partial_privacy_subjective_and_vague_complaints_are_public(self) -> None:
        for index, review in enumerate(
            (
                "Сбоку не total black, при почти 90 градусах немного видно",
                "Салфетка плохого качества, ожидал большего",
                "Приехало не то, не понравилось",
                "Надо компенсацию 100 рублей",
            )
        ):
            with self.subTest(review=review):
                result = policy_result(f"soft-{index}", review)
                self.assertEqual(result["final_route"], "public_only")
                self.assertEqual(
                    result["server_owner_policy"]["reason"],
                    "no_independent_hard_return_signal",
                )

    def test_unfortunately_is_natural_unique_and_avoids_double_empathy(self) -> None:
        inserted, action = normalize_unfortunately(
            "По фото нельзя достоверно определить причину. Оформить обращение можно в приложении."
        )
        self.assertEqual(action, "inserted_limitation")
        self.assertIn("По фото, к сожалению, нельзя", inserted)
        self.assertEqual(inserted.casefold().count("к сожалению"), 1)

        deduplicated, action = normalize_unfortunately(
            "По фото, к сожалению, нельзя определить причину. К сожалению, данных мало."
        )
        self.assertEqual(action, "limited_to_one")
        self.assertEqual(deduplicated.casefold().count("к сожалению"), 1)

        guarded, action = normalize_unfortunately(
            "Сожалеем о ситуации. По описанию, к сожалению, недостаточно данных."
        )
        self.assertEqual(action, "removed_double_empathy")
        self.assertNotIn("к сожалению", guarded.casefold())
        self.assertIn("Сожалеем", guarded)

        neutral, action = normalize_unfortunately(
            "Откройте раздел заказа и выберите нужный товар. Спасибо за покупку."
        )
        self.assertEqual(action, "unchanged")
        self.assertEqual(neutral, "Откройте раздел заказа и выберите нужный товар. Спасибо за покупку.")

    def test_semantic_evidence_is_stable(self) -> None:
        first = classify_return_guard(content("Через неделю стекло крошится"))
        second = classify_return_guard(content("Через неделю стекло крошится"))
        self.assertEqual(first, second)
        self.assertEqual(len(first["semantic_text_sha256"]), 64)


class PolicyV5ReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.runtime_dir = Path(self.temp.name)
        self.repo = AutoanswersRepository(runtime_dir=self.runtime_dir, env={})
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="test")
        with self.repo.transaction() as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswers_settings SET policy_version=?",
                (PREVIOUS_POLICY_VERSION,),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _approved(self, feedback_id: str, text: str, route: str = "wb_return") -> str:
        raw = feedback(feedback_id, text=text)
        raw["pros"] = ""
        outcome = self.repo.upsert_feedback(raw, source_stream="unanswered", run_kind="steady")
        job = self.repo.enqueue_processing(
            feedback_id,
            trigger_source="steady_sync",
            actor_id="sync",
        )
        self.repo.claim_processing_job(worker_id="ai")
        stored = self.repo.complete_generation(
            job["processing_key"],
            result=successful_result(route, final_reply=f"Исходный ответ {feedback_id}"),
            worker_id="ai",
        )
        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET status='released',reserved_usd='0',updated_at=?
                WHERE processing_key=? AND status='reserved'
                """,
                (iso_utc(), job["processing_key"]),
            )
        self.assertEqual(stored["state"], "approved")
        self.assertEqual(outcome["content_version"], 1)
        return self.repo.get_feedback(feedback_id)["publications"][0]["publication_key"]

    def _backup(self) -> None:
        backup_dir = self.runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
        backup_dir.mkdir(parents=True)
        with closing(sqlite3.connect(self.repo.db_path)) as source:
            with closing(sqlite3.connect(backup_dir / "verified.sqlite3")) as target:
                source.backup(target)

    def _apply_single(self, feedback_id: str) -> tuple[dict, dict, str]:
        publication_key = self._approved(
            feedback_id,
            "Через неделю защитное стекло крошится",
        )
        self._backup()
        deployed = {
            "runtime_sha": "d" * 40,
            "deploy_metadata_sha": "d" * 40,
            "deployment_complete": True,
            "deployed_at": "2026-08-08T12:00:00Z",
        }
        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            plan = public_plan(
                build_plan(conn, runtime_dir=self.runtime_dir, deployed_runtime=deployed)
            )
        applied = apply_plan(
            self.runtime_dir,
            expected_fingerprint=plan["plan_fingerprint"],
            deployed_runtime=deployed,
            actor="test",
            worker_hold_confirmed=True,
        )
        self.assertEqual(applied["status"], "applied")
        self.assertFalse(applied["idempotent"])
        publication_key = self.repo.get_feedback(feedback_id)["publications"][0][
            "publication_key"
        ]
        return plan, deployed, publication_key

    def test_all_unstarted_are_evaluated_and_started_write_is_untouched(self) -> None:
        self._approved("post-use", "Через неделю стекло крошится")
        self._approved("danger", "Скол на четверть экрана")
        started_key = self._approved("started", "Стекло треснуло через день")
        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET state='publishing',attempts=1,write_started_at=?,updated_at=?
                WHERE publication_key=?
                """,
                (iso_utc(), iso_utc(), started_key),
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_publication_attempts(
                    attempt_id,publication_key,attempt_number,request_reply_sha256,
                    transport_outcome,http_status,write_started_at,details_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    "started-attempt",
                    started_key,
                    1,
                    final_reply_hash("Исходный ответ started"),
                    "started",
                    None,
                    iso_utc(),
                    canonical_json({"test": True}),
                ),
            )
        with closing(self.repo._connect()) as conn:
            started_before = _non_target_invariants(conn)["started_publications"]
        self._backup()
        deployed = {
            "runtime_sha": "a" * 40,
            "deploy_metadata_sha": "a" * 40,
            "deployment_complete": True,
            "deployed_at": "2026-08-08T12:00:00Z",
        }
        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            plan = build_plan(conn, runtime_dir=self.runtime_dir, deployed_runtime=deployed)
        reviewed = public_plan(plan)
        self.assertTrue(reviewed["coverage_confirmed"])
        self.assertEqual(reviewed["counts"]["publication_total"], 3)
        self.assertEqual(reviewed["counts"]["unstarted_evaluated"], 2)
        self.assertEqual(reviewed["counts"]["started_preserved"], 1)
        self.assertEqual(reviewed["counts"]["wb_return_before"], 2)
        self.assertEqual(reviewed["counts"]["wb_return_after"], 1)

        applied = apply_plan(
            self.runtime_dir,
            expected_fingerprint=reviewed["plan_fingerprint"],
            deployed_runtime=deployed,
            actor="test-release",
            worker_hold_confirmed=True,
        )
        self.assertEqual(applied["status"], "applied")
        self.assertFalse(applied["idempotent"])
        self.assertEqual(self.repo.settings().policy_version, OWNER_POLICY_VERSION)
        self.assertEqual(
            self.repo.get_feedback("post-use")["ai_jobs"][0]["final_route"],
            "public_only",
        )
        self.assertEqual(
            self.repo.get_feedback("danger")["ai_jobs"][0]["final_route"],
            "wb_return",
        )
        with closing(self.repo._connect()) as conn:
            started_after = _non_target_invariants(conn)["started_publications"]
        self.assertEqual(started_before, started_after)
        self.assertEqual(
            self.repo.get_feedback("started")["publications"][0]["publication_key"],
            started_key,
        )

        replay = apply_plan(
            self.runtime_dir,
            expected_fingerprint=reviewed["plan_fingerprint"],
            deployed_runtime=deployed,
            actor="test-release",
            worker_hold_confirmed=True,
        )
        self.assertTrue(replay["idempotent"])
        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            evidence = readback(
                conn,
                reviewed_plan=reviewed,
                expected_fingerprint=reviewed["plan_fingerprint"],
            )
        self.assertEqual(evidence["status"], "reconciled")
        self.assertFalse(evidence["blockers"])
        self.assertEqual(evidence["actual_counts"]["stale_unstarted"], 0)
        self.assertEqual(evidence["actual_counts"]["incoherent_unstarted"], 0)

    def test_apply_fails_without_worker_hold(self) -> None:
        self._approved("hold", "Через неделю стекло разбилось")
        self._backup()
        deployed = {
            "runtime_sha": "b" * 40,
            "deploy_metadata_sha": "b" * 40,
            "deployment_complete": True,
            "deployed_at": "2026-08-08T12:00:00Z",
        }
        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            plan = public_plan(
                build_plan(conn, runtime_dir=self.runtime_dir, deployed_runtime=deployed)
            )
        with self.assertRaisesRegex(RuntimeError, "worker hold"):
            apply_plan(
                self.runtime_dir,
                expected_fingerprint=plan["plan_fingerprint"],
                deployed_runtime=deployed,
                actor="test",
                worker_hold_confirmed=False,
            )

    def test_apply_rebinds_a_zero_write_job_from_its_exact_legacy_identity(self) -> None:
        legacy_key = self._approved("legacy-job", "Скол на четверть экрана")
        started_key = self._approved("started-legacy", "Скол на четверть экрана")
        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET policy_epoch=12,policy_version='owner-policy-2026-07-21-v2'
                WHERE feedback_id='legacy-job'
                """
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET state='publishing',attempts=1,write_started_at=?,updated_at=?
                WHERE publication_key=?
                """,
                (iso_utc(), iso_utc(), started_key),
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_publication_attempts(
                    attempt_id,publication_key,attempt_number,request_reply_sha256,
                    transport_outcome,http_status,write_started_at,details_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    "started-legacy-attempt",
                    started_key,
                    1,
                    final_reply_hash("Исходный ответ started-legacy"),
                    "started",
                    None,
                    iso_utc(),
                    canonical_json({"test": True}),
                ),
            )
        legacy_before = self.repo.get_feedback("legacy-job")
        route_before = legacy_before["ai_jobs"][0]["final_route"]
        reply_before = legacy_before["ai_jobs"][0]["final_reply"]
        with closing(self.repo._connect()) as conn:
            non_target_before = _non_target_invariants(conn)
        self._backup()
        deployed = {
            "runtime_sha": "c" * 40,
            "deploy_metadata_sha": "c" * 40,
            "deployment_complete": True,
            "deployed_at": "2026-08-08T12:00:00Z",
        }
        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            internal_plan = build_plan(
                conn,
                runtime_dir=self.runtime_dir,
                deployed_runtime=deployed,
            )
        projection = internal_plan["target_projection"]
        self.assertEqual(len(projection), 1)
        self.assertEqual(projection[0]["source_job_policy_epoch"], 12)
        self.assertEqual(
            projection[0]["source_job_policy_version"],
            "owner-policy-2026-07-21-v2",
        )
        plan = public_plan(internal_plan)
        self.assertEqual(plan["counts"]["unstarted_evaluated"], 1)
        self.assertEqual(plan["counts"]["started_preserved"], 1)
        self.assertEqual(plan["counts"]["route_changed"], 0)
        self.assertEqual(plan["counts"]["reply_changed"], 0)
        self.assertEqual(plan["counts"]["metadata_only_rebound"], 1)

        applied = apply_plan(
            self.runtime_dir,
            expected_fingerprint=plan["plan_fingerprint"],
            deployed_runtime=deployed,
            actor="test",
            worker_hold_confirmed=True,
        )
        self.assertEqual(applied["status"], "applied")
        self.assertFalse(applied["idempotent"])
        self.assertEqual(applied["wb_post_count"], 0)
        self.assertEqual(applied["provider_call_count"], 0)
        job = self.repo.get_feedback("legacy-job")["ai_jobs"][0]
        self.assertEqual(job["policy_version"], OWNER_POLICY_VERSION)
        self.assertEqual(job["policy_epoch"], plan["target_policy_epoch"])
        self.assertEqual(job["final_route"], route_before)
        self.assertEqual(job["final_reply"], reply_before)
        self.assertEqual(
            self.repo.get_feedback("legacy-job")["publications"][0]["publication_key"],
            legacy_key,
        )
        with closing(self.repo._connect()) as conn:
            self.assertEqual(_non_target_invariants(conn), non_target_before)

        replay = apply_plan(
            self.runtime_dir,
            expected_fingerprint=plan["plan_fingerprint"],
            deployed_runtime=deployed,
            actor="test",
            worker_hold_confirmed=True,
        )
        self.assertTrue(replay["idempotent"])
        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            evidence = readback(
                conn,
                reviewed_plan=plan,
                expected_fingerprint=plan["plan_fingerprint"],
            )
        self.assertEqual(evidence["status"], "reconciled")
        self.assertEqual(evidence["actual_counts"]["stale_unstarted"], 0)
        self.assertEqual(evidence["actual_counts"]["metadata_stale_unstarted"], 0)
        self.assertEqual(evidence["wb_post_count"], 0)
        self.assertEqual(evidence["provider_call_count"], 0)

    def test_get_only_feedback_advances_are_a_bounded_reconciled_delta(self) -> None:
        plan, _, _ = self._apply_single("get-only-stable")
        legacy_reviewed = dict(plan)
        legacy_reviewed["non_target_invariants"] = {
            **legacy_reviewed.pop("immutable_execution_invariants"),
            **legacy_reviewed.pop("mutable_get_only_surfaces"),
        }
        legacy_reviewed.pop("external_call_counters")

        refreshed = feedback(
            "get-only-stable",
            text="Через неделю защитное стекло крошится",
            photo_query="readonly-rotated",
        )
        refreshed["pros"] = ""
        self.repo.upsert_feedback(
            refreshed,
            source_stream="detail",
            run_kind="reconciliation",
        )
        discovered = feedback(
            "get-only-new",
            text="Новый отзыв readonly sync",
            photo_query="readonly-new",
        )
        discovered["pros"] = ""
        self.repo.upsert_feedback(
            discovered,
            source_stream="unanswered",
            run_kind="steady",
        )

        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            evidence = readback(
                conn,
                reviewed_plan=legacy_reviewed,
                expected_fingerprint=plan["plan_fingerprint"],
            )
        self.assertEqual(evidence["status"], "reconciled")
        self.assertFalse(evidence["blockers"])
        self.assertFalse(evidence["immutable_execution_drift"])
        delta = evidence["get_only_observed_delta"]
        self.assertTrue(delta["bounded"])
        self.assertEqual(
            set(delta["changed_surfaces"]),
            {"feedback_truth", "feedback_versions", "feedback_media"},
        )
        self.assertEqual(delta["surfaces"]["feedback_truth"]["count_delta"], 1)
        self.assertEqual(delta["surfaces"]["feedback_versions"]["count_delta"], 1)
        self.assertEqual(delta["surfaces"]["feedback_media"]["count_delta"], 1)
        self.assertEqual(evidence["actual_counts"]["stale_unstarted"], 0)
        self.assertEqual(evidence["actual_counts"]["metadata_stale_unstarted"], 0)
        self.assertEqual(evidence["actual_counts"]["incoherent_unstarted"], 0)
        self.assertEqual(evidence["wb_post_count"], 0)
        self.assertEqual(evidence["provider_call_count"], 0)

    def test_started_and_outside_scope_execution_drift_blocks_readback(self) -> None:
        started_key = self._approved("immutable-started", "Скол на четверть экрана")
        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET state='publishing',write_started_at=?,updated_at=?
                WHERE publication_key=?
                """,
                (iso_utc(), iso_utc(), started_key),
            )
        self._backup()
        deployed = {
            "runtime_sha": "e" * 40,
            "deploy_metadata_sha": "e" * 40,
            "deployment_complete": True,
            "deployed_at": "2026-08-08T12:00:00Z",
        }
        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            plan = public_plan(
                build_plan(conn, runtime_dir=self.runtime_dir, deployed_runtime=deployed)
            )
        apply_plan(
            self.runtime_dir,
            expected_fingerprint=plan["plan_fingerprint"],
            deployed_runtime=deployed,
            actor="test",
            worker_hold_confirmed=True,
        )
        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET last_error_code='immutable-drift'
                WHERE publication_key=?
                """,
                (started_key,),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET last_error_code='immutable-drift'
                WHERE processing_key=(
                    SELECT processing_key FROM sheet_vitrina_v1_wb_publication_jobs
                    WHERE publication_key=?
                )
                """,
                (started_key,),
            )
        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            evidence = readback(
                conn,
                reviewed_plan=plan,
                expected_fingerprint=plan["plan_fingerprint"],
            )
        self.assertEqual(evidence["status"], "blocked")
        self.assertIn("immutable_execution_invariants_changed", evidence["blockers"])
        self.assertIn("started_publications", evidence["immutable_execution_drift"])
        self.assertIn(
            "jobs_outside_zero_write_scope",
            evidence["immutable_execution_drift"],
        )

    def test_wb_and_provider_call_count_changes_block_readback(self) -> None:
        plan, _, publication_key = self._apply_single("external-call-drift")
        with self.repo.transaction() as conn:
            processing_key = str(
                conn.execute(
                    "SELECT processing_key FROM sheet_vitrina_v1_wb_publication_jobs WHERE publication_key=?",
                    (publication_key,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_publication_attempts(
                    attempt_id,publication_key,attempt_number,request_reply_sha256,
                    transport_outcome,http_status,write_started_at,details_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    "unexpected-wb-post",
                    publication_key,
                    1,
                    final_reply_hash("unexpected"),
                    "started",
                    None,
                    iso_utc(),
                    canonical_json({"unexpected": True}),
                ),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET provider_call_started_at=?,updated_at=?
                WHERE processing_key=?
                """,
                (iso_utc(), iso_utc(), processing_key),
            )
        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            evidence = readback(
                conn,
                reviewed_plan=plan,
                expected_fingerprint=plan["plan_fingerprint"],
            )
        self.assertEqual(evidence["status"], "blocked")
        self.assertEqual(evidence["wb_post_count"], 1)
        self.assertEqual(evidence["provider_call_count"], 1)
        self.assertIn("wb_post_count_changed", evidence["blockers"])
        self.assertIn("provider_call_count_changed", evidence["blockers"])

    def test_cost_reservation_and_uncertainty_drift_blocks_readback(self) -> None:
        plan, _, publication_key = self._apply_single("financial-drift")
        with self.repo.transaction() as conn:
            processing_key = str(
                conn.execute(
                    "SELECT processing_key FROM sheet_vitrina_v1_wb_publication_jobs WHERE publication_key=?",
                    (publication_key,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_cost_events(
                    event_id,processing_key,media_processing_version,actual_cost_usd,incurred_at
                ) VALUES(?,?,?,?,?)
                """,
                ("unexpected-cost", processing_key, 99, "0.0001", iso_utc()),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET updated_at=? WHERE processing_key=?
                """,
                ("2026-08-08T12:00:01Z", processing_key),
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts(
                    uncertainty_id,processing_key,attempt_number,transition_run_id,
                    upper_bound_usd,effective_at,error_code,evidence_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    "unexpected-uncertainty",
                    processing_key,
                    99,
                    None,
                    "0.01",
                    iso_utc(),
                    "unexpected",
                    canonical_json({"unexpected": True}),
                    iso_utc(),
                ),
            )
        with closing(_open(self.runtime_dir, read_only=True)) as conn:
            evidence = readback(
                conn,
                reviewed_plan=plan,
                expected_fingerprint=plan["plan_fingerprint"],
            )
        self.assertEqual(evidence["status"], "blocked")
        self.assertIn("immutable_execution_invariants_changed", evidence["blockers"])
        self.assertTrue(
            {"cost_events", "reservations", "uncertainty"}.issubset(
                set(evidence["immutable_execution_drift"])
            )
        )


if __name__ == "__main__":
    unittest.main()
