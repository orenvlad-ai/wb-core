#!/usr/bin/env python3
"""Contract smoke for the mutation-incapable WBC0027 reconciliation runner."""

from __future__ import annotations

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import production_apply_runner as runner  # noqa: E402


LIVE_SHA = "41dd22a0e81a6c08c416ac35db8173431ed22808"
BRIDGE_SHA = "4f321035ca530338340d482983165699d450a1ef"
LIVE_HEAD = "8934656b96a041985747190ecd8180dbcf924a29"
BRIDGE_HEAD = "f57519a8528bd6c701384cd24e8f67af83313167"
LIVE_OPERATION = "release-v2-5e79ed4d8b65d482eae1e12ce4252e4b"
BRIDGE_OPERATION = "release-v2-76c450066ef7b2e662b6735ce09185a3"


def _pr(
    number: int, *, base_sha: str, head_sha: str, merge_sha: str
) -> dict[str, object]:
    repository = {"full_name": runner.CANONICAL_REPOSITORY}
    return {
        "number": number,
        "state": "closed",
        "draft": False,
        "merged": True,
        "merged_at": "2026-08-31T05:00:00Z",
        "merge_commit_sha": merge_sha,
        "base": {"ref": "main", "sha": base_sha, "repo": repository},
        "head": {"sha": head_sha, "repo": repository},
    }


def _release_comment(payload: dict[str, object]) -> dict[str, object]:
    return {
        "user": {"login": "github-actions[bot]"},
        "body": (
            "<!-- wb-core-release-receipt operation="
            + str(payload["operation_id"])
            + " -->\n```json\n"
            + json.dumps(payload, sort_keys=True)
            + "\n```"
        ),
    }


def _gate(run_id: int, head_sha: str) -> dict[str, object]:
    repository = {"full_name": runner.CANONICAL_REPOSITORY}
    return {
        "id": run_id,
        "name": "PR Gate",
        "path": ".github/workflows/pr-gate.yml",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "head_sha": head_sha,
        "repository": repository,
        "head_repository": repository,
    }


class _BridgeClient:
    def __init__(self) -> None:
        self.live_pr = _pr(
            1133,
            base_sha="30ee64a8003f35837a0a273716ec86c699ea826c",
            head_sha=LIVE_HEAD,
            merge_sha=LIVE_SHA,
        )
        self.bridge_pr = _pr(
            1134,
            base_sha=LIVE_SHA,
            head_sha=BRIDGE_HEAD,
            merge_sha=BRIDGE_SHA,
        )
        self.live_receipt = {
            "schema": runner.RELEASE_RECEIPT_SCHEMA,
            "state": "done",
            "operation_id": LIVE_OPERATION,
            "repository": runner.CANONICAL_REPOSITORY,
            "workflow_run_id": 33357398128,
            "pull_request": 1133,
            "base_sha": "30ee64a8003f35837a0a273716ec86c699ea826c",
            "head_sha": LIVE_HEAD,
            "plan_hash": (
                "f73975454e36767094d4bb2f08e5ada68ec7325ef20ac224d431c3b2bbad2b2e"
            ),
            "release_kind": "live_runtime",
            "merge_sha": LIVE_SHA,
            "deployed_sha": LIVE_SHA,
            "manifest": None,
            "reason_codes": [],
        }
        self.bridge_receipt = {
            "schema": runner.RELEASE_RECEIPT_SCHEMA,
            "state": "done",
            "operation_id": BRIDGE_OPERATION,
            "repository": runner.CANONICAL_REPOSITORY,
            "workflow_run_id": 33359034259,
            "pull_request": 1134,
            "base_sha": LIVE_SHA,
            "head_sha": BRIDGE_HEAD,
            "plan_hash": (
                "c63426aa535c0b1e69cd0960d6487436c4cbb839cbb263a04308997d3b225808"
            ),
            "release_kind": "repo_only",
            "merge_sha": BRIDGE_SHA,
            "deployed_sha": None,
            "manifest": None,
            "reason_codes": [],
        }
        self.bridge_comments = [_release_comment(self.bridge_receipt)]
        self.compare_status = "ahead"
        self.calls: list[str] = []

    def get(self, path: str):
        self.calls.append(path)
        if path == "/actions/runs/33357398128":
            return _gate(33357398128, LIVE_HEAD)
        if path == "/actions/runs/33359034259":
            return _gate(33359034259, BRIDGE_HEAD)
        if path == "/actions/runs/777":
            repository = {"full_name": runner.CANONICAL_REPOSITORY}
            return {
                "id": 777,
                "name": "Production Apply Runner",
                "path": runner.PRODUCTION_APPLY_WORKFLOW_PATH,
                "event": "workflow_dispatch",
                "head_branch": "main",
                "head_sha": os.environ["GITHUB_SHA"],
                "run_attempt": 1,
                "status": "in_progress",
                "conclusion": None,
                "repository": repository,
                "head_repository": repository,
            }
        if path == f"/commits/{BRIDGE_SHA}/pulls?per_page=100":
            return [self.bridge_pr]
        if path == f"/commits/{LIVE_SHA}/pulls?per_page=100":
            return [self.live_pr]
        if path == "/pulls/1134":
            return self.bridge_pr
        if path == "/pulls/1133":
            return self.live_pr
        if path == "/issues/1134/comments?per_page=100&page=1":
            return self.bridge_comments
        if path == f"/compare/{LIVE_SHA}...{BRIDGE_SHA}":
            return {
                "status": self.compare_status,
                "ahead_by": 1,
                "behind_by": 0,
                "merge_base_commit": {"sha": LIVE_SHA},
                "commits": [{"sha": BRIDGE_SHA}],
            }
        raise AssertionError(f"unexpected GitHub read: {path}")


