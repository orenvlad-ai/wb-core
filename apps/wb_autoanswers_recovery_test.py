#!/usr/bin/env python3
"""Free regression tests for pause recovery, provider outages and exact apply."""
from contextlib import closing
import json
from types import SimpleNamespace
from datetime import timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from apps.wb_autoanswers_runtime_test import MutableClock, feedback, successful_result
from apps.wb_autoanswers_media_worker_test import NoopMedia
from apps.wb_autoanswers_sync_test import FakeReadSource
from apps.wb_autoanswers_backlog_recovery_test import FakeSource
from apps import wb_autoanswers_recovery_apply as adapter
from apps.production_apply_contract import AmbiguousSubmit
from packages.application.wb_autoanswers_runtime import AutoanswersRepository, SCHEMA_VERSION, iso_utc
from packages.application.wb_autoanswers_worker import AutoanswersProcessingWorker
from packages.application.wb_autoanswers_node_bridge import NodeAutoanswersBridge, NodeBoundaryError
from packages.contracts.wb_autoanswers import NODE_BOUNDARY_VERSION, PROMPT_BUNDLE_VERSION, EVALUATION_SIGNATURE
from apps import wb_autoanswers_recovery_production_adapter as transport
from packages.application.wb_autoanswers_sync import WbFeedbackSyncService
from packages.application.wb_autoanswers_coordinator import AutoanswersCoordinator


