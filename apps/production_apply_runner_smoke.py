#!/usr/bin/env python3
"""Deterministic smoke coverage for task-scoped one-submit production apply."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import production_apply_runner as apply
from packages.application.fbs_lifecycle_manifests import attach_digest, digest


MERGE_SHA = "a" * 40
RECOVERY_RUN_ID = 32872430422
AUTHORIZATION_COMMENT_ID = 5413456865
MOUNT_PROBE_COMMENT_ID = 5413456800
RECOVERY_RELEASE_OPERATION = "release-v2-" + "1" * 32
AUTH_BODY = (
    "/wb-core authorize-goal-v1 task WBC0006 profile inventory-history-backfill "
    "target wb_core_eu_hosted_runtime_active dates 2026-03-01..2026-08-24 "
    "captures 177 components 18054 finalizations 177 full-days 172 partial-days 5"
)
WARM_AUTH_BODY = (
    "/wb-core authorize-goal-v1 task WBC0008 profile root-warm-archive-six "
    "target wb_core_eu_hosted_runtime_active sources 6 archives 6 manifests 6 "
    "unlinks 6 reclaimed-allocated-bytes 27591725056 "
    "root-minimum-bytes 26843545600 backup-floor-bytes 41105612800"
)
WBC0013_AUTH_BODY = (
    "/wb-core authorize-goal-v1 task WBC0013 profile dense-fbs-historical-recovery "
    "target wb_core_eu_hosted_runtime_active roster 71 existing 21 "
    "owner-approved-missing 50 zero-inserts 50 historical-date 2026-08-26 "
    "historical-nm 428853741 historical-version whfv_cb0657c384d5adebae01e585 "
    "historical-event ffbf_87cea959c9d600da99caa1ab68ef historical-repairs 1"
)
WBC0027_AUTH_BODY = (
    "/wb-core authorize-goal-v1 task WBC0027 "
    "profile product-capital-qualified-economics "
    "target wb_core_eu_hosted_runtime_active "
    "product-rows 1152 product-cells 24192 product-mismatches 9446 "
    "primary-rows 936 primary-cells 19656 primary-mismatches 7655 "
    "secondary-rows 216 secondary-mismatches 1791 "
    "special-date 2026-08-21 special-nm 497413772 special-cells 16 "
    "blocked-date 2026-08-15 hard-non-target-from 2026-08-30 "
    "economics-logical 298 economics-persisted 472 economics-blocked 12 "
    "protected-nm 428853741 protected-unit-cost-rub 117.537167 submits 2 "
    "predecessor-pr 1128 "
    "predecessor-release-operation release-v2-52c958d066816e6e7b2fec7b419fc530 "
    "predecessor-release-comment 5471998411 "
    "predecessor-authorization-comment 5472023099 "
    "predecessor-apply-run 33343193199 predecessor-apply-comment 5472070488 "
    "predecessor-receipt "
    "sha256:2e65b37d7a44027928143d0f8b4ab71c43638450f659c4875faf3b0d80f7b9d5 "
    "predecessor-operation production-goal-v1-89bfdc5e4e4bffcbc9f6f6aea677e389 "
    "predecessor-product-phase recovery_303ece915dfb8e89b615a84dc8f14d70 "
    "predecessor-economics-phase recovery_8fe6bf612bde74c0dec9cb3b441944b2"
)
FBS_PASSPORT = apply._load_fbs_incident_passport()
FBS_PASSPORT_DIGEST = apply.fbs_file_digest(
    apply.WBC0027_FBS_INCIDENT_PASSPORT_PATH
)
FBS_MAPPING_READBACK_DIGEST = "sha256:" + "8" * 64
FBS_IMPACT_DIGEST = "sha256:" + "9" * 64
FBS_RECOVERY_DIGEST = "sha256:" + "a" * 64
FBS_MAPPING_OPERATION = "production-goal-v2-" + "6" * 32
WBC0027_FBS_QUALITY_AUTH_BODY = (
    "/wb-core authorize-goal-v2 task WBC0027 "
    "profile fbs-lifecycle-recovery-v2 "
    "target wb_core_eu_hosted_runtime_active incident-passport "
    + FBS_PASSPORT_DIGEST
    + " mapping-operation "
    + FBS_MAPPING_OPERATION
    + " mapping-readback "
    + FBS_MAPPING_READBACK_DIGEST
    + " impact "
    + FBS_IMPACT_DIGEST
    + " recovery "
    + FBS_RECOVERY_DIGEST
    + " submits 1"
)
WBC0027_FBS_MAPPING_AUTH_BODY = (
    "/wb-core authorize-goal-v2 task WBC0027 "
    "profile fbs-identity-mapping-v2 "
    "target wb_core_eu_hosted_runtime_active incident-passport "
    + FBS_PASSPORT_DIGEST
    + " operation "
    + FBS_PASSPORT["operation_id"]
    + " inserts 1 submits 1"
)
HISTORICAL_COST_AUTH_BODY = (
    "/wb-core authorize-goal-v1 task WBC0013 "
    "profile historical-analytical-cost-carry-forward "
    "target wb_core_eu_hosted_runtime_active business-date 2026-08-26 "
    "nm 428853741 unit-cost-rub 117.537167 "
    "accepted-versions 1 ready-snapshots 1"
)
HISTORICAL_COST_APPROVAL_DIGEST = "sha256:" + "9" * 64
HISTORICAL_COST_APPROVAL_REFERENCE = (
    "github:fixture:" + HISTORICAL_COST_APPROVAL_DIGEST
)
HISTORICAL_MISSING_AUTH_BODY = (
    "/wb-core authorize-goal-v1 task WBC0010 profile historical-missing-repair "
    "target wb_core_eu_hosted_runtime_active "
    "source-operation recovery_6b52b021d0d8302fdf87004487661709 "
    "source-digest sha256:510138ca43f717751ebcbc85997bc66baec3f7c65bf89c041f52943a4eb59181 "
    "dates 7 logical-targets 1428 snapshots 9 missing-before 1302 missing-after 0 "
    "excluded-date 2026-08-26"
)


def _exercise_historical_cost_runner() -> None:
    goal = apply.validate_authorization(
        authorization(body=HISTORICAL_COST_AUTH_BODY),
        repository="orenvlad-ai/wb-core",
        pr=1050,
    )
    assert goal["profile"] == apply.HISTORICAL_COST_GOAL_PROFILE
    assert goal["max_mutation_submits"] == 1
    assert goal["owner_fixed_unit_cost_rub"] == "117.537167"
    operation = "production-goal-v1-" + "8" * 32
    evidence_dir = (
        "/opt/wb-core-runtime/state/backups/private-evidence/production-goals/"
        + operation
    )
    manifest_path = (
        evidence_dir
        + "/historical-cost-carry-forward-plan-20260828T120000Z.json"
    )
    persistence = {
        "owner": "production_apply_evidence",
        "destination": manifest_path,
        "evidence_dir": evidence_dir,
        "evidence_dir_mode": "0700",
        "file_mode": "0600",
        "parent_mode": "0700",
        "bounded_size": True,
        "atomic_publish": True,
        "no_overwrite": True,
        "durable_file_fsync": True,
        "durable_directory_fsync": True,
        "root_storage_admission": {
            "owner": "production_apply_evidence",
            "allowed": True,
        },
    }
    candidate = {
        "status": "ready",
        "query_only": True,
        "database_written": False,
        "business_date": "2026-08-26",
        "nm_id": 428853741,
        "accepted_vitrina_version_count": 1,
        "updated_ready_snapshot_count": 1,
        "target_generation_bound": True,
        "barrier_inactive": True,
        "timer_change_count": 0,
        "target_binding": {
            "validated": True,
            "target_id": "wb_core_eu_hosted_runtime_active",
            "deployed_sha": MERGE_SHA,
        },
        "manifest_path": manifest_path,
        "manifest_sha256": "sha256:" + "1" * 64,
        "material_qualification_digest": "sha256:" + "2" * 64,
        "source_digest": "sha256:" + "3" * 64,
        "non_target_digest": "sha256:" + "4" * 64,
        "other_ready_snapshots_digest": "sha256:" + "5" * 64,
        "selection_method": "owner_fixed_historical_analytical_cost_v1",
        "source_business_date": "2026-08-26",
        "source_unit_cost_rub": "117.537167",
        "owner_fixed_unit_cost_rub": "117.537167",
        "owner_authorization_digest": HISTORICAL_COST_APPROVAL_DIGEST,
        "inventory_cost_formula_version": "our_inventory_wac_wb_ff_v1",
        "physical_history_consulted": False,
        "after_metrics": {"after_required_values": list(range(1, 13))},
        "plan_persistence": persistence,
    }
    common = {
        "return_code": 0,
        "transport_ambiguous": False,
        "command_sha256": "a" * 64,
        "stdout_sha256": "b" * 64,
        "stderr_sha256": "c" * 64,
    }
    readback = {
        "status": "reconciled",
        "query_only": True,
        "database_written": False,
        "submit_count": 1,
        "business_date": "2026-08-26",
        "nm_id": 428853741,
        "selection_method": "owner_fixed_historical_analytical_cost_v1",
        "source_unit_cost_rub": "117.537167",
        "owner_fixed_unit_cost_rub": "117.537167",
        "owner_authorization_digest": HISTORICAL_COST_APPROVAL_DIGEST,
        "inventory_cost_formula_version": "our_inventory_wac_wb_ff_v1",
        "physical_history_consulted": False,
        "accepted_vitrina_version_count": 1,
        "updated_ready_snapshot_count": 1,
        "runtime_controls_changed": False,
        "timer_change_count": 0,
        "metrics": {"after_required_values": list(range(1, 13))},
    }
    sequence = iter(
        [
            {**common, "result": candidate},
            {**common, "result": candidate},
            {**common, "result": {"status": "submitted", "submit_count": 1}},
            {**common, "result": readback},
        ]
    )
    original_command = apply.command_evidence
    original_sleep = apply.time.sleep
    apply.command_evidence = lambda *_args, **_kwargs: next(sequence)
    apply.time.sleep = lambda *_args, **_kwargs: None
    try:
        result = apply.run_historical_cost_goal(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference=HISTORICAL_COST_APPROVAL_REFERENCE,
        )
    finally:
        apply.command_evidence = original_command
        apply.time.sleep = original_sleep
    assert result["state"] == "done"
    assert result["apply_count"] == result["submit_count"] == 1
    assert [item["qualification_state"] for item in result["qualification_attempts"]] == [
        "matching_witness",
        "qualified",
    ]

    blocked_sequence = iter(
        [
            {
                **common,
                "return_code": 2,
                "result": {
                    "status": "blocked",
                    "code": "owner_fixed_input_invalid",
                    "message": "typed fixed input is invalid",
                    "submit_count": 0,
                },
            }
        ]
    )
    apply.command_evidence = lambda *_args, **_kwargs: next(blocked_sequence)
    try:
        blocked = apply.run_historical_cost_goal(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference=HISTORICAL_COST_APPROVAL_REFERENCE,
        )
    finally:
        apply.command_evidence = original_command
    assert blocked["state"] == "blocked" and blocked["apply_count"] == 0
    assert blocked["failure"]["code"] == "owner_fixed_input_invalid"


def _exercise_historical_missing_runner() -> None:
    goal = apply.validate_authorization(
        authorization(body=HISTORICAL_MISSING_AUTH_BODY),
        repository="orenvlad-ai/wb-core",
        pr=1050,
    )
    assert goal["profile"] == apply.HISTORICAL_MISSING_REPAIR_GOAL_PROFILE
    assert goal["expected_logical_target_count"] == 1428
    operation = "production-goal-v1-" + "6" * 32
    evidence_dir = (
        "/opt/wb-core-runtime/state/backups/private-evidence/production-goals/"
        + operation
    )
    manifest_path = (
        evidence_dir + "/historical-missing-repair-plan-20260830T120000Z.json"
    )
    persistence = {
        "owner": "production_apply_evidence",
        "destination": manifest_path,
        "evidence_dir": evidence_dir,
        "evidence_dir_mode": "0700",
        "file_mode": "0600",
        "parent_mode": "0700",
        "bounded_size": True,
        "atomic_publish": True,
        "no_overwrite": True,
        "durable_file_fsync": True,
        "durable_directory_fsync": True,
        "root_storage_admission": {
            "owner": "production_apply_evidence",
            "allowed": True,
        },
    }
    candidate = {
        "status": "ready",
        "query_only": True,
        "database_written": False,
        "source_operation_id": goal["source_operation_id"],
        "source_digest": goal["source_digest"],
        "target_dates": [
            "2026-08-22",
            "2026-08-23",
            "2026-08-24",
            "2026-08-25",
            "2026-08-27",
            "2026-08-28",
            "2026-08-29",
        ],
        "excluded_date": "2026-08-26",
        "logical_target_count": 1428,
        "updated_ready_snapshot_count": 9,
        "current_missing_count": 1302,
        "after_missing_count": 0,
        "would_change": True,
        "target_generation_bound": True,
        "barrier_inactive": True,
        "timer_change_count": 0,
        "target_binding": {
            "validated": True,
            "target_id": "wb_core_eu_hosted_runtime_active",
            "deployed_sha": MERGE_SHA,
        },
        "manifest_path": manifest_path,
        "manifest_sha256": "sha256:" + "1" * 64,
        "material_qualification_digest": "sha256:" + "2" * 64,
        "non_target_digest": "sha256:" + "3" * 64,
        "other_ready_snapshots_digest": "sha256:" + "4" * 64,
        "plan_persistence": persistence,
    }
    readback = {
        "status": "reconciled",
        "query_only": True,
        "database_written": False,
        "submit_count": 1,
        "target_dates": candidate["target_dates"],
        "excluded_date": "2026-08-26",
        "source_operation_id": goal["source_operation_id"],
        "source_digest": goal["source_digest"],
        "logical_target_count": 1428,
        "updated_ready_snapshot_count": 9,
        "after_missing_count": 0,
        "active_target_repair_dates": [],
        "recovery_lifecycle": "retained",
        "runtime_controls_changed": False,
        "timer_change_count": 0,
    }
    common = {
        "return_code": 0,
        "transport_ambiguous": False,
        "command_sha256": "a" * 64,
        "stdout_sha256": "b" * 64,
        "stderr_sha256": "c" * 64,
    }
    sequence = iter(
        [
            {**common, "result": candidate},
            {**common, "result": candidate},
            {**common, "result": {"status": "submitted", "submit_count": 1}},
            {**common, "result": readback},
        ]
    )
    original_command = apply.command_evidence
    original_sleep = apply.time.sleep
    apply.command_evidence = lambda *_args, **_kwargs: next(sequence)
    apply.time.sleep = lambda *_args, **_kwargs: None
    try:
        result = apply.run_historical_missing_goal(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference="github:fixture:sha256:" + "d" * 64,
        )
    finally:
        apply.command_evidence = original_command
        apply.time.sleep = original_sleep
    assert result["state"] == "done"
    assert result["apply_count"] == result["submit_count"] == 1
    assert [item["qualification_state"] for item in result["qualification_attempts"]] == [
        "matching_witness",
        "qualified",
    ]


def _exercise_wbc0027_predecessor_binding(goal: dict[str, object]) -> None:
    binding = apply.WBC0027_PREDECESSOR_BINDING
    release_payload = {
        "state": "done",
        "operation_id": binding["release_operation_id"],
        "pull_request": binding["pull_request"],
        "merge_sha": "68426769aa7dffec495eac66a8f502e2903c769b",
        "deployed_sha": "68426769aa7dffec495eac66a8f502e2903c769b",
        "release_kind": "live_runtime",
    }
    apply_summary = {
        "schema": apply.APPLY_COMMENT_SUMMARY_SCHEMA,
        "state": "blocked",
        "reason": "wbc0027-product-query-only-readback-not-reconciled",
        "operation_id": binding["goal_operation_id"],
        "release_operation_id": binding["release_operation_id"],
        "pull_request": binding["pull_request"],
        "apply_count": 0,
        "artifact": {
            "name": binding["artifact_name"],
            "sha256": binding["receipt_sha256"],
        },
    }
    comments = [
        {
            "id": binding["release_comment_id"],
            "user": {"login": "github-actions[bot]"},
            "body": (
                f"<!-- {apply.RECEIPT_MARKER} "
                f"operation={binding['release_operation_id']} -->\n```json\n"
                + json.dumps(release_payload, sort_keys=True)
                + "\n```"
            ),
        },
        {
            "id": binding["authorization_comment_id"],
            "author_association": "OWNER",
            "body": apply.WBC0027_PREDECESSOR_AUTH_BODY,
        },
        {
            "id": binding["apply_comment_id"],
            "user": {"login": "github-actions[bot]"},
            "body": apply.marker(str(binding["goal_operation_id"]))
            + "\n```json\n"
            + json.dumps(apply_summary, sort_keys=True)
            + "\n```",
        },
    ]
    qualification = [
        {
            "material_qualification_digest": "sha256:" + "0" * 64,
            "phase_operation_id": binding["product_phase_operation_id"],
        },
        {
            "material_qualification_digest": "sha256:" + "0" * 64,
            "phase_operation_id": binding["product_phase_operation_id"],
        },
    ]
    receipt = {
        "schema": apply.APPLY_RECEIPT_SCHEMA,
        "state": "blocked",
        "operation_id": binding["goal_operation_id"],
        "pull_request": binding["pull_request"],
        "release_operation_id": binding["release_operation_id"],
        "authorization_comment_id": binding["authorization_comment_id"],
        "authorization_body_sha256": apply.digest(
            apply.WBC0027_PREDECESSOR_AUTH_BODY.encode("utf-8")
        ),
        "goal": {
            key: value
            for key, value in goal.items()
            if key != "supersedes_terminal_predecessor"
        },
        "apply_count": 0,
        "evidence": {
            "state": "blocked",
            "product_state": "not_applied",
            "economics_state": "not_applied",
            "qualification_attempts": {
                "product": qualification,
                "economics": [],
            },
            "product_apply": {
                "result": {
                    "error_type": "TypeError",
                    "error": (
                        "warehouse_sync_lock() got an unexpected keyword argument "
                        "'operation'"
                    ),
                    "phase_operation_id": binding["product_phase_operation_id"],
                    "production_mutation_submit_count": 0,
                }
            },
        },
    }
    predecessor = apply._validate_wbc0027_predecessor_evidence(
        goal=goal,
        comments=comments,
        run={"validated_artifact": {"id": binding["artifact_id"]}},
        receipt=receipt,
    )
    assert predecessor["state"] == "blocked"
    assert predecessor["apply_count"] == 0
    assert predecessor["private_manifests_reusable"] is False
    assert predecessor["terminal_operation_reusable"] is False

    changed = json.loads(json.dumps(receipt))
    changed["apply_count"] = 1
    try:
        apply._validate_wbc0027_predecessor_evidence(
            goal=goal,
            comments=comments,
            run={"validated_artifact": {"id": binding["artifact_id"]}},
            receipt=changed,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("mutated WBC0027 predecessor evidence was accepted")


def _exercise_wbc0027_two_phase_runner() -> None:
    goal = apply.validate_authorization(
        authorization(body=WBC0027_AUTH_BODY),
        repository="orenvlad-ai/wb-core",
        pr=1050,
    )
    assert goal["profile"] == apply.WBC0027_GOAL_PROFILE
    assert goal["max_product_submits"] == goal["max_economics_submits"] == 1
    assert goal["supersedes_terminal_predecessor"] == apply.WBC0027_PREDECESSOR_BINDING
    _exercise_wbc0027_predecessor_binding(goal)
    old_unbound_body = WBC0027_AUTH_BODY.split(" predecessor-pr ", 1)[0]
    try:
        apply.validate_authorization(
            authorization(body=old_unbound_body),
            repository="orenvlad-ai/wb-core",
            pr=1050,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("terminal WBC0027 passport grammar was blindly reused")
    fresh_operation = apply.operation_id(
        "orenvlad-ai/wb-core", 1130, 5473000000, goal
    )
    assert fresh_operation != apply.WBC0027_PREDECESSOR_BINDING["goal_operation_id"]
    assert fresh_operation != apply.operation_id(
        "orenvlad-ai/wb-core", 1130, 5473000001, goal
    )
    legacy_body = (
        "/wb-core apply-v2 pr 1126 merge "
        + MERGE_SHA
        + " deployed "
        + MERGE_SHA
        + " manifest sha256:"
        + apply.WBC0027_LEGACY_MANIFEST_SHA256
        + " operation wbc0027-product-capital-and-qualified-economics-v2"
    )
    try:
        apply.validate_legacy_authorization(
            {"author_association": "OWNER", "body": legacy_body},
            pr=1126,
            merge_sha=MERGE_SHA,
            deployed_sha=MERGE_SHA,
            manifest_sha=apply.WBC0027_LEGACY_MANIFEST_SHA256,
            operation="wbc0027-product-capital-and-qualified-economics-v2",
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("legacy WBC0027 exact-manifest authorization was reused")
    operation = "production-goal-v1-" + "6" * 32
    base = (
        "/opt/wb-core-runtime/state/backups/private-evidence/production-goals/"
        + operation
    )
    generation = {
        "generation_id": "opaque-c540-smoke",
        "manifest_sha256": "sha256:" + "8" * 64,
        "schema_revision": "operational_v1",
    }
    phase_ids = {
        "product": "recovery_" + "1" * 32,
        "economics": "recovery_" + "2" * 32,
    }

    def candidate(phase: str) -> dict[str, object]:
        manifest = (
            f"{base}/wbc0027-{phase}-plan-"
            f"20260831T120000123456Z-{'3' if phase == 'product' else '4' * 1}"
            "00000000000.json"
        )
        # Keep the generated suffix exactly twelve hexadecimal characters.
        manifest = manifest.replace("3000000000000", "333333333333").replace(
            "4000000000000", "444444444444"
        )
        size = 4_000_000 if phase == "product" else 2_000_000
        payload: dict[str, object] = {
            "status": "ready",
            "phase": phase,
            "profile": apply.WBC0027_GOAL_PROFILE,
            "target_id": "wb_core_eu_hosted_runtime_active",
            "goal_operation_id": operation,
            "deployed_sha": MERGE_SHA,
            "query_only": True,
            "database_written": False,
            "production_mutation_count": 0,
            "legacy_release_operation_reusable": False,
            "legacy_phase_operation_reusable": False,
            "manifest_path": manifest,
            "manifest_sha256": "sha256:" + ("3" if phase == "product" else "4") * 64,
            "material_qualification_digest": "sha256:"
            + ("5" if phase == "product" else "6") * 64,
            "phase_fingerprint": "sha256:"
            + ("7" if phase == "product" else "9") * 64,
            "phase_operation_id": phase_ids[phase],
            "storage_generation": generation,
            "plan_persistence": {
                "owner": "production_apply_evidence",
                "destination": manifest,
                "evidence_dir": base,
                "evidence_dir_mode": "0700",
                "file_mode": "0600",
                "parent_mode": "0700",
                "size_bytes": size,
                "max_size_bytes": 64_000_000,
                "bounded_size": True,
                "atomic_publish": True,
                "no_overwrite": True,
                "durable_file_fsync": True,
                "durable_directory_fsync": True,
                "no_create": False,
                "root_storage_admission": {
                    "owner": "production_apply_evidence",
                    "destination": manifest,
                    "destination_role": "backup",
                    "predicted_output_bytes": size,
                    "allowed": True,
                },
            },
        }
        if phase == "product":
            payload.update(
                {
                    "product_counts": {
                        "product_row_count": 1152,
                        "product_cell_count": 24192,
                        "product_mismatch_count": 9446,
                        "primary_row_count": 936,
                        "primary_cell_count": 19656,
                        "primary_mismatch_count": 7655,
                        "secondary_row_count": 216,
                        "secondary_mismatch_count": 1791,
                    },
                    "special_20260821": {
                        "as_of_date": "2026-08-21",
                        "nm_id": 497413772,
                        "cell_count": 16,
                    },
                    "hard_non_target": {"from_date": "2026-08-30"},
                    "evidence_blocked": [{"as_of_date": "2026-08-15"}],
                }
            )
        else:
            payload.update(
                {
                    "logical_repair_count": 298,
                    "persisted_repair_count": 472,
                    "evidence_blocked": [str(index) for index in range(12)],
                    "protected_invariant": {
                        "nm_id": 428853741,
                        "unit_cost_rub": "117.537167",
                    },
                    "product_phase_operation_id": phase_ids["product"],
                    "product_predecessor": {"reconciled": True},
                }
            )
        return payload

    common = {
        "return_code": 0,
        "transport_ambiguous": False,
        "command_sha256": "a" * 64,
        "stdout_sha256": "b" * 64,
        "stderr_sha256": "c" * 64,
    }

    def readback(phase: str) -> dict[str, object]:
        return {
            "status": "reconciled",
            "phase": phase,
            "profile": apply.WBC0027_GOAL_PROFILE,
            "target_id": "wb_core_eu_hosted_runtime_active",
            "goal_operation_id": operation,
            "phase_operation_id": phase_ids[phase],
            "phase_fingerprint": candidate(phase)["phase_fingerprint"],
            "deployed_sha": MERGE_SHA,
            "storage_generation": generation,
            "query_only": True,
            "database_written": False,
            "production_mutation_submit_count": 0,
            "recovery_lifecycle": "retained",
            "hard_non_target": {"all_exact": True},
            "product_exact": True,
            "economics_target_exact": phase == "economics",
            "functional_economics_missing": (
                {"2026-08-26": 12, "2026-08-29": 0}
                if phase == "economics"
                else {}
            ),
        }

    sequence = iter(
        [
            {**common, "result": candidate("product")},
            {**common, "result": candidate("product")},
            {
                **common,
                "result": {
                    "status": "applied",
                    "production_mutation_submit_count": 1,
                },
            },
            {**common, "result": readback("product")},
            {**common, "result": candidate("economics")},
            {**common, "result": candidate("economics")},
            {**common, "return_code": None, "transport_ambiguous": True},
            {**common, "result": readback("economics")},
        ]
    )
    original_command = apply.command_evidence
    original_sleep = apply.time.sleep
    calls = 0

    def fake_command(_command: list[str], *, timeout_seconds: float = 3600.0) -> dict:
        nonlocal calls
        calls += 1
        return next(sequence)

    try:
        apply.command_evidence = fake_command
        apply.time.sleep = lambda _seconds: None
        result = apply.run_wbc0027_goal(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference="github:fixture:sha256:" + "d" * 64,
        )
    finally:
        apply.command_evidence = original_command
        apply.time.sleep = original_sleep
    assert calls == 8
    assert result["state"] == "done"
    assert result["apply_count"] == 2
    assert result["product_submit_count"] == 1
    assert result["economics_submit_count"] == 1
    assert result["economics_state"] == "applied"
    assert all(
        rows[-2]["material_qualification_digest"]
        == rows[-1]["material_qualification_digest"]
        for rows in result["qualification_attempts"].values()
    )

    for malformed_revision in ("", " operational_v1", 172, None):
        malformed = candidate("product")
        malformed["storage_generation"] = {
            **generation,
            "schema_revision": malformed_revision,
        }
        invalid_sequence = iter([{**common, "result": malformed}])
        invalid_calls = 0

        def invalid_command(
            _command: list[str], *, timeout_seconds: float = 3600.0
        ) -> dict:
            nonlocal invalid_calls
            invalid_calls += 1
            return next(invalid_sequence)

        try:
            apply.command_evidence = invalid_command
            apply.time.sleep = lambda _seconds: None
            invalid = apply.run_wbc0027_goal(
                target={
                    "target_dir": "/opt/wb-core-runtime/app",
                    "ssh_destination": "wb-core-eu-root",
                },
                merge_sha=MERGE_SHA,
                goal=goal,
                operation=operation,
                approval_reference="github:fixture:sha256:" + "d" * 64,
            )
        finally:
            apply.command_evidence = original_command
            apply.time.sleep = original_sleep
        assert invalid_calls == 1
        assert invalid["state"] == "blocked"
        assert invalid["apply_count"] == 0
        assert invalid["product_state"] == "not_applied"

    drifting_sequence = []
    for index, revision in enumerate(
        (
            "operational_v1",
            "operational_v2",
            "operational_v3",
            "operational_v4",
        ),
        start=1,
    ):
        drifting = candidate("product")
        drifting["storage_generation"] = {
            **generation,
            "schema_revision": revision,
        }
        drifting["phase_fingerprint"] = "sha256:" + str(index) * 64
        drifting["phase_operation_id"] = "recovery_" + str(index) * 32
        drifting_sequence.append({**common, "result": drifting})
    drifting_results = iter(drifting_sequence)
    drift_calls = 0

    def drifting_command(
        _command: list[str], *, timeout_seconds: float = 3600.0
    ) -> dict:
        nonlocal drift_calls
        drift_calls += 1
        return next(drifting_results)

    try:
        apply.command_evidence = drifting_command
        apply.time.sleep = lambda _seconds: None
        drifted = apply.run_wbc0027_goal(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference="github:fixture:sha256:" + "d" * 64,
        )
    finally:
        apply.command_evidence = original_command
        apply.time.sleep = original_sleep
    assert drift_calls == apply.MAX_QUALIFICATION_CANDIDATES
    assert drifted["state"] == "blocked"
    assert drifted["apply_count"] == 0
    assert drifted["product_state"] == "not_applied"


def _exercise_wbc0027_fbs_quality_runner() -> None:
    storage = {
        **FBS_PASSPORT["storage"],
        "generation_epoch": "generation-smoke",
        "state": "ready",
        "manifest_contract": "storage_registry_v1",
    }
    boundary = {
        "storage": storage,
        "cutover_id": FBS_PASSPORT["cutover"]["cutover_id"],
        "cutover_manifest_digest": "sha256:" + "1" * 64,
        "forward_generation_id": FBS_PASSPORT["cutover"]["forward_generation_id"],
        "forward_generation_manifest_digest": "sha256:" + "2" * 64,
        "forward_cursor_sequence": 28_460_000,
        "source_cursor_max": 28_461_627,
        "mapping_readback_digest": FBS_MAPPING_READBACK_DIGEST,
    }
    scope = {
        "groups": [{"facility_id": "facility-smoke", "nm_id": 1001}],
        "business_dates": ["2026-08-31"],
        "target_count": 1,
        "target_sequences": [1],
        "target_row_digests": ["sha256:" + "3" * 64],
        "stable_target_digest": "sha256:" + "4" * 64,
        "target_rows": [{}],
        "location_wac_evidence": [],
        "resolved_scopes": [],
        "mapping_re_evidence": [],
        "typed_blocker_rows": [],
        "coverage": {
            "candidate_count": 1,
            "resolved_groups": [{"facility_id": "facility-smoke", "nm_id": 1001}],
            "blocked_groups": [],
            "covered_groups": [{"facility_id": "facility-smoke", "nm_id": 1001}],
            "classified_count": 1,
            "full_unresolved_scan": True,
            "all_groups_resolvable": True,
        },
    }
    history = {
        "contract": "fbs_lifecycle_same_date_history_recovery/v2",
        "date_from": "2026-08-31",
        "date_to": "2026-08-31",
        "business_dates": ["2026-08-31"],
        "captures": [{}],
        "event_evidence": [],
        "corrections": [],
        "cell_evidence": [],
        "current_business_date": "2026-08-31",
        "classification_counts": {
            "recoverable_exact": 1,
            "remain_missing_no_same_date_evidence": 4,
        },
        "blockers": [],
        "evidence_digest": "sha256:" + "b" * 64,
        "digest": "sha256:" + "5" * 64,
    }
    surface_rows = [{"scope_kind": "FACILITY_SKU", "facility_id": "facility-smoke", "nm_id": 1001}]
    candidate = attach_digest(
        {
            "contract": "fbs_lifecycle_recovery_manifest/v2",
            "operation_id": FBS_PASSPORT["operation_id"] + ":recovery",
            "target": {
                "target_id": "wb_core_eu_hosted_runtime_active",
                "runtime_sha": MERGE_SHA,
            },
            "impact_digest": FBS_IMPACT_DIGEST,
            "boundary": boundary,
            "scope": scope,
            "predicted_effects": {
                "target_count": 1,
                "outcome_counts": {},
                "lifecycle_summary": {},
                "balance_deltas": [],
                "total_quantity_delta": -1,
                "total_capital_delta_rub": "-1",
                "target_result_digest": "sha256:" + "c" * 64,
                "dependent_surface_plan": {
                    "contract": "fbs_lifecycle_dependent_surface_plan/v1",
                    "surface_kinds": ["FACILITY_SKU", "FACILITY_TOTAL", "FUNCTIONAL_ECONOMICS", "GLOBAL_SKU", "GLOBAL_TOTAL"],
                    "before": surface_rows,
                    "after": surface_rows,
                    "before_digest": digest(surface_rows),
                    "after_digest": digest(surface_rows),
                },
                "wb_write_count": 0,
            },
            "history": history,
            "baselines": {
                "non_target_digest": "sha256:" + "d" * 64,
                "wb_digest": "sha256:" + "e" * 64,
                "projection_schema_evidence": {},
                "canonical_write_seeds": {},
                "past_fulfilled_invariant": {},
            },
            "safety": {
                "default_mode": "query_only_dry_run",
                "one_submit": True,
                "writer_lock": "warehouse_functional_write_lock",
                "root_storage_admission": "production_apply_evidence",
                "target_cas": "exact_source_rows_history_base_and_effect",
                "before_image": "private_mode_0600_exclusive_create_fsync",
                "backup": "private_mode_0600_exclusive_create_fsync",
                "operation_journal": "exact_operation_authorization_storage",
                "ambiguous_transport": "query_only_readback_no_retry",
                "current_retrocopy": False,
                "immutable_history_overwrite": False,
                "wb_writes": 0,
                "mapping_writes": 0,
                "hypothetical_mapping": False,
            },
            "apply_allowed": True,
            "blockers": [],
        },
        "recovery_digest",
    )
    auth_body = WBC0027_FBS_QUALITY_AUTH_BODY.replace(
        FBS_RECOVERY_DIGEST, candidate["recovery_digest"]
    )
    goal = apply.validate_authorization(
        authorization(body=auth_body),
        repository="orenvlad-ai/wb-core",
        pr=1050,
    )
    assert goal["profile"] == apply.WBC0027_FBS_QUALITY_GOAL_PROFILE
    assert goal["impact_digest"] == FBS_IMPACT_DIGEST
    assert goal["max_mutation_submits"] == 1
    changed = auth_body.replace(FBS_IMPACT_DIGEST, "sha256:" + "0" * 64)
    try:
        apply.validate_authorization(
            authorization(body=changed), repository="orenvlad-ai/wb-core", pr=1050
        )
    except apply.ApplyError:
        raise AssertionError("versioned recovery digest grammar was rejected")

    operation = "production-goal-v2-" + "5" * 32
    command_results = iter(
        [
            {"return_code": 0, "transport_ambiguous": False, "result": candidate},
            {"return_code": 0, "transport_ambiguous": False, "result": candidate},
            {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {
                    "state": "qualified_no_submit",
                    "fingerprint": candidate["recovery_digest"],
                    "submit_count": 0,
                    "mapping_write_count": 0,
                    "recovery_write_count": 0,
                    "history_write_count": 0,
                    "wb_write_count": 0,
                },
            },
            {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {"status": "completed"},
            },
            {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {
                    "status": "completed",
                    "query_only": True,
                    "mutates_wb": False,
                    "deployed_sha": MERGE_SHA,
                    "manifest_fingerprint": candidate["recovery_digest"],
                    "summary": {
                        "operation_id": operation,
                        "authorization_reference_digest": digest(
                            "github:fixture:sha256:" + "1" * 64
                        ),
                    },
                    "source_cutoff_sequence": 28_461_627,
                    "date_from": "2026-08-31",
                    "date_to": "2026-08-31",
                    "target_count": 1,
                    "target_readback_count": 1,
                    "history_capture_count": 1,
                    "history_readback_count": 1,
                },
            },
        ]
    )
    original_command = apply.command_evidence
    original_sleep = apply.time.sleep
    calls: list[list[str]] = []

    def fake_command(command: list[str], *, timeout_seconds: float = 3600.0) -> dict:
        del timeout_seconds
        calls.append(command)
        return next(command_results)

    try:
        apply.command_evidence = fake_command
        apply.time.sleep = lambda _seconds: None
        result = apply.run_wbc0027_fbs_quality_goal(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference="github:fixture:sha256:" + "1" * 64,
        )
    finally:
        apply.command_evidence = original_command
        apply.time.sleep = original_sleep
    assert result["state"] == "done"
    assert result["apply_count"] == 1
    assert len(calls) == 5
    rendered = [" ".join(command) for command in calls]
    assert sum(" apply " in value for value in rendered) == 1
    assert sum(" readback " in value for value in rendered) == 1
    no_submit_results = iter(
        [
            {"return_code": 0, "transport_ambiguous": False, "result": candidate},
            {"return_code": 0, "transport_ambiguous": False, "result": candidate},
            {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {
                    "state": "qualified_no_submit",
                    "fingerprint": candidate["recovery_digest"],
                    "submit_count": 0,
                    "mapping_write_count": 0,
                    "recovery_write_count": 0,
                    "history_write_count": 0,
                    "wb_write_count": 0,
                },
            },
        ]
    )
    no_submit_calls: list[list[str]] = []

    def fake_no_submit(command: list[str], *, timeout_seconds: float = 3600.0) -> dict:
        del timeout_seconds
        no_submit_calls.append(command)
        return next(no_submit_results)

    try:
        apply.command_evidence = fake_no_submit
        apply.time.sleep = lambda _seconds: None
        qualified = apply.run_wbc0027_fbs_quality_goal(
            target={"target_dir": "/opt/wb-core-runtime/app", "ssh_destination": "wb-core-eu-root"},
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference="github:fixture:sha256:" + "1" * 64,
            qualification_only=True,
        )
    finally:
        apply.command_evidence = original_command
        apply.time.sleep = original_sleep
    assert qualified["state"] == "qualified_no_submit"
    assert qualified["apply_count"] == 0
    assert len(no_submit_calls) == 3
    assert all(" apply " not in " ".join(command) for command in no_submit_calls)


def _exercise_wbc0027_fbs_mapping_runner() -> None:
    goal = apply.validate_authorization(
        authorization(body=WBC0027_FBS_MAPPING_AUTH_BODY),
        repository="orenvlad-ai/wb-core",
        pr=1050,
    )
    assert goal["profile"] == apply.WBC0027_FBS_MAPPING_GOAL_PROFILE
    assert goal["incident_operation_id"] == FBS_PASSPORT["operation_id"]
    first = authorization(id=501, body=WBC0027_FBS_MAPPING_AUTH_BODY)
    duplicate = authorization(id=502, body=WBC0027_FBS_MAPPING_AUTH_BODY)
    try:
        apply.validate_unique_authorization(
            [first, duplicate],
            comment_id=501,
            repository="orenvlad-ai/wb-core",
            pr=1050,
        )
    except apply.ApplyError as exc:
        assert "not unique" in str(exc)
    else:
        raise AssertionError("duplicate equivalent OWNER passports were accepted")
    changed = WBC0027_FBS_MAPPING_AUTH_BODY.replace(
        "inserts 1", "inserts 2"
    )
    try:
        apply.validate_authorization(
            authorization(body=changed), repository="orenvlad-ai/wb-core", pr=1050
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("drifted WBC0027 FBS mapping count was accepted")

    operation = FBS_MAPPING_OPERATION
    material_cas = {
        "tuple_digest": FBS_PASSPORT["tuple"]["tuple_digest"],
        "mapping_digest": "sha256:" + "7" * 64,
        "target_digest": "sha256:" + "2" * 64,
        "storage_digest": "sha256:" + "6" * 64,
        "cutover_digest": "sha256:" + "5" * 64,
        "identity_digest": "sha256:" + "4" * 64,
        "evidence_digest": "sha256:" + "3" * 64,
    }
    material_cas["digest"] = digest(material_cas)
    proposed_mapping = {
        "mapping_id": "mapping-smoke",
        "mapping_digest": "sha256:" + "7" * 64,
    }
    candidate = attach_digest({
        "contract": "fbs_identity_mapping_manifest/v2",
        "operation_id": FBS_PASSPORT["operation_id"],
        "target": {
            "target_id": "wb_core_eu_hosted_runtime_active",
            "runtime_sha": MERGE_SHA,
            "source_runtime_sha": FBS_PASSPORT["target"]["source_runtime_sha"],
        },
        "storage": FBS_PASSPORT["storage"],
        "cutover": {
            "cutover_id": FBS_PASSPORT["cutover"]["cutover_id"],
            "cutover_manifest_digest": "sha256:" + "1" * 64,
            "forward_generation_id": FBS_PASSPORT["cutover"]["forward_generation_id"],
            "forward_generation_manifest_digest": "sha256:" + "2" * 64,
        },
        "tuple": FBS_PASSPORT["tuple"],
        "evidence": {
            "external_identity_digest": FBS_PASSPORT["evidence"]["external_identity_digest"],
            "owner_digest": "sha256:" + "3" * 64,
            "warehouse_evidence_digest": "sha256:" + "4" * 64,
            "facility_admission_digest": "sha256:" + "5" * 64,
        },
        "expectation": FBS_PASSPORT["mapping_expectation"],
        "proposed_mapping": proposed_mapping,
        "material_cas": material_cas,
        "safety": {
            "default_mode": "query_only_dry_run",
            "two_consecutive_material_witnesses_required": True,
            "writer_lock": "warehouse_functional_write_lock",
            "root_storage_admission": "production_apply_evidence",
            "private_before_image": "mode_0600_exclusive_create_fsync",
            "private_backup": "mode_0600_exclusive_create_fsync",
            "operation_journal": "exact_operation_authorization_storage",
            "one_submit": True,
            "one_insert_max": 1,
            "blind_retry": False,
            "query_only_readback": True,
            "lifecycle_debit_count": 0,
            "balance_write_count": 0,
            "history_write_count": 0,
            "public_write_count": 0,
            "outbox_write_count": 0,
            "wb_write_count": 0,
        },
        "apply_allowed": True,
        "blockers": [],
    }, "manifest_digest")
    command_results = iter(
        [
            {"return_code": 0, "transport_ambiguous": False, "result": candidate},
            {"return_code": 0, "transport_ambiguous": False, "result": candidate},
            {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {
                    "accepted": True,
                    "mapping_insert_count": 0,
                    "recovery_write_count": 0,
                    "history_write_count": 0,
                    "source_database_query_only": True,
                },
            },
            {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {"status": "completed", "mapping_insert_count": 1},
            },
            {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {
                    "status": "completed",
                    "query_only": True,
                    "target_id": "wb_core_eu_hosted_runtime_active",
                    "deployed_sha": MERGE_SHA,
                    "operation_id": operation,
                    "operation_proof_exact": True,
                    "exact_mapping_row_count": 1,
                    "mapping": {
                        "target_nm_id": FBS_PASSPORT["tuple"]["target_nm_id"],
                        "mapping_digest": proposed_mapping["mapping_digest"],
                    },
                    "readback_digest": "sha256:" + "8" * 64,
                    "mapping_insert_count": 0,
                    "recovery_write_count": 0,
                    "history_write_count": 0,
                    "wb_write_count": 0,
                },
            },
        ]
    )
    original_command = apply.command_evidence
    original_sleep = apply.time.sleep
    calls: list[list[str]] = []

    def fake_command(command: list[str], *, timeout_seconds: float = 3600.0) -> dict:
        del timeout_seconds
        calls.append(command)
        return next(command_results)

    try:
        apply.command_evidence = fake_command
        apply.time.sleep = lambda _seconds: None
        result = apply.run_wbc0027_fbs_mapping_goal(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference="github:fixture:sha256:" + "2" * 64,
        )
    finally:
        apply.command_evidence = original_command
        apply.time.sleep = original_sleep
    assert result["state"] == "done"
    assert result["apply_count"] == 1
    assert len(calls) == 5
    rendered = [" ".join(command) for command in calls]
    assert sum(" mapping-apply " in value for value in rendered) == 1
    assert sum(" mapping-readback" in value for value in rendered) == 1
    no_submit_results = iter(
        [
            {"return_code": 0, "transport_ambiguous": False, "result": candidate},
            {"return_code": 0, "transport_ambiguous": False, "result": candidate},
            {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {
                    "accepted": True,
                    "mapping_insert_count": 0,
                    "recovery_write_count": 0,
                    "history_write_count": 0,
                    "source_database_query_only": True,
                },
            },
        ]
    )
    no_submit_calls: list[list[str]] = []

    def fake_no_submit(command: list[str], *, timeout_seconds: float = 3600.0) -> dict:
        del timeout_seconds
        no_submit_calls.append(command)
        return next(no_submit_results)

    try:
        apply.command_evidence = fake_no_submit
        apply.time.sleep = lambda _seconds: None
        qualified = apply.run_wbc0027_fbs_mapping_goal(
            target={"target_dir": "/opt/wb-core-runtime/app", "ssh_destination": "wb-core-eu-root"},
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference="github:fixture:sha256:" + "2" * 64,
            qualification_only=True,
        )
    finally:
        apply.command_evidence = original_command
        apply.time.sleep = original_sleep
    assert qualified["state"] == "qualified_no_submit"
    assert qualified["apply_count"] == 0
    assert len(no_submit_calls) == 3
    assert all("mapping-apply" not in " ".join(command) for command in no_submit_calls)


def _exercise_wbc0013_two_phase_runner() -> None:
    goal = apply.validate_authorization(
        authorization(body=WBC0013_AUTH_BODY),
        repository="orenvlad-ai/wb-core",
        pr=1050,
    )
    assert goal["max_a_submits"] == goal["max_b_submits"] == 1
    operation = "production-goal-v1-" + "7" * 32
    base = (
        "/opt/wb-core-runtime/state/backups/private-evidence/production-goals/"
        + operation
    )
    common = {
        "return_code": 0,
        "transport_ambiguous": False,
        "command_sha256": "c" * 64,
        "stdout_sha256": "d" * 64,
        "stderr_sha256": "e" * 64,
    }

    def candidate(phase: str) -> dict[str, object]:
        manifest_path = f"{base}/wbc0013-{phase}-plan-20260828T120000Z.json"
        manifest_size = 108_853 if phase == "a" else 256_000
        payload: dict[str, object] = {
            "status": "ready",
            "phase": phase,
            "deployed_sha": MERGE_SHA,
            "query_only": True,
            "database_written": False,
            "manifest_path": manifest_path,
            "manifest_sha256": "sha256:" + phase * 64,
            "material_qualification_digest": "sha256:"
            + ("1" if phase == "a" else "2") * 64,
            "file_mode": "0600",
            "barrier_inactive": True,
            "target_generation_bound": True,
            "timer_change_count": 0,
            "plan_persistence": {
                "owner": "production_apply_evidence",
                "destination": manifest_path,
                "evidence_dir": base,
                "evidence_dir_mode": "0700",
                "file_mode": "0600",
                "parent_mode": "0700",
                "size_bytes": manifest_size,
                "max_size_bytes": 12_000_000,
                "bounded_size": True,
                "atomic_publish": True,
                "no_overwrite": True,
                "durable_file_fsync": True,
                "durable_directory_fsync": True,
                "root_storage_admission": {
                    "owner": "production_apply_evidence",
                    "destination": manifest_path,
                    "destination_role": "backup",
                    "predicted_output_bytes": manifest_size,
                    "allowed": True,
                },
            },
        }
        if phase == "a":
            payload.update(
                {
                    "roster_count": 71,
                    "existing_count": 21,
                    "owner_approved_missing_count": 50,
                    "original_identity_count": 12,
                    "wb_content_identity_count": 38,
                    "zero_insert_count": 50,
                }
            )
        else:
            payload.update(
                {
                    "historical_repair_count": 1,
                    "business_date": "2026-08-26",
                    "nm_id": 428853741,
                    "accepted_version_id": "whfv_cb0657c384d5adebae01e585",
                    "event_id": "ffbf_87cea959c9d600da99caa1ab68ef",
                    "exact_target_count": 1,
                    "broad_mismatch_query_performed": False,
                    "ready_shape_candidate_count": 1,
                    "ready_shape_candidate_digest": "sha256:" + "6" * 64,
                    "causal_event_count": 1,
                    "causal_event_candidate_digest": "sha256:" + "7" * 64,
                    "selection_predicate": (
                        "historical_b.exact_causal_handoff_debit_event"
                    ),
                    "selection_details_digest": "sha256:" + "3" * 64,
                    "mismatch_classification_digest": "sha256:" + "8" * 64,
                    "current_active_preserved": True,
                    "current_sync_preserved": True,
                    "current_pool_preserved": True,
                }
            )
        return payload

    # A release interruption before either durable phase is allowed to supersede
    # the candidate, but the old-SHA candidate can never cross the submit boundary.
    for phase in ("a", "b"):
        interrupted = {**candidate(phase), "deployed_sha": "f" * 40}
        try:
            apply._validate_wbc0013_candidate(
                interrupted,
                goal,
                phase=phase,
                merge_sha=MERGE_SHA,
            )
        except apply.ApplyError:
            pass
        else:
            raise AssertionError(f"WBC0013 {phase} release interruption must fail closed")

    # The adapter has no default execution mode, and every apply command requires
    # one exact private reviewed manifest plus its digest and approval reference.
    plan_command = apply._wbc0013_remote_command(
        target={
            "target_dir": "/opt/wb-core-runtime/app",
            "ssh_destination": "wb-core-eu-root",
        },
        merge_sha=MERGE_SHA,
        operation=operation,
        evidence_dir=base,
        phase="plan-a",
    )
    assert "apply-a" not in plan_command[-1]
    assert "export PYTHONPATH=/opt/wb-core-runtime/app" in plan_command[-1]
    try:
        apply._wbc0013_remote_command(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            operation=operation,
            evidence_dir=base,
            phase="apply-a",
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("WBC0013 unbound apply command must remain inert")

    sequence = iter(
        [
            {
                **common,
                "result": {
                    **candidate("a"),
                    "material_qualification_digest": "sha256:" + "0" * 64,
                },
            },
            {**common, "result": candidate("a")},
            {**common, "result": candidate("a")},
            {**common, "return_code": None, "transport_ambiguous": True},
            {
                **common,
                "result": {
                    "status": "reconciled",
                    "query_only": True,
                    "roster_count": 71,
                    "covered_roster_count": 71,
                    "zero_row_count": 50,
                    "new_explicit_zero_count": 50,
                    "document_count": 1,
                    "absolute_target_line_count": 50,
                    "movement_line_count": 0,
                    "forward_t0": "2026-08-28T12:00:00+00:00",
                    "history_write_count": 0,
                    "non_target_preserved": True,
                },
            },
            {**common, "result": candidate("b")},
            {**common, "result": candidate("b")},
            {**common, "return_code": None, "transport_ambiguous": True},
            {
                **common,
                "result": {
                    "status": "reconciled",
                    "query_only": True,
                    "historical_repair_count": 1,
                    "current_active_preserved": True,
                    "current_sync_preserved": True,
                    "current_pool_preserved": True,
                    "a_forward_zeros_preserved": True,
                    "ready_target_total_closed": True,
                    "non_target_preserved": True,
                    "historical_quantity": "1952",
                    "historical_cost_covered_quantity": "1952",
                    "historical_location_count": 3,
                    "historical_location_digest": "sha256:" + "4" * 64,
                    "historical_provenance_digest": "sha256:" + "5" * 64,
                    "target_own_cost_available": True,
                    "six_total_dependencies_available": True,
                },
            },
        ]
    )
    original_command = apply.command_evidence
    original_sleep = apply.time.sleep
    apply.command_evidence = lambda *_args, **_kwargs: next(sequence)
    apply.time.sleep = lambda *_args, **_kwargs: None
    try:
        result = apply.run_wbc0013_goal(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference="github:fixture",
        )
    finally:
        apply.command_evidence = original_command
        apply.time.sleep = original_sleep
    assert result["state"] == "done"
    assert result["apply_count"] == 2
    assert result["a_submit_count"] == result["b_submit_count"] == 1
    assert [
        item["qualification_state"] for item in result["qualification_attempts"]["a"]
    ] == ["superseded_material_drift", "matching_witness", "qualified"]
    assert [
        item["qualification_state"] for item in result["qualification_attempts"]["b"]
    ] == ["matching_witness", "qualified"]
    assert result["qualification_attempts"]["b"][-1][
        "ready_shape_candidate_count"
    ] == 1
    assert result["qualification_attempts"]["b"][-1]["causal_event_count"] == 1

    typed_error = {
        **common,
        "return_code": 2,
        "result": {
            "status": "error",
            "phase": "a",
            "stage": "qualification",
            "code": "root_storage_admission_unavailable",
            "message": (
                "RootStoragePolicyError: unregistered large root writer owner: "
                "production_apply_evidence"
            ),
            "predicate": "wbc0013.a.private_plan_persisted",
            "expected_cardinality": 1,
            "observed_cardinality": 0,
            "candidate_digest": "sha256:" + "4" * 64,
            "details_digest": "sha256:" + "9" * 64,
        },
    }
    sequence = iter([typed_error])
    apply.command_evidence = lambda *_args, **_kwargs: next(sequence)
    try:
        blocked = apply.run_wbc0013_goal(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            goal=goal,
            operation=operation,
            approval_reference="github:fixture",
        )
    finally:
        apply.command_evidence = original_command
    assert blocked["state"] == "blocked" and blocked["apply_count"] == 0
    assert blocked["failure"] == {
        "phase": "a",
        "stage": "qualification",
        "code": "root_storage_admission_unavailable",
        "message": (
            "RootStoragePolicyError: unregistered large root writer owner: "
            "production_apply_evidence"
        ),
        "predicate": "wbc0013.a.private_plan_persisted",
        "expected_cardinality": 1,
        "observed_cardinality": 0,
        "candidate_digest": "sha256:" + "4" * 64,
        "details_digest": "sha256:" + "9" * 64,
    }


def authorization(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": AUTHORIZATION_COMMENT_ID,
        "created_at": "2026-08-27T12:01:00Z",
        "author_association": "OWNER",
        "issue_url": "https://api.github.com/repos/orenvlad-ai/wb-core/issues/1050",
        "body": AUTH_BODY,
    }
    value.update(updates)
    return value


def release_comment() -> dict[str, object]:
    payload = {
        "state": "done",
        "operation_id": "release-v2-test",
        "pull_request": 1050,
        "merge_sha": MERGE_SHA,
        "deployed_sha": MERGE_SHA,
        "release_kind": "live_runtime",
    }
    return {
        "user": {"login": "github-actions[bot]"},
        "body": "<!-- wb-core-release-receipt operation=release-v2-test -->\n```json\n"
        + json.dumps(payload)
        + "\n```",
    }


def warm_mount_probe_comment() -> dict[str, object]:
    job_id = apply.warm_mount_probe_job_id(
        "orenvlad-ai/wb-core", 1050, "release-v2-test", MERGE_SHA
    )
    payload = {
        "schema": apply.WARM_MOUNT_PROBE_RECEIPT_SCHEMA,
        "state": "observed",
        "query_only": True,
        "database_written": False,
        "production_probe_count": 1,
        "job_id": job_id,
        "repository": "orenvlad-ai/wb-core",
        "pull_request": 1050,
        "release_operation_id": "release-v2-test",
        "merge_sha": MERGE_SHA,
        "deployed_sha": MERGE_SHA,
        "evidence_digest": "sha256:" + "e" * 64,
        "worker": {
            "unit_template": "wb-core-storage-recovery-sanitation@.service",
            "unit_instance": f"wb-core-storage-recovery-sanitation@{job_id}.service",
            "repo_template_sha256": "sha256:" + "f" * 64,
            "installed_template_path": (
                "/etc/systemd/system/wb-core-storage-recovery-sanitation@.service"
            ),
            "installed_template_sha256": "sha256:" + "f" * 64,
            "installed_template_matches_repo": True,
            "mount_namespace": {"link_target": "mnt:[4026532999]"},
        },
        "paths": [
            {
                "filesystem_role": role,
                "target": {"canonical_path": path},
                "semantic_identity_digest": "sha256:" + token * 64,
                "raw_candidate_count": 2 if role == "backup" else 1,
                "raw_candidates_digest": "sha256:" + token * 64,
            }
            for role, path, token in (
                ("root", "/opt/wb-core-runtime/backups", "1"),
                ("backup", "/opt/wb-core-runtime/state/backups", "2"),
                ("generation", "/opt/wb-core-runtime/state/generations", "3"),
            )
        ],
        "artifact": {
            "name": "root-warm-archive-mount-probe-pr-1050-run-123456",
            "file": "root-warm-archive-mount-probe-receipt.json",
            "sha256": "sha256:" + "4" * 64,
            "size_bytes": 1234,
        },
    }
    return {
        "id": MOUNT_PROBE_COMMENT_ID,
        "created_at": "2026-08-27T12:00:00Z",
        "user": {"login": "github-actions[bot]"},
        "body": apply.warm_mount_probe_marker(job_id)
        + "\n```json\n"
        + json.dumps(payload)
        + "\n```",
    }


def recovery_release_comment() -> dict[str, object]:
    payload = {
        "state": "done",
        "operation_id": RECOVERY_RELEASE_OPERATION,
        "pull_request": 1050,
        "merge_sha": MERGE_SHA,
        "deployed_sha": MERGE_SHA,
        "release_kind": "live_runtime",
    }
    return {
        "id": 10,
        "user": {"login": "github-actions[bot]"},
        "body": (
            f"<!-- wb-core-release-receipt operation={RECOVERY_RELEASE_OPERATION} -->"
            "\n```json\n" + json.dumps(payload) + "\n```"
        ),
    }


def dry_payload(material: str = "b") -> dict[str, object]:
    return {
        "status": "ready",
        "deployed_sha": MERGE_SHA,
        "date_from": "2026-03-01",
        "date_to": "2026-08-24",
        "date_count": 177,
        "inserted_capture_count": 177,
        "inserted_component_count": 18054,
        "inserted_finalization_count": 177,
        "full_date_count": 172,
        "partial_date_count": 5,
        "unavailable_date_count": 0,
        "manifest_path": (
            "/opt/wb-core-runtime/state/backups/private-evidence/production-goals/"
            "op/inventory-history-backfill-plan-20260825T140615Z.json"
        ),
        "manifest_sha256": "sha256:" + "c" * 64,
        "material_qualification_digest": "sha256:" + material * 64,
        "source_watermarks_digest": "sha256:" + "d" * 64,
        "target_history_digest": "sha256:" + "e" * 64,
    }


def readback_payload() -> dict[str, object]:
    return {
        "status": "reconciled",
        "query_only": True,
        "inserted_capture_count": 177,
        "inserted_component_count": 18054,
        "inserted_finalization_count": 177,
        "visible_history_date_count": 177,
        "visible_history_quality": {"full": 172, "partial": 5, "unavailable": 0},
        "exact_manifest_apply_receipt_count": 1,
        "total_inventory_history_apply_receipt_count": 1,
        "non_target_preserved": True,
    }


def recovery_receipt() -> dict[str, object]:
    goal = apply.validate_authorization(
        authorization(), repository="orenvlad-ai/wb-core", pr=1050
    )
    operation = apply.operation_id(
        "orenvlad-ai/wb-core",
        1050,
        AUTHORIZATION_COMMENT_ID,
        goal,
    )
    manifest_sha = "sha256:" + "c" * 64
    recovered_readback = {
        **readback_payload(),
        "mode": "query-only-readback",
        "database_written": False,
        "deployed_sha": MERGE_SHA,
        "manifest_sha256": manifest_sha,
    }
    return {
        "schema": apply.APPLY_RECEIPT_SCHEMA,
        "state": "done",
        "operation_id": operation,
        "repository": "orenvlad-ai/wb-core",
        "pull_request": 1050,
        "release_operation_id": RECOVERY_RELEASE_OPERATION,
        "merge_sha": MERGE_SHA,
        "deployed_sha": MERGE_SHA,
        "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
        "authorization_body_sha256": apply.digest(AUTH_BODY.encode("utf-8")),
        "goal": goal,
        "apply_count": 1,
        "evidence": {
            "state": "done",
            "reason": "reconciled",
            "apply_count": 1,
            "qualified_manifest": {"sha256": manifest_sha},
            "apply": {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {
                    "status": "reconciled",
                    "database_written": True,
                    "manifest_sha256": manifest_sha,
                    "non_target_preserved": True,
                },
            },
            "readback": {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": recovered_readback,
            },
        },
    }


def receipt_zip(receipt: dict[str, object]) -> tuple[bytes, str]:
    raw = apply.canonical_json_bytes(receipt) + b"\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(apply.RECOVERY_ARTIFACT_FILE, raw)
    return buffer.getvalue(), apply.digest(raw)


class RecoveryClient:
    repository = "orenvlad-ai/wb-core"

    def __init__(self, receipt: dict[str, object]) -> None:
        self.raw_zip, self.receipt_sha256 = receipt_zip(receipt)
        self.comments: list[dict[str, object]] = [recovery_release_comment()]
        self.post_count = 0
        self.run_updates: dict[str, object] = {}

    def get(self, path: str) -> object:
        if path == f"/actions/runs/{RECOVERY_RUN_ID}":
            run: dict[str, object] = {
                "id": RECOVERY_RUN_ID,
                "name": apply.RECOVERY_WORKFLOW_NAME,
                "path": apply.RECOVERY_WORKFLOW_PATH,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "failure",
                "head_branch": "main",
                "head_sha": MERGE_SHA,
                "repository": {"full_name": self.repository},
            }
            run.update(self.run_updates)
            return run
        if path.startswith(f"/actions/runs/{RECOVERY_RUN_ID}/artifacts?"):
            return {
                "artifacts": [
                    {
                        "id": 55,
                        "name": apply._recovery_artifact_name(1050, RECOVERY_RUN_ID),
                        "expired": False,
                        "size_in_bytes": len(self.raw_zip),
                        "workflow_run": {
                            "id": RECOVERY_RUN_ID,
                            "head_branch": "main",
                            "head_sha": MERGE_SHA,
                        },
                    }
                ]
            }
        if path == f"/issues/comments/{AUTHORIZATION_COMMENT_ID}":
            return authorization()
        if path.startswith("/issues/1050/comments?"):
            return list(self.comments)
        raise AssertionError(f"unexpected recovery GET: {path}")

    def request(self, method: str, path: str, **kwargs: object) -> object:
        assert method == "GET"
        assert path == "/actions/artifacts/55/zip"
        assert kwargs.get("raw") is True
        return self.raw_zip

    def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        assert path == "/issues/1050/comments"
        self.post_count += 1
        comment = {
            "id": 99,
            "user": {"login": "github-actions[bot]"},
            "body": body["body"],
        }
        self.comments.append(comment)
        return comment


def _run_dynamic_sequence(
    sequence: list[dict[str, object]],
    *,
    authorization_comment: dict[str, object] | None = None,
) -> dict[str, object]:
    original = apply.command_evidence
    original_sleep = apply.time.sleep

    def fake(
        _command: list[str], *, timeout_seconds: float = 3600.0
    ) -> dict[str, object]:
        del timeout_seconds
        assert sequence
        return sequence.pop(0)

    apply.command_evidence = fake
    apply.time.sleep = lambda _seconds: None
    try:
        goal = apply.validate_authorization(
            authorization_comment or authorization(),
            repository="orenvlad-ai/wb-core",
            pr=1050,
        )
        readiness = (
            {
                "readiness_id": "readiness-v2-" + "6" * 32 + "-a01",
                "projection_manifest_path": (
                    "/opt/wb-core-runtime/state/private-evidence/"
                    "root-warm-archive-readiness/readiness-v2-"
                    + "6" * 32
                    + "-a01"
                    + "/root-warm-archive-readiness-projection-20260826T120000Z.json"
                ),
                "projection_manifest_sha256": "sha256:" + "5" * 64,
                "material_qualification_digest": "sha256:" + "8" * 64,
                "immutable_non_target_digest": "sha256:" + "7" * 64,
                "mutable_canonical_topology_digest": "sha256:" + "6" * 64,
                "material_partition": "immutable_safety_v1",
                "mutable_safety_predicates": {"passed": True},
            }
            if goal["profile"] == apply.WARM_ARCHIVE_GOAL_PROFILE
            else None
        )
        return apply.run_dynamic_goal(
            target={
                "target_dir": "/opt/wb-core-runtime/app",
                "ssh_destination": "wb-core-eu-root",
            },
            merge_sha=MERGE_SHA,
            goal=goal,
            operation="op",
            approval_reference="github:scope-authorization",
            warm_readiness=readiness,
        )
    finally:
        apply.command_evidence = original
        apply.time.sleep = original_sleep


def _exercise_compact_oversized_blocked_receipt() -> None:
    operation = "production-goal-v1-" + "9" * 32
    receipt = {
        "schema": apply.APPLY_RECEIPT_SCHEMA,
        "state": "blocked",
        "operation_id": operation,
        "repository": "orenvlad-ai/wb-core",
        "pull_request": 1050,
        "release_operation_id": "release-v2-test",
        "merge_sha": MERGE_SHA,
        "deployed_sha": MERGE_SHA,
        "apply_count": 0,
        "evidence": {
            "state": "blocked",
            "reason": "immutable material CAS drifted after qualification",
            "error": {
                "code": "material_cas_drift",
                "type": "WarmArchiveError",
                "message": "x" * 100_000,
                "evidence": {"bounded": True},
            },
            "component_diff": {
                "schema": "wb-core.root-warm-archive-material-cas-diff/v1",
                "before_material_digest": "sha256:" + "1" * 64,
                "after_material_digest": "sha256:" + "2" * 64,
                "changed_component_count": 200,
                "changed_json_paths": [
                    f"/targets/{index}/identity" for index in range(200)
                ],
                "components": [
                    {
                        "json_path": f"/targets/{index}/identity",
                        "classification": "exact_target_source",
                        "before_component_digest": "sha256:" + "3" * 64,
                        "after_component_digest": "sha256:" + "4" * 64,
                        "before_safe_evidence": {"padding": "a" * 2000},
                        "after_safe_evidence": {"padding": "b" * 2000},
                    }
                    for index in range(200)
                ],
            },
        },
    }

    class LimitClient:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail
            self.post_count = 0
            self.body = ""

        def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
            assert path == "/issues/1050/comments"
            self.post_count += 1
            self.body = str(body["body"])
            assert len(self.body.encode("utf-8")) < apply.MAX_GITHUB_COMMENT_BYTES
            if self.fail:
                raise apply.ApplyError("HTTP 422: body is too long")
            return {
                "id": 1,
                "user": {"login": "github-actions[bot]"},
                "body": self.body,
            }

    with tempfile.TemporaryDirectory(prefix="oversized-apply-receipt-") as directory:
        path = Path(directory) / apply.RECOVERY_ARTIFACT_FILE
        apply._write_receipt(path, receipt)
        full_raw = path.read_bytes()
        assert len(full_raw) > apply.MAX_GITHUB_COMMENT_BYTES
        client = LimitClient()
        apply._publish_compact_apply_receipt(
            client,
            pr=1050,
            receipt=receipt,
            receipt_path=path,
            artifact_name=apply._receipt_artifact_name(1050, 123),
        )
        assert client.post_count == 1
        summary = json.loads(client.body.split("```json", 1)[1].split("```", 1)[0])
        assert summary["state"] == "blocked"
        assert summary["operation_id"] == operation
        assert summary["apply_count"] == 0
        assert summary["error"]["code"] == "material_cas_drift"
        assert summary["component_diff_summary"]["changed_component_count"] == 200
        assert summary["artifact"]["sha256"] == "sha256:" + apply.digest(full_raw)
        assert path.read_bytes() == full_raw

        rejected = LimitClient(fail=True)
        try:
            apply._publish_compact_apply_receipt(
                rejected,
                pr=1050,
                receipt=receipt,
                receipt_path=path,
                artifact_name=apply._receipt_artifact_name(1050, 124),
            )
        except apply.ApplyError as exc:
            assert str(exc).startswith("HTTP 422")
        else:
            raise AssertionError("publication failure was unexpectedly hidden")
        assert rejected.post_count == 1
        assert path.read_bytes() == full_raw


def _exercise_worker_mount_probe() -> None:
    job_id = apply.warm_mount_probe_job_id(
        "orenvlad-ai/wb-core", 1050, "release-v2-test", MERGE_SHA
    )
    target = {
        "target_dir": "/opt/wb-core-runtime/app",
        "ssh_destination": "wb-core-eu-root",
    }
    submit_command = apply._warm_mount_probe_submit_remote_command(
        target=target, merge_sha=MERGE_SHA, job_id=job_id
    )
    status_command = apply._warm_mount_probe_status_remote_command(
        target=target, merge_sha=MERGE_SHA, job_id=job_id
    )
    assert "--operation warm-archive-mount-probe" in submit_command[-1]
    assert " submit " in submit_command[-1]
    assert " status " in status_command[-1]
    assert "authorize" not in submit_command[-1]

    paths = []
    for role, device in (("root", 2049), ("backup", 2065), ("generation", 2081)):
        raw = {
            "raw_line": f"fixture raw mountinfo {role}",
            "mount_id": 100 + device,
            "parent_mount_id": 29,
            "device_major": 8,
            "device_minor": device - 2048,
            "major_minor": f"8:{device - 2048}",
            "mount_root": "/",
            "mount_point": f"/fixture/{role}",
            "mount_options": ["rw"],
            "optional_fields": [],
            "filesystem_type": "ext4",
            "source": f"/dev/{role}",
            "super_options": ["rw"],
            "filesystem_uuid": f"uuid-{role}",
            "source_device": device,
        }
        raw_candidates = [raw]
        if role == "backup":
            overlapping = json.loads(json.dumps(raw))
            overlapping["mount_id"] = int(raw["mount_id"]) + 1
            overlapping["parent_mount_id"] = int(raw["mount_id"])
            overlapping["mount_options"] = ["rw", "nosuid"]
            raw_candidates.append(overlapping)
        paths.append(
            {
                "filesystem_role": role,
                "target": {
                    "canonical_path": f"/fixture/{role}",
                    "path_device": device,
                    "path_inode": 10 + device,
                    "canonical_family_anchor": f"/fixture/{role}",
                    "anchor_device": device,
                    "anchor_inode": 20 + device,
                },
                "semantic_identity_digest": "sha256:" + role[0] * 64,
                "raw_candidate_count": len(raw_candidates),
                "raw_candidates_digest": "sha256:" + role[-1] * 64,
                "raw_mount_candidates": raw_candidates,
                "candidate_proofs": [
                    {
                        "raw_candidate_digest": "sha256:" + "a" * 64,
                        "semantic_identity_digest": "sha256:" + "b" * 64,
                    }
                    for _item in raw_candidates
                ],
            }
        )
    probe = {
        "schema": "wb-core.root-warm-archive-mount-probe/v1",
        "status": "observed",
        "query_only": True,
        "database_written": False,
        "archive_mutation_count": 0,
        "source_unlink_count": 0,
        "service_restart_count": 0,
        "timer_change_count": 0,
        "job_id": job_id,
        "deployed_sha": MERGE_SHA,
        "path_count": 3,
        "paths": paths,
        "worker": {
            "unit_template": "wb-core-storage-recovery-sanitation@.service",
            "unit_instance": f"wb-core-storage-recovery-sanitation@{job_id}.service",
            "repo_template_sha256": "sha256:" + "f" * 64,
            "installed_template_path": (
                "/etc/systemd/system/wb-core-storage-recovery-sanitation@.service"
            ),
            "installed_template_sha256": "sha256:" + "f" * 64,
            "installed_template_matches_repo": True,
            "mount_namespace": {"link_target": "mnt:[4026532999]"},
        },
        "evidence_digest": "sha256:" + "e" * 64,
    }
    status = {
        "status": "succeeded",
        "terminal": True,
        "request": {
            "job_id": job_id,
            "deployed_sha": MERGE_SHA,
            "operation": "warm-archive-mount-probe",
        },
        "result": probe,
    }

    class ProbeClient:
        def __init__(self) -> None:
            self.comments: list[dict[str, object]] = [release_comment()]
            self.post_count = 0

        def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
            assert path == "/issues/1050/comments"
            self.post_count += 1
            comment = {
                "id": 991,
                "user": {"login": "github-actions[bot]"},
                "body": body["body"],
            }
            self.comments.append(comment)
            return comment

    client = ProbeClient()
    args = argparse.Namespace(
        repository="orenvlad-ai/wb-core",
        pr=1050,
        release_operation_id="release-v2-test",
    )
    sequence = [
        {
            "command_sha256": "submit",
            "return_code": 0,
            "stdout_sha256": "submit-out",
            "stderr_sha256": "submit-err",
            "transport_ambiguous": False,
            "result": {"status": "queued"},
        },
        {
            "command_sha256": "status",
            "return_code": 0,
            "stdout_sha256": "status-out",
            "stderr_sha256": "status-err",
            "transport_ambiguous": False,
            "result": status,
        },
    ]
    original_command = apply.command_evidence
    original_run = apply.subprocess.run
    original_target = apply._canonical_target
    original_configure = apply.configure_deploy_environment
    original_run_id = os.environ.get("GITHUB_RUN_ID")
    apply.command_evidence = lambda *_args, **_kwargs: sequence.pop(0)
    apply.subprocess.run = lambda *_args, **_kwargs: object()
    apply._canonical_target = lambda: target
    apply.configure_deploy_environment = lambda _directory: None
    os.environ["GITHUB_RUN_ID"] = "123456"
    try:
        with tempfile.TemporaryDirectory(prefix="warm-mount-probe-smoke-") as directory:
            args.output = Path(directory) / "probe.json"
            assert (
                apply._run_warm_mount_probe_mode(
                    args=args,
                    client=client,
                    pr={"merge_commit_sha": MERGE_SHA},
                    comments=list(client.comments),
                )
                == 0
            )
            receipt = json.loads(args.output.read_text(encoding="utf-8"))
            assert receipt["state"] == "observed"
            assert receipt["production_probe_count"] == 1
            assert receipt["probe"]["paths"] == paths
            assert not sequence
            assert client.post_count == 1
            apply.command_evidence = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("terminal mount probe must not resubmit")
            )
            args.output = Path(directory) / "probe-repeat.json"
            assert (
                apply._run_warm_mount_probe_mode(
                    args=args,
                    client=client,
                    pr={"merge_commit_sha": MERGE_SHA},
                    comments=list(client.comments),
                )
                == 0
            )
            repeated = json.loads(args.output.read_text(encoding="utf-8"))
            assert repeated["idempotent"] is True
            assert repeated["production_probe_count"] == 0
            assert client.post_count == 1
    finally:
        apply.command_evidence = original_command
        apply.subprocess.run = original_run
        apply._canonical_target = original_target
        apply.configure_deploy_environment = original_configure
        if original_run_id is None:
            os.environ.pop("GITHUB_RUN_ID", None)
        else:
            os.environ["GITHUB_RUN_ID"] = original_run_id


def _exact_pr1143_release_binding_contract() -> None:
    base_sha = "3a3b7b31b38a1670c4409bb534677b81b0b02168"
    head_sha = "fca1c66d1d5f010e762b3fc94505448c90aa6c23"
    merge_sha = "1d3a4c6074157d4f5e040846da3c61f5506e8797"
    operation = "release-v2-b3cbca1ace1f88413a5da5be0c7ce4dd"
    manifest_path = "release/production-mutations/wbc0027_fbs_lifecycle_incident.json"
    manifest_raw = (ROOT / manifest_path).read_bytes()
    manifest_sha = apply.digest(manifest_raw)
    manifest = {
        "operation_id": "wbc0027-fbs-identity-428855758-v2",
        "path": manifest_path,
        "sha256": manifest_sha,
    }
    receipt = {
        "base_sha": base_sha,
        "deployed_sha": merge_sha,
        "head_sha": head_sha,
        "manifest": manifest,
        "merge_sha": merge_sha,
        "operation_id": operation,
        "plan_hash": "b63a646506e5051aa214b007a99e4494850a4f7665352a914d87452110b9a261",
        "pull_request": 1143,
        "reason_codes": [],
        "release_kind": "production_mutation",
        "repository": "orenvlad-ai/wb-core",
        "schema": "wb-core.release-receipt/v2",
        "state": "awaiting_apply",
        "workflow_run_id": 33414596664,
    }
    raw_receipt = apply.canonical_json_bytes(receipt) + b"\n"
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("release-receipt.json", raw_receipt)
    raw_zip = archive_buffer.getvalue()
    comment = {
        "id": 5481503347,
        "user": {"login": "github-actions[bot]"},
        "body": (
            f"<!-- wb-core-release-receipt operation={operation} -->\n"
            "Protocol-v2 one-shot release receipt:\n```json\n"
            + json.dumps(receipt, indent=2, sort_keys=True)
            + "\n```"
        ),
    }

    class Client:
        repository = "orenvlad-ai/wb-core"

        def __init__(self) -> None:
            self.comments = [comment]
            self.archive_digest = "sha256:" + apply.digest(raw_zip)

        def get(self, path: str):
            if path == "/pulls/1143":
                return {"merged": True, "base": {"sha": base_sha}, "head": {"sha": head_sha}, "merge_commit_sha": merge_sha}
            if path.startswith("/issues/1143/comments?"):
                return self.comments
            if path == "/actions/runs/33414596664":
                return {"name": "PR Gate", "path": ".github/workflows/pr-gate.yml", "event": "pull_request", "status": "completed", "conclusion": "success", "head_sha": head_sha}
            if path.startswith("/actions/artifacts?"):
                return {"artifacts": [{"id": 9767013211, "name": "release-receipt-33414596664", "size_in_bytes": len(raw_zip), "digest": self.archive_digest, "expired": False, "workflow_run": {"id": 33415566222, "head_sha": base_sha}}]}
            if path == "/actions/runs/33415566222":
                return {"name": "Release Runner", "path": ".github/workflows/release-runner.yml", "event": "workflow_run", "status": "completed", "conclusion": "success", "head_sha": base_sha}
            if path == f"/contents/{manifest_path}?ref={merge_sha}":
                return {"content": base64.b64encode(manifest_raw).decode("ascii")}
            raise AssertionError(path)

        def request(self, method: str, path: str, **_kwargs):
            assert method == "GET"
            assert path == "/actions/artifacts/9767013211/zip"
            return raw_zip

    client = Client()
    binding = apply.collect_exact_release_binding(
        client,
        pr=1143,
        release_operation=operation,
        expected_kind="production_mutation",
        expected_state="awaiting_apply",
        expected_manifest=manifest,
    )
    assert binding["comment_id"] == 5481503347
    assert binding["gate_run_id"] == 33414596664
    assert binding["release_run_id"] == 33415566222
    assert binding["artifact_file_sha256"] == "sha256:" + apply.digest(raw_receipt)
    client.comments = [comment, {**comment, "id": 5481503348}]
    try:
        apply.collect_exact_release_binding(
            client,
            pr=1143,
            release_operation=operation,
            expected_kind="production_mutation",
            expected_state="awaiting_apply",
            expected_manifest=manifest,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("duplicate exact release receipts were accepted")
    client.comments = [comment]
    client.archive_digest = "sha256:" + "0" * 64
    try:
        apply.collect_exact_release_binding(
            client,
            pr=1143,
            release_operation=operation,
            expected_kind="production_mutation",
            expected_state="awaiting_apply",
            expected_manifest=manifest,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("drifted release artifact archive was accepted")


def _correction_base_ancestry_contract() -> None:
    source_merge = "1" * 40
    correction_base = "2" * 40
    intervening_head = "3" * 40
    operation = "release-v2-" + "4" * 32
    receipt = {
        "schema": apply.RELEASE_RECEIPT_SCHEMA,
        "state": "done",
        "operation_id": operation,
        "pull_request": 1144,
        "release_kind": "repo_only",
        "base_sha": source_merge,
        "head_sha": intervening_head,
        "merge_sha": correction_base,
        "deployed_sha": None,
    }
    comment = {
        "id": 55,
        "user": {"login": "github-actions[bot]"},
        "body": (
            f"<!-- wb-core-release-receipt operation={operation} -->\n"
            "Protocol-v2 one-shot release receipt:\n```json\n"
            + json.dumps(receipt, sort_keys=True)
            + "\n```"
        ),
    }
    files = [
        {
            "filename": "docs/architecture/07_codex_execution_protocol.md",
            "status": "modified",
            "sha": "5" * 40,
            "additions": 22,
            "deletions": 10,
            "changes": 32,
        },
        {
            "filename": "ci/test_planner_smoke.py",
            "status": "modified",
            "sha": "6" * 40,
            "additions": 25,
            "deletions": 1,
            "changes": 26,
        },
    ]

    class Client:
        repository = "orenvlad-ai/wb-core"

        def get(self, path: str):
            if path == f"/compare/{source_merge}...{correction_base}":
                return {
                    "status": "ahead",
                    "ahead_by": 1,
                    "behind_by": 0,
                    "merge_base_commit": {"sha": source_merge},
                    "commits": [{"sha": correction_base}],
                }
            if path == f"/commits/{correction_base}/pulls?per_page=100":
                return [
                    {
                        "number": 1144,
                        "merged_at": "2026-08-31T18:53:07Z",
                        "merge_commit_sha": correction_base,
                        "base": {"ref": "main"},
                    }
                ]
            if path == "/pulls/1144":
                return {
                    "number": 1144,
                    "merged": True,
                    "draft": False,
                    "state": "closed",
                    "merge_commit_sha": correction_base,
                    "changed_files": 2,
                    "base": {
                        "ref": "main",
                        "sha": source_merge,
                        "repo": {"full_name": "orenvlad-ai/wb-core"},
                    },
                    "head": {
                        "sha": intervening_head,
                        "repo": {"full_name": "orenvlad-ai/wb-core"},
                    },
                }
            if path.startswith("/issues/1144/comments?"):
                return [comment]
            if path == "/pulls/1144/files?per_page=100&page=1":
                return files
            raise AssertionError(path)

    source = {"receipt": {"merge_sha": source_merge}}
    correction = {"receipt": {"base_sha": correction_base}}
    original_collect = apply.collect_exact_release_binding
    try:
        apply.collect_exact_release_binding = lambda *_args, **kwargs: {
            "receipt": receipt,
            "gate_run_id": 101,
            "release_run_id": 102,
            "artifact_id": 103,
            "artifact_archive_digest": "sha256:" + "7" * 64,
            "artifact_file_sha256": "sha256:" + "8" * 64,
        } if kwargs == {
            "pr": 1144,
            "release_operation": operation,
            "expected_kind": "repo_only",
            "expected_state": "done",
            "expected_manifest": None,
        } else (_ for _ in ()).throw(AssertionError(kwargs))
        proof = apply.collect_correction_base_ancestry(
            Client(), source_release=source, correction_release=correction
        )
        assert proof["status"] == "trusted_non_interfering_descendant"
        assert proof["source_merge_sha"] == source_merge
        assert proof["correction_base_sha"] == correction_base
        assert len(proof["intervening_releases"]) == 1
        assert proof["intervening_releases"][0]["pull_request"] == 1144
        assert {
            row["path"]
            for row in proof["intervening_releases"][0]["path_proof"]["changed_files"]
        } == {row["filename"] for row in files}
        direct = apply.collect_correction_base_ancestry(
            Client(),
            source_release=source,
            correction_release={"receipt": {"base_sha": source_merge}},
        )
        assert direct["status"] == "direct"
        files.append(
            {
                "filename": ".github/workflows/production-apply.yml",
                "status": "modified",
                "sha": "9" * 40,
                "additions": 1,
                "deletions": 1,
                "changes": 2,
            }
        )
        try:
            apply._collect_non_interfering_pr_files(
                Client(), pr_number=1144, pr={"changed_files": 3}
            )
        except apply.ApplyError:
            pass
        else:
            raise AssertionError("interfering workflow bridge was accepted")
    finally:
        apply.collect_exact_release_binding = original_collect


def _workflow_dispatch_contract() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "production-apply.yml"
    ruby_decoder = r"""