def _exercise_workflow_bridge_binding() -> None:
    historical_integrity = runner._wbc0027_runtime_source_integrity(
        deployed_sha=LIVE_SHA,
        bridge_sha=BRIDGE_SHA,
    )
    assert (
        historical_integrity["comparison"]
        == "byte_identical_repo_only_bridge"
    )
    assert historical_integrity["paths"] == sorted(
        historical_integrity["paths"], key=lambda item: item["path"]
    )
    client = _BridgeClient()
    live = runner._wbc0027_trusted_release(
        client,  # type: ignore[arg-type]
        pr_number=1133,
        pr=client.live_pr,
        comments=[_release_comment(client.live_receipt)],
        merge_sha=LIVE_SHA,
        release_kind="live_runtime",
        operation=LIVE_OPERATION,
    )
    names = (
        "GITHUB_EVENT_NAME",
        "GITHUB_REF",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_RUN_ID",
        "GITHUB_SHA",
    )
    previous = {name: os.environ.get(name) for name in names}
    original_head = runner._git_checkout_head
    original_blob = runner._git_blob_binding
    changed_path = ""

    def blob(commit: str, path: str) -> dict[str, object]:
        blob_sha = "a" * 40
        if path == runner.PRODUCTION_APPLY_WORKFLOW_PATH:
            blob_sha = "b" * 40
        if path == changed_path and commit == BRIDGE_SHA:
            blob_sha = "c" * 40
        return {
            "path": path,
            "mode": "100644",
            "git_blob_sha": blob_sha,
            "size_bytes": 123,
        }

    try:
        os.environ.update(
            {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_REPOSITORY": runner.CANONICAL_REPOSITORY,
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_RUN_ID": "777",
                "GITHUB_SHA": BRIDGE_SHA,
            }
        )
        runner._git_checkout_head = lambda: os.environ["GITHUB_SHA"]
        runner._git_blob_binding = blob
        bridge = runner._wbc0027_workflow_bridge(
            client=client,  # type: ignore[arg-type]
            deployed_release=live,
        )
        assert bridge["pull_request"] == 1134
        assert bridge["operation_id"] == BRIDGE_OPERATION
        assert bridge["state"] == "done"
        assert bridge["release_kind"] == "repo_only"
        assert bridge["merge_sha"] == BRIDGE_SHA
        assert bridge["ancestry"]["status"] == "ahead"
        assert (
            bridge["runtime_source_integrity"]["comparison"]
            == "byte_identical_repo_only_bridge"
        )

        changed_path = "apps/production_apply_runner.py"
        try:
            runner._wbc0027_workflow_bridge(
                client=client,  # type: ignore[arg-type]
                deployed_release=live,
            )
        except runner.ApplyError as exc:
            assert "runtime source changed" in str(exc)
        else:
            raise AssertionError("changed reconciliation app blob was accepted")
        changed_path = ""

        client.compare_status = "diverged"
        try:
            runner._wbc0027_workflow_bridge(
                client=client,  # type: ignore[arg-type]
                deployed_release=live,
            )
        except runner.ApplyError as exc:
            assert "ancestry" in str(exc)
        else:
            raise AssertionError("non-descendant workflow bridge was accepted")
        client.compare_status = "ahead"

        saved_comments = client.bridge_comments
        client.bridge_comments = []
        try:
            runner._wbc0027_workflow_bridge(
                client=client,  # type: ignore[arg-type]
                deployed_release=live,
            )
        except runner.ApplyError as exc:
            assert "missing or ambiguous" in str(exc)
        else:
            raise AssertionError("workflow bridge without exact receipt was accepted")
        client.bridge_comments = saved_comments

        wrong_receipt = dict(client.bridge_receipt)
        wrong_receipt["plan_hash"] = "0" * 64
        client.bridge_comments = [_release_comment(wrong_receipt)]
        try:
            runner._wbc0027_workflow_bridge(
                client=client,  # type: ignore[arg-type]
                deployed_release=live,
            )
        except runner.ApplyError as exc:
            assert "receipt binding is invalid" in str(exc)
        else:
            raise AssertionError("workflow bridge with wrong receipt was accepted")
        client.bridge_comments = saved_comments

        os.environ["GITHUB_SHA"] = LIVE_SHA
        direct = runner._wbc0027_workflow_bridge(
            client=client,  # type: ignore[arg-type]
            deployed_release=live,
        )
        assert direct["pull_request"] == 1133
        assert direct["operation_id"] == LIVE_OPERATION
        assert direct["release_kind"] == "live_runtime"
        assert direct["ancestry"]["status"] == "identical"
        assert (
            direct["runtime_source_integrity"]["comparison"]
            == "direct_deployed_checkout"
        )
        assert not any(call.startswith("POST ") for call in client.calls)
    finally:
        runner._git_checkout_head = original_head
        runner._git_blob_binding = original_blob
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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
            "economics_source_ready_row_count": source[
                "economics_source_ready_row_count"
            ],
            "economics_source_raw_non_target_row_count": source[
                "economics_source_raw_non_target_row_count"
            ],
            "economics_source_raw_non_target_digest": source[
                "economics_source_raw_non_target_digest"
            ],
            "economics_target_identities": source["economics_target_identities"],
            "economics_target_before_hashes": source["economics_target_before_hashes"],
            "economics_target_before_digest": source["economics_target_before_digest"],
            "economics_target_after_hashes": source["economics_target_after_hashes"],
            "economics_target_after_digest": source["economics_target_after_digest"],
            "economics_target_removed_digests": source[
                "economics_target_removed_digests"
            ],
            "economics_target_changed_cell_counts": source[
                "economics_target_changed_cell_counts"
            ],
            "source_phase_contract": source["source_phase_contract"],
            "source_adapter_rehearsal_digest": source[
                "source_adapter_rehearsal_digest"
            ],
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
        "workflow_bridge": {
            "pull_request": 1131,
            "operation_id": "release-v2-" + "6" * 32,
            "state": "done",
            "release_kind": "repo_only",
            "merge_sha": "7" * 40,
        },
    }