class FailBridge:
    def __init__(self, code):
        self.code = code
        self.calls = 0
    def run(self, **kwargs):
        self.calls += 1
        raise NodeBoundaryError("fake failure", code=self.code, retryable=True, retry_after_seconds=90)


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.clock = MutableClock()
        self.repo = AutoanswersRepository(runtime_dir=Path(self.temp.name), now_factory=self.clock, env={})
    def tearDown(self):
        self.temp.cleanup()
    def inventory(self, ids, **kwargs):
        self.repo.save_sync_cursor("wb_feedback_full_unanswered_inventory", cursor={
            "completed_at": iso_utc(self.clock()), "coverage_confirmed": True, "feedback_ids": ids, **kwargs}, successful=True)
    def insert(self, name, *, empty=False):
        row = feedback(name)
        if empty:
            row.update(text="", pros="", cons="", photoLinks=[])
        return self.repo.upsert_feedback(row, source_stream="unanswered", run_kind="steady")
    def enable(self):
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
    def terminal(self, name, code, *, manual=False):
        self.insert(name)
        job = self.repo.enqueue_processing(name, trigger_source="steady_sync", actor_id="sync")
        claimed = self.repo.claim_processing_job(worker_id="ai")
        self.assertEqual(claimed["processing_key"], job["processing_key"])
        self.repo.record_processing_terminal(job["processing_key"], error_code=code, worker_id="ai")
        if manual:
            with self.repo.transaction() as conn:
                conn.execute("UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET manual_started=1 WHERE processing_key=?", (job["processing_key"],))
        return job["processing_key"]
    def test_off_manual_force_off_and_long_pause_admit_only_official_inventory(self):
        for mode in ("off", "manual", "force_off"):
            with self.subTest(mode=mode):
                self.repo.update_settings(master_enabled=False, actor_id="admin")
                if mode == "manual":
                    self.repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
                if mode == "force_off":
                    self.repo.env = {"WB_AUTOANSWERS_FORCE_OFF": "true"}
                self.insert(mode)
                self.clock.advance(7 * 86400)
                self.repo.env = {}
                self.enable()
                self.inventory([mode])
                recovered = self.repo.recover_unanswered_inventory(actor_id="recovery")
                self.assertEqual(recovered["admitted"], 1)
                jobs = self.repo.get_feedback(mode)["ai_jobs"]
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["state"], "queued")
                self.assertEqual(self.repo.recover_unanswered_inventory(actor_id="recovery")["admitted"], 0)
        self.insert("absent-local")
        self.inventory([])
        self.assertEqual(self.repo.recover_unanswered_inventory(actor_id="recovery")["admitted"], 0)
        self.assertEqual(self.repo.get_feedback("absent-local")["ai_jobs"], [])
    def test_stale_inventory_does_not_authorize_recovery(self):
        self.insert("old")
        self.enable()
        self.inventory(["old"])
        self.clock.advance(16 * 60)
        self.assertTrue(self.repo.recover_unanswered_inventory(actor_id="recovery")["inventory_pending"])
        self.assertEqual(self.repo.get_feedback("old")["ai_jobs"], [])
    def test_old_terminal_allowlist_preserves_same_key_attempts_audit_and_exclusions(self):
        self.enable()
        safe = self.terminal("technical", "NODE_BOUNDARY_ERROR")
        self.terminal("policy", "owner_policy_unsafe_public_reply")
        self.terminal("manual", "ENOENT", manual=True)
        self.terminal("semantic", "CONTENT_VALIDATION_FAILED")
        self.terminal("not-on-wb", "ENOENT")
        with self.repo.transaction() as conn:
            conn.execute("UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET attempts=273 WHERE processing_key=?", (safe,))
        self.inventory(["technical", "policy", "manual", "semantic"])
        result = self.repo.recover_unanswered_inventory(actor_id="recovery")
        self.assertEqual(result["technical_recovered"], 1)
        job = self.repo.get_feedback("technical")["ai_jobs"][0]
        self.assertEqual((job["processing_key"], job["attempts"], job["processing_kind"]), (safe, 273, "safe_public_template"))
        self.assertIn("processing_terminal_error", {a["event_type"] for a in self.repo.get_feedback("technical")["audit"]})
        for name in ("policy", "manual", "semantic", "not-on-wb"):
            self.assertEqual(self.repo.get_feedback(name)["ai_jobs"][0]["state"], "terminal_error")
        live = self.repo.progress_status()["live_backlog"]
        self.assertEqual(live["unresolved"], 4)
        self.assertEqual(live["policy_error"], 2)
    def test_queued_and_retry_jobs_rebind_after_off_on_without_new_key(self):
        self.enable()
        self.insert("queued-before-off")
        job = self.repo.enqueue_processing("queued-before-off", trigger_source="steady_sync", actor_id="sync")
        self.repo.update_settings(master_enabled=False, actor_id="admin"); self.enable()
        self.inventory(["queued-before-off"])
        self.assertEqual(self.repo.recover_unanswered_inventory(actor_id="recovery")["rebound"], 1)
        claimed = self.repo.claim_processing_job(worker_id="ai")
        self.assertEqual(claimed["processing_key"], job["processing_key"])
        self.repo.record_processing_retry(job["processing_key"], error_code="OPENAI_HTTP_429", retry_after_seconds=60, worker_id="ai")
        self.repo.update_settings(master_enabled=False, actor_id="admin"); self.enable()
        self.inventory(["queued-before-off"])
        self.assertEqual(self.repo.recover_unanswered_inventory(actor_id="recovery")["rebound"], 1)
    def test_crashed_boundary_preserves_hold_and_recovers_without_paid_replay(self):
        self.enable(); self.insert("crashed")
        job = self.repo.enqueue_processing("crashed", trigger_source="steady_sync", actor_id="sync")
        self.repo.claim_processing_job(worker_id="ai", lease_seconds=10)
        self.repo.mark_provider_call_started(job["processing_key"], worker_id="ai")
        self.clock.advance(11); self.repo.reconcile_stale_reservations()
        self.inventory(["crashed"])
        self.assertEqual(self.repo.recover_unanswered_inventory(actor_id="recovery")["crash_recovered"], 1)
        with closing(self.repo._connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts").fetchone()[0], 1)
        worker = AutoanswersProcessingWorker(repository=self.repo, bridge=FailBridge("OPENAI_NETWORK"), media_processor=NoopMedia(), worker_id="ai")
        result = worker.run_once()
        self.assertEqual((result["processing_key"], result["model_calls"]), (job["processing_key"], 0))
        self.insert("fresh-after-crash")
        self.repo.enqueue_processing("fresh-after-crash", trigger_source="steady_sync", actor_id="sync")
        self.assertEqual(self.repo.claim_processing_job(worker_id="ai")["feedback_id"], "fresh-after-crash")
    def test_unlisted_crash_after_confirmed_429_keeps_unquantified_global_latch(self):
        self.enable(); self.insert("orphan")
        key = self.repo.enqueue_processing("orphan", trigger_source="steady_sync", actor_id="sync")["processing_key"]
        self.repo.claim_processing_job(worker_id="ai"); self.repo.mark_provider_call_started(key, worker_id="ai")
        self.repo.record_processing_boundary_failure(key, error_code="OPENAI_HTTP_429", cost_uncertain=False, worker_id="ai")
        self.clock.advance(61); self.repo.claim_processing_job(worker_id="ai", lease_seconds=10)
        self.repo.mark_provider_call_started(key, worker_id="ai")
        self.clock.advance(11); self.repo.reconcile_stale_reservations()
        self.insert("other"); self.inventory(["other"])
        self.repo.recover_unanswered_inventory(actor_id="recovery")
        with closing(self.repo._connect()) as conn:
            self.assertEqual(conn.execute("SELECT stop_reason FROM sheet_vitrina_v1_wb_autoanswers_runtime_state").fetchone()[0], "budget_state_unknown")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts").fetchone()[0], 0)
        self.assertIsNone(self.repo.claim_processing_job(worker_id="ai"))

    def test_crash_after_partial_usage_preserves_known_cost_and_unknown_residual(self):
        for error in ("OPENAI_NETWORK", "OPENAI_HTTP_429"):
            with self.subTest(error=error):
                # Use an independent database to retain exact current-attempt
                # evidence and distinguish known failure from partial usage.
                with TemporaryDirectory() as directory:
                    clock = MutableClock()
                    repo = AutoanswersRepository(runtime_dir=Path(directory), now_factory=clock, env={})
                    repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
                    repo.upsert_feedback(feedback("partial"), source_stream="unanswered", run_kind="steady")
                    key = repo.enqueue_processing("partial", trigger_source="steady_sync", actor_id="sync")["processing_key"]
                    repo.claim_processing_job(worker_id="ai", lease_seconds=10); repo.mark_provider_call_started(key, worker_id="ai")
                    repo.record_failed_processing_usage(key, actual_cost_usd="0.01", usage={}, role_calls=1, error_code=error, worker_id="ai")
                    clock.advance(11); repo.reconcile_stale_reservations()
                    repo.save_sync_cursor("wb_feedback_full_unanswered_inventory", cursor={"coverage_confirmed": True, "completed_at": iso_utc(clock()), "feedback_ids": ["partial"]}, successful=True)
                    self.assertEqual(repo.recover_unanswered_inventory(actor_id="recovery")["crash_recovered"], 1)
                    with closing(repo._connect()) as conn:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts").fetchone()[0], int(error == "OPENAI_NETWORK"))
                        self.assertEqual(float(conn.execute("SELECT actual_cost_usd FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events").fetchone()[0]), 0.01)

    def test_unlisted_partial_crash_blocks_claim_before_boundary_can_be_overwritten(self):
        self.enable(); self.insert("partial-orphan")
        key = self.repo.enqueue_processing("partial-orphan", trigger_source="steady_sync", actor_id="sync")["processing_key"]
        self.repo.claim_processing_job(worker_id="ai", lease_seconds=10); self.repo.mark_provider_call_started(key, worker_id="ai")
        with closing(self.repo._connect()) as conn:
            boundary = conn.execute("SELECT provider_call_started_at FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations WHERE processing_key=?", (key,)).fetchone()[0]
        self.repo.record_failed_processing_usage(key, actual_cost_usd="0.01", usage={}, role_calls=1, error_code="OPENAI_NETWORK", worker_id="ai")
        self.clock.advance(11); self.repo.reconcile_stale_reservations()
        self.insert("other"); self.inventory(["other"])
        self.repo.recover_unanswered_inventory(actor_id="recovery")
        self.assertIsNone(self.repo.claim_processing_job(worker_id="ai"))
        with closing(self.repo._connect()) as conn:
            self.assertEqual(conn.execute("SELECT stop_reason FROM sheet_vitrina_v1_wb_autoanswers_runtime_state").fetchone()[0], "budget_state_unknown")
            self.assertEqual(conn.execute("SELECT provider_call_started_at FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations WHERE processing_key=?", (key,)).fetchone()[0], boundary)
            self.assertEqual(float(conn.execute("SELECT actual_cost_usd FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events").fetchone()[0]), .01)

    def test_worker_off_race_retry_rebinds_on_resume(self):
        self.enable(); self.insert("paused")
        key = self.repo.enqueue_processing("paused", trigger_source="steady_sync", actor_id="sync")["processing_key"]
        self.repo.claim_processing_job(worker_id="ai")
        self.repo.update_settings(master_enabled=False, actor_id="admin")
        self.repo.record_processing_retry(key, error_code="master_switch_off", retry_after_seconds=60, worker_id="ai")
        self.enable(); self.clock.advance(61); self.inventory(["paused"])
        self.assertEqual(self.repo.recover_unanswered_inventory(actor_id="recovery")["rebound"], 1)
        self.assertEqual(self.repo.claim_processing_job(worker_id="ai")["processing_key"], key)

    def test_cooldown_free_queue_does_not_starve_behind_active_run_paid_priority(self):
        self.repo.update_settings(master_enabled=True, mode="manual", actor_id="admin")
        row = feedback("paid"); row["productValuation"] = 1
        self.repo.upsert_feedback(row, source_stream="unanswered", run_kind="steady")
        self.insert("free", empty=True)
        preview = self.repo.preview_mode_transition("auto_all", actor_id="admin", run_max_usd="0.5")
        self.repo.apply_mode_transition("auto_all", actor_id="admin", preview_id=preview["preview_id"])
        self.repo.reconcile_policy_sweep_once(worker_id="test")
        self.inventory(["paid", "free"]); self.repo.recover_unanswered_inventory(actor_id="recovery")
        self.repo.record_provider_failure(error_code="OPENAI_HTTP_429")
        claim = self.repo.claim_processing_job(worker_id="ai")
        self.assertEqual((claim["feedback_id"], claim["processing_kind"]), ("free", "rating_only_template"))
    def test_quota_new_failure_uses_bounded_probe_old_terminal_needs_resume(self):
        self.enable(); self.insert("quota")
        key = self.repo.enqueue_processing("quota", trigger_source="steady_sync", actor_id="sync")["processing_key"]
        worker = AutoanswersProcessingWorker(repository=self.repo, bridge=FailBridge("OPENAI_INSUFFICIENT_QUOTA"), media_processor=NoopMedia(), worker_id="ai")
        self.assertEqual(worker.run_once()["state"], "retryable_error")
        self.assertIsNone(worker.run_once()); self.clock.advance(900)
        self.assertEqual(worker.run_once()["state"], "queued")
        self.assertEqual(worker.run_once()["model_calls"], 0)
        with self.repo.transaction() as conn:
            conn.execute("UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state='terminal_error',last_error_code='OPENAI_INSUFFICIENT_QUOTA',final_reply=NULL,completed_at=? WHERE processing_key=?", (iso_utc(self.clock()), key))
            conn.execute("DELETE FROM sheet_vitrina_v1_wb_publication_jobs WHERE processing_key=?", (key,))
        self.inventory(["quota"])
        self.assertEqual(self.repo.recover_unanswered_inventory(actor_id="recovery")["technical_recovered"], 0)
        self.repo.update_settings(master_enabled=False, actor_id="admin"); self.enable(); self.inventory(["quota"])
        self.assertEqual(self.repo.recover_unanswered_inventory(actor_id="recovery")["technical_recovered"], 1)
    def test_shared_429_pause_bounded_retry_and_free_template_continues(self):
        self.enable()
        for name in ("a", "b"):
            self.insert(name)
            self.repo.enqueue_processing(name, trigger_source="steady_sync", actor_id="sync")
        bridge = FailBridge("OPENAI_HTTP_429")
        worker = AutoanswersProcessingWorker(repository=self.repo, bridge=bridge, media_processor=NoopMedia(), worker_id="ai")
        self.assertEqual(worker.run_once()["state"], "retryable_error")
        self.assertTrue(self.repo.provider_pause_status()["active"])
        self.assertIsNone(worker.run_once())
        self.clock.advance(90)
        self.assertEqual(worker.run_once()["state"], "queued")
        self.assertEqual(worker.run_once()["model_calls"], 0)
        self.assertEqual(bridge.calls, 2)
        with closing(self.repo._connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts").fetchone()[0], 0)
    def test_network_failure_uncertain_hold_429_known_failure_no_hold(self):
        self.enable()
        self.insert("network")
        self.repo.enqueue_processing("network", trigger_source="steady_sync", actor_id="sync")
        worker = AutoanswersProcessingWorker(repository=self.repo, bridge=FailBridge("OPENAI_NETWORK"), media_processor=NoopMedia(), worker_id="ai")
        self.assertEqual(worker.run_once()["state"], "retryable_error")
        with closing(self.repo._connect()) as conn:
            hold = conn.execute("SELECT * FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts").fetchone()
            self.assertEqual(hold["error_code"], "OPENAI_NETWORK")
        self.assertGreater(float(hold["upper_bound_usd"]), 0)
    def test_absent_tail_detail_reconciliation_preserves_unknown_and_excludes_wbru(self):
        for name in ("answered", "unknown", "already-wbru"):
            self.insert(name)
        row = feedback("already-wbru"); row["state"] = "wbRu"
        self.repo.upsert_feedback(row, source_stream="detail", run_kind="detail_readback")
        self.inventory([])
        class Source(FakeReadSource):
            def fetch_detail(_self, name):
                return feedback(name, answer="Ответ WB") if name == "answered" else None
        service = WbFeedbackSyncService(repository=self.repo, source=Source(), now_factory=self.clock)
        self.assertEqual(service.reconcile_absent_unanswered_tick(batch_size=10), {"checked": 2, "resolved": 1})
        self.assertEqual(self.repo.get_feedback("unknown")["answer"]["text"], "")
        self.assertEqual(self.repo.local_unanswered_count(), 1)
    def test_startup_resume_and_epoch_change_start_full_inventory_immediately(self):
        self.enable()
        source = FakeReadSource()
        source.pages = []
        service = WbFeedbackSyncService(repository=self.repo, source=source, now_factory=self.clock)
        class Worker:
            def run_once(_self): return None
        coordinator = AutoanswersCoordinator(repository=self.repo, sync_service=service, processing_worker=Worker(), publication_worker=Worker(), worker_id="test")
        for event in ("startup", "resume", "epoch"):
            if event == "resume": self.clock.advance(7 * 86400)
            if event == "epoch":
                self.repo.update_settings(master_enabled=False, actor_id="admin"); self.enable()
            result = coordinator.run_once()
            self.assertIsNotNone(result["full_unanswered_inventory"], (event, result["errors"]))
            self.assertFalse(result["errors"])


class BoundaryCompatibilityTest(unittest.TestCase):
    def invoke_failure(self, code, message, *, partial_cost=0, operation="run", execution_mode="live"):
        envelope = {"boundary_version": NODE_BOUNDARY_VERSION, "bundle_version": PROMPT_BUNDLE_VERSION,
                    "evaluation_signature": EVALUATION_SIGNATURE, "ok": False,
                    "error": {"code": code, "message": message, "partial_cost_usd": partial_cost,
                              "partial_usage": {"input_tokens": 100}, "partial_role_calls": int(partial_cost > 0)}}
        completed = SimpleNamespace(returncode=1, stdout=json.dumps(envelope), stderr="")
        with patch("packages.application.wb_autoanswers_node_bridge.subprocess.run", return_value=completed):
            with self.assertRaises(NodeBoundaryError) as failure:
                NodeAutoanswersBridge(env={})._invoke({"operation": operation, "execution_mode": execution_mode})
        return failure.exception
    def test_original_runner_network_timeouts_and_invalid_response_are_retryable_unknown(self):
        for original, message, normalized in (
            ("NODE_BOUNDARY_ERROR", "fetch failed", "OPENAI_NETWORK"),
            ("ECONNRESET", "socket closed", "OPENAI_NETWORK"),
            ("23", "The operation was aborted due to timeout", "OPENAI_TIMEOUT"),
            ("20", "This operation was aborted", "OPENAI_TIMEOUT"),
            ("OPENAI_HTTP_200", "Responses API HTTP 200", "OPENAI_RESPONSE_INVALID"),
            ("OPENAI_HTTP_204", "Responses API HTTP 204", "OPENAI_RESPONSE_INVALID"),
            ("OPENAI_HTTP_503", "Responses API HTTP 503", "OPENAI_HTTP_503"),
        ):
            with self.subTest(original=original):
                error = self.invoke_failure(original, message, partial_cost=.01)
                self.assertEqual(error.code, normalized)
                self.assertTrue(error.retryable)
                self.assertTrue(error.provider_cost_uncertain)
                self.assertEqual(error.partial_cost_usd, .01)
                self.assertEqual(error.partial_role_calls, 1)
    def test_confirmed_429_quota_auth_and_domain_failures_do_not_gain_unknown_cost(self):
        for code, retryable in (("OPENAI_HTTP_429", True), ("OPENAI_INSUFFICIENT_QUOTA", True),
                                ("OPENAI_HTTP_401", False), ("OPENAI_API_KEY_MISSING", False),
                                ("NODE_BOUNDARY_ERROR", False)):
            with self.subTest(code=code):
                error = self.invoke_failure(code, "deterministic error", partial_cost=.01)
                self.assertEqual(error.code, code)
                self.assertEqual(error.retryable, retryable)
                self.assertFalse(error.provider_cost_uncertain)
    def test_manual_guard_and_fixture_errors_are_not_provider_normalized(self):
        error = self.invoke_failure("NODE_BOUNDARY_ERROR", "fetch failed", operation="guard_final")
        self.assertEqual(error.code, "NODE_BOUNDARY_ERROR")
        self.assertFalse(error.retryable)
        error = self.invoke_failure("NODE_BOUNDARY_ERROR", "fetch failed", execution_mode="fixture")
        self.assertFalse(error.retryable)
    def test_registered_transport_explicitly_enables_gate_for_only_canonical_remote_process(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"operation_id": "recovery-gate-test", "state": "not_submitted"}))
        with patch.object(transport, "configure_ssh"), patch.object(transport, "trusted_main_sha", return_value="a" * 40), patch.object(transport.subprocess, "run", side_effect=run):
            transport.WbAutoanswersRecoveryProductionAdapter().readback({"capture_only": True}, "recovery-gate-test")
        self.assertEqual(calls[0][-8:], ["env", "WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true", "python3", transport.REMOTE_APP,
                                      "--runtime-dir", "/opt/wb-core-runtime/state", "--env-file", "/opt/wb-ai/.env"])
        self.assertEqual(calls[0][-9], "wb-core-eu-root")


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.repo = AutoanswersRepository(runtime_dir=self.runtime, env={})
        self.repo.update_settings(master_enabled=True, mode="auto_all", actor_id="admin")
        self.details = {"unsafe": feedback("unsafe")}
        self.source = FakeSource(self.details)
        self.repo.upsert_feedback(self.details["unsafe"], source_stream="unanswered", run_kind="steady")
        job = self.repo.enqueue_processing("unsafe", trigger_source="steady_sync", actor_id="sync")
        self.repo.claim_processing_job(worker_id="ai")
        self.repo.record_processing_terminal(job["processing_key"], error_code="owner_policy_unsafe_public_reply", worker_id="ai")
        backup = self.runtime / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}" / "verified.sqlite3"
        backup.parent.mkdir(parents=True)
        with sqlite3.connect(self.repo.db_path) as source:
            with sqlite3.connect(backup) as destination: source.backup(destination)
        self.backup = backup
        self.sha_patch = patch.object(adapter.domain, "_deployed_runtime_evidence", return_value={"runtime_sha": "a" * 40, "deployment_complete": True})
        self.sha_patch.start()
    def tearDown(self):
        self.sha_patch.stop(); self.temp.cleanup()
    def invoke(self, action, request, **kwargs):
        return adapter.execute({"action": action, "operation_id": "recovery-test-0001", "request": request,
            "expected_runtime_sha": "a" * 40, "actor": "test", **kwargs}, runtime_dir=self.runtime, env_file=self.runtime / "absent", source=self.source)
    def request(self):
        manifest = self.invoke("preview", {"capture_only": True})["scope"]["manifest"]
        return {"manifest": manifest, "approval_reference": "explicit answer-all", "recovery_reference": str(self.backup)}
    def test_capture_preview_apply_atomic_enqueue_readback_unsafe_audit_never_reapproved(self):
        request = self.request()
        audited = {"outcome": "ready", "final_reply": "Экран остался цел", "result": successful_result()}
        with patch.object(AutoanswersRepository, "_completed_node_evidence", return_value=audited):
            preview = self.invoke("preview", request)
            self.assertEqual(preview["scope"]["actions"][0]["action"], "safe_public_recovery")
            self.assertEqual(self.invoke("readback", request)["state"], "not_submitted")
            result = self.invoke("apply", request, expected_prestate=preview["prestate_sha256"], expected_candidate=preview["candidate_sha256"])
            self.assertEqual(result["disposition"], "submitted")
        self.assertEqual(self.invoke("readback", request)["state"], "applied")
        job = self.repo.get_feedback("unsafe")["ai_jobs"][0]
        self.assertEqual(job["processing_kind"], "safe_public_template")
        self.assertFalse(job["final_reply"])
        self.assertEqual(self.invoke("apply", request)["disposition"], "already_applied")
    def test_fingerprint_drift_does_not_claim_or_mutate(self):
        request = self.request()
        with self.assertRaisesRegex(ValueError, "drift"):
            self.invoke("apply", request, expected_prestate="wrong", expected_candidate="wrong")
        self.assertEqual(self.invoke("readback", request)["state"], "not_submitted")
        self.assertEqual(self.repo.get_feedback("unsafe")["ai_jobs"][0]["state"], "terminal_error")
    def test_interrupted_apply_retained_marker_readback_only(self):
        request = self.request(); preview = self.invoke("preview", request)
        with patch.object(adapter.domain, "apply_plan", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(AmbiguousSubmit):
                self.invoke("apply", request, expected_prestate=preview["prestate_sha256"], expected_candidate=preview["candidate_sha256"])
        self.assertEqual(self.invoke("readback", request)["state"], "ambiguous")
        with patch.object(adapter.domain, "apply_plan") as apply:
            with self.assertRaises(AmbiguousSubmit): self.invoke("apply", request)
            apply.assert_not_called()
    def test_capture_cannot_apply_and_request_change_cannot_reuse_operation(self):
        with self.assertRaisesRegex(ValueError, "capture"):
            self.invoke("apply", {"capture_only": True})
