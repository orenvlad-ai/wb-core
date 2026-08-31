#!/usr/bin/env python3
"""Contract smoke for the mutation-incapable WBC0027 reconciliation runner."""

from __future__ import annotations

from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import production_apply_runner as runner  # noqa: E402


def _context() -> dict:
    source = runner.WBC0027_RECONCILIATION_SOURCE
    return {
        "source": {
            "pull_request": source["pull_request"],
            "run_id": source["run_id"],
            "run_head_sha": source["deployed_sha"],
            "artifact_id": source["artifact_id"],
            "artifact_name": source["artifact_name"],
            "artifact_archive_digest": source["artifact_archive_digest"],
            "receipt_file": runner.RECOVERY_ARTIFACT_FILE,
            "receipt_sha256": "sha256:" + source["receipt_sha256"],
            "blocked_comment_id": source["blocked_comment_id"],
            "blocked_comment_digest": "sha256:" + "1" * 64,
            "authorization_comment_id": source["authorization_comment_id"],
            "authorization_body_sha256": source["authorization_body_sha256"],
            "authorization_reference": (
                "github:orenvlad-ai/wb-core:pr:1129:comment:5472278622:sha256:"
                + source["authorization_body_sha256"]
            ),
            "operation_id": source["operation_id"],
            "release_operation_id": source["release_operation_id"],
            "deployed_sha": source["deployed_sha"],
            "product_phase_operation_id": source["product_phase_operation_id"],
            "economics_phase_operation_id": source["economics_phase_operation_id"],
            "economics_manifest_path": source["economics_manifest_path"],
            "economics_manifest_sha256": source["economics_manifest_sha256"],
            "economics_phase_fingerprint": source["economics_phase_fingerprint"],
            "storage_generation": dict(source["storage_generation"]),
            "material_qualification_digest": "sha256:" + "2" * 64,
        },
        "reconciliation_release": {
            "pull_request": 1130,
            "operation_id": "release-v2-" + "3" * 32,
            "release_kind": "live_runtime",
            "merge_sha": "4" * 40,
            "deployed_sha": "4" * 40,
            "workflow_run_id": 123,
            "plan_hash": "sha256:" + "5" * 64,
        },
    }


def _result(context: dict) -> dict:
    semantic = {
        "contract_name": "wbc0027_economics_semantic_non_target_digest/v1",
        "scope_version": "ready_snapshot_target_slices_removed_v1",
        "row_count": 224,
        "target_row_count": 3,
        "component_digests": {
            "identities": "sha256:" + "6" * 64,
            "semantic_payloads": "sha256:" + "7" * 64,
            "rows": "sha256:" + "8" * 64,
        },
        "digest": "sha256:" + "9" * 64,
    }
    source = context["source"]
    release = context["reconciliation_release"]
    return {
        "contract_name": "wbc0027_existing_operation_reconciliation/v1",
        "status": "reconciled_existing_operation",
        "terminal_disposition": "supersede_false_quarantine_receipt",
        "profile": runner.WBC0027_GOAL_PROFILE,
        "target_id": runner.CANONICAL_PRODUCTION_TARGET_ID,
        "goal_operation_id": source["operation_id"],
        "product_phase_operation_id": source["product_phase_operation_id"],
        "economics_phase_operation_id": source["economics_phase_operation_id"],
        "source_deployed_sha": source["deployed_sha"],
        "reconciliation_deployed_sha": release["merge_sha"],
        "storage_generation": source["storage_generation"],
        "source_apply": {
            "run_id": source["run_id"],
            "artifact_id": source["artifact_id"],
            "artifact_name": source["artifact_name"],
            "receipt_sha256": source["receipt_sha256"],
            "comment_id": source["blocked_comment_id"],
            "authorization_reference": source["authorization_reference"],
        },
        "reconciliation_release": {
            "pull_request": release["pull_request"],
            "operation_id": release["operation_id"],
        },
        "source_recovery_row": {
            "operation_id": source["economics_phase_operation_id"],
            "lifecycle": "quarantined",
            "quarantine_reason": "non_target_digest_drift_after_mutation",
        },
        "source_recovery_row_digest": "sha256:" + "a" * 64,
        "transition_digest": "sha256:" + "b" * 64,
        "undo_row_count": 3,
        "undo_digest": "sha256:" + "c" * 64,
        "current_target_digest": "sha256:" + "d" * 64,
        "current_target_hashes": ["sha256:" + value * 64 for value in "ef0"],
        "legacy_raw_non_target_digest": "sha256:" + "1" * 64,
        "semantic_non_target_before": semantic,
        "semantic_non_target_after": semantic,
        "semantic_non_target_current": semantic,
        "product_capital": {"status": "published_exact", "mismatch_count": 0},
        "hard_non_target": {"all_exact": True},
        "functional_economics_missing": {"2026-08-26": 12, "2026-08-29": 0},
        "query_only": True,
        "database_written": False,
        "production_mutation_count": 0,
        "product_replay_count": 0,
        "economics_replay_count": 0,
    }