def _result(context: dict) -> dict:
    source = context["source"]
    release = context["reconciliation_release"]
    return {
        "contract_name": "wbc0027_existing_operation_reconciliation/v1",
        "runtime_source_binding_contract": (
            runner.WBC0027_RUNTIME_SOURCE_BINDING_CONTRACT
        ),
        "status": "reconciled_existing_operation",
        "qualification_status": "qualified",
        "repeat_disposition": "already_qualifiable",
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
            "after_digest": source["economics_target_after_digest"],
            "non_target_digest": source["economics_source_raw_non_target_digest"],
        },
        "source_recovery_row_digest": "sha256:" + "a" * 64,
        "transition_digest": "sha256:" + "b" * 64,
        "undo_row_count": 3,
        "undo_digest": "sha256:" + "c" * 64,
        "target_before_digest": source["economics_target_before_digest"],
        "target_after_digest": source["economics_target_after_digest"],
        "current_target_digest": source["economics_target_after_digest"],
        "current_target_hashes": source["economics_target_after_hashes"],
        "source_transaction": {
            "contract_name": "wbc0027_source_economics_transaction_legacy_adapter/v1",
            "source_ready_row_count": source["economics_source_ready_row_count"],
            "source_raw_non_target": {
                "contract_name": "wbc0027_legacy_raw_non_target_aggregate/v1",
                "row_count": source["economics_source_raw_non_target_row_count"],
                "digest": source["economics_source_raw_non_target_digest"],
                "binding": "exact_source_manifest_and_recovery_row",
            },
            "source_semantic_components_reconstructable": False,
            "source_adapter_rehearsal_digest": source[
                "source_adapter_rehearsal_digest"
            ],
            "target_rows": [
                {
                    "identity": identity,
                    "changed_cell_count": changed,
                    "before_sha256": before,
                    "planned_after_sha256": after,
                    "target_removed_before_digest": removed,
                    "target_removed_planned_after_digest": removed,
                }
                for identity, before, after, removed, changed in zip(
                    source["economics_target_identities"],
                    source["economics_target_before_hashes"],
                    source["economics_target_after_hashes"],
                    source["economics_target_removed_digests"],
                    source["economics_target_changed_cell_counts"],
                    strict=True,
                )
            ],
            "write_set": {
                "row_count": 3,
                "cell_count": 472,
                "undo_row_count": 3,
                "undo_rows_verified": True,
                "undo_artifact_verified": True,
                "expected_after_image_count": 3,
            },
            "ordering": {
                "cas_before_images_verified": True,
                "exact_after_readback_verified": True,
                "source_code_semantic_before_after_equal": True,
                "source_code_commit_before_retain": True,
                "exact_retain_mismatch_caused_quarantine": True,
                "mutation_running_transition_index": 4,
                "quarantine_after_commit_index": 5,
            },
            "source_code": {
                "contract_name": "wbc0027_source_economics_code_order/v1",
                "deployed_sha": source["deployed_sha"],
                "phase_contract": source["source_phase_contract"],
                "immutable_order": [
                    "before_image_cas",
                    "exact_after_readback",
                    "semantic_non_target_equality",
                    "commit",
                    "retain",
                ],
            },
        },
        "temporal_non_target_drift": {
            "contract_name": "wbc0027_temporal_non_target_drift/v1",
            "classification": "later_non_target_evolution",
            "changed": True,
            "source_ready_row_count": 224,
            "current_ready_row_count": 225,
            "source_raw_non_target_row_count": 221,
            "current_raw_non_target_row_count": 222,
            "source_target_row_count": 3,
            "current_target_row_count": 3,
            "source_semantic_components_available": False,
            "source_semantic_reconstruction_permitted": False,
            "current_component_digests": {
                "identities": (
                    "sha256:9709363e72d0cb2a34e97938c448f5bf59784431bb1ab8a91c37b7bb6c37d581"
                ),
                "semantic_payloads": (
                    "sha256:ccd70f358f12aeebeb002e83431c7812612b65589b284c2e53f94f2c8de51b3c"
                ),
                "rows": (
                    "sha256:b8d8b14531c9a1506459125bd880c7a6b4d169b24af53a9346f1b5ca897885c3"
                ),
            },
            "current_semantic_digest": (
                "sha256:a8cd7a0185a23f1a6d9ec1b398bf69522a6d53316939683a96041c122b26a07e"
            ),
            "source_raw_non_target_digest": source[
                "economics_source_raw_non_target_digest"
            ],
            "current_raw_non_target_digest": (
                "sha256:9afa5cfa2532c2f524e10c400a6a259e42e69fcf3000006d8d6ab9df30728fcf"
            ),
            "derived_added_rows": [],
            "observed_late_ordinary_rows": [
                {
                    "identity": [
                        "registry_upload_bundle_v1__2026-06-08T00:00:00Z",
                        "2026-08-30",
                        "2026-08-30__2026-08-31__sheet_vitrina_v1_temporal_live_v1__current",
                    ],
                    "plan_sha256": (
                        "sha256:782f84896d84ea54368da753108c29ea59c4ba71d36da53f2357c9a06f1951ab"
                    ),
                    "refreshed_at": "2026-08-31T01:39:50Z",
                }
            ],
            "diff_derivation": "not_derivable_from_source_aggregate_digest",
            "equality_gate": False,
            "effect": "receipt_evidence_only_not_target_approval",
        },
        "protected_invariant": {"nm_id": 428853741, "unit_cost_rub": "117.537167"},
        "evidence_blocked": [f"2026-08-26|blocked-{index}" for index in range(12)],
        "product_capital": {
            "status": "published_exact",
            "scope_count": 1152,
            "cell_count": 24192,
            "mismatch_count": 0,
        },
        "hard_non_target": {
            "all_exact": True,
            "from_date": "2026-08-30",
            "observed_date_count": 2,
        },
        "functional_economics_missing": {"2026-08-26": 12, "2026-08-29": 0},
        "query_only": True,
        "database_written": False,
        "production_mutation_count": 0,
        "product_replay_count": 0,
        "economics_replay_count": 0,
    }