def decode(node)
  case node
  when Psych::Nodes::Mapping
    node.children.each_slice(2).to_h { |key, value| [decode(key), decode(value)] }
  when Psych::Nodes::Sequence
    node.children.map { |value| decode(value) }
  when Psych::Nodes::Scalar
    node.value
  else
    raise "unsupported YAML node #{node.class}"
  end
end
puts JSON.generate(decode(Psych.parse_file(ARGV.fetch(0)).root))
"""
    parsed = subprocess.run(
        ["ruby", "-rjson", "-ryaml", "-e", ruby_decoder, str(workflow_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    workflow = json.loads(parsed.stdout)
    dispatch_inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    if len(dispatch_inputs) > 25:
        raise AssertionError(
            "production-apply workflow_dispatch exceeds GitHub's 25-input limit: "
            f"{len(dispatch_inputs)}"
        )
    expected_input_names = (
        "authorization_mode",
        "pr",
        "release_operation_id",
        "merge_sha",
        "deployed_sha",
        "manifest_sha256",
        "operation_id",
        "source_run_id",
        "source_artifact_id",
        "source_artifact_name",
        "source_receipt_sha256",
        "authorization_comment_id",
        "blocked_comment_id",
        "reconciliation_pr",
        "reconciliation_release_operation_id",
        "prior_reconciliation_run_id",
        "prior_reconciliation_artifact_id",
        "prior_reconciliation_artifact_name",
        "prior_reconciliation_receipt_sha256",
        "prior_reconciliation_comment_id",
        "prior_reconciliation_a02_run_id",
        "prior_reconciliation_a02_artifact_id",
        "prior_reconciliation_a02_artifact_name",
        "prior_reconciliation_a02_receipt_sha256",
        "prior_reconciliation_a02_comment_id",
    )
    assert tuple(dispatch_inputs) == expected_input_names
    assert all("description" in item for item in dispatch_inputs.values())
    assert {
        name: item["required"] for name, item in dispatch_inputs.items()
    } == {
        name: "true" if name in {"authorization_mode", "pr"} else "false"
        for name in expected_input_names
    }
    number_inputs = {
        "pr",
        "source_run_id",
        "source_artifact_id",
        "authorization_comment_id",
        "blocked_comment_id",
        "reconciliation_pr",
        "prior_reconciliation_run_id",
        "prior_reconciliation_artifact_id",
        "prior_reconciliation_comment_id",
        "prior_reconciliation_a02_run_id",
        "prior_reconciliation_a02_artifact_id",
        "prior_reconciliation_a02_comment_id",
    }
    assert {name: item["type"] for name, item in dispatch_inputs.items()} == {
        name: (
            "choice"
            if name == "authorization_mode"
            else "number"
            if name in number_inputs
            else "string"
        )
        for name in expected_input_names
    }
    zero_default_inputs = {
        "source_artifact_id",
        "authorization_comment_id",
        "blocked_comment_id",
        "reconciliation_pr",
        "prior_reconciliation_run_id",
        "prior_reconciliation_artifact_id",
        "prior_reconciliation_comment_id",
        "prior_reconciliation_a02_run_id",
        "prior_reconciliation_a02_artifact_id",
        "prior_reconciliation_a02_comment_id",
    }
    assert {
        name: item["default"]
        for name, item in dispatch_inputs.items()
        if "default" in item
    } == {
        "authorization_mode": "scope-goal",
        **{name: "0" for name in zero_default_inputs},
    }
    assert dispatch_inputs["authorization_mode"]["options"] == [
        "scope-goal",
        "exact-manifest",
        "receipt-recovery",
        "warm-archive-readiness",
        "warm-archive-mount-probe",
        "warm-archive-receipt-reconciliation",
        "wbc0027-receipt-reconciliation",
        "fbs-mapping-qualification",
        "fbs-impact-generation",
        "fbs-recovery-qualification",
        "fbs-mapping-apply",
        "fbs-recovery-apply",
    ]
    assert all(
        "options" not in item
        for name, item in dispatch_inputs.items()
        if name != "authorization_mode"
    )

    def strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for child in value for item in strings(child)]
        if isinstance(value, dict):
            return [item for child in value.values() for item in strings(child)]
        return []

    expected_job_inputs = {
        "apply_once": {
            "authorization_comment_id",
            "authorization_mode",
            "deployed_sha",
            "manifest_sha256",
            "merge_sha",
            "operation_id",
            "pr",
            "release_operation_id",
        },
        "fbs_v2": {
            "authorization_comment_id",
            "authorization_mode",
            "manifest_sha256",
            "pr",
            "reconciliation_pr",
            "reconciliation_release_operation_id",
            "release_operation_id",
        },
        "warm_archive_mount_probe": {
            "authorization_mode",
            "pr",
            "release_operation_id",
        },
        "warm_archive_readiness": {
            "authorization_comment_id",
            "authorization_mode",
            "pr",
            "release_operation_id",
        },
        "recover_receipt": {
            "authorization_comment_id",
            "authorization_mode",
            "operation_id",
            "pr",
            "source_artifact_name",
            "source_receipt_sha256",
            "source_run_id",
        },
        "warm_archive_receipt_reconciliation": {
            "authorization_comment_id",
            "authorization_mode",
            "blocked_comment_id",
            "operation_id",
            "pr",
            "prior_reconciliation_a02_artifact_id",
            "prior_reconciliation_a02_artifact_name",
            "prior_reconciliation_a02_comment_id",
            "prior_reconciliation_a02_receipt_sha256",
            "prior_reconciliation_a02_run_id",
            "prior_reconciliation_artifact_id",
            "prior_reconciliation_artifact_name",
            "prior_reconciliation_comment_id",
            "prior_reconciliation_receipt_sha256",
            "prior_reconciliation_run_id",
            "reconciliation_pr",
            "reconciliation_release_operation_id",
            "source_artifact_name",
            "source_receipt_sha256",
            "source_run_id",
        },
        "wbc0027_receipt_reconciliation": {
            "authorization_comment_id",
            "authorization_mode",
            "blocked_comment_id",
            "operation_id",
            "pr",
            "prior_reconciliation_run_id",
            "reconciliation_pr",
            "reconciliation_release_operation_id",
            "source_artifact_id",
            "source_artifact_name",
            "source_receipt_sha256",
            "source_run_id",
        },
    }
    jobs = workflow["jobs"]
    actual_job_inputs = {
        job_name: {
            match
            for value in strings(job)
            for match in re.findall(r"inputs\.([a-z0-9_]+)", value)
        }
        for job_name, job in jobs.items()
    }
    assert actual_job_inputs == expected_job_inputs
    assert set().union(*actual_job_inputs.values()) == set(expected_input_names)
    warm_runs = [
        step["run"]
        for step in jobs["warm_archive_receipt_reconciliation"]["steps"]
        if "run" in step
    ]
    assert sum(
        "--reconciliation-attempt v2-a01" in run for run in warm_runs
    ) == 3
    assert not any("inputs.reconciliation_attempt" in run for run in warm_runs)


def _exercise_wbc0027_stdlib_dependency_isolation() -> None:
    script = r'''
import sys
from copy import deepcopy
from pathlib import Path

root = Path.cwd()
sys.path.insert(0, str(root))
assert "openpyxl" not in sys.modules
assert "apps.wbc0027_capital_recovery" not in sys.modules
from apps import production_apply_runner as runner
from apps.wbc0027_capital_recovery_runner_smoke import _context, _result

blocked = runner.WBC0027_BLOCKED_RECONCILIATION_PREDECESSOR
assert blocked["run_id"] == 33370422066
assert blocked["artifact_id"] == 9749833454
assert blocked["receipt_sha256"] == "518fc39f3c7a17e84a247075f540ef393aed0110b827d276d322075de1000951"
assert blocked["evidence_digest"] == "sha256:87017b579f91e8c49de9111a38098cfef5e02f401467ba1726fb15ed736f9e3b"
context = _context()
result = _result(context)
result["source_recovery_row"]["after_digest"] = ""
assert runner._valid_wbc0027_finalize_result(result, context=context)
drifted = deepcopy(result)
drifted["source_transaction"]["ordering"]["source_code_commit_before_retain"] = False
assert not runner._valid_wbc0027_finalize_result(drifted, context=context)
assert "openpyxl" not in sys.modules
assert "apps.wbc0027_capital_recovery" not in sys.modules
'''
    completed = subprocess.run(
        [sys.executable, "-S", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def main() -> None:
    _exact_pr1143_release_binding_contract()
    _correction_base_ancestry_contract()
    _workflow_dispatch_contract()
    _exercise_wbc0027_stdlib_dependency_isolation()
    _exercise_compact_oversized_blocked_receipt()
    _exercise_worker_mount_probe()
    _exercise_wbc0027_two_phase_runner()
    _exercise_wbc0027_fbs_quality_runner()
    _exercise_wbc0027_fbs_mapping_runner()
    _exercise_wbc0013_two_phase_runner()
    _exercise_historical_cost_runner()
    _exercise_historical_missing_runner()
    goal = apply.validate_authorization(
        authorization(), repository="orenvlad-ai/wb-core", pr=1050
    )
    assert goal["date_count"] == 177
    assert goal["max_mutation_submits"] == 1
    assert goal["max_pre_submit_regenerations"] == 3
    warm_authorization = authorization(body=WARM_AUTH_BODY)
    warm_goal = apply.validate_authorization(
        warm_authorization, repository="orenvlad-ai/wb-core", pr=1050
    )
    assert warm_goal["expected_source_count"] == 6
    assert warm_goal["expected_reclaimed_allocated_bytes"] == 27_591_725_056
    projection_path = (
        "/opt/wb-core-runtime/state/private-evidence/root-warm-archive-readiness/"
        "readiness-v2-"
        + "6" * 32
        + "-a01"
        + "/root-warm-archive-readiness-projection-20260826T120000Z.json"
    )
    warm_dry_command = apply._remote_command(
        target={
            "target_dir": "/opt/wb-core-runtime/app",
            "ssh_destination": "wb-core-eu-root",
        },
        merge_sha=MERGE_SHA,
        goal=warm_goal,
        operation="production-goal-v1-" + "4" * 32,
        evidence_dir=(
            "/opt/wb-core-runtime/state/private-evidence/production-goals/"
            "production-goal-v1-" + "4" * 32
        ),
        mode="dry-run",
        projection_manifest_path=projection_path,
        projection_manifest_sha256="sha256:" + "5" * 64,
    )
    assert warm_dry_command[-1].count(" dry-run ") == 1
    assert projection_path in warm_dry_command[-1]
    readiness_command = apply._warm_readiness_remote_command(
        target={
            "target_dir": "/opt/wb-core-runtime/app",
            "ssh_destination": "wb-core-eu-root",
        },
        merge_sha=MERGE_SHA,
        readiness_id="readiness-v2-" + "6" * 32 + "-a01",
    )
    assert "production-goal-v1" not in readiness_command[-1]
    assert " readiness " in readiness_command[-1]
    try:
        apply.validate_authorization(
            authorization(body=WARM_AUTH_BODY.replace("sources 6", "sources 5")),
            repository="orenvlad-ai/wb-core",
            pr=1050,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("warm archive count drift must fail closed")
    for invalid in (
        authorization(author_association="CONTRIBUTOR"),
        authorization(body=AUTH_BODY.replace("full-days 172", "full-days 171")),
        authorization(body=AUTH_BODY.replace("2026-08-24", "2026-08-23")),
        authorization(
            issue_url="https://api.github.com/repos/orenvlad-ai/wb-core/issues/1051"
        ),
    ):
        try:
            apply.validate_authorization(
                invalid, repository="orenvlad-ai/wb-core", pr=1050
            )
        except (apply.ApplyError, ValueError):
            pass
        else:
            raise AssertionError("invalid scope authorization must fail closed")

    parsed = apply.parse_release_receipt(
        [release_comment()],
        pr=1050,
        release_operation="release-v2-test",
        merge_sha=MERGE_SHA,
    )
    assert parsed["state"] == "done"
    parsed_probe = apply.parse_warm_mount_probe_receipt(
        [warm_mount_probe_comment()],
        repository="orenvlad-ai/wb-core",
        pr=1050,
        release_operation="release-v2-test",
        merge_sha=MERGE_SHA,
    )
    assert parsed_probe["comment_id"] == MOUNT_PROBE_COMMENT_ID
    warm_operation = apply.operation_id(
        "orenvlad-ai/wb-core",
        1050,
        AUTHORIZATION_COMMENT_ID,
        warm_goal,
    )
    readiness_id = apply.warm_readiness_id(
        "orenvlad-ai/wb-core",
        1050,
        "release-v2-test",
        AUTHORIZATION_COMMENT_ID,
        warm_operation,
        1,
    )
    healthy_systemd_gate = {
        "expected_unit_count": 27,
        "observed_unit_count": 27,
        "expected_pair_count": 12,
        "observed_pair_count": 12,
        "healthy": True,
        "failing_unit_count": 0,
        "failing_units": [],
        "failing_pair_count": 0,
        "failing_pairs": [],
        "resample_required_pair_names": [],
        "units": [
            {
                "name": f"unit-{index}",
                "classification": "healthy-fixture",
                "healthy": True,
            }
            for index in range(27)
        ],
        "pairs": [
            {
                "timer_name": f"timer-{index}",
                "owner_name": f"owner-{index}",
                "classification": "waiting_with_inactive_success_owner",
                "healthy": True,
                "resample_required": False,
            }
            for index in range(12)
        ],
        "pair_resample_evidence": {
            "attempted": False,
            "attempt_count": 0,
            "samples": [],
        },
    }
    assert apply._valid_warm_systemd_service_gate(
        healthy_systemd_gate, require_healthy=True
    )
    assert not apply._valid_warm_systemd_service_gate(
        {key: value for key, value in healthy_systemd_gate.items() if key != "pairs"},
        require_healthy=True,
    )
    readiness_payload = {
        "schema": apply.WARM_READINESS_RECEIPT_SCHEMA,
        "state": "ready",
        "attempt": 1,
        "readiness_id": readiness_id,
        "repository": "orenvlad-ai/wb-core",
        "pull_request": 1050,
        "release_operation_id": "release-v2-test",
        "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
        "goal_operation_id": warm_operation,
        "mount_probe_job_id": parsed_probe["job_id"],
        "mount_probe_evidence_digest": parsed_probe["evidence_digest"],
        "mount_probe_artifact": parsed_probe["artifact"],
        "mount_probe_comment_id": parsed_probe["comment_id"],
        "merge_sha": MERGE_SHA,
        "deployed_sha": MERGE_SHA,
        "projection_manifest_path": (
            "/opt/wb-core-runtime/state/private-evidence/"
            f"root-warm-archive-readiness/{readiness_id}/"
            "root-warm-archive-readiness-projection-20260826T120000Z.json"
        ),
        "projection_manifest_sha256": "sha256:" + "5" * 64,
        "material_qualification_digest": "sha256:" + "8" * 64,
        "immutable_non_target_digest": "sha256:" + "7" * 64,
        "mutable_canonical_topology_digest": "sha256:" + "6" * 64,
        "material_cas_components": [
            {
                "json_path": "/targets/fixture/identity",
                "classification": "exact_target_source",
                "cas_role": "immutable",
                "digest": "sha256:" + "8" * 64,
                "safe_evidence": {"key": "fixture"},
            }
        ],
        "material_partition": "immutable_safety_v1",
        "mutable_safety_predicates": {"passed": True},
        "mutable_canonical_observations": [
            {"key": key, "ordinary_mutable_fields": {"size_bytes": 1}}
            for key in (
                "finance_raw_current",
                "operational_current",
                "autoanswers_current",
            )
        ],
        "systemd_service_gate": healthy_systemd_gate,
    }
    parsed_readiness = apply.parse_warm_readiness_receipt(
        [
            warm_mount_probe_comment(),
            {
                "user": {"login": "github-actions[bot]"},
                "body": apply.warm_readiness_marker(readiness_id)
                + "\n```json\n"
                + json.dumps(readiness_payload)
                + "\n```",
            },
        ],
        repository="orenvlad-ai/wb-core",
        pr=1050,
        release_operation="release-v2-test",
        merge_sha=MERGE_SHA,
        authorization_comment_id=AUTHORIZATION_COMMENT_ID,
        goal_operation_id=warm_operation,
    )
    assert parsed_readiness == readiness_payload
    second_readiness_id = apply.warm_readiness_id(
        "orenvlad-ai/wb-core",
        1050,
        "release-v2-test",
        AUTHORIZATION_COMMENT_ID,
        warm_operation,
        2,
    )
    assert second_readiness_id != readiness_id
    blocked_first_readiness = {
        **readiness_payload,
        "state": "blocked",
        "reason": "required_systemd_service_gate_blocked",
    }
    first_blocked_comment = {
        "user": {"login": "github-actions[bot]"},
        "body": apply.warm_readiness_marker(readiness_id)
        + "\n```json\n"
        + json.dumps(blocked_first_readiness)
        + "\n```",
    }
    try:
        apply.parse_warm_readiness_receipt(
            [warm_mount_probe_comment(), first_blocked_comment],
            repository="orenvlad-ai/wb-core",
            pr=1050,
            release_operation="release-v2-test",
            merge_sha=MERGE_SHA,
            authorization_comment_id=AUTHORIZATION_COMMENT_ID,
            goal_operation_id=warm_operation,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("blocked readiness receipt must never become reusable")
    second_readiness_payload = {
        **readiness_payload,
        "attempt": 2,
        "readiness_id": second_readiness_id,
        "projection_manifest_path": (
            "/opt/wb-core-runtime/state/private-evidence/"
            f"root-warm-archive-readiness/{second_readiness_id}/"
            "root-warm-archive-readiness-projection-20260827T120000Z.json"
        ),
    }
    parsed_second_readiness = apply.parse_warm_readiness_receipt(
        [
            warm_mount_probe_comment(),
            first_blocked_comment,
            {
                "user": {"login": "github-actions[bot]"},
                "body": apply.warm_readiness_marker(second_readiness_id)
                + "\n```json\n"
                + json.dumps(second_readiness_payload)
                + "\n```",
            },
        ],
        repository="orenvlad-ai/wb-core",
        pr=1050,
        release_operation="release-v2-test",
        merge_sha=MERGE_SHA,
        authorization_comment_id=AUTHORIZATION_COMMENT_ID,
        goal_operation_id=warm_operation,
    )
    assert parsed_second_readiness == second_readiness_payload
    second_ready_comment = {
        "user": {"login": "github-actions[bot]"},
        "body": apply.warm_readiness_marker(second_readiness_id)
        + "\n```json\n"
        + json.dumps(second_readiness_payload)
        + "\n```",
    }
    invalid_sequences = [
        [second_ready_comment],
        [first_blocked_comment, first_blocked_comment],
        [
            {
                "user": {"login": "github-actions[bot]"},
                "body": (
                    "<!-- wb-core-root-warm-archive-readiness-receipt "
                    "readiness=readiness-v2-"
                    + "f" * 32
                    + "-a04 -->\n```json\n"
                    + json.dumps(
                        {
                            **second_readiness_payload,
                            "attempt": 4,
                            "readiness_id": "readiness-v2-" + "f" * 32 + "-a04",
                        }
                    )
                    + "\n```"
                ),
            }
        ],
        [
            {
                "user": {"login": "github-actions[bot]"},
                "body": apply.warm_readiness_marker(readiness_id)
                + "\n```json\n"
                + json.dumps(readiness_payload)
                + "\n```",
            },
            {
                "user": {"login": "github-actions[bot]"},
                "body": apply.warm_readiness_marker(second_readiness_id)
                + "\n```json\n"
                + json.dumps(
                    {
                        **second_readiness_payload,
                        "state": "blocked",
                        "reason": "later-blocked-after-ready",
                    }
                )
                + "\n```",
            },
        ],
    ]
    for invalid_comments in invalid_sequences:
        try:
            apply._collect_warm_readiness_attempts(
                invalid_comments,
                repository="orenvlad-ai/wb-core",
                pr=1050,
                release_operation="release-v2-test",
                merge_sha=MERGE_SHA,
                authorization_comment_id=AUTHORIZATION_COMMENT_ID,
                goal_operation_id=warm_operation,
            )
        except apply.ApplyError:
            pass
        else:
            raise AssertionError("invalid bounded readiness sequence must fail closed")
    systemd_gate = {
        "classification": "required_units_unhealthy",
        "failing_unit_count": 1,
        "failing_units": [
            {
                "name": "wb-core-warehouse-functional-sync.service",
                "classification": "real_unhealthy_owning_service",
                "Result": "exit-code",
                "ExecMainStatus": "1",
            }
        ],
        "units": [{"name": f"unit-{index}"} for index in range(27)],
    }
    callback = apply._readiness_callback_summary(
        [
            {
                "message": "required production service/timer health is not ready",
                "classification": "required_units_unhealthy",
                "evidence": {"systemd_service_gate": systemd_gate},
            }
        ]
    )
    assert callback[0]["systemd_service_gate"] == {
        "classification": "required_units_unhealthy",
        "failing_unit_count": 1,
        "failing_units": systemd_gate["failing_units"],
        "failing_pair_count": None,
        "failing_pairs": None,
        "pair_resample_summary": None,
    }
    try:
        apply.parse_release_receipt(
            [{**release_comment(), "user": {"login": "contributor"}}],
            pr=1050,
            release_operation="release-v2-test",
            merge_sha=MERGE_SHA,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("untrusted release receipt must fail closed")

    legacy_body = (
        "/wb-core apply-v2 pr 1041 merge "
        + MERGE_SHA
        + " deployed "
        + MERGE_SHA
        + " manifest sha256:"
        + "b" * 64
        + " operation op-1"
    )
    apply.validate_legacy_authorization(
        {"author_association": "OWNER", "body": legacy_body},
        pr=1041,
        merge_sha=MERGE_SHA,
        deployed_sha=MERGE_SHA,
        manifest_sha="b" * 64,
        operation="op-1",
    )
    legacy_release_operation = "release-v2-" + "7" * 32
    legacy_release_payload = {
        "schema": "wb-core.release-receipt/v2",
        "state": "awaiting_apply",
        "operation_id": legacy_release_operation,
        "repository": "orenvlad-ai/wb-core",
        "pull_request": 1041,
        "release_kind": "production_mutation",
        "merge_sha": MERGE_SHA,
        "deployed_sha": MERGE_SHA,
        "reason_codes": [],
        "manifest": {
            "sha256": "b" * 64,
            "operation_id": "op-1",
            "path": "release/production-mutations/fixture.json",
        },
    }
    parsed_legacy_release = apply.parse_legacy_release_receipt(
        [
            {
                "user": {"login": "github-actions[bot]"},
                "body": (
                    f"<!-- wb-core-release-receipt operation={legacy_release_operation} -->"
                    "\n```json\n"
                    + json.dumps(legacy_release_payload)
                    + "\n```"
                ),
            }
        ],
        pr=1041,
        merge_sha=MERGE_SHA,
        manifest_sha="b" * 64,
        operation="op-1",
    )
    assert parsed_legacy_release["operation_id"] == legacy_release_operation
    assert parsed_legacy_release["manifest"]["operation_id"] == "op-1"
    try:
        apply.parse_legacy_release_receipt(
            [
                {
                    "user": {"login": "github-actions[bot]"},
                    "body": (
                        f"<!-- wb-core-release-receipt operation={legacy_release_operation} -->"
                        "\n```json\n"
                        + json.dumps({**legacy_release_payload, "pull_request": 1042})
                        + "\n```"
                    ),
                }
            ],
            pr=1041,
            merge_sha=MERGE_SHA,
            manifest_sha="b" * 64,
            operation="op-1",
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("foreign legacy release PR binding must fail closed")
    hosted_identity = "WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE"
    hosted_options = "WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS"
    os.environ["WB_CORE_DEPLOY_SSH_KEY"] = "fixture-private-key\n"
    os.environ["WB_CORE_DEPLOY_KNOWN_HOSTS"] = "fixture.example ssh-ed25519 AAAA\n"
    os.environ[hosted_identity] = "prior-identity"
    os.environ[hosted_options] = "prior-options"
    legacy_environment_assertion = (
        "import os,pathlib;"
        "p=pathlib.Path(os.environ['WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE']);"
        "assert p.is_file();"
        "assert p.read_text() == 'fixture-private-key\\n';"
        "assert 'StrictHostKeyChecking=yes' in "
        "os.environ['WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS']"
    )
    try:
        legacy_result = apply._run_legacy_commands_with_deploy_environment(
            {
                "commands": {
                    phase: [sys.executable, "-c", legacy_environment_assertion]
                    for phase in ("dry_run", "apply", "readback", "reconcile")
                }
            }
        )
        assert legacy_result["state"] == "done"
        assert legacy_result["apply_count"] == 1
        assert os.environ[hosted_identity] == "prior-identity"
        assert os.environ[hosted_options] == "prior-options"
    finally:
        os.environ.pop("WB_CORE_DEPLOY_SSH_KEY", None)
        os.environ.pop("WB_CORE_DEPLOY_KNOWN_HOSTS", None)
        os.environ.pop(hosted_identity, None)
        os.environ.pop(hosted_options, None)

    common = {
        "command_sha256": "f" * 64,
        "return_code": 0,
        "stdout_sha256": "1" * 64,
        "stderr_sha256": "2" * 64,
        "transport_ambiguous": False,
    }
    success = _run_dynamic_sequence(
        [
            {**common, "result": dry_payload()},
            {**common, "result": dry_payload()},
            {**common, "result": {"status": "reconciled"}},
            {**common, "result": readback_payload()},
        ]
    )
    assert success["state"] == "done"
    assert success["apply_count"] == 1
    assert len(success["qualification_attempts"]) == 2
    assert [
        item["qualification_state"] for item in success["qualification_attempts"]
    ] == [
        "matching_witness",
        "qualified",
    ]

    ambiguous_but_reconciled = _run_dynamic_sequence(
        [
            {**common, "result": dry_payload()},
            {**common, "result": dry_payload()},
            {
                "command_sha256": "3" * 64,
                "return_code": None,
                "transport_ambiguous": True,
                "error": "TimeoutExpired",
            },
            {**common, "result": readback_payload()},
        ]
    )
    assert ambiguous_but_reconciled["state"] == "done"
    assert ambiguous_but_reconciled["apply_count"] == 1

    warm_manifest_sha = "sha256:" + "9" * 64
    warm_candidate = {
        "status": "ready",
        "database_written": False,
        "deployed_sha": MERGE_SHA,
        "source_count": 6,
        "expected_unlink_count": 6,
        "expected_reclaimed_allocated_bytes": 27_591_725_056,
        "root_minimum_after_bytes": 26_843_545_600,
        "required_backup_floor_bytes": 41_105_612_800,
        "capacity_guard_passed": True,
        "openers_count": 0,
        "locks_count": 0,
        "holds_count": 0,
        "manifest_path": (
            "/opt/wb-core-runtime/state/private-evidence/production-goals/op/"
            "root-warm-archive-plan-20260826T120000Z.json"
        ),
        "manifest_sha256": warm_manifest_sha,
        "material_qualification_digest": "sha256:" + "8" * 64,
        "immutable_non_target_digest": "sha256:" + "7" * 64,
        "mutable_canonical_topology_digest": "sha256:" + "6" * 64,
        "material_partition": "immutable_safety_v1",
        "mutable_safety_predicates": {"passed": True},
        "material_cas_components": [
            {
                "json_path": "/targets/fixture/identity",
                "classification": "exact_target_source",
                "cas_role": "immutable",
                "digest": "sha256:" + "8" * 64,
                "safe_evidence": {"key": "fixture"},
            }
        ],
        "mutable_canonical_observations": [
            {"key": key, "ordinary_mutable_fields": {"size_bytes": 1}}
            for key in (
                "finance_raw_current",
                "operational_current",
                "autoanswers_current",
            )
        ],
        "readiness_id": "readiness-v2-" + "6" * 32 + "-a01",
        "projection_manifest_path": (
            "/opt/wb-core-runtime/state/private-evidence/"
            "root-warm-archive-readiness/readiness-v2-"
            + "6" * 32
            + "-a01"
            + "/root-warm-archive-readiness-projection-20260826T120000Z.json"
        ),
        "projection_manifest_sha256": "sha256:" + "5" * 64,
        "activity_evidence": [{"classification": "clean"} for _ in range(6)],
    }
    warm_readback = {
        "status": "reconciled",
        "query_only": True,
        "source_count": 6,
        "source_absent_count": 6,
        "archive_count": 6,
        "manifest_count": 6,
        "raw_unlink_count": 6,
        "reclaimed_allocated_bytes": 27_591_725_056,
        "root_minimum_passed": True,
        "backup_capacity_guard_passed": True,
        "services_healthy": True,
        "non_target_preserved": True,
        "mutation_scope_reconciliation": {
            "exact": True,
            "non_target_unlink_move_write_count": 0,
        },
        "promo_action_count": 0,
        "business_data_mutation_count": 0,
        "exact_manifest_apply_receipt_count": 1,
    }
    warm_candidate_after_autoanswers_growth = json.loads(json.dumps(warm_candidate))
    warm_candidate_after_autoanswers_growth["mutable_canonical_observations"][2][
        "ordinary_mutable_fields"
    ] = {"size_bytes": 4097, "mtime_ns": 1787830000000000000}
    warm_success = _run_dynamic_sequence(
        [
            {**common, "result": warm_candidate},
            {**common, "result": warm_candidate_after_autoanswers_growth},
            {**common, "result": {"status": "queued"}},
            {**common, "result": warm_readback},
        ],
        authorization_comment=warm_authorization,
    )
    assert warm_success["state"] == "done"
    assert warm_success["apply_count"] == 1
    assert len(warm_success["qualification_attempts"]) == 2
    assert (
        warm_success["qualification_attempts"][0]["mutable_canonical_observations"][2][
            "ordinary_mutable_fields"
        ]
        != warm_success["qualification_attempts"][1]["mutable_canonical_observations"][
            2
        ]["ordinary_mutable_fields"]
    )
    warm_operation = apply.operation_id(
        "orenvlad-ai/wb-core", 1050, AUTHORIZATION_COMMENT_ID, warm_goal
    )
    warm_recovery_readback = {
        **warm_readback,
        "deployed_sha": MERGE_SHA,
        "manifest_sha256": warm_manifest_sha,
        "job": {
            "request": {
                "operation": "warm-archive-apply",
                "manifest_sha256": warm_manifest_sha,
            }
        },
    }
    apply._validate_recovery_receipt(
        {
            "schema": apply.APPLY_RECEIPT_SCHEMA,
            "state": "done",
            "operation_id": warm_operation,
            "repository": "orenvlad-ai/wb-core",
            "pull_request": 1050,
            "release_operation_id": RECOVERY_RELEASE_OPERATION,
            "merge_sha": MERGE_SHA,
            "deployed_sha": MERGE_SHA,
            "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
            "goal": warm_goal,
            "apply_count": 1,
            "evidence": {
                "state": "done",
                "reason": "reconciled",
                "apply_count": 1,
                "qualified_manifest": {"sha256": warm_manifest_sha},
                "apply": {"transport_ambiguous": True},
                "readback": {
                    "return_code": 0,
                    "transport_ambiguous": False,
                    "result": warm_recovery_readback,
                },
            },
        },
        repository="orenvlad-ai/wb-core",
        pr=1050,
        merge_sha=MERGE_SHA,
        run_head_sha=MERGE_SHA,
        authorization_comment_id=AUTHORIZATION_COMMENT_ID,
        expected_operation=warm_operation,
        goal=warm_goal,
    )
    warm_scope_escape = json.loads(json.dumps(warm_recovery_readback))
    warm_scope_escape["mutation_scope_reconciliation"][
        "non_target_unlink_move_write_count"
    ] = 1
    try:
        invalid_warm_recovery = {
            "schema": apply.APPLY_RECEIPT_SCHEMA,
            "state": "done",
            "operation_id": warm_operation,
            "repository": "orenvlad-ai/wb-core",
            "pull_request": 1050,
            "release_operation_id": RECOVERY_RELEASE_OPERATION,
            "merge_sha": MERGE_SHA,
            "deployed_sha": MERGE_SHA,
            "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
            "goal": warm_goal,
            "apply_count": 1,
            "evidence": {
                "state": "done",
                "reason": "reconciled",
                "apply_count": 1,
                "qualified_manifest": {"sha256": warm_manifest_sha},
                "apply": {"transport_ambiguous": True},
                "readback": {
                    "return_code": 0,
                    "transport_ambiguous": False,
                    "result": warm_scope_escape,
                },
            },
        }
        apply._validate_recovery_receipt(
            invalid_warm_recovery,
            repository="orenvlad-ai/wb-core",
            pr=1050,
            merge_sha=MERGE_SHA,
            run_head_sha=MERGE_SHA,
            authorization_comment_id=AUTHORIZATION_COMMENT_ID,
            expected_operation=warm_operation,
            goal=warm_goal,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("warm recovery must fail on non-target mutation evidence")

    drift = _run_dynamic_sequence(
        [
            {**common, "result": dry_payload("a")},
            {**common, "result": dry_payload("b")},
            {**common, "result": dry_payload("c")},
            {**common, "result": dry_payload("d")},
        ]
    )
    assert drift["state"] == "blocked"
    assert drift["apply_count"] == 0
    assert len(drift["qualification_attempts"]) == 4
    assert drift["qualification_attempts"][-1]["qualification_state"] == (
        "unstable_at_bound"
    )

    recovered_receipt = recovery_receipt()
    recovery_operation = str(recovered_receipt["operation_id"])
    recovery_client = RecoveryClient(recovered_receipt)
    recovery_args = argparse.Namespace(
        repository="orenvlad-ai/wb-core",
        pr=1050,
        authorization_comment_id=AUTHORIZATION_COMMENT_ID,
        source_run_id=RECOVERY_RUN_ID,
        source_artifact_name=apply._recovery_artifact_name(1050, RECOVERY_RUN_ID),
        source_receipt_sha256=recovery_client.receipt_sha256,
        operation_id=recovery_operation,
    )
    original_command_evidence = apply.command_evidence
    apply.command_evidence = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("receipt recovery must not execute a production command")
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="production-receipt-recovery-smoke-"
        ) as directory:
            recovery_args.output = Path(directory) / "receipt.json"
            assert (
                apply._run_receipt_recovery(
                    args=recovery_args,
                    client=recovery_client,
                    pr={"merge_commit_sha": MERGE_SHA},
                    comments=list(recovery_client.comments),
                )
                == 0
            )
            assert (
                json.loads(recovery_args.output.read_text(encoding="utf-8"))
                == recovered_receipt
            )
            assert recovery_client.post_count == 1
            recovery_args.output = Path(directory) / "receipt-repeat.json"
            assert (
                apply._run_receipt_recovery(
                    args=recovery_args,
                    client=recovery_client,
                    pr={"merge_commit_sha": MERGE_SHA},
                    comments=list(recovery_client.comments),
                )
                == 0
            )
            assert recovery_client.post_count == 1
    finally:
        apply.command_evidence = original_command_evidence

    goal = apply.validate_authorization(
        authorization(), repository="orenvlad-ai/wb-core", pr=1050
    )
    invalid_receipts = []
    for field, value in (
        ("state", "blocked"),
        ("pull_request", 1051),
        ("operation_id", "production-goal-v1-" + "f" * 32),
    ):
        invalid = json.loads(json.dumps(recovered_receipt))
        invalid[field] = value
        invalid_receipts.append(invalid)
    for invalid in invalid_receipts:
        try:
            apply._validate_recovery_receipt(
                invalid,
                repository="orenvlad-ai/wb-core",
                pr=1050,
                merge_sha=MERGE_SHA,
                run_head_sha=MERGE_SHA,
                authorization_comment_id=AUTHORIZATION_COMMENT_ID,
                expected_operation=recovery_operation,
                goal=goal,
            )
        except apply.ApplyError:
            pass
        else:
            raise AssertionError(
                "non-done or wrongly bound recovery receipt must fail closed"
            )
    try:
        apply._extract_recovery_receipt(
            recovery_client.raw_zip,
            "f" * 64,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("wrong recovery receipt digest must fail closed")
    try:
        apply._collect_recovery_receipt(
            recovery_client,
            pr=1050,
            run_id=RECOVERY_RUN_ID,
            artifact_name="wrong-artifact",
            receipt_sha256=recovery_client.receipt_sha256,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("wrong recovery artifact name must fail closed")
    wrong_run_client = RecoveryClient(recovered_receipt)
    wrong_run_client.run_updates["id"] = RECOVERY_RUN_ID + 1
    try:
        apply._collect_recovery_receipt(
            wrong_run_client,
            pr=1050,
            run_id=RECOVERY_RUN_ID,
            artifact_name=apply._recovery_artifact_name(1050, RECOVERY_RUN_ID),
            receipt_sha256=wrong_run_client.receipt_sha256,
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("wrong recovery source run must fail closed")
    duplicate_comments = [
        *recovery_client.comments,
        {**recovery_client.comments[-1], "id": 100},
    ]
    with tempfile.TemporaryDirectory(
        prefix="production-receipt-duplicate-smoke-"
    ) as directory:
        recovery_args.output = Path(directory) / "duplicate.json"
        try:
            apply._run_receipt_recovery(
                args=recovery_args,
                client=recovery_client,
                pr={"merge_commit_sha": MERGE_SHA},
                comments=duplicate_comments,
            )
        except apply.ApplyError:
            pass
        else:
            raise AssertionError("duplicate recovery comments must fail closed")

    workflow = (ROOT / ".github" / "workflows" / "production-apply.yml").read_text(
        encoding="utf-8"
    )
    assert "pull-requests: read" in workflow
    assert "if-no-files-found: error" in workflow
    assert "Upload immutable FBS receipt before marker" in workflow
    assert workflow.index("Upload immutable FBS receipt before marker") < workflow.index(
        "Verify downloaded artifact and publish exact marker"
    )
    apply_job, recovery_and_reconciliation = workflow.split("\n  recover_receipt:\n", 1)
    recovery_job, reconciliation_job = recovery_and_reconciliation.split(
        "\n  warm_archive_receipt_reconciliation:\n", 1
    )
    assert "pull-requests: write" in apply_job
    assert "actions: read" in apply_job
    assert "--authorization-mode warm-archive-readiness" in apply_job
    assert "--authorization-mode warm-archive-mount-probe" in apply_job
    assert "--authorization-comment-id" in apply_job
    assert "product-capital-qualified-economics" in apply_job
    assert "WBC0027 runs product then fresh economics" in apply_job
    assert "root-warm-archive-readiness-receipt.json" in apply_job
    assert "root-warm-archive-mount-probe-receipt.json" in apply_job
    assert "actions: read" in recovery_job
    assert "pull-requests: write" in recovery_job
    assert "--authorization-mode receipt-recovery" in recovery_job
    for forbidden in (
        "environment: production",
        "WB_CORE_DEPLOY_SSH_KEY",
        "WB_CORE_DEPLOY_KNOWN_HOSTS",
        "pip install",
    ):
        assert forbidden not in recovery_job
    assert "environment: production" in reconciliation_job
    assert "actions: read" in reconciliation_job
    assert "ref: ${{ github.sha }}" in reconciliation_job
    assert (
        "--authorization-mode warm-archive-receipt-reconciliation" in reconciliation_job
    )
    assert "--reconciliation-phase preflight" in reconciliation_job
    assert "--reconciliation-phase collect" in reconciliation_job
    assert "--reconciliation-phase publish" in reconciliation_job
    for exact_a02_input in (
        "--reconciliation-attempt",
        "--prior-reconciliation-artifact-id",
        "--prior-reconciliation-artifact-name",
        "--prior-reconciliation-receipt-sha256",
        "--prior-reconciliation-comment-id",
    ):
        assert reconciliation_job.count(exact_a02_input) == 3
    assert reconciliation_job.count("--prior-reconciliation-run-id") == 6
    assert reconciliation_job.index(
        "Upload full immutable reconciliation evidence first"
    ) < (reconciliation_job.index("publish one compact supersession marker"))
    assert reconciliation_job.count("Execute one bounded query-only SSH probe") == 1
    assert "pip install" not in reconciliation_job
    print("production_apply_runner_smoke: ok")


if __name__ == "__main__":
    main()
