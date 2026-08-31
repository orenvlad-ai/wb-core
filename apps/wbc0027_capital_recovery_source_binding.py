#!/usr/bin/env python3
"""Pure stdlib-only source binding for WBC0027 reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CONTRACT_NAME = "wbc0027_reconciliation_runtime_source_binding/v1"
WORKFLOW_PATH = ".github/workflows/production-apply.yml"
LEGACY_SOURCE_TRANSACTION_CONTRACT = (
    "wbc0027_source_economics_transaction_legacy_adapter/v1"
)
LEGACY_SOURCE_RAW_NON_TARGET_CONTRACT = (
    "wbc0027_legacy_raw_non_target_aggregate/v1"
)
LEGACY_SOURCE_CODE_ORDER_CONTRACT = "wbc0027_source_economics_code_order/v1"

# These are the executable owners of the trusted GitHub receipt validation and
# the deployed finalize-only readback boundary.  A repo-only workflow bridge may
# differ from the deployed release only while every one of these Git blobs is
# byte-identical.  Changing this list changes this module and therefore selects
# the live-runtime WBC0027 release lane itself.
PATHS = (
    "apps/github_release_runner.py",
    "apps/production_apply_runner.py",
    "apps/release_protocol.py",
    "apps/wbc0027_capital_recovery.py",
    "apps/wbc0027_capital_recovery_source_binding.py",
    "ci/test_planner.py",
    "packages/application/registry_upload_db_backed_runtime.py",
    "packages/application/root_storage_policy.py",
    "packages/application/sqlite_contention.py",
    "packages/application/storage_registry.py",
    "packages/application/warehouse_business_projection.py",
    "packages/application/warehouse_functional_lock.py",
    "packages/application/warehouse_recovery_policy.py",
    "packages/application/warehouse_sync_lock.py",
)

LEGACY_SOURCE_TRANSACTION_BINDING = {
    "pull_request": 1129,
    "run_id": 33345644125,
    "artifact_id": 9741910399,
    "artifact_name": "production-apply-receipt-pr-1129-run-33345644125",
    "receipt_sha256": (
        "sha256:843d1eb81d92ac16a51bc21fb92256916e4c9c3a353d3221ebc1a82df80bf9f5"
    ),
    "blocked_comment_id": 5472359912,
    "authorization_comment_id": 5472278622,
    "authorization_body_sha256": (
        "b2cfb8bf9f20ecfe7a9075f42ff443a144d4550ee07c8418482988ad2542d3ad"
    ),
    "goal_operation_id": "production-goal-v1-5024719a64fa9707b72d938ebf8a2127",
    "product_phase_operation_id": "recovery_9b9d1d2ad66035d080ec2bced855201e",
    "economics_phase_operation_id": "recovery_ae66a56f72d90b469b75d8adb893c51f",
    "source_deployed_sha": "876f5f307a2053d66544dd1c8950f94f77f92ddb",
    "source_phase_contract": "wbc0027_capital_recovery_phase_v3",
    "manifest_sha256": (
        "sha256:675fcb98fdcc74ce2d30c4e907c9c5330f7878fee929027c536b5a6f03ec47c4"
    ),
    "phase_fingerprint": (
        "sha256:2d6004dcd37b8d3becd31231d6d2a77e4ab1c5262757f355ddf1413f1d24b542"
    ),
    "storage_generation": {
        "generation_id": "operational-c54072027f14f90b374b",
        "manifest_sha256": (
            "sha256:8cdd437b7357042092a8be2e1fdce028af2444c81a464465dbadd557b57a2ffb"
        ),
        "schema_revision": "operational_v1",
    },
    "source_ready_row_count": 224,
    "source_raw_non_target_row_count": 221,
    "source_raw_non_target_digest": (
        "sha256:55e057f0f18109cc01b4f78583c96facc78e5e4ef09f205c80d2c1991d57a858"
    ),
    "target_identities": (
        (
            "registry_upload_bundle_v1__2026-06-08T00:00:00Z",
            "2026-08-25",
            "2026-08-25__2026-08-26__sheet_vitrina_v1_temporal_live_v1__current",
        ),
        (
            "registry_upload_bundle_v1__2026-06-08T00:00:00Z",
            "2026-08-26",
            "2026-08-26__partial_group_wb_api__2026-08-28T04:59:24Z",
        ),
        (
            "registry_upload_bundle_v1__2026-06-08T00:00:00Z",
            "2026-08-29",
            "2026-08-29__2026-08-30__sheet_vitrina_v1_temporal_live_v1__current",
        ),
    ),
    "target_business_dates": (("2026-08-26",), ("2026-08-26",), ("2026-08-29",)),
    "target_before_hashes": (
        "sha256:a20d88e0208dd0ebec804c8f9a61f9734f6e43880ad3908f48bb63aaa340d3c7",
        "sha256:baee41dea0e85ae958b5149f6c944af7e77d6f42cd3b46f868ddea0038912fd4",
        "sha256:bddbad60bdfa0cb99668d2d0ab663ae5fa10f021ad169c692fc0804a9978272e",
    ),
    "target_before_digest": (
        "sha256:edca64a735d30abad44c5b31a0603baf06932ecdad8b3cedba0af9dcd980b67e"
    ),
    "target_after_hashes": (
        "sha256:d186a89de1910576b20b86c707056b85f72e5b7a69e175db8ef6d041a1432f1d",
        "sha256:54b0bdd0d36564c884071ec2e848945d0c1152113571fc7bb7ef2608a6d69c2e",
        "sha256:ef848227b66e26ebe37643e752abac252c9a63c85fe0957a9bcae78736392c68",
    ),
    "target_after_digest": (
        "sha256:359736666b74cc4b4b87eb5a6b4bce6e309a29f35b64d2548966a13fbfc58424"
    ),
    "target_removed_digests": (
        "sha256:9d01f65d8e87705cae5aaf4eb1b5599312ca4bb05a02e0826c46d89899ea90a8",
        "sha256:a593fb1b110178d9d2bffc2476191c9fb6bc34878ce1692fafee42f5c40a63f7",
        "sha256:6f108a3bc53fb737a51ebd1cb9fe5e264aef155d7f5f11cac605ddb3c56d886c",
    ),
    "target_changed_cell_counts": (174, 174, 124),
    "target_identities_digest": (
        "sha256:d834c5886f2c529799e2595f24b9f2563e59661685d30eb1817fee7f324fda88"
    ),
    "rehearsal_result_digest": (
        "sha256:3598233834edfdc236bff126dfd9a25f432d36e44a1ed97abad9123d079cf4aa"
    ),
}


class SourceBindingError(ValueError):
    """The caller did not present the exact immutable legacy source."""


def legacy_authorization_reference(
    binding: Mapping[str, Any] = LEGACY_SOURCE_TRANSACTION_BINDING,
) -> str:
    source = binding
    return (
        "github:orenvlad-ai/wb-core:pr:1129:comment:"
        f"{source['authorization_comment_id']}:sha256:"
        f"{source['authorization_body_sha256']}"
    )


def validate_legacy_source_transaction_binding(
    *,
    goal_operation_id: str,
    source_deployed_sha: str,
    source_manifest_sha256: str,
    source_phase_operation_id: str,
    source_phase_fingerprint: str,
    source_storage_generation: Mapping[str, Any],
    source_run_id: int,
    source_artifact_id: int,
    source_artifact_name: str,
    source_receipt_sha256: str,
    source_comment_id: int,
    authorization_reference: str,
    binding: Mapping[str, Any] = LEGACY_SOURCE_TRANSACTION_BINDING,
) -> None:
    """Admit the missing-after-digest exception for one exact source."""

    source = binding
    observed = {
        "goal_operation_id": goal_operation_id,
        "source_deployed_sha": source_deployed_sha,
        "source_manifest_sha256": source_manifest_sha256,
        "source_phase_operation_id": source_phase_operation_id,
        "source_phase_fingerprint": source_phase_fingerprint,
        "source_storage_generation": dict(source_storage_generation),
        "source_run_id": source_run_id,
        "source_artifact_id": source_artifact_id,
        "source_artifact_name": source_artifact_name,
        "source_receipt_sha256": source_receipt_sha256,
        "source_comment_id": source_comment_id,
        "authorization_reference": authorization_reference,
    }
    expected = {
        "goal_operation_id": source["goal_operation_id"],
        "source_deployed_sha": source["source_deployed_sha"],
        "source_manifest_sha256": source["manifest_sha256"],
        "source_phase_operation_id": source["economics_phase_operation_id"],
        "source_phase_fingerprint": source["phase_fingerprint"],
        "source_storage_generation": source["storage_generation"],
        "source_run_id": source["run_id"],
        "source_artifact_id": source["artifact_id"],
        "source_artifact_name": source["artifact_name"],
        "source_receipt_sha256": source["receipt_sha256"],
        "source_comment_id": source["blocked_comment_id"],
        "authorization_reference": legacy_authorization_reference(source),
    }
    if observed != expected:
        raise SourceBindingError("WBC0027 legacy source transaction binding drifted")


def _source_allowlist_exact(source: Mapping[str, Any]) -> bool:
    binding = LEGACY_SOURCE_TRANSACTION_BINDING
    try:
        validate_legacy_source_transaction_binding(
            goal_operation_id=str(source["operation_id"]),
            source_deployed_sha=str(source["deployed_sha"]),
            source_manifest_sha256=str(source["economics_manifest_sha256"]),
            source_phase_operation_id=str(source["economics_phase_operation_id"]),
            source_phase_fingerprint=str(source["economics_phase_fingerprint"]),
            source_storage_generation=source["storage_generation"],
            source_run_id=int(source["run_id"]),
            source_artifact_id=int(source["artifact_id"]),
            source_artifact_name=str(source["artifact_name"]),
            source_receipt_sha256=str(source["receipt_sha256"]),
            source_comment_id=int(source["blocked_comment_id"]),
            authorization_reference=str(source["authorization_reference"]),
        )
    except (KeyError, TypeError, ValueError, SourceBindingError):
        return False
    exact_evidence = {
        "source_phase_contract": source.get("source_phase_contract"),
        "source_ready_row_count": source.get("economics_source_ready_row_count"),
        "source_raw_non_target_row_count": source.get(
            "economics_source_raw_non_target_row_count"
        ),
        "source_raw_non_target_digest": source.get(
            "economics_source_raw_non_target_digest"
        ),
        "target_identities": source.get("economics_target_identities"),
        "target_before_hashes": source.get("economics_target_before_hashes"),
        "target_before_digest": source.get("economics_target_before_digest"),
        "target_after_hashes": source.get("economics_target_after_hashes"),
        "target_after_digest": source.get("economics_target_after_digest"),
        "target_removed_digests": source.get("economics_target_removed_digests"),
        "target_changed_cell_counts": source.get(
            "economics_target_changed_cell_counts"
        ),
        "rehearsal_result_digest": source.get("source_adapter_rehearsal_digest"),
    }
    expected_evidence = {
        key: binding[key]
        for key in (
            "source_phase_contract",
            "source_ready_row_count",
            "source_raw_non_target_row_count",
            "source_raw_non_target_digest",
            "target_identities",
            "target_before_hashes",
            "target_before_digest",
            "target_after_hashes",
            "target_after_digest",
            "target_removed_digests",
            "target_changed_cell_counts",
            "rehearsal_result_digest",
        )
    }
    for key in (
        "target_identities",
        "target_before_hashes",
        "target_after_hashes",
        "target_removed_digests",
        "target_changed_cell_counts",
    ):
        value = exact_evidence[key]
        if isinstance(value, list):
            exact_evidence[key] = tuple(
                tuple(item) if isinstance(item, list) else item for item in value
            )
    return exact_evidence == expected_evidence


def _legacy_transaction_proof_exact(
    source_transaction: Mapping[str, Any],
) -> bool:
    source = LEGACY_SOURCE_TRANSACTION_BINDING
    target_rows = source_transaction.get("target_rows")
    if not isinstance(target_rows, list) or len(target_rows) != 3:
        return False
    expected_rows = []
    for identity, business_dates, before, after, removed, changed in zip(
        source["target_identities"],
        source["target_business_dates"],
        source["target_before_hashes"],
        source["target_after_hashes"],
        source["target_removed_digests"],
        source["target_changed_cell_counts"],
        strict=True,
    ):
        expected_rows.append(
            {
                "identity": list(identity),
                "business_dates": list(business_dates),
                "changed_cell_count": changed,
                "before_sha256": before,
                "planned_after_sha256": after,
                "target_removed_before_digest": removed,
                "target_removed_planned_after_digest": removed,
            }
        )
    expected = {
        "contract_name": LEGACY_SOURCE_TRANSACTION_CONTRACT,
        "source_ready_row_count": source["source_ready_row_count"],
        "source_raw_non_target": {
            "contract_name": LEGACY_SOURCE_RAW_NON_TARGET_CONTRACT,
            "row_count": source["source_raw_non_target_row_count"],
            "digest": source["source_raw_non_target_digest"],
            "binding": "exact_source_manifest_and_recovery_row",
        },
        "source_semantic_components_reconstructable": False,
        "source_adapter_rehearsal_digest": source["rehearsal_result_digest"],
        "target_rows": expected_rows,
        "target_identities_digest": source["target_identities_digest"],
        "target_before_digest": source["target_before_digest"],
        "target_planned_after_digest": source["target_after_digest"],
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
            "contract_name": LEGACY_SOURCE_CODE_ORDER_CONTRACT,
            "deployed_sha": source["source_deployed_sha"],
            "phase_contract": source["source_phase_contract"],
            "immutable_order": [
                "before_image_cas",
                "exact_after_readback",
                "semantic_non_target_equality",
                "commit",
                "retain",
            ],
        },
    }
    return dict(source_transaction) == expected


def valid_source_recovery_after_digest(
    source_row: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    source_transaction: Mapping[str, Any],
) -> bool:
    """Validate exact after-digest or the one complete legacy exception."""

    if not _source_allowlist_exact(source):
        return False
    expected = source.get("economics_target_after_digest")
    observed = source_row.get("after_digest")
    if isinstance(expected, str) and expected and observed == expected:
        return True
    if observed != "":
        return False
    return bool(
        source_row.get("operation_id")
        == LEGACY_SOURCE_TRANSACTION_BINDING["economics_phase_operation_id"]
        and source_row.get("lifecycle") == "quarantined"
        and source_row.get("quarantine_reason")
        == "non_target_digest_drift_after_mutation"
        and source_row.get("non_target_digest")
        == LEGACY_SOURCE_TRANSACTION_BINDING["source_raw_non_target_digest"]
        and _legacy_transaction_proof_exact(source_transaction)
    )


if len(PATHS) != len(set(PATHS)) or tuple(sorted(PATHS)) != PATHS:
    raise RuntimeError("WBC0027 reconciliation source paths must be unique and sorted")