def main() -> None:
    _exercise_workflow_bridge_binding()
    context = _context()
    result = _result(context)
    assert runner._valid_wbc0027_finalize_result(result, context=context)

    exact_legacy_empty = deepcopy(result)
    exact_legacy_empty["source_recovery_row"]["after_digest"] = ""
    assert runner._valid_wbc0027_finalize_result(
        exact_legacy_empty, context=context
    )
    missing_legacy_identity = deepcopy(context["source"])
    del missing_legacy_identity["run_id"]
    assert not runner._valid_wbc0027_source_recovery_after_digest(
        {"after_digest": ""},
        source=missing_legacy_identity,
        source_transaction=result["source_transaction"],
    )
    foreign_legacy_identity = deepcopy(context["source"])
    foreign_legacy_identity["run_id"] += 1
    assert not runner._valid_wbc0027_source_recovery_after_digest(
        {"after_digest": ""},
        source=foreign_legacy_identity,
        source_transaction=result["source_transaction"],
    )
    canonical_source_transaction = {
        "contract_name": "wbc0027_economics_semantic_non_target_digest/v1"
    }
    assert not runner._valid_wbc0027_source_recovery_after_digest(
        {"after_digest": ""},
        source=context["source"],
        source_transaction=canonical_source_transaction,
    )
    exact_legacy_wrong = deepcopy(result)
    exact_legacy_wrong["source_recovery_row"]["after_digest"] = (
        "sha256:" + "f" * 64
    )
    assert not runner._valid_wbc0027_finalize_result(
        exact_legacy_wrong, context=context
    )
    assert runner._valid_wbc0027_source_recovery_after_digest(
        result["source_recovery_row"],
        source=context["source"],
        source_transaction=result["source_transaction"],
    )

    for scope_count in (None, 1151):
        scope_drift = deepcopy(result)
        if scope_count is None:
            del scope_drift["product_capital"]["scope_count"]
        else:
            scope_drift["product_capital"]["scope_count"] = scope_count
        assert not runner._valid_wbc0027_finalize_result(scope_drift, context=context)
    row_count_only = deepcopy(result)
    del row_count_only["product_capital"]["scope_count"]
    row_count_only["product_capital"]["row_count"] = 1152
    assert not runner._valid_wbc0027_finalize_result(row_count_only, context=context)
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
    target_drift = deepcopy(result)
    target_drift["current_target_hashes"][0] = "sha256:" + "f" * 64
    assert not runner._valid_wbc0027_finalize_result(target_drift, context=context)
    source_drift = deepcopy(result)
    source_drift["source_transaction"]["target_rows"][0]["before_sha256"] = (
        "sha256:" + "f" * 64
    )
    assert not runner._valid_wbc0027_finalize_result(source_drift, context=context)
    source_semantic_invented = deepcopy(result)
    source_semantic_invented["source_transaction"]["source_semantic_non_target"] = {
        "digest": "sha256:" + "f" * 64
    }
    assert not runner._valid_wbc0027_finalize_result(
        source_semantic_invented, context=context
    )

    duplicate_marker = runner._wbc0027_reconciliation_marker(
        str(context["source"]["operation_id"])
    )
    try:
        runner._existing_wbc0027_reconciliation_marker(
            [{"body": duplicate_marker}, {"body": duplicate_marker}],
            context=context,
            client=None,  # type: ignore[arg-type]
        )
    except runner.ApplyError as exc:
        assert "duplicate or ambiguous" in str(exc)
    else:
        raise AssertionError("duplicate WBC0027 source marker was accepted")

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
        "workflow_bridge": context["workflow_bridge"],
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
    assert "--no-create" in shell
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