def main() -> None:
    context = _context()
    result = _result(context)
    assert runner._valid_wbc0027_finalize_result(result, context=context)
    for field, foreign in (
        ("production_mutation_count", 1),
        ("product_replay_count", 1),
        ("economics_replay_count", 1),
        ("database_written", True),
    ):
        changed = deepcopy(result)
        changed[field] = foreign
        assert not runner._valid_wbc0027_finalize_result(changed, context=context)
    missing = deepcopy(result)
    del missing["source_recovery_row"]
    assert not runner._valid_wbc0027_finalize_result(missing, context=context)
    foreign = deepcopy(result)
    foreign["source_apply"]["artifact_id"] += 1
    assert not runner._valid_wbc0027_finalize_result(foreign, context=context)

    receipt = {
        "schema": runner.WBC0027_RECONCILIATION_RECEIPT_SCHEMA,
        "state": "done",
        "terminal_disposition": "done/reconciled_existing_operation",
        "query_only": True,
        "database_written": False,
        "production_mutation_count": 0,
        "product_replay_count": 0,
        "economics_replay_count": 0,
        "source": context["source"],
        "reconciliation_release": context["reconciliation_release"],
        "probe": {
            "return_code": 0,
            "transport_ambiguous": False,
            "result": result,
        },
    }
    receipt["evidence_digest"] = runner.payload_digest(receipt)
    assert runner._validate_wbc0027_reconciliation_receipt(
        receipt, context=context
    )

    command = runner._wbc0027_finalize_remote_command(
        target={"target_dir": "/srv/wb-core", "ssh_destination": "fixture"},
        context=context,
    )
    shell = command[-1]
    assert "finalize-only" in shell
    assert " production apply " not in shell
    assert "/apps/wbc0027_capital_recovery.py" in shell

    foreign_zip = io.BytesIO()
    with zipfile.ZipFile(foreign_zip, "w") as archive:
        archive.writestr("foreign.json", json.dumps(receipt))
    try:
        runner._extract_wbc0027_reconciliation_artifact(
            foreign_zip.getvalue(), expected_sha256="0" * 64
        )
    except runner.ApplyError:
        pass
    else:
        raise AssertionError("foreign reconciliation artifact was accepted")

    workflow = (ROOT / ".github/workflows/production-apply.yml").read_text(
        encoding="utf-8"
    )
    job = workflow.split("  wbc0027_receipt_reconciliation:", 1)[1]
    assert "--reconciliation-phase preflight" in job
    assert "--reconciliation-phase collect" in job
    assert "--reconciliation-phase publish" in job
    assert job.index("Upload full immutable WBC0027 reconciliation evidence first") < job.index(
        "Verify uploaded evidence and publish one supersession marker"
    )
    print("wbc0027_reconciliation_runner_smoke: OK")


if __name__ == "__main__":
    main()
