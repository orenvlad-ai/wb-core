#!/usr/bin/env python3
"""Production-shaped smoke for WBC0010 exact Autoanswers recovery."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0010_autoanswers_budget_recovery as recovery


def _plan() -> dict:
    return {
        "contract": "wb_autoanswers_budget_reconciliation_v1",
        "candidate_count": 1,
        "settings_revision": "sha256:" + "b" * 64,
        "active_run_cap": {
            "transition_run_id": "transition-1",
            "run_max_usd": "10.0",
            "run_max_paid_reviews": 200,
        },
        "plan_fingerprint": "sha256:" + "a" * 64,
        "pre_change_digest": "sha256:" + "a" * 64,
        "runtime": {"stop_reason": "budget_state_unknown"},
        "holds": [
            {
                "processing_key": recovery.INCIDENT_PROCESSING_KEY,
                "feedback_id": recovery.INCIDENT_FEEDBACK_ID,
                "content_version": 1,
                "provider_call_started_at": recovery.INCIDENT_PROVIDER_STARTED_AT,
                "reservation_status": "released",
                "released_reason": "stale_or_orphaned",
                "reservation_actual_cost_usd": "0",
                "job_state": "processing",
                "last_error_code": None,
                "completed_at": None,
                "lease_owner": "worker-before-release",
                "lease_until": "2026-08-27T15:21:46.753793Z",
                "recovery_action": "append_hold_and_terminalize_interrupted_processing",
                "upper_bound_usd": recovery.MAXIMUM_UNCERTAINTY_USD,
            }
        ],
        "expected_affected_records": {
            "uncertainty_holds_inserted": 1,
            "processing_jobs_terminalized": 1,
            "audit_events_appended": 2,
            "runtime_state_rows_updated": 1,
            "provider_calls_created": 0,
            "cost_events_created": 0,
            "wb_writes_created": 0,
        },
        "non_target_invariants": {
            "provider_calls_unchanged": True,
            "cost_events_unchanged": True,
            "wb_writes_unchanged": True,
            "reservation_evidence_unchanged": True,
            "non_target_jobs_unchanged": True,
            "target_job_change_bounded_to_terminal_lifecycle": True,
        },
    }


def _lifecycle() -> dict:
    return {
        "result": {
            "lifecycle": {
                "business_mode": "auto_all",
                "desired": True,
                "actual": True,
                "suspended_by_master": False,
                "lifecycle_state": "running",
                "drift_status": "matched",
                "stop_reason": "",
                "transition_run_id": "transition-1",
            },
            "settings": {
                "mode": "auto_all",
                "master_enabled": True,
                "policy_epoch": 25,
            },
            "master_policy": {"master_desired": True},
        }
    }


def _readback() -> dict:
    return {
        "result": {
            "status": "confirmed",
            "budget": {"budget_state": "conservative_unverified"},
            "readback": {
                "confirmed": True,
                "unresolved_count": 0,
                "stop_reason": "",
                "terminalized_interruptions": [
                    {
                        "processing_key": recovery.INCIDENT_PROCESSING_KEY,
                        "job_state": "terminal_error",
                        "last_error_code": "provider_boundary_interrupted_unknown_result",
                        "attempts": 1,
                        "lease_owner": None,
                        "lease_until": None,
                        "completed_at": "2026-08-30T10:00:00Z",
                        "job_actual_cost_usd": "0",
                        "reservation_status": "released",
                        "reservation_actual_cost_usd": "0",
                        "released_reason": "stale_or_orphaned",
                        "provider_call_started_at": recovery.INCIDENT_PROVIDER_STARTED_AT,
                        "hold_count": 1,
                        "hold_upper_bound_usd": 0.1,
                        "cost_event_count": 0,
                        "failed_cost_event_count": 0,
                        "publication_job_count": 0,
                        "details": {
                            "plan_fingerprint": "sha256:" + "a" * 64,
                            "provider_call_replayed": False,
                            "wb_post_created": False,
                            "actual_cost_asserted": False,
                        },
                    }
                ],
            },
        }
    }


class OuterRecoveryTest(unittest.TestCase):
    def test_ambiguous_submit_is_never_repeated_and_ends_in_query_only_readback(self) -> None:
        with TemporaryDirectory() as directory:
            calls: list[tuple[str, ...]] = []

            def fake_hosted(arguments: list[str], *, allow_failure: bool = False) -> dict:
                del allow_failure
                key = tuple(arguments)
                calls.append(key)
                if key == ("autoanswers-budget-reconciliation", "dry-run"):
                    return {"result": _plan()}
                if key == ("autoanswers-lifecycle", "status"):
                    return _lifecycle()
                if key[:2] == ("autoanswers-budget-reconciliation", "apply"):
                    return {"status": "transport_ambiguous", "return_code": 255}
                if key == ("autoanswers-budget-reconciliation", "readback"):
                    return _readback()
                raise AssertionError(key)

            with (
                patch.dict(os.environ, {"RUNNER_TEMP": directory}, clear=False),
                patch.object(recovery, "_run_hosted", side_effect=fake_hosted),
            ):
                planned = recovery.dry_run()
                self.assertEqual(planned["status"], "ready")
                applied = recovery.apply()
                self.assertEqual(applied["status"], "complete")
                self.assertEqual(applied["apply_count"], 1)
                repeated = recovery.apply()
                self.assertEqual(repeated["status"], "complete")

            apply_calls = [
                call
                for call in calls
                if call[:2] == ("autoanswers-budget-reconciliation", "apply")
            ]
            self.assertEqual(len(apply_calls), 1)
            self.assertGreaterEqual(
                calls.count(("autoanswers-budget-reconciliation", "readback")),
                2,
            )

    def test_dry_run_rejects_any_second_or_changed_boundary(self) -> None:
        plan = _plan()
        plan["candidate_count"] = 2
        plan["holds"].append(dict(plan["holds"][0]))
        with TemporaryDirectory() as directory:
            with (
                patch.dict(os.environ, {"RUNNER_TEMP": directory}, clear=False),
                patch.object(
                    recovery,
                    "_run_hosted",
                    return_value={"result": plan},
                ),
            ):
                with self.assertRaisesRegex(
                    recovery.Wbc0010AutoanswersRecoveryError,
                    "exact one unresolved",
                ):
                    recovery.dry_run()


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(OuterRecoveryTest))
    for name in (
        "apps.wb_autoanswers_runtime_test.RuntimeTest.test_budget_uncertainty_reconciliation_appends_hold_without_fake_spend",
        "apps.wb_autoanswers_runtime_test.RuntimeTest.test_budget_reconciliation_terminalizes_orphaned_provider_boundary_without_replay",
        "apps.wb_autoanswers_runtime_test.RuntimeTest.test_budget_reconciliation_fingerprint_binds_complete_settings",
        "apps.wb_autoanswers_activation_test.ActivationTest.test_deploy_quiesce_drains_active_provider_job_without_service_stop",
        "apps.wb_autoanswers_activation_test.ActivationTest.test_deploy_quiesce_timeout_restores_timers_without_killing_worker",
        "apps.wb_autoanswers_activation_test.ActivationTest.test_deploy_quiesce_accepts_terminal_failed_oneshot_and_preserves_no_active_timer",
    ):
        suite.addTests(loader.loadTestsFromName(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
