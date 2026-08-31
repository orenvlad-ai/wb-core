#!/usr/bin/env python3
"""One-submit production apply from a durable task-scoped authorization."""

from __future__ import annotations

import argparse
import base64
from datetime import date, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Mapping
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.github_release_runner import (  # noqa: E402
    GitHubClient,
    RECEIPT_MARKER,
    RECEIPT_SCHEMA as RELEASE_RECEIPT_SCHEMA,
    canonical_json_bytes,
    configure_deploy_environment,
    exact_sha,
    is_actions_bot_comment,
    list_comments,
    operation_id as release_operation_id,
)
from apps.release_protocol import (  # noqa: E402
    CANONICAL_PRODUCTION_TARGET_ID,
    CANONICAL_REPOSITORY,
    validate_production_manifest,
)
from apps.wbc0027_capital_recovery_source_binding import (  # noqa: E402
    CONTRACT_NAME as WBC0027_RUNTIME_SOURCE_BINDING_CONTRACT,
    PATHS as WBC0027_RUNTIME_SOURCE_PATHS,
    WORKFLOW_PATH as PRODUCTION_APPLY_WORKFLOW_PATH,
)
from packages.application.root_storage_policy import (  # noqa: E402
    storage_destination_root,
)


APPLY_RECEIPT_SCHEMA = "wb-core.production-apply-receipt/v4"
APPLY_MARKER = "wb-core-production-apply-receipt"
APPLY_COMMENT_SUMMARY_SCHEMA = "wb-core.production-apply-comment-summary/v1"
WARM_READINESS_RECEIPT_SCHEMA = "wb-core.root-warm-archive-readiness-receipt/v4"
WARM_READINESS_MARKER = "wb-core-root-warm-archive-readiness-receipt"
WARM_MOUNT_PROBE_RECEIPT_SCHEMA = "wb-core.root-warm-archive-mount-probe-receipt/v1"
WARM_MOUNT_PROBE_MARKER = "wb-core-root-warm-archive-mount-probe-receipt"
GOAL_PROFILE = "inventory-history-backfill"
WARM_ARCHIVE_GOAL_PROFILE = "root-warm-archive-six"
WBC0013_GOAL_PROFILE = "dense-fbs-historical-recovery"
WBC0027_GOAL_PROFILE = "product-capital-qualified-economics"
WBC0027_LEGACY_MANIFEST_SHA256 = (
    "84a4bef9d6cba4c969988d880ab56bde06db307f3caf87a42305f7fe8c8680ee"
)
WBC0027_LEGACY_MANIFEST_OPERATIONS = frozenset(
    {
        "wbc0027-product-capital-and-qualified-economics-v1",
        "wbc0027-product-capital-and-qualified-economics-v2",
    }
)
WBC0027_PREDECESSOR_BINDING = {
    "pull_request": 1128,
    "release_operation_id": "release-v2-52c958d066816e6e7b2fec7b419fc530",
    "release_comment_id": 5471998411,
    "authorization_comment_id": 5472023099,
    "apply_run_id": 33343193199,
    "apply_comment_id": 5472070488,
    "artifact_id": 9741186908,
    "artifact_name": "production-apply-receipt-pr-1128-run-33343193199",
    "receipt_sha256": (
        "sha256:2e65b37d7a44027928143d0f8b4ab71c43638450f659c4875faf3b0d80f7b9d5"
    ),
    "goal_operation_id": "production-goal-v1-89bfdc5e4e4bffcbc9f6f6aea677e389",
    "product_phase_operation_id": "recovery_303ece915dfb8e89b615a84dc8f14d70",
    "economics_phase_operation_id": "recovery_8fe6bf612bde74c0dec9cb3b441944b2",
}
WBC0027_PREDECESSOR_AUTH_BODY = (
    "/wb-core authorize-goal-v1 task WBC0027 "
    "profile product-capital-qualified-economics "
    "target wb_core_eu_hosted_runtime_active "
    "product-rows 1152 product-cells 24192 product-mismatches 9446 "
    "primary-rows 936 primary-cells 19656 primary-mismatches 7655 "
    "secondary-rows 216 secondary-mismatches 1791 "
    "special-date 2026-08-21 special-nm 497413772 special-cells 16 "
    "blocked-date 2026-08-15 hard-non-target-from 2026-08-30 "
    "economics-logical 298 economics-persisted 472 economics-blocked 12 "
    "protected-nm 428853741 protected-unit-cost-rub 117.537167 submits 2"
)
WBC0027_RECONCILIATION_RECEIPT_SCHEMA = (
    "wb-core.wbc0027-existing-operation-reconciliation-receipt/v1"
)
WBC0027_RECONCILIATION_SUMMARY_SCHEMA = (
    "wb-core.wbc0027-existing-operation-reconciliation-summary/v1"
)
WBC0027_RECONCILIATION_MARKER = (
    "wb-core-wbc0027-existing-operation-reconciliation"
)
WBC0027_RECONCILIATION_ARTIFACT_FILE = (
    "wbc0027-existing-operation-reconciliation-receipt.json"
)
WBC0027_RECONCILIATION_SOURCE = {
    "pull_request": 1129,
    "run_id": 33345644125,
    "artifact_id": 9741910399,
    "artifact_name": "production-apply-receipt-pr-1129-run-33345644125",
    "artifact_archive_digest": (
        "sha256:4149736a3ec3c24a369d8b6ca819aae6565fdeae49f80ddd6588baa048ee8807"
    ),
    "receipt_sha256": (
        "843d1eb81d92ac16a51bc21fb92256916e4c9c3a353d3221ebc1a82df80bf9f5"
    ),
    "blocked_comment_id": 5472359912,
    "authorization_comment_id": 5472278622,
    "authorization_body_sha256": (
        "b2cfb8bf9f20ecfe7a9075f42ff443a144d4550ee07c8418482988ad2542d3ad"
    ),
    "operation_id": "production-goal-v1-5024719a64fa9707b72d938ebf8a2127",
    "deployed_sha": "876f5f307a2053d66544dd1c8950f94f77f92ddb",
    "release_operation_id": "release-v2-6da7932622d180ce46e9e6770de148fa",
    "product_phase_operation_id": "recovery_9b9d1d2ad66035d080ec2bced855201e",
    "economics_phase_operation_id": "recovery_ae66a56f72d90b469b75d8adb893c51f",
    "economics_manifest_path": (
        "/opt/wb-core-runtime/state/backups/private-evidence/production-goals/"
        "production-goal-v1-5024719a64fa9707b72d938ebf8a2127/"
        "wbc0027-economics-plan-20260831T004927088748Z-6906446fda0d.json"
    ),
    "economics_manifest_sha256": (
        "sha256:675fcb98fdcc74ce2d30c4e907c9c5330f7878fee929027c536b5a6f03ec47c4"
    ),
    "economics_phase_fingerprint": (
        "sha256:2d6004dcd37b8d3becd31231d6d2a77e4ab1c5262757f355ddf1413f1d24b542"
    ),
    "economics_source_ready_row_count": 224,
    "economics_source_raw_non_target_row_count": 221,
    "economics_source_raw_non_target_digest": (
        "sha256:55e057f0f18109cc01b4f78583c96facc78e5e4ef09f205c80d2c1991d57a858"
    ),
    "economics_target_identities": [
        [
            "registry_upload_bundle_v1__2026-06-08T00:00:00Z",
            "2026-08-25",
            "2026-08-25__2026-08-26__sheet_vitrina_v1_temporal_live_v1__current",
        ],
        [
            "registry_upload_bundle_v1__2026-06-08T00:00:00Z",
            "2026-08-26",
            "2026-08-26__partial_group_wb_api__2026-08-28T04:59:24Z",
        ],
        [
            "registry_upload_bundle_v1__2026-06-08T00:00:00Z",
            "2026-08-29",
            "2026-08-29__2026-08-30__sheet_vitrina_v1_temporal_live_v1__current",
        ],
    ],
    "economics_target_before_hashes": [
        "sha256:a20d88e0208dd0ebec804c8f9a61f9734f6e43880ad3908f48bb63aaa340d3c7",
        "sha256:baee41dea0e85ae958b5149f6c944af7e77d6f42cd3b46f868ddea0038912fd4",
        "sha256:bddbad60bdfa0cb99668d2d0ab663ae5fa10f021ad169c692fc0804a9978272e",
    ],
    "economics_target_before_digest": (
        "sha256:edca64a735d30abad44c5b31a0603baf06932ecdad8b3cedba0af9dcd980b67e"
    ),
    "economics_target_after_hashes": [
        "sha256:d186a89de1910576b20b86c707056b85f72e5b7a69e175db8ef6d041a1432f1d",
        "sha256:54b0bdd0d36564c884071ec2e848945d0c1152113571fc7bb7ef2608a6d69c2e",
        "sha256:ef848227b66e26ebe37643e752abac252c9a63c85fe0957a9bcae78736392c68",
    ],
    "economics_target_after_digest": (
        "sha256:359736666b74cc4b4b87eb5a6b4bce6e309a29f35b64d2548966a13fbfc58424"
    ),
    "economics_target_removed_digests": [
        "sha256:9d01f65d8e87705cae5aaf4eb1b5599312ca4bb05a02e0826c46d89899ea90a8",
        "sha256:a593fb1b110178d9d2bffc2476191c9fb6bc34878ce1692fafee42f5c40a63f7",
        "sha256:6f108a3bc53fb737a51ebd1cb9fe5e264aef155d7f5f11cac605ddb3c56d886c",
    ],
    "economics_target_changed_cell_counts": [174, 174, 124],
    "source_phase_contract": "wbc0027_capital_recovery_phase_v3",
    "source_adapter_rehearsal_digest": (
        "sha256:3598233834edfdc236bff126dfd9a25f432d36e44a1ed97abad9123d079cf4aa"
    ),
    "storage_generation": {
        "generation_id": "operational-c54072027f14f90b374b",
        "manifest_sha256": (
            "sha256:8cdd437b7357042092a8be2e1fdce028af2444c81a464465dbadd557b57a2ffb"
        ),
        "schema_revision": "operational_v1",
    },
}
HISTORICAL_COST_GOAL_PROFILE = "historical-analytical-cost-carry-forward"
HISTORICAL_MISSING_REPAIR_GOAL_PROFILE = "historical-missing-repair"
WARM_ARCHIVE_LEGACY_EVIDENCE_BASE = (
    Path("/opt/wb-core-runtime/state") / "private-evidence" / "production-goals"
)
MAX_QUALIFICATION_CANDIDATES = 4
MAX_WARM_READINESS_ATTEMPTS = 3
MAX_GITHUB_COMMENT_BYTES = 65_536
RECOVERY_WORKFLOW_NAME = "Production Apply Runner"
RECOVERY_WORKFLOW_PATH = ".github/workflows/production-apply.yml"
RECOVERY_ARTIFACT_FILE = "production-apply-receipt.json"
MAX_RECOVERY_ARTIFACT_BYTES = 8 * 1024 * 1024
WARM_RECONCILIATION_RECEIPT_SCHEMA = (
    "wb-core.root-warm-archive-reconciliation-receipt/v3"
)
WARM_RECONCILIATION_SUMMARY_SCHEMA = (
    "wb-core.root-warm-archive-reconciliation-comment-summary/v3"
)
LEGACY_WARM_RECONCILIATION_RECEIPT_SCHEMA = (
    "wb-core.root-warm-archive-reconciliation-receipt/v1"
)
LEGACY_WARM_RECONCILIATION_SUMMARY_SCHEMA = (
    "wb-core.root-warm-archive-reconciliation-comment-summary/v1"
)
LEGACY_WARM_RECONCILIATION_A02_RECEIPT_SCHEMA = (
    "wb-core.root-warm-archive-reconciliation-receipt/v2"
)
LEGACY_WARM_RECONCILIATION_A02_SUMMARY_SCHEMA = (
    "wb-core.root-warm-archive-reconciliation-comment-summary/v2"
)
LEGACY_WARM_RECONCILIATION_SEQUENCE_SCHEMA = (
    "wb-core.root-warm-archive-reconciliation-sequence/v1"
)
WARM_RECONCILIATION_GENERATION_SCHEMA = (
    "wb-core.root-warm-archive-reconciliation-generation/v2"
)
WARM_RECONCILIATION_ATTEMPT = "v2-a01"
WARM_RECONCILIATION_SOURCE_PR = 1075
WARM_RECONCILIATION_SOURCE_RUN_ID = 33061965717
WARM_RECONCILIATION_SOURCE_ARTIFACT_ID = 9642978355
WARM_RECONCILIATION_SOURCE_ARTIFACT_NAME = (
    "production-apply-receipt-pr-1075-run-33061965717"
)
WARM_RECONCILIATION_SOURCE_ARCHIVE_DIGEST = (
    "sha256:c54c654e9a16d338f4afd2bc7f1ab500851850651afac08f6cd21c3d69badebd"
)
WARM_RECONCILIATION_SOURCE_RECEIPT_SHA256 = (
    "fdab4802fd8a57eb6b3ad79d89fd527efdf3848bfbff5b3c0e3381fee76415c0"
)
WARM_RECONCILIATION_SOURCE_AUTHORIZATION_COMMENT_ID = 5437409674
WARM_RECONCILIATION_SOURCE_BLOCKED_COMMENT_ID = 5437848287
WARM_RECONCILIATION_SOURCE_DEPLOYED_SHA = "7d83c5d0ddf6bf86d6359409ef0f9a7bb4ad4747"
WARM_RECONCILIATION_SOURCE_OPERATION_ID = (
    "production-goal-v1-8692b24cb2491927bdadd5dec06a15d8"
)
WARM_RECONCILIATION_SOURCE_JOB_ID = (
    "d8176c48b41b6d128aa9adacb3aa50f1d464dc318cc9cc8df58d3be637649d2d"
)
WARM_RECONCILIATION_A01_RUN_ID = 33069817619
WARM_RECONCILIATION_A01_ARTIFACT_ID = 9645283377
WARM_RECONCILIATION_A01_ARTIFACT_NAME = (
    "root-warm-archive-reconciliation-pr-1075-run-33069817619"
)
WARM_RECONCILIATION_A01_RECEIPT_SHA256 = (
    "1b99b7a01127f963af31b0cafb2a764e928eb839662af665b1afa4646b9c4847"
)
WARM_RECONCILIATION_A01_ARCHIVE_DIGEST = (
    "sha256:779b8e35c5b9fbb6940bc9d18fc7ecea807f1f5714e9ae104d4ba42970352dee"
)
WARM_RECONCILIATION_A01_COMMENT_ID = 5438726868
WARM_RECONCILIATION_A01_MARKER_DIGEST = (
    "sha256:008afc6e862c1443ad5331474103b4ab074f4ca18d947f70a826aedca0fa11c3"
)
WARM_RECONCILIATION_A01_RELEASE_PR = 1076
WARM_RECONCILIATION_A01_RELEASE_MERGE_SHA = "98736484237d1f5af052cfa3c0a8d96c7d87ff3b"
WARM_RECONCILIATION_A02_RUN_ID = 33073151214
WARM_RECONCILIATION_A02_ARTIFACT_ID = 9646668764
WARM_RECONCILIATION_A02_ARTIFACT_NAME = (
    "root-warm-archive-reconciliation-pr-1075-run-33073151214"
)
WARM_RECONCILIATION_A02_RECEIPT_SHA256 = (
    "ce87472b71d1545cb8383ec417b1d83cba1c5f46568beb6249b9e66368d4030a"
)
WARM_RECONCILIATION_A02_ARCHIVE_DIGEST = (
    "sha256:8d57e4ddbb19856c545f4028b92d3fa6228230cf903826d0d51c38c09283e6f9"
)
WARM_RECONCILIATION_A02_COMMENT_ID = 5439297992
WARM_RECONCILIATION_A02_MARKER_DIGEST = (
    "sha256:14acd8ee3991c4bc4ea172e43096db905c8847bf4df1f62240d6953d25d781af"
)
WARM_RECONCILIATION_A02_RELEASE_PR = 1077
WARM_RECONCILIATION_A02_RELEASE_MERGE_SHA = "a9f63435223f57a02d18d3280c0b3b56c4982e82"
WARM_RECONCILIATION_CANONICAL_MODULE_SHA256 = (
    "sha256:24c3a2243338419f755aff78583b978aa0e5197ffc9e6b215466c7d4a11f501d"
)
WARM_RECONCILIATION_SERVICE_NAMES_DIGEST = (
    "sha256:29eb924bbb0c7dfa7081d2d29cfcdef9957986b344a34b25d1879e48f00fec60"
)
WARM_RECONCILIATION_MARKER = "wb-core-root-warm-archive-reconciliation-receipt"
WARM_RECONCILIATION_ARTIFACT_FILE = "root-warm-archive-reconciliation-receipt.json"
MAX_WARM_RECONCILIATION_ARTIFACT_BYTES = 8 * 1024 * 1024
WARM_RECONCILIATION_ZERO_ACTIONS = frozenset(
    {
        "readiness",
        "submit",
        "apply",
        "job_creation",
        "archive_worker",
        "readback_batch",
        "full_restore",
        "decompression_to_file",
        "temporary_file_creation",
        "lock_acquisition",
        "service_start_or_restart",
        "timer_change",
        "sql_or_file_write",
        "unlink",
    }
)
TARGET_FILE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "hosted_runtime_target__europe_api.json"
)
AUTH_RE = re.compile(
    r"^/wb-core authorize-goal-v1 task (?P<task>WBC[0-9]{4}) "
    r"profile (?P<profile>[a-z0-9-]{1,80}) "
    r"target (?P<target>[A-Za-z0-9._:-]{1,160}) "
    r"dates (?P<date_from>[0-9]{4}-[0-9]{2}-[0-9]{2})\.\."
    r"(?P<date_to>[0-9]{4}-[0-9]{2}-[0-9]{2}) "
    r"captures (?P<captures>[1-9][0-9]*) "
    r"components (?P<components>[1-9][0-9]*) "
    r"finalizations (?P<finalizations>[1-9][0-9]*) "
    r"full-days (?P<full_days>[0-9]+) partial-days (?P<partial_days>[0-9]+)$"
)
WARM_ARCHIVE_AUTH_RE = re.compile(
    r"^/wb-core authorize-goal-v1 task (?P<task>WBC[0-9]{4}) "
    r"profile (?P<profile>root-warm-archive-six) "
    r"target (?P<target>[A-Za-z0-9._:-]{1,160}) "
    r"sources (?P<sources>[1-9][0-9]*) archives (?P<archives>[1-9][0-9]*) "
    r"manifests (?P<manifests>[1-9][0-9]*) unlinks (?P<unlinks>[1-9][0-9]*) "
    r"reclaimed-allocated-bytes (?P<reclaimed>[1-9][0-9]*) "
    r"root-minimum-bytes (?P<root_minimum>[1-9][0-9]*) "
    r"backup-floor-bytes (?P<backup_floor>[1-9][0-9]*)$"
)
WBC0013_AUTH_RE = re.compile(
    r"^/wb-core authorize-goal-v1 task (?P<task>WBC0013) "
    r"profile (?P<profile>dense-fbs-historical-recovery) "
    r"target (?P<target>[A-Za-z0-9._:-]{1,160}) "
    r"roster (?P<roster>[1-9][0-9]*) existing (?P<existing>[1-9][0-9]*) "
    r"owner-approved-missing (?P<missing>[1-9][0-9]*) "
    r"zero-inserts (?P<zero_inserts>[1-9][0-9]*) "
    r"historical-date (?P<historical_date>[0-9]{4}-[0-9]{2}-[0-9]{2}) "
    r"historical-nm (?P<historical_nm>[1-9][0-9]*) "
    r"historical-version (?P<historical_version>whfv_[a-z0-9_]{8,120}) "
    r"historical-event (?P<historical_event>ffbf_[a-z0-9]{8,120}) "
    r"historical-repairs (?P<historical_repairs>[1-9][0-9]*)$"
)
WBC0027_AUTH_RE = re.compile(
    r"^/wb-core authorize-goal-v1 task (?P<task>WBC0027) "
    r"profile (?P<profile>product-capital-qualified-economics) "
    r"target (?P<target>[A-Za-z0-9._:-]{1,160}) "
    r"product-rows (?P<product_rows>[1-9][0-9]*) "
    r"product-cells (?P<product_cells>[1-9][0-9]*) "
    r"product-mismatches (?P<product_mismatches>[1-9][0-9]*) "
    r"primary-rows (?P<primary_rows>[1-9][0-9]*) "
    r"primary-cells (?P<primary_cells>[1-9][0-9]*) "
    r"primary-mismatches (?P<primary_mismatches>[1-9][0-9]*) "
    r"secondary-rows (?P<secondary_rows>[1-9][0-9]*) "
    r"secondary-mismatches (?P<secondary_mismatches>[1-9][0-9]*) "
    r"special-date (?P<special_date>[0-9]{4}-[0-9]{2}-[0-9]{2}) "
    r"special-nm (?P<special_nm>[1-9][0-9]*) "
    r"special-cells (?P<special_cells>[1-9][0-9]*) "
    r"blocked-date (?P<blocked_date>[0-9]{4}-[0-9]{2}-[0-9]{2}) "
    r"hard-non-target-from (?P<hard_from>[0-9]{4}-[0-9]{2}-[0-9]{2}) "
    r"economics-logical (?P<economics_logical>[1-9][0-9]*) "
    r"economics-persisted (?P<economics_persisted>[1-9][0-9]*) "
    r"economics-blocked (?P<economics_blocked>[1-9][0-9]*) "
    r"protected-nm (?P<protected_nm>[1-9][0-9]*) "
    r"protected-unit-cost-rub (?P<protected_cost>[1-9][0-9]*\.[0-9]{6}) "
    r"submits (?P<submits>[1-9][0-9]*) "
    r"predecessor-pr (?P<predecessor_pr>[1-9][0-9]*) "
    r"predecessor-release-operation "
    r"(?P<predecessor_release_operation>release-v2-[0-9a-f]{32}) "
    r"predecessor-release-comment (?P<predecessor_release_comment>[1-9][0-9]*) "
    r"predecessor-authorization-comment "
    r"(?P<predecessor_authorization_comment>[1-9][0-9]*) "
    r"predecessor-apply-run (?P<predecessor_apply_run>[1-9][0-9]*) "
    r"predecessor-apply-comment (?P<predecessor_apply_comment>[1-9][0-9]*) "
    r"predecessor-receipt (?P<predecessor_receipt>sha256:[0-9a-f]{64}) "
    r"predecessor-operation "
    r"(?P<predecessor_operation>production-goal-v1-[0-9a-f]{32}) "
    r"predecessor-product-phase "
    r"(?P<predecessor_product_phase>recovery_[0-9a-f]{32}) "
    r"predecessor-economics-phase "
    r"(?P<predecessor_economics_phase>recovery_[0-9a-f]{32})$"
)
HISTORICAL_COST_AUTH_RE = re.compile(
    r"^/wb-core authorize-goal-v1 task (?P<task>WBC0013) "
    r"profile (?P<profile>historical-analytical-cost-carry-forward) "
    r"target (?P<target>[A-Za-z0-9._:-]{1,160}) "
    r"business-date (?P<business_date>[0-9]{4}-[0-9]{2}-[0-9]{2}) "
    r"nm (?P<nm_id>[1-9][0-9]*) unit-cost-rub "
    r"(?P<unit_cost_rub>[1-9][0-9]*\.[0-9]{6}) "
    r"accepted-versions (?P<versions>[1-9][0-9]*) "
    r"ready-snapshots (?P<snapshots>[1-9][0-9]*)$"
)
HISTORICAL_MISSING_REPAIR_AUTH_RE = re.compile(
    r"^/wb-core authorize-goal-v1 task (?P<task>WBC0010) "
    r"profile (?P<profile>historical-missing-repair) "
    r"target (?P<target>[A-Za-z0-9._:-]{1,160}) "
    r"source-operation (?P<source_operation>recovery_[a-z0-9]{32}) "
    r"source-digest (?P<source_digest>sha256:[0-9a-f]{64}) "
    r"dates (?P<dates>[1-9][0-9]*) logical-targets (?P<targets>[1-9][0-9]*) "
    r"snapshots (?P<snapshots>[1-9][0-9]*) "
    r"missing-before (?P<missing_before>[1-9][0-9]*) missing-after (?P<missing_after>[0-9]+) "
    r"excluded-date (?P<excluded_date>[0-9]{4}-[0-9]{2}-[0-9]{2})$"
)
LEGACY_AUTH_RE = re.compile(
    r"^/wb-core apply-v2 pr (?P<pr>[1-9][0-9]*) merge (?P<merge>[0-9a-f]{40}) "
    r"deployed (?P<deployed>[0-9a-f]{40}) manifest sha256:(?P<manifest>[0-9a-f]{64}) "
    r"operation (?P<operation>[A-Za-z0-9._:-]{1,160})$"
)


class ApplyError(RuntimeError):
    pass


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def payload_digest(value: Any) -> str:
    return "sha256:" + digest(canonical_json_bytes(value))


def marker(operation: str) -> str:
    return f"<!-- {APPLY_MARKER} operation={operation} -->"


def warm_reconciliation_marker(operation: str, attempt: str | None = None) -> str:
    suffix = f" attempt={attempt}" if attempt is not None else ""
    return f"<!-- {WARM_RECONCILIATION_MARKER} operation={operation}{suffix} -->"


def warm_readiness_id(
    repository: str,
    pr: int,
    release_operation: str,
    authorization_comment_id: int,
    goal_operation_id: str,
    attempt: int,
) -> str:
    if not 1 <= int(attempt) <= MAX_WARM_READINESS_ATTEMPTS:
        raise ApplyError("warm archive readiness attempt is out of bounds")
    material = canonical_json_bytes(
        {
            "contract": WARM_READINESS_RECEIPT_SCHEMA,
            "repository": repository,
            "pull_request": pr,
            "release_operation_id": release_operation,
            "authorization_comment_id": int(authorization_comment_id),
            "goal_operation_id": goal_operation_id,
        }
    )
    return "readiness-v2-" + digest(material)[:32] + f"-a{int(attempt):02d}"


def warm_readiness_marker(readiness_id: str) -> str:
    return f"<!-- {WARM_READINESS_MARKER} readiness={readiness_id} -->"


def warm_mount_probe_job_id(
    repository: str,
    pr: int,
    release_operation: str,
    merge_sha: str,
) -> str:
    return digest(
        canonical_json_bytes(
            {
                "contract": WARM_MOUNT_PROBE_RECEIPT_SCHEMA,
                "repository": repository,
                "pull_request": int(pr),
                "release_operation_id": release_operation,
                "deployed_sha": merge_sha,
                "unit_template": ("wb-core-storage-recovery-sanitation@.service"),
            }
        )
    )


def warm_mount_probe_marker(job_id: str) -> str:
    return f"<!-- {WARM_MOUNT_PROBE_MARKER} job={job_id} -->"


def _valid_warm_systemd_service_gate(
    service_gate: Any, *, require_healthy: bool
) -> bool:
    if not isinstance(service_gate, Mapping):
        return False
    units = service_gate.get("units")
    pairs = service_gate.get("pairs")
    resample = service_gate.get("pair_resample_evidence")
    if (
        service_gate.get("expected_unit_count") != 27
        or service_gate.get("observed_unit_count") != 27
        or service_gate.get("expected_pair_count") != 12
        or service_gate.get("observed_pair_count") != 12
        or not isinstance(units, list)
        or len(units) != 27
        or not isinstance(pairs, list)
        or len(pairs) != 12
        or not isinstance(resample, Mapping)
        or not isinstance(resample.get("samples"), list)
        or not all(
            isinstance(row, Mapping)
            and row.get("name")
            and row.get("classification")
            and "healthy" in row
            for row in units
        )
        or not all(
            isinstance(pair, Mapping)
            and pair.get("timer_name")
            and pair.get("owner_name")
            and pair.get("classification")
            and "healthy" in pair
            and "resample_required" in pair
            for pair in pairs
        )
    ):
        return False
    return bool(
        not require_healthy
        or (
            service_gate.get("healthy") is True
            and service_gate.get("failing_unit_count") == 0
            and service_gate.get("failing_pair_count") == 0
            and not service_gate.get("resample_required_pair_names")
        )
    )


def parse_release_receipt(
    comments: list[Mapping[str, Any]],
    *,
    pr: int,
    release_operation: str,
    merge_sha: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if (
            f"<!-- {RECEIPT_MARKER} operation={release_operation} -->" not in body
            or "```json" not in body
            or not is_actions_bot_comment(comment)
        ):
            continue
        try:
            payload_text = body.split("```json", 1)[1].split("```", 1)[0]
            payload = json.loads(payload_text)
        except (IndexError, json.JSONDecodeError):
            continue
        if (
            payload.get("state") == "done"
            and payload.get("operation_id") == release_operation
            and payload.get("pull_request") == pr
            and payload.get("merge_sha") == merge_sha
            and payload.get("deployed_sha") == merge_sha
            and payload.get("release_kind") == "live_runtime"
        ):
            matches.append(payload)
    if len(matches) != 1:
        raise ApplyError("exact live-runtime release receipt is missing or ambiguous")
    return matches[0]


def parse_repo_only_release_receipt(
    comments: list[Mapping[str, Any]],
    *,
    pr: int,
    release_operation: str,
    merge_sha: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    marker_text = f"<!-- {RECEIPT_MARKER} operation={release_operation} -->"
    for comment in comments:
        body = str(comment.get("body") or "")
        if marker_text not in body or not is_actions_bot_comment(comment):
            continue
        try:
            payload = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
        except (IndexError, json.JSONDecodeError) as exc:
            raise ApplyError("repo-only release receipt is malformed") from exc
        if (
            payload.get("schema") == "wb-core.release-receipt/v2"
            and payload.get("state") == "done"
            and payload.get("operation_id") == release_operation
            and payload.get("repository") == CANONICAL_REPOSITORY
            and payload.get("pull_request") == pr
            and payload.get("release_kind") == "repo_only"
            and payload.get("merge_sha") == merge_sha
            and payload.get("deployed_sha") is None
            and payload.get("manifest") is None
            and not payload.get("reason_codes")
        ):
            matches.append(dict(payload))
        else:
            raise ApplyError("repo-only release receipt binding is invalid")
    if len(matches) != 1:
        raise ApplyError("exact repo-only release receipt is missing or ambiguous")
    return matches[0]


def parse_warm_mount_probe_receipt(
    comments: list[Mapping[str, Any]],
    *,
    repository: str,
    pr: int,
    release_operation: str,
    merge_sha: str,
) -> dict[str, Any]:
    job_id = warm_mount_probe_job_id(
        repository,
        pr,
        release_operation,
        merge_sha,
    )
    marker_text = warm_mount_probe_marker(job_id)
    matches: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if marker_text not in body or not is_actions_bot_comment(comment):
            continue
        try:
            payload = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
        except (IndexError, json.JSONDecodeError) as exc:
            raise ApplyError(
                "bound warm archive mount probe receipt is malformed"
            ) from exc
        paths = payload.get("paths")
        artifact = payload.get("artifact")
        worker = payload.get("worker")
        valid = bool(
            payload.get("schema") == WARM_MOUNT_PROBE_RECEIPT_SCHEMA
            and payload.get("state") == "observed"
            and payload.get("query_only") is True
            and payload.get("database_written") is False
            and payload.get("production_probe_count") == 1
            and payload.get("job_id") == job_id
            and payload.get("repository") == repository
            and payload.get("pull_request") == pr
            and payload.get("release_operation_id") == release_operation
            and payload.get("merge_sha") == merge_sha
            and payload.get("deployed_sha") == merge_sha
            and re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(payload.get("evidence_digest") or ""),
            )
            is not None
            and isinstance(worker, Mapping)
            and worker.get("unit_template")
            == "wb-core-storage-recovery-sanitation@.service"
            and worker.get("unit_instance")
            == f"wb-core-storage-recovery-sanitation@{job_id}.service"
            and re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(worker.get("repo_template_sha256") or ""),
            )
            is not None
            and worker.get("installed_template_path")
            == "/etc/systemd/system/wb-core-storage-recovery-sanitation@.service"
            and worker.get("installed_template_sha256")
            == worker.get("repo_template_sha256")
            and worker.get("installed_template_matches_repo") is True
            and isinstance(worker.get("mount_namespace"), Mapping)
            and isinstance(paths, list)
            and len(paths) == 3
            and all(isinstance(item, Mapping) for item in paths)
            and {item.get("filesystem_role") for item in paths}
            == {"root", "backup", "generation"}
            and any(
                item.get("filesystem_role") == "backup"
                and int(item.get("raw_candidate_count") or 0) > 1
                for item in paths
            )
            and all(
                re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    str(item.get("semantic_identity_digest") or ""),
                )
                is not None
                and re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    str(item.get("raw_candidates_digest") or ""),
                )
                is not None
                and isinstance(item.get("target"), Mapping)
                for item in paths
            )
            and {
                item.get("filesystem_role"): item["target"].get("canonical_path")
                for item in paths
            }
            == {
                "root": "/opt/wb-core-runtime/backups",
                "backup": "/opt/wb-core-runtime/state/backups",
                "generation": "/opt/wb-core-runtime/state/generations",
            }
            and isinstance(artifact, Mapping)
            and artifact.get("file") == "root-warm-archive-mount-probe-receipt.json"
            and re.fullmatch(
                rf"root-warm-archive-mount-probe-pr-{pr}-run-[1-9][0-9]*",
                str(artifact.get("name") or ""),
            )
            is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("sha256") or ""))
            is not None
            and int(artifact.get("size_bytes") or 0) > 0
        )
        if not valid:
            raise ApplyError("bound warm archive mount probe receipt is invalid")
        comment_id = comment.get("id")
        created_at = str(comment.get("created_at") or "")
        if (
            not isinstance(comment_id, int)
            or comment_id <= 0
            or not created_at.endswith("Z")
        ):
            raise ApplyError(
                "bound warm archive mount probe comment identity is invalid"
            )
        matches.append(
            {
                **dict(payload),
                "comment_id": comment_id,
                "comment_created_at": created_at,
            }
        )
    if len(matches) != 1:
        raise ApplyError("exact worker mount probe receipt is missing or ambiguous")
    return matches[0]


def parse_warm_readiness_receipt(
    comments: list[Mapping[str, Any]],
    *,
    repository: str,
    pr: int,
    release_operation: str,
    merge_sha: str,
    authorization_comment_id: int,
    goal_operation_id: str,
) -> dict[str, Any]:
    mount_probe = parse_warm_mount_probe_receipt(
        comments,
        repository=repository,
        pr=pr,
        release_operation=release_operation,
        merge_sha=merge_sha,
    )
    attempts = _collect_warm_readiness_attempts(
        comments,
        repository=repository,
        pr=pr,
        release_operation=release_operation,
        merge_sha=merge_sha,
        authorization_comment_id=authorization_comment_id,
        goal_operation_id=goal_operation_id,
    )
    ready_attempts = [
        payload for payload in attempts.values() if payload.get("state") == "ready"
    ]
    if len(ready_attempts) != 1:
        raise ApplyError(
            "exact ready warm-archive readiness receipt is missing or ambiguous"
        )
    payload = ready_attempts[0]
    service_gate = payload.get("systemd_service_gate")
    expected_mount_probe_job = warm_mount_probe_job_id(
        repository,
        pr,
        release_operation,
        merge_sha,
    )
    if (
        payload.get("attempt") != max(attempts)
        or payload.get("mount_probe_job_id") != expected_mount_probe_job
        or payload.get("mount_probe_evidence_digest")
        != mount_probe.get("evidence_digest")
        or payload.get("mount_probe_artifact") != mount_probe.get("artifact")
        or payload.get("mount_probe_comment_id") != mount_probe.get("comment_id")
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(payload.get("mount_probe_evidence_digest") or ""),
        )
        is None
        or not isinstance(payload.get("mount_probe_artifact"), Mapping)
        or re.fullmatch(
            r"/opt/wb-core-runtime/state/private-evidence/root-warm-archive-readiness/"
            r"readiness-v2-[0-9a-f]{32}-a[0-9]{2}/"
            r"root-warm-archive-readiness-projection-[0-9]{8}T[0-9]{6}Z\.json",
            str(payload.get("projection_manifest_path") or ""),
        )
        is None
        or any(
            re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get(field) or "")) is None
            for field in (
                "projection_manifest_sha256",
                "material_qualification_digest",
                "immutable_non_target_digest",
                "mutable_canonical_topology_digest",
            )
        )
        or payload.get("material_partition") != "immutable_safety_v1"
        or (payload.get("mutable_safety_predicates") or {}).get("passed") is not True
        or not isinstance(payload.get("mutable_canonical_observations"), list)
        or len(payload["mutable_canonical_observations"]) < 3
        or not _valid_warm_systemd_service_gate(service_gate, require_healthy=True)
    ):
        raise ApplyError("ready warm archive sequence receipt is invalid")
    return payload


def _collect_warm_readiness_attempts(
    comments: list[Mapping[str, Any]],
    *,
    repository: str,
    pr: int,
    release_operation: str,
    merge_sha: str,
    authorization_comment_id: int,
    goal_operation_id: str,
) -> dict[int, dict[str, Any]]:
    attempts: dict[int, dict[str, Any]] = {}
    expected_mount_probe_job = warm_mount_probe_job_id(
        repository,
        pr,
        release_operation,
        merge_sha,
    )
    expected_markers = {
        attempt: warm_readiness_marker(
            warm_readiness_id(
                repository,
                pr,
                release_operation,
                authorization_comment_id,
                goal_operation_id,
                attempt,
            )
        )
        for attempt in range(1, MAX_WARM_READINESS_ATTEMPTS + 1)
    }
    for comment in comments:
        body = str(comment.get("body") or "")
        if WARM_READINESS_MARKER not in body or not is_actions_bot_comment(comment):
            continue
        if "```json" not in body:
            if any(value in body for value in expected_markers.values()):
                raise ApplyError("bound warm archive readiness receipt is malformed")
            continue
        try:
            payload = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
        except (IndexError, json.JSONDecodeError):
            if any(value in body for value in expected_markers.values()):
                raise ApplyError("bound warm archive readiness receipt is malformed")
            continue
        attempt = payload.get("attempt")
        binding_matches = bool(
            payload.get("repository") == repository
            and payload.get("pull_request") == pr
            and payload.get("release_operation_id") == release_operation
            and payload.get("authorization_comment_id") == authorization_comment_id
            and payload.get("goal_operation_id") == goal_operation_id
        )
        if not binding_matches:
            continue
        if (
            not isinstance(attempt, int)
            or not 1 <= attempt <= MAX_WARM_READINESS_ATTEMPTS
        ):
            raise ApplyError("bound warm archive readiness attempt is out of bounds")
        readiness = warm_readiness_id(
            repository,
            pr,
            release_operation,
            authorization_comment_id,
            goal_operation_id,
            attempt,
        )
        if warm_readiness_marker(readiness) not in body:
            raise ApplyError("bound warm archive readiness marker is invalid")
        common_valid = bool(
            payload.get("schema") == WARM_READINESS_RECEIPT_SCHEMA
            and payload.get("state") in {"ready", "blocked"}
            and payload.get("readiness_id") == readiness
            and payload.get("repository") == repository
            and payload.get("pull_request") == pr
            and payload.get("release_operation_id") == release_operation
            and payload.get("authorization_comment_id") == authorization_comment_id
            and payload.get("goal_operation_id") == goal_operation_id
            and payload.get("mount_probe_job_id") == expected_mount_probe_job
            and re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(payload.get("mount_probe_evidence_digest") or ""),
            )
            is not None
            and isinstance(payload.get("mount_probe_artifact"), Mapping)
            and isinstance(payload.get("mount_probe_comment_id"), int)
            and int(payload.get("mount_probe_comment_id")) > 0
            and payload.get("merge_sha") == merge_sha
            and payload.get("deployed_sha") == merge_sha
        )
        if not common_valid:
            raise ApplyError("bound warm archive readiness receipt is invalid")
        if attempt in attempts:
            raise ApplyError("duplicate warm archive readiness attempt")
        attempts[attempt] = payload
    if sorted(attempts) != list(range(1, len(attempts) + 1)):
        raise ApplyError("warm archive readiness attempt sequence is not contiguous")
    ready_numbers = [
        attempt
        for attempt, payload in attempts.items()
        if payload.get("state") == "ready"
    ]
    if len(ready_numbers) > 1 or (ready_numbers and ready_numbers[0] != max(attempts)):
        raise ApplyError("warm archive readiness terminal sequence is invalid")
    return attempts


def parse_legacy_release_receipt(
    comments: list[Mapping[str, Any]],
    *,
    pr: int,
    merge_sha: str,
    manifest_sha: str,
    operation: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if (
            f"<!-- {RECEIPT_MARKER} " not in body
            or "```json" not in body
            or not is_actions_bot_comment(comment)
        ):
            continue
        try:
            payload = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
        except (IndexError, json.JSONDecodeError):
            continue
        manifest = payload.get("manifest")
        release_operation = str(payload.get("operation_id") or "")
        if (
            payload.get("schema") == "wb-core.release-receipt/v2"
            and payload.get("state") == "awaiting_apply"
            and re.fullmatch(r"release-v2-[0-9a-f]{32}", release_operation) is not None
            and f"<!-- {RECEIPT_MARKER} operation={release_operation} -->" in body
            and payload.get("repository") == CANONICAL_REPOSITORY
            and payload.get("pull_request") == pr
            and payload.get("release_kind") == "production_mutation"
            and payload.get("merge_sha") == merge_sha
            and payload.get("deployed_sha") == merge_sha
            and not payload.get("reason_codes")
            and isinstance(manifest, Mapping)
            and manifest.get("sha256") == manifest_sha
            and manifest.get("operation_id") == operation
        ):
            matches.append(payload)
    if len(matches) != 1:
        raise ApplyError("awaiting-apply receipt is missing or ambiguous")
    return matches[0]


def validate_legacy_authorization(
    comment: Mapping[str, Any],
    *,
    pr: int,
    merge_sha: str,
    deployed_sha: str,
    manifest_sha: str,
    operation: str,
) -> None:
    if (
        operation in WBC0027_LEGACY_MANIFEST_OPERATIONS
        or manifest_sha == WBC0027_LEGACY_MANIFEST_SHA256
    ):
        raise ApplyError("superseded WBC0027 exact-manifest authorization is non-reusable")
    association = str(comment.get("author_association") or "").upper()
    if association not in {"OWNER", "MEMBER"}:
        raise ApplyError("apply authorization association is not OWNER or MEMBER")
    match = LEGACY_AUTH_RE.fullmatch(str(comment.get("body") or "").strip())
    if match is None:
        raise ApplyError("apply authorization body is not exact protocol-v2 syntax")
    expected = {
        "pr": str(pr),
        "merge": merge_sha,
        "deployed": deployed_sha,
        "manifest": manifest_sha,
        "operation": operation,
    }
    if match.groupdict() != expected:
        raise ApplyError("apply authorization binding mismatch")


def validate_authorization(
    comment: Mapping[str, Any],
    *,
    repository: str,
    pr: int,
) -> dict[str, Any]:
    association = str(comment.get("author_association") or "").upper()
    if association not in {"OWNER", "MEMBER"}:
        raise ApplyError("task authorization association is not OWNER or MEMBER")
    issue_url = str(comment.get("issue_url") or "")
    expected_suffix = f"/repos/{repository}/issues/{pr}"
    if not issue_url.endswith(expected_suffix):
        raise ApplyError("task authorization is not attached to the exact pull request")
    body = str(comment.get("body") or "").strip()
    match = AUTH_RE.fullmatch(body)
    warm_match = WARM_ARCHIVE_AUTH_RE.fullmatch(body)
    wbc0013_match = WBC0013_AUTH_RE.fullmatch(body)
    wbc0027_match = WBC0027_AUTH_RE.fullmatch(body)
    historical_cost_match = HISTORICAL_COST_AUTH_RE.fullmatch(body)
    historical_missing_match = HISTORICAL_MISSING_REPAIR_AUTH_RE.fullmatch(body)
    if (
        match is None
        and warm_match is None
        and wbc0013_match is None
        and wbc0027_match is None
        and historical_cost_match is None
        and historical_missing_match is None
    ):
        raise ApplyError("task authorization body is not exact goal-v1 syntax")
    raw = (
        match
        or warm_match
        or wbc0013_match
        or wbc0027_match
        or historical_cost_match
        or historical_missing_match
    ).groupdict()
    if raw["target"] != CANONICAL_PRODUCTION_TARGET_ID:
        raise ApplyError("task authorization target is not canonical production")
    if warm_match is not None:
        goal = {
            "contract": "wb-core.production-goal-passport/v1",
            "task": raw["task"],
            "profile": raw["profile"],
            "target_id": raw["target"],
            "expected_source_count": int(raw["sources"]),
            "expected_archive_count": int(raw["archives"]),
            "expected_manifest_count": int(raw["manifests"]),
            "expected_unlink_count": int(raw["unlinks"]),
            "expected_reclaimed_allocated_bytes": int(raw["reclaimed"]),
            "root_minimum_after_bytes": int(raw["root_minimum"]),
            "required_backup_floor_bytes": int(raw["backup_floor"]),
            "max_mutation_submits": 1,
            "max_pre_submit_regenerations": MAX_QUALIFICATION_CANDIDATES - 1,
            "reversible": True,
        }
        if (
            goal["task"] != "WBC0008"
            or goal["expected_source_count"] != 6
            or goal["expected_archive_count"] != 6
            or goal["expected_manifest_count"] != 6
            or goal["expected_unlink_count"] != 6
            or goal["root_minimum_after_bytes"] != 25 * 1024**3
            or goal["required_backup_floor_bytes"] <= 8 * 1024**3
        ):
            raise ApplyError("warm archive authorization scope is not exact block 006")
        return goal
    if wbc0013_match is not None:
        goal = {
            "contract": "wb-core.production-goal-passport/v1",
            "task": "WBC0013",
            "profile": WBC0013_GOAL_PROFILE,
            "target_id": raw["target"],
            "expected_roster_count": int(raw["roster"]),
            "expected_existing_count": int(raw["existing"]),
            "expected_owner_approved_missing_count": int(raw["missing"]),
            "expected_original_identity_count": 12,
            "expected_wb_content_identity_count": 38,
            "expected_zero_insert_count": int(raw["zero_inserts"]),
            "historical_business_date": raw["historical_date"],
            "historical_nm_id": int(raw["historical_nm"]),
            "historical_version_id": raw["historical_version"],
            "historical_event_id": raw["historical_event"],
            "expected_historical_repair_count": int(raw["historical_repairs"]),
            "max_a_submits": 1,
            "max_b_submits": 1,
            "max_pre_submit_regenerations": MAX_QUALIFICATION_CANDIDATES - 1,
            "timer_changes_allowed": False,
            "reversible": True,
        }
        if (
            goal["expected_roster_count"] != 71
            or goal["expected_existing_count"] != 21
            or goal["expected_owner_approved_missing_count"] != 50
            or goal["expected_zero_insert_count"] != 50
            or goal["historical_business_date"] != "2026-08-26"
            or goal["historical_nm_id"] != 428853741
            or goal["historical_version_id"]
            != "whfv_cb0657c384d5adebae01e585"
            or goal["historical_event_id"]
            != "ffbf_87cea959c9d600da99caa1ab68ef"
            or goal["expected_historical_repair_count"] != 1
            or goal["expected_existing_count"]
            + goal["expected_owner_approved_missing_count"]
            != goal["expected_roster_count"]
        ):
            raise ApplyError("WBC0013 authorization scope is not exact SSS017")
        return goal
    if wbc0027_match is not None:
        predecessor = {
            "pull_request": int(raw["predecessor_pr"]),
            "release_operation_id": raw["predecessor_release_operation"],
            "release_comment_id": int(raw["predecessor_release_comment"]),
            "authorization_comment_id": int(
                raw["predecessor_authorization_comment"]
            ),
            "apply_run_id": int(raw["predecessor_apply_run"]),
            "apply_comment_id": int(raw["predecessor_apply_comment"]),
            "artifact_id": WBC0027_PREDECESSOR_BINDING["artifact_id"],
            "artifact_name": WBC0027_PREDECESSOR_BINDING["artifact_name"],
            "receipt_sha256": raw["predecessor_receipt"],
            "goal_operation_id": raw["predecessor_operation"],
            "product_phase_operation_id": raw["predecessor_product_phase"],
            "economics_phase_operation_id": raw["predecessor_economics_phase"],
        }
        goal = {
            "contract": "wb-core.production-goal-passport/v1",
            "task": "WBC0027",
            "profile": WBC0027_GOAL_PROFILE,
            "target_id": raw["target"],
            "expected_product_row_count": int(raw["product_rows"]),
            "expected_product_cell_count": int(raw["product_cells"]),
            "expected_product_mismatch_count": int(raw["product_mismatches"]),
            "expected_primary_row_count": int(raw["primary_rows"]),
            "expected_primary_cell_count": int(raw["primary_cells"]),
            "expected_primary_mismatch_count": int(raw["primary_mismatches"]),
            "expected_secondary_row_count": int(raw["secondary_rows"]),
            "expected_secondary_mismatch_count": int(raw["secondary_mismatches"]),
            "special_business_date": raw["special_date"],
            "special_nm_id": int(raw["special_nm"]),
            "expected_special_cell_count": int(raw["special_cells"]),
            "evidence_blocked_date": raw["blocked_date"],
            "hard_non_target_from": raw["hard_from"],
            "expected_economics_logical_count": int(raw["economics_logical"]),
            "expected_economics_persisted_count": int(raw["economics_persisted"]),
            "expected_economics_blocked_count": int(raw["economics_blocked"]),
            "protected_nm_id": int(raw["protected_nm"]),
            "protected_unit_cost_rub": raw["protected_cost"],
            "max_product_submits": 1,
            "max_economics_submits": 1,
            "max_pre_submit_regenerations": MAX_QUALIFICATION_CANDIDATES - 1,
            "timer_changes_allowed": False,
            "reversible": True,
            "supersedes_terminal_predecessor": predecessor,
        }
        exact = {
            "expected_product_row_count": 1152,
            "expected_product_cell_count": 24192,
            "expected_product_mismatch_count": 9446,
            "expected_primary_row_count": 936,
            "expected_primary_cell_count": 19656,
            "expected_primary_mismatch_count": 7655,
            "expected_secondary_row_count": 216,
            "expected_secondary_mismatch_count": 1791,
            "special_business_date": "2026-08-21",
            "special_nm_id": 497413772,
            "expected_special_cell_count": 16,
            "evidence_blocked_date": "2026-08-15",
            "hard_non_target_from": "2026-08-30",
            "expected_economics_logical_count": 298,
            "expected_economics_persisted_count": 472,
            "expected_economics_blocked_count": 12,
            "protected_nm_id": 428853741,
            "protected_unit_cost_rub": "117.537167",
        }
        if (
            any(goal.get(key) != value for key, value in exact.items())
            or int(raw["submits"]) != 2
            or predecessor != WBC0027_PREDECESSOR_BINDING
        ):
            raise ApplyError("WBC0027 authorization scope is not exact")
        return goal
    if historical_cost_match is not None:
        goal = {
            "contract": "wb-core.production-goal-passport/v1",
            "task": "WBC0013",
            "profile": HISTORICAL_COST_GOAL_PROFILE,
            "target_id": raw["target"],
            "business_date": raw["business_date"],
            "nm_id": int(raw["nm_id"]),
            "owner_fixed_unit_cost_rub": raw["unit_cost_rub"],
            "expected_accepted_version_count": int(raw["versions"]),
            "expected_updated_ready_snapshot_count": int(raw["snapshots"]),
            "max_mutation_submits": 1,
            "max_pre_submit_regenerations": MAX_QUALIFICATION_CANDIDATES - 1,
            "timer_changes_allowed": False,
            "reversible": True,
        }
        if (
            goal["business_date"] != "2026-08-26"
            or goal["nm_id"] != 428853741
            or goal["owner_fixed_unit_cost_rub"] != "117.537167"
            or goal["expected_accepted_version_count"] != 1
            or goal["expected_updated_ready_snapshot_count"] != 1
        ):
            raise ApplyError("historical analytical cost authorization is not exact")
        return goal
    if historical_missing_match is not None:
        goal = {
            "contract": "wb-core.production-goal-passport/v1",
            "task": "WBC0010",
            "profile": HISTORICAL_MISSING_REPAIR_GOAL_PROFILE,
            "target_id": raw["target"],
            "source_operation_id": raw["source_operation"],
            "source_digest": raw["source_digest"],
            "expected_date_count": int(raw["dates"]),
            "expected_logical_target_count": int(raw["targets"]),
            "expected_updated_ready_snapshot_count": int(raw["snapshots"]),
            "expected_missing_before_count": int(raw["missing_before"]),
            "expected_missing_after_count": int(raw["missing_after"]),
            "excluded_date": raw["excluded_date"],
            "max_mutation_submits": 1,
            "max_pre_submit_regenerations": MAX_QUALIFICATION_CANDIDATES - 1,
            "timer_changes_allowed": False,
            "reversible": True,
        }
        if (
            goal["source_operation_id"]
            != "recovery_6b52b021d0d8302fdf87004487661709"
            or goal["source_digest"]
            != "sha256:510138ca43f717751ebcbc85997bc66baec3f7c65bf89c041f52943a4eb59181"
            or goal["expected_date_count"] != 7
            or goal["expected_logical_target_count"] != 1428
            or goal["expected_updated_ready_snapshot_count"] != 9
            or goal["expected_missing_before_count"] != 1302
            or goal["expected_missing_after_count"] != 0
            or goal["excluded_date"] != "2026-08-26"
        ):
            raise ApplyError("historical missing repair authorization is not exact")
        return goal
    if raw["profile"] != GOAL_PROFILE:
        raise ApplyError("task authorization profile is unsupported")
    date_from = date.fromisoformat(raw["date_from"])
    date_to = date.fromisoformat(raw["date_to"])
    date_count = (date_to - date_from).days + 1
    if date_count <= 0 or date_count > 730:
        raise ApplyError("task authorization date scope is invalid")
    goal: dict[str, Any] = {
        "contract": "wb-core.production-goal-passport/v1",
        "task": raw["task"],
        "profile": raw["profile"],
        "target_id": raw["target"],
        "date_from": raw["date_from"],
        "date_to": raw["date_to"],
        "date_count": date_count,
        "expected_inserted_capture_count": int(raw["captures"]),
        "expected_inserted_component_count": int(raw["components"]),
        "expected_inserted_finalization_count": int(raw["finalizations"]),
        "expected_full_date_count": int(raw["full_days"]),
        "expected_partial_date_count": int(raw["partial_days"]),
        "max_mutation_submits": 1,
        "max_pre_submit_regenerations": MAX_QUALIFICATION_CANDIDATES - 1,
        "reversible": True,
    }
    if goal["expected_inserted_capture_count"] != date_count:
        raise ApplyError("task authorization capture count does not match date scope")
    if goal["expected_inserted_finalization_count"] != date_count:
        raise ApplyError(
            "task authorization finalization count does not match date scope"
        )
    if (
        goal["expected_full_date_count"] + goal["expected_partial_date_count"]
        != date_count
    ):
        raise ApplyError(
            "task authorization quality partition does not match date scope"
        )
    if goal["expected_inserted_component_count"] < date_count:
        raise ApplyError("task authorization component bound is invalid")
    return goal


def operation_id(
    repository: str, pr: int, comment_id: int, goal: Mapping[str, Any]
) -> str:
    material = canonical_json_bytes(
        {
            "repository": repository,
            "pull_request": pr,
            "authorization_comment_id": comment_id,
            "goal": goal,
        }
    )
    return "production-goal-v1-" + digest(material)[:32]


def _comment_with_id(
    comments: list[Mapping[str, Any]], comment_id: int
) -> Mapping[str, Any]:
    matches = [item for item in comments if item.get("id") == int(comment_id)]
    if len(matches) != 1:
        raise ApplyError("WBC0027 predecessor comment is missing or ambiguous")
    return matches[0]


def _validate_wbc0027_predecessor_evidence(
    *,
    goal: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
    run: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    binding = goal.get("supersedes_terminal_predecessor")
    if binding != WBC0027_PREDECESSOR_BINDING:
        raise ApplyError("WBC0027 predecessor binding is not exact")

    release_comment = _comment_with_id(
        comments, int(WBC0027_PREDECESSOR_BINDING["release_comment_id"])
    )
    authorization_comment = _comment_with_id(
        comments, int(WBC0027_PREDECESSOR_BINDING["authorization_comment_id"])
    )
    apply_comment = _comment_with_id(
        comments, int(WBC0027_PREDECESSOR_BINDING["apply_comment_id"])
    )
    release_operation = str(WBC0027_PREDECESSOR_BINDING["release_operation_id"])
    old_operation = str(WBC0027_PREDECESSOR_BINDING["goal_operation_id"])
    if not (
        is_actions_bot_comment(release_comment)
        and f"<!-- {RECEIPT_MARKER} operation={release_operation} -->"
        in str(release_comment.get("body") or "")
        and str(authorization_comment.get("author_association") or "").upper()
        in {"OWNER", "MEMBER"}
        and str(authorization_comment.get("body") or "").strip()
        == WBC0027_PREDECESSOR_AUTH_BODY
        and is_actions_bot_comment(apply_comment)
        and marker(old_operation) in str(apply_comment.get("body") or "")
    ):
        raise ApplyError("WBC0027 predecessor comments are not exact immutable evidence")
    try:
        release_payload = json.loads(
            str(release_comment.get("body") or "")
            .split("```json", 1)[1]
            .split("```", 1)[0]
        )
        apply_summary = json.loads(
            str(apply_comment.get("body") or "")
            .split("```json", 1)[1]
            .split("```", 1)[0]
        )
    except (IndexError, json.JSONDecodeError) as exc:
        raise ApplyError("WBC0027 predecessor comment payload is malformed") from exc
    expected_release = {
        "state": "done",
        "operation_id": release_operation,
        "pull_request": WBC0027_PREDECESSOR_BINDING["pull_request"],
        "merge_sha": "68426769aa7dffec495eac66a8f502e2903c769b",
        "deployed_sha": "68426769aa7dffec495eac66a8f502e2903c769b",
        "release_kind": "live_runtime",
    }
    if any(release_payload.get(key) != value for key, value in expected_release.items()):
        raise ApplyError("WBC0027 predecessor release receipt binding is invalid")
    artifact = apply_summary.get("artifact")
    if not (
        apply_summary.get("schema") == APPLY_COMMENT_SUMMARY_SCHEMA
        and apply_summary.get("state") == "blocked"
        and apply_summary.get("reason")
        == "wbc0027-product-query-only-readback-not-reconciled"
        and apply_summary.get("operation_id") == old_operation
        and apply_summary.get("release_operation_id") == release_operation
        and apply_summary.get("pull_request")
        == WBC0027_PREDECESSOR_BINDING["pull_request"]
        and apply_summary.get("apply_count") == 0
        and isinstance(artifact, Mapping)
        and artifact.get("name") == WBC0027_PREDECESSOR_BINDING["artifact_name"]
        and artifact.get("sha256") == WBC0027_PREDECESSOR_BINDING["receipt_sha256"]
    ):
        raise ApplyError("WBC0027 predecessor blocked marker binding is invalid")

    validated_artifact = run.get("validated_artifact")
    evidence = receipt.get("evidence")
    predecessor_goal = dict(goal)
    predecessor_goal.pop("supersedes_terminal_predecessor", None)
    product_apply = evidence.get("product_apply") if isinstance(evidence, Mapping) else None
    product_result = (
        product_apply.get("result") if isinstance(product_apply, Mapping) else None
    )
    qualifications = (
        (evidence.get("qualification_attempts") or {}).get("product")
        if isinstance(evidence, Mapping)
        else None
    )
    if not (
        isinstance(validated_artifact, Mapping)
        and validated_artifact.get("id") == WBC0027_PREDECESSOR_BINDING["artifact_id"]
        and receipt.get("schema") == APPLY_RECEIPT_SCHEMA
        and receipt.get("state") == "blocked"
        and receipt.get("operation_id") == old_operation
        and receipt.get("pull_request") == WBC0027_PREDECESSOR_BINDING["pull_request"]
        and receipt.get("release_operation_id") == release_operation
        and receipt.get("authorization_comment_id")
        == WBC0027_PREDECESSOR_BINDING["authorization_comment_id"]
        and receipt.get("authorization_body_sha256")
        == digest(WBC0027_PREDECESSOR_AUTH_BODY.encode("utf-8"))
        and receipt.get("goal") == predecessor_goal
        and receipt.get("apply_count") == 0
        and isinstance(evidence, Mapping)
        and evidence.get("state") == "blocked"
        and evidence.get("product_state") == "not_applied"
        and evidence.get("economics_state") == "not_applied"
        and isinstance(product_result, Mapping)
        and product_result.get("error_type") == "TypeError"
        and product_result.get("error")
        == "warehouse_sync_lock() got an unexpected keyword argument 'operation'"
        and product_result.get("phase_operation_id")
        == WBC0027_PREDECESSOR_BINDING["product_phase_operation_id"]
        and product_result.get("production_mutation_submit_count") == 0
        and isinstance(qualifications, list)
        and len(qualifications) == 2
        and qualifications[0].get("material_qualification_digest")
        == qualifications[1].get("material_qualification_digest")
        and not (evidence.get("qualification_attempts") or {}).get("economics")
    ):
        raise ApplyError("WBC0027 predecessor receipt is not exact zero-submit evidence")
    return {
        "binding": dict(WBC0027_PREDECESSOR_BINDING),
        "state": "blocked",
        "apply_count": 0,
        "product_lifecycle": "missing",
        "economics_lifecycle": "missing",
        "private_manifests_reusable": False,
        "terminal_operation_reusable": False,
    }


def _collect_wbc0027_predecessor_evidence(
    client: GitHubClient, goal: Mapping[str, Any]
) -> dict[str, Any]:
    comments = list_comments(
        client, int(WBC0027_PREDECESSOR_BINDING["pull_request"])
    )
    run, receipt = _collect_recovery_receipt(
        client,
        pr=int(WBC0027_PREDECESSOR_BINDING["pull_request"]),
        run_id=int(WBC0027_PREDECESSOR_BINDING["apply_run_id"]),
        artifact_name=str(WBC0027_PREDECESSOR_BINDING["artifact_name"]),
        receipt_sha256=str(WBC0027_PREDECESSOR_BINDING["receipt_sha256"]).removeprefix(
            "sha256:"
        ),
        expected_conclusion="success",
    )
    return _validate_wbc0027_predecessor_evidence(
        goal=goal,
        comments=comments,
        run=run,
        receipt=receipt,
    )


def _canonical_target() -> dict[str, Any]:
    payload = json.loads(TARGET_FILE.read_text(encoding="utf-8"))
    expected = {
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
        "target_status": "active",
        "target_role": "primary_live",
        "target_lifecycle": "current_live",
        "ssh_destination": "wb-core-eu-root",
        "target_dir": "/opt/wb-core-runtime/app",
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ApplyError(f"canonical production target mismatch: {field}")
    return payload


def _ssh_command() -> list[str]:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=20",
    ]
    identity = os.environ.get("WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE", "").strip()
    if identity:
        command.extend(["-i", identity])
    options = os.environ.get("WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS", "").strip()
    if options:
        command.extend(shlex.split(options))
    return command


def _remote_command(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    goal: Mapping[str, Any],
    operation: str,
    evidence_dir: str,
    mode: str,
    manifest_path: str = "",
    manifest_sha256: str = "",
    approval_reference: str = "",
    projection_manifest_path: str = "",
    projection_manifest_sha256: str = "",
) -> list[str]:
    if mode not in {"dry-run", "apply", "readback"}:
        raise ApplyError("unsupported remote production-goal mode")
    target_dir = str(target["target_dir"])
    warm_archive = goal["profile"] == WARM_ARCHIVE_GOAL_PROFILE
    if warm_archive:
        parts = [
            "python3",
            f"{target_dir}/apps/root_storage_warm_archive.py",
            "--runtime-dir",
            "/opt/wb-core-runtime/state",
            "--root-backups",
            "/opt/wb-core-runtime/backups",
            "--deployed-sha",
            merge_sha,
            "--deployed-sha-file",
            f"{target_dir}/.wb-core-runtime-sha",
            "--evidence-dir",
            evidence_dir,
            "--operation-id",
            operation,
        ]
    else:
        parts = [
            "python3",
            f"{target_dir}/apps/sheet_vitrina_v1_inventory_history_backfill.py",
            "--runtime-dir",
            "/opt/wb-core-runtime/state",
            "--evidence-dir",
            evidence_dir,
            "--deployed-sha",
            merge_sha,
            "--deployed-sha-file",
            f"{target_dir}/.wb-core-runtime-sha",
            "--date-from",
            str(goal["date_from"]),
            "--date-to",
            str(goal["date_to"]),
        ]
    if mode in {"apply", "readback"}:
        normalized_manifest_path = posixpath.normpath(manifest_path)
        normalized_evidence_dir = posixpath.normpath(evidence_dir)
        if (
            normalized_manifest_path != manifest_path
            or posixpath.dirname(normalized_manifest_path) != normalized_evidence_dir
            or re.fullmatch(
                (
                    r"root-warm-archive-plan-[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?\.json"
                    if warm_archive
                    else r"inventory-history-backfill-plan-[0-9]{8}T[0-9]{6}Z\.json"
                ),
                posixpath.basename(normalized_manifest_path),
            )
            is None
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha256)
        ):
            raise ApplyError(
                "remote manifest binding escapes authorized evidence scope"
            )
        if not warm_archive:
            parts.extend(
                [
                    "--manifest",
                    manifest_path,
                    "--manifest-sha256",
                    manifest_sha256,
                ]
            )
    if mode == "dry-run" and warm_archive:
        if (
            re.fullmatch(
                r"/opt/wb-core-runtime/state/private-evidence/root-warm-archive-readiness/"
                r"readiness-v2-[0-9a-f]{32}-a[0-9]{2}/"
                r"root-warm-archive-readiness-projection-[0-9]{8}T[0-9]{6}Z\.json",
                projection_manifest_path,
            )
            is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", projection_manifest_sha256) is None
        ):
            raise ApplyError("warm archive dry-run lacks exact ready projection")
        parts.extend(
            [
                "dry-run",
                "--projection-manifest",
                projection_manifest_path,
                "--projection-manifest-sha256",
                projection_manifest_sha256,
            ]
        )
    if mode == "apply":
        if not approval_reference or len(approval_reference) > 500:
            raise ApplyError("task authorization reference is invalid")
        if warm_archive:
            job_id = digest(
                canonical_json_bytes(
                    {
                        "contract": "root-warm-archive-job-v1",
                        "operation": operation,
                        "manifest_sha256": manifest_sha256,
                        "deployed_sha": merge_sha,
                    }
                )
            )
            parts = [
                "python3",
                f"{target_dir}/apps/storage_recovery_sanitation_job.py",
                "--runtime-dir",
                "/opt/wb-core-runtime/state",
                "--root-backups",
                "/opt/wb-core-runtime/backups",
                "--deployed-sha-file",
                f"{target_dir}/.wb-core-runtime-sha",
                "submit",
                "--job-id",
                job_id,
                "--deployed-sha",
                merge_sha,
                "--operation",
                "warm-archive-apply",
                "--manifest",
                manifest_path,
                "--manifest-sha256",
                manifest_sha256,
                "--goal-operation-id",
                operation,
                "--approval-reference",
                approval_reference,
            ]
        else:
            parts.extend(["--apply", "--approval-reference", approval_reference])
    elif mode == "readback":
        if warm_archive:
            job_id = digest(
                canonical_json_bytes(
                    {
                        "contract": "root-warm-archive-job-v1",
                        "operation": operation,
                        "manifest_sha256": manifest_sha256,
                        "deployed_sha": merge_sha,
                    }
                )
            )
            parts.extend(
                [
                    "readback",
                    "--manifest",
                    manifest_path,
                    "--manifest-sha256",
                    manifest_sha256,
                    "--job-id",
                    job_id,
                    "--wait-seconds",
                    "43200",
                ]
            )
        else:
            parts.append("--readback")
    elif warm_archive and mode != "dry-run":
        parts.append("dry-run")
    evidence_setup = (
        "install -d -m 0700 " + shlex.quote(evidence_dir)
        if mode == "dry-run"
        else "test -d "
        + shlex.quote(evidence_dir)
        + ' && test "$(stat -c %a '
        + shlex.quote(evidence_dir)
        + ')" = 700'
    )
    shell = (
        "set -eu; umask 077; "
        + evidence_setup
        + "; cd "
        + shlex.quote(target_dir)
        + "; "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def _warm_readiness_remote_command(
    *, target: Mapping[str, Any], merge_sha: str, readiness_id: str
) -> list[str]:
    if re.fullmatch(r"readiness-v2-[0-9a-f]{32}-a[0-9]{2}", readiness_id) is None:
        raise ApplyError("warm archive readiness id is invalid")
    target_dir = str(target["target_dir"])
    evidence_dir = (
        "/opt/wb-core-runtime/state/private-evidence/root-warm-archive-readiness/"
        + readiness_id
    )
    parts = [
        "python3",
        f"{target_dir}/apps/root_storage_warm_archive.py",
        "--runtime-dir",
        "/opt/wb-core-runtime/state",
        "--root-backups",
        "/opt/wb-core-runtime/backups",
        "--deployed-sha",
        merge_sha,
        "--deployed-sha-file",
        f"{target_dir}/.wb-core-runtime-sha",
        "--evidence-dir",
        evidence_dir,
        "readiness",
        "--readiness-id",
        readiness_id,
    ]
    shell = (
        "set -eu; umask 077; install -d -m 0700 "
        + shlex.quote(evidence_dir)
        + "; cd "
        + shlex.quote(target_dir)
        + "; "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def _warm_mount_probe_submit_remote_command(
    *, target: Mapping[str, Any], merge_sha: str, job_id: str
) -> list[str]:
    if re.fullmatch(r"[0-9a-f]{64}", job_id) is None:
        raise ApplyError("warm archive mount probe job id is invalid")
    target_dir = str(target["target_dir"])
    parts = [
        "python3",
        f"{target_dir}/apps/storage_recovery_sanitation_job.py",
        "--runtime-dir",
        "/opt/wb-core-runtime/state",
        "--root-backups",
        "/opt/wb-core-runtime/backups",
        "--deployed-sha-file",
        f"{target_dir}/.wb-core-runtime-sha",
        "submit",
        "--job-id",
        job_id,
        "--deployed-sha",
        merge_sha,
        "--operation",
        "warm-archive-mount-probe",
    ]
    shell = (
        "set -eu; umask 077; cd "
        + shlex.quote(target_dir)
        + "; "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def _warm_mount_probe_status_remote_command(
    *, target: Mapping[str, Any], merge_sha: str, job_id: str
) -> list[str]:
    if re.fullmatch(r"[0-9a-f]{64}", job_id) is None:
        raise ApplyError("warm archive mount probe job id is invalid")
    target_dir = str(target["target_dir"])
    parts = [
        "python3",
        f"{target_dir}/apps/storage_recovery_sanitation_job.py",
        "--runtime-dir",
        "/opt/wb-core-runtime/state",
        "--root-backups",
        "/opt/wb-core-runtime/backups",
        "--deployed-sha-file",
        f"{target_dir}/.wb-core-runtime-sha",
        "status",
        "--job-id",
        job_id,
        "--deployed-sha",
        merge_sha,
    ]
    shell = (
        "set -eu; umask 077; cd "
        + shlex.quote(target_dir)
        + "; "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def command_evidence(
    command: list[str], *, timeout_seconds: float = 3600.0
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "command_sha256": digest(canonical_json_bytes(command)),
            "return_code": None,
            "transport_ambiguous": True,
            "error": type(exc).__name__,
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    return {
        "command_sha256": digest(canonical_json_bytes(command)),
        "return_code": result.returncode,
        "stdout_sha256": digest(result.stdout.encode("utf-8")),
        "stderr_sha256": digest(result.stderr.encode("utf-8")),
        "transport_ambiguous": False,
        "result": payload if isinstance(payload, Mapping) else None,
    }


def _activity_receipt_summary(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    result = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        hold = item.get("hold_evidence") or {}
        provenance = item.get("provenance") or {}
        result.append(
            {
                "gate": item.get("gate"),
                "source_path": item.get("source_path"),
                "classification": item.get("classification"),
                "identity_before": item.get("identity_before"),
                "identity_after": item.get("identity_after"),
                "identity_matches_expected": item.get("identity_matches_expected"),
                "sha256_verified": item.get("sha256_verified"),
                "sha256_matches_expected": item.get("sha256_matches_expected"),
                "material_stable_during_gate": item.get("material_stable_during_gate"),
                "sidecars": item.get("sidecars"),
                "fd_openers": item.get("fd_openers"),
                "kernel_locks": item.get("kernel_locks"),
                "hold_evidence": {
                    "classification": hold.get("classification"),
                    "marker_paths": hold.get("marker_paths"),
                    "hold_xattr_names": hold.get("hold_xattr_names"),
                },
                "provenance": {
                    "digest": provenance.get("digest"),
                    "error": item.get("provenance_error"),
                    "matches_expected": item.get("provenance_matches_expected"),
                },
                "related_process_observations": item.get(
                    "related_process_observations"
                ),
                "blockers": item.get("blockers"),
            }
        )
    return result


def _readiness_callback_summary(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    result = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        evidence = (
            item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
        )
        service_gate = item.get("systemd_service_gate") or evidence.get(
            "systemd_service_gate"
        )
        service_gate_summary = (
            {
                "classification": service_gate.get("classification"),
                "failing_unit_count": service_gate.get("failing_unit_count"),
                "failing_units": service_gate.get("failing_units"),
                "failing_pair_count": service_gate.get("failing_pair_count"),
                "failing_pairs": service_gate.get("failing_pairs"),
                "pair_resample_summary": (
                    {
                        key: service_gate["pair_resample_evidence"].get(key)
                        for key in (
                            "attempted",
                            "attempt_count",
                            "resolved_healthy",
                            "remaining_resample_required_pair_names",
                        )
                    }
                    if isinstance(service_gate.get("pair_resample_evidence"), Mapping)
                    else None
                ),
            }
            if isinstance(service_gate, Mapping)
            else None
        )
        result.append(
            {
                "message": item.get("message"),
                "source_path": item.get("source_path"),
                "classification": item.get("classification"),
                "blockers": item.get("blockers"),
                "fd_openers": item.get("fd_openers"),
                "kernel_locks": item.get("kernel_locks"),
                "systemd_service_gate": service_gate_summary,
            }
        )
    return result


def _material_component_diff_summary(
    before_rows: Any, after_rows: Any
) -> dict[str, Any]:
    def indexed(rows: Any) -> dict[str, Mapping[str, Any]]:
        if not isinstance(rows, list):
            return {}
        return {
            str(item.get("json_path") or ""): item
            for item in rows
            if isinstance(item, Mapping) and item.get("json_path")
        }

    before = indexed(before_rows)
    after = indexed(after_rows)
    changed = []
    for path in sorted(set(before) | set(after)):
        earlier = before.get(path)
        later = after.get(path)
        if (earlier or {}).get("digest") == (later or {}).get("digest"):
            continue
        if (earlier or later or {}).get("cas_role") == "observation_only":
            continue
        changed.append(
            {
                "json_path": path,
                "classification": (earlier or later or {}).get("classification"),
                "before_component_digest": (earlier or {}).get("digest"),
                "after_component_digest": (later or {}).get("digest"),
                "before_safe_evidence": (earlier or {}).get("safe_evidence"),
                "after_safe_evidence": (later or {}).get("safe_evidence"),
            }
        )
    return {
        "schema": "wb-core.root-warm-archive-material-cas-diff/v1",
        "changed_component_count": len(changed),
        "changed_json_paths": [item["json_path"] for item in changed],
        "components": changed,
    }


def _validate_candidate(
    payload: Mapping[str, Any],
    goal: Mapping[str, Any],
    *,
    warm_readiness: Mapping[str, Any] | None = None,
) -> None:
    if goal["profile"] == WARM_ARCHIVE_GOAL_PROFILE:
        expected = {
            "status": "ready",
            "source_count": goal["expected_source_count"],
            "expected_unlink_count": goal["expected_unlink_count"],
            "expected_reclaimed_allocated_bytes": goal[
                "expected_reclaimed_allocated_bytes"
            ],
            "root_minimum_after_bytes": goal["root_minimum_after_bytes"],
            "capacity_guard_passed": True,
            "openers_count": 0,
            "locks_count": 0,
            "holds_count": 0,
        }
        for field, value in expected.items():
            if payload.get(field) != value:
                raise ApplyError(f"dynamic manifest escaped authorized goal: {field}")
        if int(payload.get("required_backup_floor_bytes") or 0) < int(
            goal["required_backup_floor_bytes"]
        ):
            raise ApplyError("dynamic warm archive backup floor weakened owner floor")
        if payload.get("database_written") is not False:
            raise ApplyError("warm archive qualification unexpectedly wrote data")
        for field in (
            "manifest_sha256",
            "material_qualification_digest",
            "immutable_non_target_digest",
            "mutable_canonical_topology_digest",
        ):
            if (
                re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get(field) or ""))
                is None
            ):
                raise ApplyError(f"dynamic warm archive digest is invalid: {field}")
        if (
            not isinstance(warm_readiness, Mapping)
            or payload.get("readiness_id") != warm_readiness.get("readiness_id")
            or payload.get("projection_manifest_path")
            != warm_readiness.get("projection_manifest_path")
            or payload.get("projection_manifest_sha256")
            != warm_readiness.get("projection_manifest_sha256")
            or payload.get("material_qualification_digest")
            != warm_readiness.get("material_qualification_digest")
            or payload.get("immutable_non_target_digest")
            != warm_readiness.get("immutable_non_target_digest")
            or payload.get("mutable_canonical_topology_digest")
            != warm_readiness.get("mutable_canonical_topology_digest")
            or payload.get("material_partition") != "immutable_safety_v1"
            or (payload.get("mutable_safety_predicates") or {}).get("passed")
            is not True
            or not isinstance(payload.get("material_cas_components"), list)
            or not payload.get("material_cas_components")
            or not isinstance(payload.get("activity_evidence"), list)
            or len(payload["activity_evidence"]) != goal["expected_source_count"]
            or not isinstance(payload.get("mutable_canonical_observations"), list)
            or len(payload["mutable_canonical_observations"]) < 3
        ):
            raise ApplyError("dynamic warm archive readiness binding is invalid")
        return
    expected = {
        "status": "ready",
        "date_from": goal["date_from"],
        "date_to": goal["date_to"],
        "date_count": goal["date_count"],
        "inserted_capture_count": goal["expected_inserted_capture_count"],
        "inserted_component_count": goal["expected_inserted_component_count"],
        "inserted_finalization_count": goal["expected_inserted_finalization_count"],
        "full_date_count": goal["expected_full_date_count"],
        "partial_date_count": goal["expected_partial_date_count"],
        "unavailable_date_count": 0,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ApplyError(f"dynamic manifest escaped authorized goal: {field}")
    if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("deployed_sha") or "")):
        raise ApplyError("dynamic manifest deployed SHA is invalid")
    if not re.fullmatch(
        r"sha256:[0-9a-f]{64}", str(payload.get("manifest_sha256") or "")
    ):
        raise ApplyError("dynamic manifest digest is invalid")
    if not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        str(payload.get("material_qualification_digest") or ""),
    ):
        raise ApplyError("dynamic material qualification digest is invalid")


def _wbc0013_remote_command(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    operation: str,
    evidence_dir: str,
    phase: str,
    manifest_path: str = "",
    manifest_sha256: str = "",
    approval_reference: str = "",
) -> list[str]:
    allowed = {"plan-a", "apply-a", "readback-a", "plan-b", "apply-b", "readback-b"}
    if phase not in allowed:
        raise ApplyError("unsupported WBC0013 recovery phase")
    target_dir = str(target["target_dir"])
    parts = [
        "python3",
        f"{target_dir}/apps/wbc0013_fbs_recovery.py",
        phase,
        "--runtime-dir",
        "/opt/wb-core-runtime/state",
        "--target-file",
        f"{target_dir}/artifacts/registry_upload_http_entrypoint/input/"
        "hosted_runtime_target__europe_api.json",
        "--deployed-sha",
        merge_sha,
        "--evidence-dir",
        evidence_dir,
        "--operation-id",
        operation,
    ]
    if phase.startswith("apply-"):
        if (
            posixpath.dirname(posixpath.normpath(manifest_path)) != evidence_dir
            or posixpath.normpath(manifest_path) != manifest_path
            or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha256) is None
            or not approval_reference
            or len(approval_reference) > 500
        ):
            raise ApplyError("WBC0013 reviewed manifest binding is invalid")
        parts.extend(
            [
                "--manifest",
                manifest_path,
                "--manifest-sha256",
                manifest_sha256,
                "--approval-reference",
                approval_reference,
            ]
        )
    setup = (
        "install -d -m 0700 " + shlex.quote(evidence_dir)
        if phase == "plan-a"
        else "test -d "
        + shlex.quote(evidence_dir)
        + ' && test "$(stat -c %a '
        + shlex.quote(evidence_dir)
        + ')" = 700'
    )
    shell = (
        "set -eu; umask 077; "
        + setup
        + "; cd "
        + shlex.quote(target_dir)
        + "; export PYTHONPATH="
        + shlex.quote(target_dir)
        + "; "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def _wbc0027_remote_command(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    operation: str,
    evidence_dir: str,
    action: str,
    phase: str,
    product_phase_operation_id: str = "",
    manifest_path: str = "",
    manifest_sha256: str = "",
    phase_operation_id: str = "",
    phase_fingerprint: str = "",
    approval_reference: str = "",
) -> list[str]:
    if action not in {"plan", "apply", "readback"} or phase not in {
        "product",
        "economics",
    }:
        raise ApplyError("unsupported WBC0027 recovery action")
    if re.fullmatch(r"production-goal-v1-[0-9a-f]{32}", operation) is None:
        raise ApplyError("WBC0027 goal operation namespace is invalid")
    target_dir = str(target["target_dir"])
    parts = [
        "python3",
        f"{target_dir}/apps/wbc0027_capital_recovery.py",
        "--runtime-dir",
        "/opt/wb-core-runtime/state",
        "--deployed-sha-file",
        f"{target_dir}/.wb-core-runtime-sha",
        "--expected-deployed-sha",
        merge_sha,
        "--profile",
        WBC0027_GOAL_PROFILE,
        "--target-id",
        CANONICAL_PRODUCTION_TARGET_ID,
        "--operation-id",
        operation,
        "--evidence-dir",
        evidence_dir,
        action,
        "--phase",
        phase,
    ]
    if action == "plan" and phase == "economics":
        if re.fullmatch(r"recovery_[0-9a-f]{32}", product_phase_operation_id) is None:
            raise ApplyError("WBC0027 economics predecessor identity is invalid")
        parts.extend(
            ["--product-phase-operation-id", product_phase_operation_id]
        )
    if action in {"apply", "readback"}:
        if (
            posixpath.normpath(manifest_path) != manifest_path
            or posixpath.dirname(manifest_path) != evidence_dir
            or re.fullmatch(
                rf"{re.escape(evidence_dir)}/wbc0027-{phase}-plan-"
                r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}\.json",
                manifest_path,
            )
            is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha256) is None
            or re.fullmatch(r"recovery_[0-9a-f]{32}", phase_operation_id) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", phase_fingerprint) is None
        ):
            raise ApplyError("WBC0027 reviewed phase bindings are invalid")
        parts.extend(
            [
                "--manifest",
                manifest_path,
                "--manifest-sha256",
                manifest_sha256,
                "--phase-operation-id",
                phase_operation_id,
                "--phase-fingerprint",
                phase_fingerprint,
            ]
        )
        if action == "apply":
            if not approval_reference or len(approval_reference) > 500:
                raise ApplyError("WBC0027 approval reference is invalid")
            parts.extend(["--approval-reference", approval_reference])
    setup = (
        "install -d -m 0700 " + shlex.quote(evidence_dir)
        if action == "plan" and phase == "product"
        else "test -d "
        + shlex.quote(evidence_dir)
        + ' && test "$(stat -c %a '
        + shlex.quote(evidence_dir)
        + ')" = 700'
    )
    shell = (
        "set -eu; umask 077; "
        + setup
        + "; cd "
        + shlex.quote(target_dir)
        + "; export PYTHONPATH="
        + shlex.quote(target_dir)
        + "; "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def _validate_wbc0027_candidate(
    payload: Mapping[str, Any],
    goal: Mapping[str, Any],
    *,
    phase: str,
    merge_sha: str,
    operation: str,
    evidence_dir: str,
    product_phase_operation_id: str = "",
) -> None:
    expected = {
        "status": "ready",
        "phase": phase,
        "profile": WBC0027_GOAL_PROFILE,
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
        "goal_operation_id": operation,
        "deployed_sha": merge_sha,
        "query_only": True,
        "database_written": False,
        "production_mutation_count": 0,
        "legacy_release_operation_reusable": False,
        "legacy_phase_operation_reusable": False,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ApplyError(f"WBC0027 {phase} candidate escaped goal: {field}")
    for field in (
        "manifest_sha256",
        "material_qualification_digest",
        "phase_fingerprint",
    ):
        value = payload.get(field)
        if (
            not isinstance(value, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
        ):
            raise ApplyError(f"WBC0027 {phase} candidate digest is invalid: {field}")
    phase_operation_id = payload.get("phase_operation_id")
    if not isinstance(phase_operation_id, str) or re.fullmatch(
        r"recovery_[0-9a-f]{32}", phase_operation_id
    ) is None:
        raise ApplyError("WBC0027 phase operation identity is invalid")
    generation = payload.get("storage_generation")
    generation_id = (
        generation.get("generation_id") if isinstance(generation, Mapping) else None
    )
    schema_revision = (
        generation.get("schema_revision") if isinstance(generation, Mapping) else None
    )
    manifest_sha256 = (
        generation.get("manifest_sha256") if isinstance(generation, Mapping) else None
    )
    if not (
        isinstance(generation, Mapping)
        and isinstance(generation_id, str)
        and bool(generation_id)
        and generation_id.strip() == generation_id
        and isinstance(schema_revision, str)
        and bool(schema_revision)
        and schema_revision.strip() == schema_revision
        and isinstance(manifest_sha256, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha256)
    ):
        raise ApplyError("WBC0027 StoreRegistry generation binding is invalid")
    manifest_path = str(payload.get("manifest_path") or "")
    if (
        posixpath.dirname(manifest_path) != evidence_dir
        or re.fullmatch(
            rf"{re.escape(evidence_dir)}/wbc0027-{phase}-plan-"
            r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}\.json",
            manifest_path,
        )
        is None
    ):
        raise ApplyError("WBC0027 private manifest path is invalid")
    persistence = payload.get("plan_persistence")
    admission = persistence.get("root_storage_admission") if isinstance(persistence, Mapping) else None
    size = persistence.get("size_bytes") if isinstance(persistence, Mapping) else None
    maximum = persistence.get("max_size_bytes") if isinstance(persistence, Mapping) else None
    if not (
        isinstance(persistence, Mapping)
        and persistence.get("owner") == "production_apply_evidence"
        and persistence.get("destination") == manifest_path
        and persistence.get("evidence_dir") == evidence_dir
        and persistence.get("evidence_dir_mode") == "0700"
        and persistence.get("file_mode") == "0600"
        and persistence.get("parent_mode") == "0700"
        and isinstance(size, int)
        and not isinstance(size, bool)
        and isinstance(maximum, int)
        and not isinstance(maximum, bool)
        and 0 < size <= maximum <= 64_000_000
        and persistence.get("bounded_size") is True
        and persistence.get("atomic_publish") is True
        and persistence.get("no_overwrite") is True
        and persistence.get("durable_file_fsync") is True
        and persistence.get("durable_directory_fsync") is True
        and persistence.get("no_create") is False
        and isinstance(admission, Mapping)
        and admission.get("owner") == "production_apply_evidence"
        and admission.get("destination") == manifest_path
        and admission.get("destination_role") == "backup"
        and admission.get("predicted_output_bytes") == size
        and admission.get("allowed") is True
    ):
        raise ApplyError("WBC0027 private plan persistence receipt is invalid")
    if phase == "product":
        counts = payload.get("product_counts")
        count_bindings = {
            "product_row_count": goal["expected_product_row_count"],
            "product_cell_count": goal["expected_product_cell_count"],
            "product_mismatch_count": goal["expected_product_mismatch_count"],
            "primary_row_count": goal["expected_primary_row_count"],
            "primary_cell_count": goal["expected_primary_cell_count"],
            "primary_mismatch_count": goal["expected_primary_mismatch_count"],
            "secondary_row_count": goal["expected_secondary_row_count"],
            "secondary_mismatch_count": goal["expected_secondary_mismatch_count"],
        }
        special = payload.get("special_20260821")
        hard_non_target = payload.get("hard_non_target")
        evidence_blocked = payload.get("evidence_blocked")
        if not isinstance(counts, Mapping) or any(
            not isinstance(counts.get(key), int)
            or isinstance(counts.get(key), bool)
            or counts.get(key) != value
            for key, value in count_bindings.items()
        ) or not (
            isinstance(special, Mapping)
            and isinstance(hard_non_target, Mapping)
            and isinstance(evidence_blocked, list)
            and bool(evidence_blocked)
            and special.get("as_of_date") == goal["special_business_date"]
            and special.get("nm_id") == goal["special_nm_id"]
            and special.get("cell_count") == goal["expected_special_cell_count"]
            and hard_non_target.get("from_date")
            == goal["hard_non_target_from"]
            and isinstance(evidence_blocked[0], Mapping)
            and evidence_blocked[0].get("as_of_date")
            == goal["evidence_blocked_date"]
        ):
            raise ApplyError("WBC0027 product qualification is not exact")
    else:
        evidence_blocked = payload.get("evidence_blocked")
        protected = payload.get("protected_invariant")
        if not (
            isinstance(payload.get("logical_repair_count"), int)
            and not isinstance(payload.get("logical_repair_count"), bool)
            and payload.get("logical_repair_count")
            == goal["expected_economics_logical_count"]
            and isinstance(payload.get("persisted_repair_count"), int)
            and not isinstance(payload.get("persisted_repair_count"), bool)
            and payload.get("persisted_repair_count")
            == goal["expected_economics_persisted_count"]
            and isinstance(evidence_blocked, list)
            and len(evidence_blocked)
            == goal["expected_economics_blocked_count"]
            and isinstance(protected, Mapping)
            and protected.get("nm_id")
            == goal["protected_nm_id"]
            and protected.get("unit_cost_rub")
            == goal["protected_unit_cost_rub"]
            and payload.get("product_phase_operation_id")
            == product_phase_operation_id
            and (payload.get("product_predecessor") or {}).get("reconciled") is True
        ):
            raise ApplyError("WBC0027 economics qualification is not exact")


def _validate_wbc0013_candidate(
    payload: Mapping[str, Any],
    goal: Mapping[str, Any],
    *,
    phase: str,
    merge_sha: str,
) -> None:
    common = {
        "status": "ready",
        "phase": phase,
        "deployed_sha": merge_sha,
        "query_only": True,
        "database_written": False,
        "file_mode": "0600",
        "barrier_inactive": True,
        "target_generation_bound": True,
        "timer_change_count": 0,
    }
    expected = (
        {
            **common,
            "roster_count": goal["expected_roster_count"],
            "existing_count": goal["expected_existing_count"],
            "owner_approved_missing_count": goal[
                "expected_owner_approved_missing_count"
            ],
            "original_identity_count": goal["expected_original_identity_count"],
            "wb_content_identity_count": goal[
                "expected_wb_content_identity_count"
            ],
            "zero_insert_count": goal["expected_zero_insert_count"],
        }
        if phase == "a"
        else {
            **common,
            "historical_repair_count": goal["expected_historical_repair_count"],
            "business_date": goal["historical_business_date"],
            "nm_id": goal["historical_nm_id"],
            "accepted_version_id": goal["historical_version_id"],
            "event_id": goal["historical_event_id"],
            "current_active_preserved": True,
            "current_sync_preserved": True,
            "current_pool_preserved": True,
        }
    )
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ApplyError(f"WBC0013 {phase} qualification escaped goal: {field}")
    if phase == "b":
        if (
            payload.get("exact_target_count") != 1
            or payload.get("ready_shape_candidate_count") != 1
            or payload.get("causal_event_count") != 1
            or payload.get("broad_mismatch_query_performed") is not False
            or not str(payload.get("selection_predicate") or "").startswith(
                "historical_b."
            )
        ):
            raise ApplyError("WBC0013 B bounded selector cardinality is invalid")
        for field in (
            "ready_shape_candidate_digest",
            "causal_event_candidate_digest",
            "selection_details_digest",
            "mismatch_classification_digest",
        ):
            if (
                re.fullmatch(
                    r"sha256:[0-9a-f]{64}", str(payload.get(field) or "")
                )
                is None
            ):
                raise ApplyError(f"WBC0013 B selector digest is invalid: {field}")
    for field in ("manifest_sha256", "material_qualification_digest"):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get(field) or "")) is None:
            raise ApplyError(
                f"WBC0013 {phase} qualification digest is invalid: {field}"
            )
    manifest_path = str(payload.get("manifest_path") or "")
    if (
        re.fullmatch(
            r"/.+/production-goals/production-goal-v1-[0-9a-f]{32}/"
            rf"wbc0013-{phase}-plan-[0-9]{{8}}T[0-9]{{6}}Z\.json",
            manifest_path,
        )
        is None
    ):
        raise ApplyError(f"WBC0013 {phase} private manifest path is invalid")
    persistence = payload.get("plan_persistence")
    admission = (
        persistence.get("root_storage_admission")
        if isinstance(persistence, Mapping)
        else None
    )
    manifest_size = (
        persistence.get("size_bytes") if isinstance(persistence, Mapping) else None
    )
    maximum_size = (
        persistence.get("max_size_bytes") if isinstance(persistence, Mapping) else None
    )
    if not (
        isinstance(persistence, Mapping)
        and persistence.get("owner") == "production_apply_evidence"
        and persistence.get("destination") == manifest_path
        and persistence.get("evidence_dir") == posixpath.dirname(manifest_path)
        and persistence.get("evidence_dir_mode") == "0700"
        and persistence.get("file_mode") == "0600"
        and persistence.get("parent_mode") == "0700"
        and isinstance(manifest_size, int)
        and not isinstance(manifest_size, bool)
        and isinstance(maximum_size, int)
        and not isinstance(maximum_size, bool)
        and 0 < manifest_size <= maximum_size <= 12_000_000
        and persistence.get("bounded_size") is True
        and persistence.get("atomic_publish") is True
        and persistence.get("no_overwrite") is True
        and persistence.get("durable_file_fsync") is True
        and persistence.get("durable_directory_fsync") is True
        and isinstance(admission, Mapping)
        and admission.get("owner") == "production_apply_evidence"
        and admission.get("destination") == manifest_path
        and admission.get("destination_role") == "backup"
        and admission.get("predicted_output_bytes") == manifest_size
        and admission.get("allowed") is True
    ):
        raise ApplyError(f"WBC0013 {phase} private plan persistence receipt is invalid")


def _wbc0013_typed_failure(
    evidence: Mapping[str, Any], *, phase: str, stage: str
) -> dict[str, Any]:
    payload = evidence.get("result")
    if isinstance(payload, Mapping) and payload.get("status") == "error":
        candidate = {
            "phase": str(payload.get("phase") or ""),
            "stage": str(payload.get("stage") or ""),
            "code": str(payload.get("code") or ""),
            "message": str(payload.get("message") or ""),
            "predicate": str(payload.get("predicate") or ""),
            "expected_cardinality": payload.get("expected_cardinality"),
            "observed_cardinality": payload.get("observed_cardinality"),
            "candidate_digest": str(payload.get("candidate_digest") or ""),
            "details_digest": str(payload.get("details_digest") or ""),
        }
        if (
            candidate["phase"] == phase
            and candidate["stage"] == stage
            and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}", candidate["code"])
            and 0 < len(candidate["message"]) <= 500
            and re.fullmatch(
                r"[a-zA-Z0-9_.:-]{1,200}", candidate["predicate"]
            )
            and isinstance(candidate["expected_cardinality"], int)
            and int(candidate["expected_cardinality"]) >= 0
            and isinstance(candidate["observed_cardinality"], int)
            and int(candidate["observed_cardinality"]) >= 0
            and re.fullmatch(
                r"sha256:[0-9a-f]{64}", candidate["candidate_digest"]
            )
            and re.fullmatch(
                r"sha256:[0-9a-f]{64}", candidate["details_digest"]
            )
        ):
            return candidate
    transport = bool(evidence.get("transport_ambiguous"))
    return {
        "phase": phase,
        "stage": stage,
        "code": (
            "transport_ambiguous" if transport else "typed_failure_payload_invalid"
        ),
        "message": (
            "command transport outcome is ambiguous"
            if transport
            else "command failed without one valid typed failure payload"
        ),
        "predicate": f"wbc0013.{phase}.{stage}.command_success",
        "expected_cardinality": 1,
        "observed_cardinality": 0,
        "candidate_digest": payload_digest([]),
        "details_digest": payload_digest(
            {
                "return_code": evidence.get("return_code"),
                "transport_ambiguous": transport,
                "stdout_sha256": evidence.get("stdout_sha256"),
                "stderr_sha256": evidence.get("stderr_sha256"),
            }
        ),
    }


def _historical_cost_remote_command(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    operation: str,
    evidence_dir: str,
    goal: Mapping[str, Any],
    phase: str,
    manifest_path: str = "",
    manifest_sha256: str = "",
    approval_reference: str = "",
) -> list[str]:
    if phase not in {"plan", "apply", "readback"}:
        raise ApplyError("unsupported historical analytical cost phase")
    target_dir = str(target["target_dir"])
    authorization_match = re.search(
        r"(sha256:[0-9a-f]{64})$", str(approval_reference or "")
    )
    if authorization_match is None:
        raise ApplyError("historical analytical cost authorization digest is invalid")
    owner_authorization_digest = authorization_match.group(1)
    parts = [
        "python3",
        f"{target_dir}/apps/sheet_vitrina_v1_historical_cost_carry_forward.py",
        "--runtime-dir",
        "/opt/wb-core-runtime/state",
        "--target-file",
        f"{target_dir}/artifacts/registry_upload_http_entrypoint/input/"
        "hosted_runtime_target__europe_api.json",
        "--deployed-sha",
        merge_sha,
        "--evidence-dir",
        evidence_dir,
        "--operation-id",
        operation,
        "--business-date",
        str(goal["business_date"]),
        "--nm-id",
        str(goal["nm_id"]),
        "--owner-fixed-unit-cost-rub",
        str(goal["owner_fixed_unit_cost_rub"]),
        "--owner-authorization-digest",
        owner_authorization_digest,
    ]
    if phase in {"apply", "readback"}:
        if (
            posixpath.dirname(posixpath.normpath(manifest_path)) != evidence_dir
            or posixpath.normpath(manifest_path) != manifest_path
            or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha256) is None
        ):
            raise ApplyError("historical analytical cost manifest binding is invalid")
        parts.extend(["--manifest", manifest_path])
    if phase == "apply":
        if len(approval_reference) > 500:
            raise ApplyError("historical analytical cost approval binding is invalid")
        parts.extend(
            [
                "--apply",
                "--manifest-sha256",
                manifest_sha256,
                "--approval-reference",
                approval_reference,
            ]
        )
    elif phase == "readback":
        parts.append("--readback")
    setup = (
        "install -d -m 0700 " + shlex.quote(evidence_dir)
        if phase == "plan"
        else "test -d "
        + shlex.quote(evidence_dir)
        + ' && test "$(stat -c %a '
        + shlex.quote(evidence_dir)
        + ')" = 700'
    )
    shell = (
        "set -eu; umask 077; "
        + setup
        + "; cd "
        + shlex.quote(target_dir)
        + "; export PYTHONPATH="
        + shlex.quote(target_dir)
        + "; "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def _validate_historical_cost_candidate(
    payload: Mapping[str, Any],
    goal: Mapping[str, Any],
    *,
    merge_sha: str,
    evidence_dir: str,
    owner_authorization_digest: str,
) -> None:
    expected = {
        "status": "ready",
        "query_only": True,
        "database_written": False,
        "business_date": goal["business_date"],
        "nm_id": goal["nm_id"],
        "accepted_vitrina_version_count": goal[
            "expected_accepted_version_count"
        ],
        "updated_ready_snapshot_count": goal[
            "expected_updated_ready_snapshot_count"
        ],
        "target_generation_bound": True,
        "barrier_inactive": True,
        "timer_change_count": 0,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ApplyError(
                f"historical analytical cost qualification escaped goal: {field}"
            )
    binding = payload.get("target_binding")
    if not (
        isinstance(binding, Mapping)
        and binding.get("validated") is True
        and binding.get("target_id") == CANONICAL_PRODUCTION_TARGET_ID
        and binding.get("deployed_sha") == merge_sha
    ):
        raise ApplyError("historical analytical cost target binding is invalid")
    for field in (
        "manifest_sha256",
        "material_qualification_digest",
        "source_digest",
        "non_target_digest",
        "other_ready_snapshots_digest",
    ):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get(field) or "")) is None:
            raise ApplyError(
                f"historical analytical cost digest is invalid: {field}"
            )
    if not (
        payload.get("selection_method")
        == "owner_fixed_historical_analytical_cost_v1"
        and payload.get("source_business_date") == goal["business_date"]
        and payload.get("source_unit_cost_rub")
        == goal["owner_fixed_unit_cost_rub"]
        and payload.get("owner_fixed_unit_cost_rub")
        == goal["owner_fixed_unit_cost_rub"]
        and payload.get("owner_authorization_digest")
        == owner_authorization_digest
        and payload.get("inventory_cost_formula_version")
        == "our_inventory_wac_wb_ff_v1"
        and payload.get("physical_history_consulted") is False
    ):
        raise ApplyError("historical analytical owner-fixed source is invalid")
    required_values = (payload.get("after_metrics") or {}).get(
        "after_required_values"
    )
    if not (
        isinstance(required_values, list)
        and len(required_values) == 12
        and all(value not in {None, ""} for value in required_values)
    ):
        raise ApplyError("historical analytical cost formula closure is incomplete")
    manifest_path = str(payload.get("manifest_path") or "")
    if not (
        posixpath.dirname(manifest_path) == evidence_dir
        and re.fullmatch(
            r"historical-cost-carry-forward-plan-[0-9]{8}T[0-9]{6}Z\.json",
            posixpath.basename(manifest_path),
        )
    ):
        raise ApplyError("historical analytical cost private manifest path is invalid")
    persistence = payload.get("plan_persistence")
    admission = (
        persistence.get("root_storage_admission")
        if isinstance(persistence, Mapping)
        else None
    )
    if not (
        isinstance(persistence, Mapping)
        and persistence.get("owner") == "production_apply_evidence"
        and persistence.get("destination") == manifest_path
        and persistence.get("evidence_dir") == evidence_dir
        and persistence.get("evidence_dir_mode") == "0700"
        and persistence.get("file_mode") == "0600"
        and persistence.get("parent_mode") == "0700"
        and persistence.get("bounded_size") is True
        and persistence.get("atomic_publish") is True
        and persistence.get("no_overwrite") is True
        and persistence.get("durable_file_fsync") is True
        and persistence.get("durable_directory_fsync") is True
        and isinstance(admission, Mapping)
        and admission.get("owner") == "production_apply_evidence"
        and admission.get("allowed") is True
    ):
        raise ApplyError("historical analytical cost plan persistence is invalid")


def run_historical_cost_goal(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    goal: Mapping[str, Any],
    operation: str,
    approval_reference: str,
) -> dict[str, Any]:
    evidence_dir = str(
        storage_destination_root("production_apply_evidence")
        / "production-goals"
        / operation
    )
    attempts: list[dict[str, Any]] = []
    authorization_match = re.search(
        r"(sha256:[0-9a-f]{64})$", str(approval_reference or "")
    )
    if authorization_match is None:
        raise ApplyError("historical analytical cost authorization digest is invalid")
    owner_authorization_digest = authorization_match.group(1)
    previous = ""
    candidate: Mapping[str, Any] | None = None
    for attempt in range(1, MAX_QUALIFICATION_CANDIDATES + 1):
        evidence = command_evidence(
            _historical_cost_remote_command(
                target=target,
                merge_sha=merge_sha,
                operation=operation,
                evidence_dir=evidence_dir,
                goal=goal,
                phase="plan",
                approval_reference=approval_reference,
            )
        )
        payload = evidence.get("result")
        try:
            if evidence.get("return_code") != 0 or not isinstance(payload, Mapping):
                raise ApplyError("query-only qualification command failed")
            _validate_historical_cost_candidate(
                payload,
                goal,
                merge_sha=merge_sha,
                evidence_dir=evidence_dir,
                owner_authorization_digest=owner_authorization_digest,
            )
        except ApplyError as exc:
            attempts.append(
                {
                    **evidence,
                    "attempt": attempt,
                    "qualification_state": "blocked",
                    "typed_failure": {
                        "code": str(
                            (payload or {}).get("code")
                            if isinstance(payload, Mapping)
                            else "qualification_failed"
                        ),
                        "message": str(exc)[:500],
                        "details_digest": payload_digest(payload),
                        "submit_count": 0,
                    },
                }
            )
            return {
                "state": "blocked",
                "reason": "historical-cost-qualification-blocked",
                "apply_count": 0,
                "qualification_attempts": attempts,
                "failure": attempts[-1]["typed_failure"],
            }
        current = str(payload["material_qualification_digest"])
        attempts.append(
            {
                **{key: value for key, value in evidence.items() if key != "result"},
                "attempt": attempt,
                "manifest_path": payload["manifest_path"],
                "manifest_sha256": payload["manifest_sha256"],
                "material_qualification_digest": current,
                "qualification_state": "candidate",
            }
        )
        if current == previous:
            attempts[-2]["qualification_state"] = "matching_witness"
            attempts[-1]["qualification_state"] = "qualified"
            candidate = payload
            break
        if len(attempts) > 1:
            attempts[-2]["qualification_state"] = "superseded_material_drift"
        previous = current
        if attempt < MAX_QUALIFICATION_CANDIDATES:
            time.sleep(1.1)
    if candidate is None:
        attempts[-1]["qualification_state"] = "unstable_at_bound"
        return {
            "state": "blocked",
            "reason": "historical-cost-material-cas-not-qualified",
            "apply_count": 0,
            "qualification_attempts": attempts,
            "failure": {
                "code": "material_cas_unstable",
                "message": "bounded query-only qualification did not stabilize",
                "details_digest": payload_digest(attempts),
                "submit_count": 0,
            },
        }
    apply_evidence = command_evidence(
        _historical_cost_remote_command(
            target=target,
            merge_sha=merge_sha,
            operation=operation,
            evidence_dir=evidence_dir,
            goal=goal,
            phase="apply",
            manifest_path=str(candidate["manifest_path"]),
            manifest_sha256=str(candidate["manifest_sha256"]),
            approval_reference=approval_reference,
        )
    )
    readback_evidence = command_evidence(
        _historical_cost_remote_command(
            target=target,
            merge_sha=merge_sha,
            operation=operation,
            evidence_dir=evidence_dir,
            goal=goal,
            phase="readback",
            manifest_path=str(candidate["manifest_path"]),
            manifest_sha256=str(candidate["manifest_sha256"]),
            approval_reference=approval_reference,
        )
    )
    readback = readback_evidence.get("result")
    apply_payload = apply_evidence.get("result")
    required_values = (
        (readback.get("metrics") or {}).get("after_required_values")
        if isinstance(readback, Mapping)
        else None
    )
    reconciled = bool(
        readback_evidence.get("return_code") == 0
        and isinstance(readback, Mapping)
        and readback.get("status") == "reconciled"
        and readback.get("query_only") is True
        and readback.get("database_written") is False
        and readback.get("submit_count") == 1
        and readback.get("business_date") == goal["business_date"]
        and readback.get("nm_id") == goal["nm_id"]
        and readback.get("selection_method")
        == "owner_fixed_historical_analytical_cost_v1"
        and readback.get("source_unit_cost_rub")
        == goal["owner_fixed_unit_cost_rub"]
        and readback.get("owner_fixed_unit_cost_rub")
        == goal["owner_fixed_unit_cost_rub"]
        and readback.get("owner_authorization_digest")
        == owner_authorization_digest
        and readback.get("inventory_cost_formula_version")
        == "our_inventory_wac_wb_ff_v1"
        and readback.get("physical_history_consulted") is False
        and readback.get("accepted_vitrina_version_count") == 1
        and readback.get("updated_ready_snapshot_count") == 1
        and readback.get("runtime_controls_changed") is False
        and readback.get("timer_change_count") == 0
        and isinstance(required_values, list)
        and len(required_values) == 12
        and all(value not in {None, ""} for value in required_values)
    )
    submit_count = int(
        (
            readback.get("submit_count")
            if isinstance(readback, Mapping)
            else apply_payload.get("submit_count")
            if isinstance(apply_payload, Mapping)
            else 0
        )
        or 0
    )
    result = {
        "state": "done" if reconciled else "blocked",
        "reason": (
            "reconciled"
            if reconciled
            else "historical-cost-query-only-readback-not-reconciled"
        ),
        "apply_count": 1,
        "submit_count": submit_count,
        "qualification_attempts": attempts,
        "apply": apply_evidence,
        "readback": readback_evidence,
    }
    if not reconciled:
        result["failure"] = {
            "code": str(
                (readback or {}).get("code")
                if isinstance(readback, Mapping)
                else "readback_not_reconciled"
            ),
            "message": "query-only readback did not prove exact reconciliation",
            "details_digest": payload_digest(readback),
            "submit_count": submit_count,
        }
    return result


def _historical_missing_remote_command(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    operation: str,
    evidence_dir: str,
    phase: str,
    manifest_path: str = "",
    manifest_sha256: str = "",
    approval_reference: str = "",
) -> list[str]:
    if phase not in {"plan", "apply", "readback"}:
        raise ApplyError("unsupported historical missing repair phase")
    target_dir = str(target["target_dir"])
    parts = [
        "python3",
        f"{target_dir}/apps/sheet_vitrina_v1_historical_missing_repair.py",
        "--runtime-dir",
        "/opt/wb-core-runtime/state",
        "--target-file",
        f"{target_dir}/artifacts/registry_upload_http_entrypoint/input/"
        "hosted_runtime_target__europe_api.json",
        "--expected-deployed-sha",
        merge_sha,
        "--evidence-dir",
        evidence_dir,
        "--operation-id",
        operation,
    ]
    if phase in {"apply", "readback"}:
        if (
            posixpath.dirname(posixpath.normpath(manifest_path)) != evidence_dir
            or posixpath.normpath(manifest_path) != manifest_path
            or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha256) is None
        ):
            raise ApplyError("historical missing repair manifest binding is invalid")
        parts.extend(["--manifest-path", manifest_path])
    if phase == "apply":
        if len(approval_reference) > 500:
            raise ApplyError("historical missing repair approval binding is invalid")
        parts.extend(
            [
                "--apply",
                "--expected-manifest-sha256",
                manifest_sha256,
                "--approval-reference",
                approval_reference,
            ]
        )
    elif phase == "readback":
        parts.append("--readback")
    setup = (
        "install -d -m 0700 " + shlex.quote(evidence_dir)
        if phase == "plan"
        else "test -d "
        + shlex.quote(evidence_dir)
        + ' && test "$(stat -c %a '
        + shlex.quote(evidence_dir)
        + ')" = 700'
    )
    shell = (
        "set -eu; umask 077; "
        + setup
        + "; cd "
        + shlex.quote(target_dir)
        + "; export PYTHONPATH="
        + shlex.quote(target_dir)
        + "; "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def _validate_historical_missing_candidate(
    payload: Mapping[str, Any],
    goal: Mapping[str, Any],
    *,
    merge_sha: str,
    evidence_dir: str,
) -> None:
    expected = {
        "status": "ready",
        "query_only": True,
        "database_written": False,
        "source_operation_id": goal["source_operation_id"],
        "source_digest": goal["source_digest"],
        "excluded_date": goal["excluded_date"],
        "logical_target_count": goal["expected_logical_target_count"],
        "updated_ready_snapshot_count": goal[
            "expected_updated_ready_snapshot_count"
        ],
        "current_missing_count": goal["expected_missing_before_count"],
        "after_missing_count": goal["expected_missing_after_count"],
        "would_change": True,
        "target_generation_bound": True,
        "barrier_inactive": True,
        "timer_change_count": 0,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ApplyError(f"historical missing qualification escaped goal: {field}")
    target_dates = payload.get("target_dates")
    if not (
        isinstance(target_dates, list)
        and len(target_dates) == goal["expected_date_count"]
        and target_dates
        == [
            "2026-08-22",
            "2026-08-23",
            "2026-08-24",
            "2026-08-25",
            "2026-08-27",
            "2026-08-28",
            "2026-08-29",
        ]
    ):
        raise ApplyError("historical missing target dates are invalid")
    binding = payload.get("target_binding")
    if not (
        isinstance(binding, Mapping)
        and binding.get("validated") is True
        and binding.get("target_id") == CANONICAL_PRODUCTION_TARGET_ID
        and binding.get("deployed_sha") == merge_sha
    ):
        raise ApplyError("historical missing target binding is invalid")
    for field in (
        "manifest_sha256",
        "material_qualification_digest",
        "source_digest",
        "non_target_digest",
        "other_ready_snapshots_digest",
    ):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get(field) or "")) is None:
            raise ApplyError(f"historical missing digest is invalid: {field}")
    manifest_path = str(payload.get("manifest_path") or "")
    if not (
        posixpath.dirname(manifest_path) == evidence_dir
        and re.fullmatch(
            r"historical-missing-repair-plan-[0-9]{8}T[0-9]{6}Z\.json",
            posixpath.basename(manifest_path),
        )
    ):
        raise ApplyError("historical missing private manifest path is invalid")
    persistence = payload.get("plan_persistence")
    admission = (
        persistence.get("root_storage_admission")
        if isinstance(persistence, Mapping)
        else None
    )
    if not (
        isinstance(persistence, Mapping)
        and persistence.get("owner") == "production_apply_evidence"
        and persistence.get("destination") == manifest_path
        and persistence.get("evidence_dir") == evidence_dir
        and persistence.get("evidence_dir_mode") == "0700"
        and persistence.get("file_mode") == "0600"
        and persistence.get("parent_mode") == "0700"
        and persistence.get("bounded_size") is True
        and persistence.get("atomic_publish") is True
        and persistence.get("no_overwrite") is True
        and persistence.get("durable_file_fsync") is True
        and persistence.get("durable_directory_fsync") is True
        and isinstance(admission, Mapping)
        and admission.get("owner") == "production_apply_evidence"
        and admission.get("allowed") is True
    ):
        raise ApplyError("historical missing plan persistence is invalid")


def run_historical_missing_goal(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    goal: Mapping[str, Any],
    operation: str,
    approval_reference: str,
) -> dict[str, Any]:
    evidence_dir = str(
        storage_destination_root("production_apply_evidence")
        / "production-goals"
        / operation
    )
    attempts: list[dict[str, Any]] = []
    previous = ""
    candidate: Mapping[str, Any] | None = None
    for attempt in range(1, MAX_QUALIFICATION_CANDIDATES + 1):
        evidence = command_evidence(
            _historical_missing_remote_command(
                target=target,
                merge_sha=merge_sha,
                operation=operation,
                evidence_dir=evidence_dir,
                phase="plan",
            )
        )
        payload = evidence.get("result")
        try:
            if evidence.get("return_code") != 0 or not isinstance(payload, Mapping):
                raise ApplyError("query-only historical missing qualification failed")
            _validate_historical_missing_candidate(
                payload,
                goal,
                merge_sha=merge_sha,
                evidence_dir=evidence_dir,
            )
        except ApplyError as exc:
            attempts.append(
                {
                    **evidence,
                    "attempt": attempt,
                    "qualification_state": "blocked",
                    "typed_failure": {
                        "code": str(
                            (payload or {}).get("code")
                            if isinstance(payload, Mapping)
                            else "qualification_failed"
                        ),
                        "message": str(exc)[:500],
                        "details_digest": payload_digest(payload),
                        "submit_count": 0,
                    },
                }
            )
            return {
                "state": "blocked",
                "reason": "historical-missing-qualification-blocked",
                "apply_count": 0,
                "qualification_attempts": attempts,
                "failure": attempts[-1]["typed_failure"],
            }
        current = str(payload["material_qualification_digest"])
        attempts.append(
            {
                **{key: value for key, value in evidence.items() if key != "result"},
                "attempt": attempt,
                "manifest_path": payload["manifest_path"],
                "manifest_sha256": payload["manifest_sha256"],
                "material_qualification_digest": current,
                "qualification_state": "candidate",
            }
        )
        if current == previous:
            attempts[-2]["qualification_state"] = "matching_witness"
            attempts[-1]["qualification_state"] = "qualified"
            candidate = payload
            break
        if len(attempts) > 1:
            attempts[-2]["qualification_state"] = "superseded_material_drift"
        previous = current
        if attempt < MAX_QUALIFICATION_CANDIDATES:
            time.sleep(1.1)
    if candidate is None:
        attempts[-1]["qualification_state"] = "unstable_at_bound"
        return {
            "state": "blocked",
            "reason": "historical-missing-material-cas-not-qualified",
            "apply_count": 0,
            "qualification_attempts": attempts,
            "failure": {
                "code": "material_cas_unstable",
                "message": "bounded query-only qualification did not stabilize",
                "details_digest": payload_digest(attempts),
                "submit_count": 0,
            },
        }
    apply_evidence = command_evidence(
        _historical_missing_remote_command(
            target=target,
            merge_sha=merge_sha,
            operation=operation,
            evidence_dir=evidence_dir,
            phase="apply",
            manifest_path=str(candidate["manifest_path"]),
            manifest_sha256=str(candidate["manifest_sha256"]),
            approval_reference=approval_reference,
        )
    )
    readback_evidence = command_evidence(
        _historical_missing_remote_command(
            target=target,
            merge_sha=merge_sha,
            operation=operation,
            evidence_dir=evidence_dir,
            phase="readback",
            manifest_path=str(candidate["manifest_path"]),
            manifest_sha256=str(candidate["manifest_sha256"]),
        )
    )
    readback = readback_evidence.get("result")
    apply_payload = apply_evidence.get("result")
    reconciled = bool(
        readback_evidence.get("return_code") == 0
        and isinstance(readback, Mapping)
        and readback.get("status") == "reconciled"
        and readback.get("query_only") is True
        and readback.get("database_written") is False
        and readback.get("submit_count") == 1
        and readback.get("target_dates")
        == [
            "2026-08-22",
            "2026-08-23",
            "2026-08-24",
            "2026-08-25",
            "2026-08-27",
            "2026-08-28",
            "2026-08-29",
        ]
        and readback.get("excluded_date") == goal["excluded_date"]
        and readback.get("source_operation_id") == goal["source_operation_id"]
        and readback.get("source_digest") == goal["source_digest"]
        and readback.get("logical_target_count")
        == goal["expected_logical_target_count"]
        and readback.get("updated_ready_snapshot_count")
        == goal["expected_updated_ready_snapshot_count"]
        and readback.get("after_missing_count")
        == goal["expected_missing_after_count"]
        and readback.get("active_target_repair_dates") == []
        and readback.get("recovery_lifecycle") == "retained"
        and readback.get("runtime_controls_changed") is False
        and readback.get("timer_change_count") == 0
    )
    submit_count = int(
        (
            readback.get("submit_count")
            if isinstance(readback, Mapping)
            else apply_payload.get("submit_count")
            if isinstance(apply_payload, Mapping)
            else 0
        )
        or 0
    )
    result = {
        "state": "done" if reconciled else "blocked",
        "reason": (
            "reconciled"
            if reconciled
            else "historical-missing-query-only-readback-not-reconciled"
        ),
        "apply_count": 1,
        "submit_count": submit_count,
        "qualification_attempts": attempts,
        "apply": apply_evidence,
        "readback": readback_evidence,
    }
    if not reconciled:
        result["failure"] = {
            "code": str(
                (readback or {}).get("code")
                if isinstance(readback, Mapping)
                else "readback_not_reconciled"
            ),
            "message": "query-only readback did not prove exact reconciliation",
            "details_digest": payload_digest(readback),
            "submit_count": submit_count,
        }
    return result


def run_wbc0013_goal(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    goal: Mapping[str, Any],
    operation: str,
    approval_reference: str,
) -> dict[str, Any]:
    evidence_dir = str(
        storage_destination_root("production_apply_evidence")
        / "production-goals"
        / operation
    )
    qualification: dict[str, list[dict[str, Any]]] = {"a": [], "b": []}

    def qualify(phase: str) -> Mapping[str, Any] | None:
        previous = ""
        for attempt in range(1, MAX_QUALIFICATION_CANDIDATES + 1):
            evidence = command_evidence(
                _wbc0013_remote_command(
                    target=target,
                    merge_sha=merge_sha,
                    operation=operation,
                    evidence_dir=evidence_dir,
                    phase=f"plan-{phase}",
                )
            )
            payload = evidence.get("result")
            if evidence.get("return_code") != 0 or not isinstance(payload, Mapping):
                qualification[phase].append(
                    {
                        **evidence,
                        "attempt": attempt,
                        "typed_failure": _wbc0013_typed_failure(
                            evidence, phase=phase, stage="qualification"
                        ),
                    }
                )
                return None
            try:
                _validate_wbc0013_candidate(
                    payload, goal, phase=phase, merge_sha=merge_sha
                )
            except ApplyError as exc:
                typed_failure = {
                    "phase": phase,
                    "stage": "qualification",
                    "code": "qualification_candidate_invalid",
                    "message": str(exc)[:500],
                    "predicate": f"wbc0013.{phase}.qualification_candidate_valid",
                    "expected_cardinality": 1,
                    "observed_cardinality": 0,
                    "candidate_digest": payload_digest([payload]),
                    "details_digest": payload_digest(payload),
                }
                qualification[phase].append(
                    {
                        **evidence,
                        "attempt": attempt,
                        "typed_failure": typed_failure,
                    }
                )
                return None
            current = str(payload["material_qualification_digest"])
            qualification[phase].append(
                {
                    **{
                        key: value for key, value in evidence.items() if key != "result"
                    },
                    "attempt": attempt,
                    "manifest_path": payload["manifest_path"],
                    "manifest_sha256": payload["manifest_sha256"],
                    "plan_persistence": dict(payload["plan_persistence"]),
                    "material_qualification_digest": current,
                    **(
                        {
                            "exact_target_count": payload["exact_target_count"],
                            "ready_shape_candidate_count": payload[
                                "ready_shape_candidate_count"
                            ],
                            "ready_shape_candidate_digest": payload[
                                "ready_shape_candidate_digest"
                            ],
                            "causal_event_count": payload["causal_event_count"],
                            "causal_event_candidate_digest": payload[
                                "causal_event_candidate_digest"
                            ],
                            "selection_predicate": payload["selection_predicate"],
                            "selection_details_digest": payload[
                                "selection_details_digest"
                            ],
                            "mismatch_classification_digest": payload[
                                "mismatch_classification_digest"
                            ],
                            "broad_mismatch_query_performed": payload[
                                "broad_mismatch_query_performed"
                            ],
                        }
                        if phase == "b"
                        else {}
                    ),
                }
            )
            if current == previous:
                qualification[phase][-2]["qualification_state"] = "matching_witness"
                qualification[phase][-1]["qualification_state"] = "qualified"
                return payload
            if len(qualification[phase]) > 1:
                qualification[phase][-2]["qualification_state"] = (
                    "superseded_material_drift"
                )
            qualification[phase][-1]["qualification_state"] = "candidate"
            previous = current
            if attempt < MAX_QUALIFICATION_CANDIDATES:
                time.sleep(1.1)
        qualification[phase][-1]["qualification_state"] = "unstable_at_bound"
        return None

    a_candidate = qualify("a")
    if a_candidate is None:
        failure = qualification["a"][-1].get("typed_failure")
        return {
            "state": "blocked",
            "reason": "wbc0013-a-material-cas-not-qualified",
            "apply_count": 0,
            "qualification_attempts": qualification,
            "failure": failure,
        }
    a_apply = command_evidence(
        _wbc0013_remote_command(
            target=target,
            merge_sha=merge_sha,
            operation=operation,
            evidence_dir=evidence_dir,
            phase="apply-a",
            manifest_path=str(a_candidate["manifest_path"]),
            manifest_sha256=str(a_candidate["manifest_sha256"]),
            approval_reference=approval_reference,
        )
    )
    a_readback = command_evidence(
        _wbc0013_remote_command(
            target=target,
            merge_sha=merge_sha,
            operation=operation,
            evidence_dir=evidence_dir,
            phase="readback-a",
        )
    )
    a_result = a_readback.get("result")
    if not (
        a_readback.get("return_code") == 0
        and isinstance(a_result, Mapping)
        and a_result.get("status") == "reconciled"
        and a_result.get("query_only") is True
        and a_result.get("roster_count") == goal["expected_roster_count"]
        and a_result.get("covered_roster_count") == goal["expected_roster_count"]
        and a_result.get("zero_row_count") == goal["expected_zero_insert_count"]
        and a_result.get("new_explicit_zero_count")
        == goal["expected_zero_insert_count"]
        and a_result.get("document_count") == 1
        and a_result.get("absolute_target_line_count")
        == goal["expected_zero_insert_count"]
        and a_result.get("movement_line_count") == 0
        and bool(a_result.get("forward_t0"))
        and a_result.get("history_write_count") == 0
        and a_result.get("non_target_preserved") is True
    ):
        failure_evidence = (
            a_apply
            if a_apply.get("return_code") != 0
            else a_readback
            if a_readback.get("return_code") != 0
            else None
        )
        failure = (
            _wbc0013_typed_failure(
                failure_evidence,
                phase="a",
                stage=("submit" if failure_evidence is a_apply else "readback"),
            )
            if failure_evidence is not None
            else {
                "phase": "a",
                "stage": "readback",
                "code": "readback_not_reconciled",
                "message": "query-only A readback did not prove exact reconciliation",
                "predicate": "wbc0013.a.readback_exact_reconciliation",
                "expected_cardinality": 1,
                "observed_cardinality": 0,
                "candidate_digest": payload_digest([a_result]),
                "details_digest": payload_digest(a_result),
            }
        )
        return {
            "state": "blocked",
            "reason": "wbc0013-a-query-only-readback-not-reconciled",
            "apply_count": 1,
            "qualification_attempts": qualification,
            "a_apply": a_apply,
            "a_readback": a_readback,
            "failure": failure,
        }
    b_candidate = qualify("b")
    if b_candidate is None:
        failure = qualification["b"][-1].get("typed_failure")
        return {
            "state": "blocked",
            "reason": "wbc0013-b-material-cas-not-qualified",
            "apply_count": 1,
            "qualification_attempts": qualification,
            "a_apply": a_apply,
            "a_readback": a_readback,
            "failure": failure,
        }
    b_apply = command_evidence(
        _wbc0013_remote_command(
            target=target,
            merge_sha=merge_sha,
            operation=operation,
            evidence_dir=evidence_dir,
            phase="apply-b",
            manifest_path=str(b_candidate["manifest_path"]),
            manifest_sha256=str(b_candidate["manifest_sha256"]),
            approval_reference=approval_reference,
        )
    )
    b_readback = command_evidence(
        _wbc0013_remote_command(
            target=target,
            merge_sha=merge_sha,
            operation=operation,
            evidence_dir=evidence_dir,
            phase="readback-b",
        )
    )
    b_result = b_readback.get("result")
    reconciled = bool(
        b_readback.get("return_code") == 0
        and isinstance(b_result, Mapping)
        and b_result.get("status") == "reconciled"
        and b_result.get("query_only") is True
        and b_result.get("historical_repair_count")
        == goal["expected_historical_repair_count"]
        and b_result.get("current_active_preserved") is True
        and b_result.get("current_sync_preserved") is True
        and b_result.get("current_pool_preserved") is True
        and b_result.get("a_forward_zeros_preserved") is True
        and b_result.get("ready_target_total_closed") is True
        and b_result.get("non_target_preserved") is True
        and b_result.get("historical_quantity") == "1952"
        and b_result.get("historical_cost_covered_quantity") == "1952"
        and b_result.get("historical_location_count") == 3
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(b_result.get("historical_location_digest") or ""),
        )
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(b_result.get("historical_provenance_digest") or ""),
        )
        and b_result.get("target_own_cost_available") is True
        and b_result.get("six_total_dependencies_available") is True
    )
    result = {
        "state": "done" if reconciled else "blocked",
        "reason": (
            "reconciled"
            if reconciled
            else "wbc0013-b-query-only-readback-not-reconciled"
        ),
        "apply_count": 2,
        "a_submit_count": 1,
        "b_submit_count": 1,
        "qualification_attempts": qualification,
        "a_apply": a_apply,
        "a_readback": a_readback,
        "b_apply": b_apply,
        "b_readback": b_readback,
    }
    if not reconciled:
        failure_evidence = (
            b_apply
            if b_apply.get("return_code") != 0
            else b_readback
            if b_readback.get("return_code") != 0
            else None
        )
        result["failure"] = (
            _wbc0013_typed_failure(
                failure_evidence,
                phase="b",
                stage=("submit" if failure_evidence is b_apply else "readback"),
            )
            if failure_evidence is not None
            else {
                "phase": "b",
                "stage": "readback",
                "code": "readback_not_reconciled",
                "message": "query-only B readback did not prove exact reconciliation",
                "predicate": "wbc0013.b.readback_exact_reconciliation",
                "expected_cardinality": 1,
                "observed_cardinality": 0,
                "candidate_digest": payload_digest([b_result]),
                "details_digest": payload_digest(b_result),
            }
        )
    return result


def run_wbc0027_goal(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    goal: Mapping[str, Any],
    operation: str,
    approval_reference: str,
) -> dict[str, Any]:
    evidence_dir = str(
        storage_destination_root("production_apply_evidence")
        / "production-goals"
        / operation
    )
    qualification: dict[str, list[dict[str, Any]]] = {
        "product": [],
        "economics": [],
    }

    def qualify(
        phase: str, *, product_phase_operation_id: str = ""
    ) -> Mapping[str, Any] | None:
        previous: dict[str, Any] | None = None
        for attempt in range(1, MAX_QUALIFICATION_CANDIDATES + 1):
            evidence = command_evidence(
                _wbc0027_remote_command(
                    target=target,
                    merge_sha=merge_sha,
                    operation=operation,
                    evidence_dir=evidence_dir,
                    action="plan",
                    phase=phase,
                    product_phase_operation_id=product_phase_operation_id,
                )
            )
            payload = evidence.get("result")
            if evidence.get("return_code") != 0 or not isinstance(payload, Mapping):
                qualification[phase].append({**evidence, "attempt": attempt})
                return None
            try:
                _validate_wbc0027_candidate(
                    payload,
                    goal,
                    phase=phase,
                    merge_sha=merge_sha,
                    operation=operation,
                    evidence_dir=evidence_dir,
                    product_phase_operation_id=product_phase_operation_id,
                )
            except ApplyError as exc:
                qualification[phase].append(
                    {
                        **evidence,
                        "attempt": attempt,
                        "candidate_validation_error": str(exc)[:500],
                    }
                )
                return None
            current = {
                "material_qualification_digest": payload[
                    "material_qualification_digest"
                ],
                "phase_fingerprint": payload["phase_fingerprint"],
                "phase_operation_id": payload["phase_operation_id"],
                "storage_generation": dict(payload["storage_generation"]),
                "deployed_sha": payload["deployed_sha"],
            }
            qualification[phase].append(
                {
                    **{key: value for key, value in evidence.items() if key != "result"},
                    "attempt": attempt,
                    "manifest_path": payload["manifest_path"],
                    "manifest_sha256": payload["manifest_sha256"],
                    "phase_operation_id": payload["phase_operation_id"],
                    "phase_fingerprint": payload["phase_fingerprint"],
                    "material_qualification_digest": current[
                        "material_qualification_digest"
                    ],
                    "storage_generation": dict(payload["storage_generation"]),
                    "plan_persistence": dict(payload["plan_persistence"]),
                }
            )
            if previous is not None and current == previous:
                qualification[phase][-2]["qualification_state"] = "matching_witness"
                qualification[phase][-1]["qualification_state"] = "qualified"
                return payload
            if len(qualification[phase]) > 1:
                qualification[phase][-2]["qualification_state"] = (
                    "superseded_material_or_generation_drift"
                )
            qualification[phase][-1]["qualification_state"] = "candidate"
            previous = current
            if attempt < MAX_QUALIFICATION_CANDIDATES:
                time.sleep(1.1)
        qualification[phase][-1]["qualification_state"] = "unstable_at_bound"
        return None

    def invoke(candidate: Mapping[str, Any], phase: str, action: str) -> dict[str, Any]:
        return command_evidence(
            _wbc0027_remote_command(
                target=target,
                merge_sha=merge_sha,
                operation=operation,
                evidence_dir=evidence_dir,
                action=action,
                phase=phase,
                manifest_path=str(candidate["manifest_path"]),
                manifest_sha256=str(candidate["manifest_sha256"]),
                phase_operation_id=str(candidate["phase_operation_id"]),
                phase_fingerprint=str(candidate["phase_fingerprint"]),
                approval_reference=approval_reference if action == "apply" else "",
            )
        )

    def applied_submit_count(evidence: Mapping[str, Any]) -> int:
        if evidence.get("transport_ambiguous") is True:
            return 1
        payload = evidence.get("result")
        if not isinstance(payload, Mapping):
            return 0
        value = payload.get("production_mutation_submit_count")
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    def readback_exact(
        evidence: Mapping[str, Any], *, phase: str, candidate: Mapping[str, Any]
    ) -> bool:
        payload = evidence.get("result")
        return bool(
            evidence.get("return_code") == 0
            and evidence.get("transport_ambiguous") is False
            and isinstance(payload, Mapping)
            and payload.get("status") == "reconciled"
            and payload.get("phase") == phase
            and payload.get("profile") == WBC0027_GOAL_PROFILE
            and payload.get("target_id") == CANONICAL_PRODUCTION_TARGET_ID
            and payload.get("goal_operation_id") == operation
            and payload.get("phase_operation_id") == candidate["phase_operation_id"]
            and payload.get("phase_fingerprint") == candidate["phase_fingerprint"]
            and payload.get("deployed_sha") == merge_sha
            and payload.get("storage_generation")
            == candidate["storage_generation"]
            and payload.get("query_only") is True
            and payload.get("database_written") is False
            and payload.get("production_mutation_submit_count") == 0
            and payload.get("recovery_lifecycle") == "retained"
            and payload.get("hard_non_target", {}).get("all_exact") is True
            and (
                payload.get("product_exact") is True
                if phase == "product"
                else payload.get("economics_target_exact") is True
                and payload.get("functional_economics_missing")
                == {"2026-08-26": 12, "2026-08-29": 0}
            )
        )

    product_candidate = qualify("product")
    if product_candidate is None:
        return {
            "state": "blocked",
            "reason": "wbc0027-product-material-not-qualified",
            "apply_count": 0,
            "product_state": "not_applied",
            "economics_state": "not_applied",
            "qualification_attempts": qualification,
        }
    product_apply = invoke(product_candidate, "product", "apply")
    product_readback = invoke(product_candidate, "product", "readback")
    product_submits = applied_submit_count(product_apply)
    if not readback_exact(
        product_readback,
        phase="product",
        candidate=product_candidate,
    ):
        product_payload = product_apply.get("result")
        product_state = (
            str(product_payload.get("status"))
            if isinstance(product_payload, Mapping)
            and product_payload.get("status")
            in {"not_applied", "ambiguous", "applied_pending_reconciliation"}
            else "ambiguous"
            if product_apply.get("transport_ambiguous") is True or product_submits
            else "not_applied"
        )
        return {
            "state": "blocked",
            "reason": "wbc0027-product-query-only-readback-not-reconciled",
            "apply_count": product_submits,
            "product_state": product_state,
            "economics_state": "not_applied",
            "qualification_attempts": qualification,
            "product_apply": product_apply,
            "product_readback": product_readback,
        }
    economics_candidate = qualify(
        "economics",
        product_phase_operation_id=str(product_candidate["phase_operation_id"]),
    )
    if economics_candidate is None:
        return {
            "state": "blocked",
            "reason": "wbc0027-economics-material-not-qualified",
            "apply_count": product_submits,
            "product_state": "applied",
            "economics_state": "not_applied",
            "qualification_attempts": qualification,
            "product_apply": product_apply,
            "product_readback": product_readback,
        }
    economics_apply = invoke(economics_candidate, "economics", "apply")
    economics_readback = invoke(economics_candidate, "economics", "readback")
    economics_submits = applied_submit_count(economics_apply)
    economics_exact = readback_exact(
        economics_readback,
        phase="economics",
        candidate=economics_candidate,
    )
    economics_payload = economics_apply.get("result")
    economics_state = (
        "applied"
        if economics_exact
        else str(economics_payload.get("status"))
        if isinstance(economics_payload, Mapping)
        and economics_payload.get("status")
        in {"not_applied", "ambiguous", "applied_pending_reconciliation"}
        else "ambiguous"
        if economics_apply.get("transport_ambiguous") is True or economics_submits
        else "not_applied"
    )
    return {
        "state": "done" if economics_exact else "blocked",
        "reason": (
            "reconciled"
            if economics_exact
            else "wbc0027-economics-query-only-readback-not-reconciled"
        ),
        "apply_count": product_submits + economics_submits,
        "product_submit_count": product_submits,
        "economics_submit_count": economics_submits,
        "product_state": "applied",
        "economics_state": economics_state,
        "qualification_attempts": qualification,
        "product_candidate": {
            key: product_candidate[key]
            for key in (
                "manifest_path",
                "manifest_sha256",
                "phase_operation_id",
                "phase_fingerprint",
                "material_qualification_digest",
                "storage_generation",
            )
        },
        "economics_candidate": {
            key: economics_candidate[key]
            for key in (
                "manifest_path",
                "manifest_sha256",
                "phase_operation_id",
                "phase_fingerprint",
                "material_qualification_digest",
                "storage_generation",
            )
        },
        "product_apply": product_apply,
        "product_readback": product_readback,
        "economics_apply": economics_apply,
        "economics_readback": economics_readback,
    }


def run_dynamic_goal(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    goal: Mapping[str, Any],
    operation: str,
    approval_reference: str,
    warm_readiness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    warm_archive = goal["profile"] == WARM_ARCHIVE_GOAL_PROFILE
    if warm_archive and not isinstance(warm_readiness, Mapping):
        raise ApplyError(
            "warm archive operation requires a ready pre-operation receipt"
        )
    # The completed WBC 0008 exact-six protocol is immutable: its operation
    # identities and manifest bindings remain on the historical evidence path.
    # Only future/current generic production-goal evidence is registry-routed.
    evidence_dir = (
        str(WARM_ARCHIVE_LEGACY_EVIDENCE_BASE / operation)
        if warm_archive
        else str(
            storage_destination_root("production_apply_evidence")
            / "production-goals"
            / operation
        )
    )
    attempts: list[dict[str, Any]] = []
    previous_material_digest = ""
    previous_material_components: Any = None
    candidate: Mapping[str, Any] | None = None
    for attempt in range(1, MAX_QUALIFICATION_CANDIDATES + 1):
        evidence = command_evidence(
            _remote_command(
                target=target,
                merge_sha=merge_sha,
                goal=goal,
                operation=operation,
                evidence_dir=evidence_dir,
                mode="dry-run",
                projection_manifest_path=(
                    str(warm_readiness["projection_manifest_path"])
                    if warm_readiness is not None
                    else ""
                ),
                projection_manifest_sha256=(
                    str(warm_readiness["projection_manifest_sha256"])
                    if warm_readiness is not None
                    else ""
                ),
            )
        )
        payload = evidence.get("result")
        if evidence.get("return_code") != 0 or not isinstance(payload, Mapping):
            return {
                "state": "blocked",
                "reason": "jit-material-preflight-failed",
                "apply_count": 0,
                "qualification_attempts": [*attempts, evidence],
            }
        try:
            _validate_candidate(payload, goal, warm_readiness=warm_readiness)
        except ApplyError as exc:
            return {
                "state": "blocked",
                "reason": str(exc),
                "apply_count": 0,
                "qualification_attempts": [*attempts, evidence],
            }
        if payload.get("deployed_sha") != merge_sha:
            raise ApplyError(
                "dynamic manifest is not bound to exact deployed merge SHA"
            )
        candidate_evidence = {
            **{key: value for key, value in evidence.items() if key != "result"},
            "attempt": attempt,
            "manifest_path": payload["manifest_path"],
            "manifest_sha256": payload["manifest_sha256"],
            "material_qualification_digest": payload["material_qualification_digest"],
            "qualification_state": "candidate",
        }
        if goal["profile"] == WARM_ARCHIVE_GOAL_PROFILE:
            candidate_evidence.update(
                {
                    "source_count": payload["source_count"],
                    "expected_reclaimed_allocated_bytes": payload[
                        "expected_reclaimed_allocated_bytes"
                    ],
                    "immutable_non_target_digest": payload[
                        "immutable_non_target_digest"
                    ],
                    "mutable_canonical_topology_digest": payload[
                        "mutable_canonical_topology_digest"
                    ],
                    "mutable_canonical_observations": payload[
                        "mutable_canonical_observations"
                    ],
                    "readiness_id": payload["readiness_id"],
                    "projection_manifest_sha256": payload["projection_manifest_sha256"],
                    "activity_evidence": _activity_receipt_summary(
                        payload["activity_evidence"]
                    ),
                    "material_partition": payload["material_partition"],
                    "mutable_safety_predicates": payload["mutable_safety_predicates"],
                    "material_cas_components_digest": "sha256:"
                    + digest(canonical_json_bytes(payload["material_cas_components"])),
                }
            )
        else:
            candidate_evidence.update(
                {
                    "source_watermarks_digest": payload["source_watermarks_digest"],
                    "target_history_digest": payload["target_history_digest"],
                }
            )
        attempts.append(candidate_evidence)
        current_material_digest = str(payload["material_qualification_digest"])
        if current_material_digest == previous_material_digest:
            attempts[-2]["qualification_state"] = "matching_witness"
            attempts[-1]["qualification_state"] = "qualified"
            candidate = payload
            break
        if len(attempts) > 1:
            attempts[-2]["qualification_state"] = "superseded_material_drift"
            if goal["profile"] == WARM_ARCHIVE_GOAL_PROFILE:
                attempts[-1]["drift_from_previous"] = _material_component_diff_summary(
                    previous_material_components,
                    payload.get("material_cas_components"),
                )
        previous_material_digest = current_material_digest
        previous_material_components = payload.get("material_cas_components")
        if attempt < MAX_QUALIFICATION_CANDIDATES:
            time.sleep(1.1)
    if candidate is None:
        if attempts:
            attempts[-1]["qualification_state"] = "unstable_at_bound"
        return {
            "state": "blocked",
            "reason": "material-cas-did-not-survive-bounded-qualification",
            "apply_count": 0,
            "qualification_attempts": attempts,
        }

    manifest_path = str(candidate["manifest_path"])
    manifest_sha256 = str(candidate["manifest_sha256"])
    try:
        apply_command = _remote_command(
            target=target,
            merge_sha=merge_sha,
            goal=goal,
            operation=operation,
            evidence_dir=evidence_dir,
            mode="apply",
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            approval_reference=approval_reference,
        )
    except ApplyError as exc:
        return {
            "state": "blocked",
            "reason": str(exc),
            "apply_count": 0,
            "qualification_attempts": attempts,
        }
    apply_evidence = command_evidence(apply_command)
    # This is the single mutation submit boundary. It is never repeated,
    # including after a nonzero exit or ambiguous SSH transport.
    readback_evidence = command_evidence(
        _remote_command(
            target=target,
            merge_sha=merge_sha,
            goal=goal,
            operation=operation,
            evidence_dir=evidence_dir,
            mode="readback",
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
        ),
        timeout_seconds=(
            43260.0 if goal["profile"] == WARM_ARCHIVE_GOAL_PROFILE else 3600.0
        ),
    )
    readback = readback_evidence.get("result")
    if goal["profile"] == WARM_ARCHIVE_GOAL_PROFILE:
        reconciled = bool(
            readback_evidence.get("return_code") == 0
            and isinstance(readback, Mapping)
            and readback.get("status") == "reconciled"
            and readback.get("query_only") is True
            and readback.get("source_count") == goal["expected_source_count"]
            and readback.get("source_absent_count") == goal["expected_source_count"]
            and readback.get("archive_count") == goal["expected_archive_count"]
            and readback.get("manifest_count") == goal["expected_manifest_count"]
            and readback.get("raw_unlink_count") == goal["expected_unlink_count"]
            and readback.get("reclaimed_allocated_bytes")
            == goal["expected_reclaimed_allocated_bytes"]
            and readback.get("root_minimum_passed") is True
            and readback.get("backup_capacity_guard_passed") is True
            and readback.get("services_healthy") is True
            and readback.get("non_target_preserved") is True
            and isinstance(readback.get("mutation_scope_reconciliation"), Mapping)
            and readback["mutation_scope_reconciliation"].get("exact") is True
            and readback["mutation_scope_reconciliation"].get(
                "non_target_unlink_move_write_count"
            )
            == 0
            and readback.get("promo_action_count") == 0
            and readback.get("business_data_mutation_count") == 0
            and readback.get("exact_manifest_apply_receipt_count") == 1
        )
    else:
        reconciled = bool(
            readback_evidence.get("return_code") == 0
            and isinstance(readback, Mapping)
            and readback.get("status") == "reconciled"
            and readback.get("query_only") is True
            and readback.get("inserted_capture_count")
            == goal["expected_inserted_capture_count"]
            and readback.get("inserted_component_count")
            == goal["expected_inserted_component_count"]
            and readback.get("inserted_finalization_count")
            == goal["expected_inserted_finalization_count"]
            and readback.get("visible_history_date_count") == goal["date_count"]
            and readback.get("visible_history_quality")
            == {
                "full": goal["expected_full_date_count"],
                "partial": goal["expected_partial_date_count"],
                "unavailable": 0,
            }
            and readback.get("exact_manifest_apply_receipt_count") == 1
            and readback.get("total_inventory_history_apply_receipt_count") == 1
            and readback.get("non_target_preserved") is True
        )
    return {
        "state": "done" if reconciled else "blocked",
        "reason": "reconciled" if reconciled else "post-submit-readback-not-reconciled",
        "apply_count": 1,
        "qualification_attempts": attempts,
        "qualified_manifest": {
            "path": manifest_path,
            "sha256": manifest_sha256,
            "material_qualification_digest": candidate["material_qualification_digest"],
        },
        "apply": apply_evidence,
        "readback": readback_evidence,
    }


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(receipt) + b"\n")


def _find_mapping(payload: Any, key: str) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
        for child_key in sorted(payload):
            found = _find_mapping(payload[child_key], key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_mapping(item, key)
            if found is not None:
                return found
    return None


def _compact_component_diff(payload: Any, *, limit: int = 24) -> dict[str, Any] | None:
    diff = _find_mapping(payload, "component_diff")
    if (
        diff is None
        and isinstance(payload, Mapping)
        and payload.get("schema") == ("wb-core.root-warm-archive-material-cas-diff/v1")
    ):
        diff = payload
    if diff is None:
        return None
    source_components = [
        *(diff.get("components") or []),
        *(diff.get("observation_changes") or []),
    ]
    components = [
        {
            "json_path": item.get("json_path"),
            "classification": item.get("classification"),
            "before_component_digest": item.get("before_component_digest"),
            "after_component_digest": item.get("after_component_digest"),
        }
        for item in source_components
        if isinstance(item, Mapping)
    ]
    changed_paths = list(diff.get("changed_json_paths") or [])
    changed_paths.extend(diff.get("blocked_observation_json_paths") or [])
    return {
        "schema": diff.get("schema"),
        "before_material_digest": diff.get("before_material_digest"),
        "after_material_digest": diff.get("after_material_digest"),
        "changed_component_count": int(
            diff.get("changed_component_count") or len(components)
        ),
        "changed_json_paths": changed_paths[:limit],
        "components": components[:limit],
        "summary_truncated": len(components) > limit,
    }


def _compact_job(payload: Any) -> dict[str, Any] | None:
    readback = (
        (((payload or {}).get("evidence") or {}).get("readback") or {}).get("result")
        if isinstance(payload, Mapping)
        else None
    )
    job = readback.get("job") if isinstance(readback, Mapping) else None
    if not isinstance(job, Mapping):
        job = _find_mapping(payload, "job")
    if not isinstance(job, Mapping):
        return None
    request = job.get("request") or {}
    return {
        "job_id": job.get("job_id") or request.get("job_id"),
        "status": job.get("status"),
        "terminal": job.get("terminal"),
        "attempt": job.get("attempt"),
        "request_digest": job.get("request_digest") or request.get("request_digest"),
        "result_digest": job.get("result_digest"),
    }


def _compact_error(payload: Any) -> dict[str, Any] | None:
    error = _find_mapping(payload, "error")
    if not isinstance(error, Mapping):
        return None
    return {
        "code": error.get("code"),
        "type": error.get("type"),
        "message": str(error.get("message") or "")[:1000],
        "evidence_digest": (
            "sha256:" + digest(canonical_json_bytes(error.get("evidence")))
            if isinstance(error.get("evidence"), Mapping)
            else None
        ),
    }


def _receipt_artifact_name(pr: int, run_id: int) -> str:
    return f"production-apply-receipt-pr-{pr}-run-{run_id}"


def _compact_apply_comment_body(
    receipt: Mapping[str, Any],
    *,
    artifact_name: str,
    receipt_sha256: str,
    receipt_size_bytes: int,
    component_limit: int = 24,
) -> str:
    operation = str(receipt["operation_id"])
    summary = {
        "schema": APPLY_COMMENT_SUMMARY_SCHEMA,
        "state": receipt.get("state"),
        "operation_id": operation,
        "pull_request": receipt.get("pull_request"),
        "release_operation_id": receipt.get("release_operation_id"),
        "merge_sha": receipt.get("merge_sha"),
        "deployed_sha": receipt.get("deployed_sha"),
        "apply_count": receipt.get("apply_count"),
        "reason": ((receipt.get("evidence") or {}).get("reason")),
        "job": _compact_job(receipt),
        "error": _compact_error(receipt),
        "component_diff_summary": _compact_component_diff(
            receipt, limit=component_limit
        ),
        "artifact": {
            "name": artifact_name,
            "file": RECOVERY_ARTIFACT_FILE,
            "sha256": "sha256:" + receipt_sha256,
            "size_bytes": int(receipt_size_bytes),
            "retention_days": 90,
        },
        "full_receipt": {
            "schema": receipt.get("schema"),
            "sha256": "sha256:" + receipt_sha256,
        },
    }
    body = (
        marker(operation)
        + "\nCompact terminal production apply summary; full immutable evidence is in the bound Actions artifact."
        + "\n```json\n"
        + json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n```"
    )
    if len(body.encode("utf-8")) >= MAX_GITHUB_COMMENT_BYTES:
        if component_limit > 4:
            return _compact_apply_comment_body(
                receipt,
                artifact_name=artifact_name,
                receipt_sha256=receipt_sha256,
                receipt_size_bytes=receipt_size_bytes,
                component_limit=4,
            )
        summary["component_diff_summary"] = {
            "digest": (
                "sha256:"
                + digest(
                    canonical_json_bytes(
                        _compact_component_diff(receipt, limit=component_limit)
                    )
                )
            ),
            "changed_component_count": (
                (_compact_component_diff(receipt, limit=0) or {}).get(
                    "changed_component_count"
                )
            ),
            "summary_truncated": True,
        }
        if isinstance(summary.get("error"), Mapping):
            summary["error"] = {
                **summary["error"],
                "message": str(summary["error"].get("message") or "")[:256],
            }
        body = (
            marker(operation)
            + "\nCompact terminal production apply summary; full immutable evidence is in the bound Actions artifact."
            + "\n```json\n"
            + json.dumps(
                summary,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n```"
        )
    if len(body.encode("utf-8")) >= MAX_GITHUB_COMMENT_BYTES:
        raise ApplyError("compact apply receipt comment exceeds GitHub limit")
    return body


def _publish_compact_apply_receipt(
    client: GitHubClient,
    *,
    pr: int,
    receipt: Mapping[str, Any],
    receipt_path: Path,
    artifact_name: str,
) -> Mapping[str, Any]:
    raw = receipt_path.read_bytes()
    expected = canonical_json_bytes(receipt) + b"\n"
    if raw != expected:
        raise ApplyError("immutable apply receipt bytes drifted before publication")
    body = _compact_apply_comment_body(
        receipt,
        artifact_name=artifact_name,
        receipt_sha256=digest(raw),
        receipt_size_bytes=len(raw),
    )
    published = client.post(f"/issues/{pr}/comments", {"body": body})
    if not isinstance(published, Mapping) or published.get("body") != body:
        raise ApplyError("compact apply receipt publication response mismatch")
    return published


def _warm_readiness_artifact_name(pr: int, run_id: int) -> str:
    return f"root-warm-archive-readiness-pr-{pr}-run-{run_id}"


def _compact_warm_service_gate(service_gate: Any) -> dict[str, Any] | None:
    if not isinstance(service_gate, Mapping):
        return None
    resample = service_gate.get("pair_resample_evidence") or {}
    return {
        "expected_unit_count": service_gate.get("expected_unit_count"),
        "observed_unit_count": service_gate.get("observed_unit_count"),
        "expected_pair_count": service_gate.get("expected_pair_count"),
        "observed_pair_count": service_gate.get("observed_pair_count"),
        "classification": service_gate.get("classification"),
        "healthy": service_gate.get("healthy"),
        "failing_unit_count": service_gate.get("failing_unit_count"),
        "failing_pair_count": service_gate.get("failing_pair_count"),
        "resample_required_pair_names": service_gate.get(
            "resample_required_pair_names"
        ),
        "units": [
            {
                "name": row.get("name"),
                "classification": row.get("classification"),
                "healthy": row.get("healthy"),
            }
            for row in service_gate.get("units") or []
            if isinstance(row, Mapping)
        ],
        "pairs": [
            {
                "timer_name": row.get("timer_name"),
                "owner_name": row.get("owner_name"),
                "classification": row.get("classification"),
                "healthy": row.get("healthy"),
                "resample_required": row.get("resample_required"),
            }
            for row in service_gate.get("pairs") or []
            if isinstance(row, Mapping)
        ],
        "pair_resample_evidence": {
            "attempted": resample.get("attempted"),
            "attempt_count": resample.get("attempt_count"),
            "resolved_healthy": resample.get("resolved_healthy"),
            "remaining_resample_required_pair_names": resample.get(
                "remaining_resample_required_pair_names"
            ),
            "samples": [
                {
                    "attempt": sample.get("attempt"),
                    "unit_names": sample.get("unit_names"),
                    "unit_count": len(sample.get("units") or []),
                    "pair_count": len(sample.get("pairs") or []),
                }
                for sample in resample.get("samples") or []
                if isinstance(sample, Mapping)
            ],
        },
    }


def _compact_mutable_observations(rows: Any) -> list[dict[str, Any]]:
    return [
        {
            "key": row.get("key"),
            "owner": row.get("owner"),
            "classification": row.get("classification"),
            "topology": row.get("topology"),
            "ordinary_mutable_fields": row.get("ordinary_mutable_fields"),
            "open_handle_relationship_count": len(
                row.get("open_handle_relationships") or []
            ),
            "open_handle_relationships_digest": "sha256:"
            + digest(canonical_json_bytes(row.get("open_handle_relationships") or [])),
        }
        for row in rows or []
        if isinstance(row, Mapping)
    ]


def _compact_warm_readiness_comment_body(
    receipt: Mapping[str, Any],
    *,
    artifact_name: str,
    receipt_sha256: str,
    receipt_size_bytes: int,
) -> str:
    readiness = str(receipt["readiness_id"])
    comment_receipt = {
        key: receipt.get(key)
        for key in (
            "schema",
            "state",
            "reason",
            "attempt",
            "query_only",
            "database_written",
            "readiness_id",
            "repository",
            "pull_request",
            "release_operation_id",
            "authorization_comment_id",
            "goal_operation_id",
            "mount_probe_job_id",
            "mount_probe_evidence_digest",
            "mount_probe_artifact",
            "mount_probe_comment_id",
            "merge_sha",
            "deployed_sha",
            "projection_manifest_path",
            "projection_manifest_sha256",
            "material_qualification_digest",
            "material_partition",
            "immutable_non_target_digest",
            "mutable_canonical_topology_digest",
            "mutable_canonical_observations",
            "mutable_safety_predicates",
            "expected_reclaimed_allocated_bytes",
            "required_backup_floor_bytes",
            "root_minimum_after_bytes",
        )
    }
    comment_receipt.update(
        {
            "mutable_canonical_observations": _compact_mutable_observations(
                receipt.get("mutable_canonical_observations")
            ),
            "systemd_service_gate": _compact_warm_service_gate(
                receipt.get("systemd_service_gate")
            ),
            "callback": list(receipt.get("callback") or [])[:12],
            "component_diff_summary": _compact_component_diff(receipt, limit=24),
            "artifact": {
                "name": artifact_name,
                "file": "root-warm-archive-readiness-receipt.json",
                "sha256": "sha256:" + receipt_sha256,
                "size_bytes": int(receipt_size_bytes),
                "retention_days": 90,
            },
            "evidence_sha256": "sha256:"
            + digest(canonical_json_bytes(receipt.get("evidence"))),
        }
    )
    body = (
        warm_readiness_marker(readiness)
        + "\nCompact terminal query-only warm archive readiness summary; full immutable evidence is in the bound Actions artifact."
        + "\n```json\n"
        + json.dumps(
            comment_receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n```"
    )
    if len(body.encode("utf-8")) >= MAX_GITHUB_COMMENT_BYTES:
        comment_receipt["callback"] = []
        diff = comment_receipt.get("component_diff_summary")
        comment_receipt["component_diff_summary"] = (
            {
                "digest": "sha256:" + digest(canonical_json_bytes(diff)),
                "changed_component_count": (diff or {}).get("changed_component_count"),
                "summary_truncated": True,
            }
            if diff is not None
            else None
        )
        body = (
            warm_readiness_marker(readiness)
            + "\nCompact terminal query-only warm archive readiness summary; full immutable evidence is in the bound Actions artifact."
            + "\n```json\n"
            + json.dumps(
                comment_receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n```"
        )
    if len(body.encode("utf-8")) >= MAX_GITHUB_COMMENT_BYTES:
        raise ApplyError("compact warm readiness comment exceeds GitHub limit")
    return body


def _recovery_artifact_name(pr: int, run_id: int) -> str:
    return f"production-apply-receipt-pr-{pr}-run-{run_id}"


def _extract_recovery_receipt(raw_zip: bytes, expected_sha256: str) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if len(files) != 1 or files[0].filename != RECOVERY_ARTIFACT_FILE:
                raise ApplyError("recovery artifact shape is invalid")
            if (
                files[0].file_size <= 0
                or files[0].file_size > MAX_RECOVERY_ARTIFACT_BYTES
            ):
                raise ApplyError("recovery receipt file size is invalid")
            raw_receipt = archive.read(files[0])
    except zipfile.BadZipFile as exc:
        raise ApplyError("recovery artifact ZIP is invalid") from exc
    if digest(raw_receipt) != expected_sha256:
        raise ApplyError("recovery receipt digest mismatch")
    try:
        receipt = json.loads(raw_receipt.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplyError("recovery receipt JSON is invalid") from exc
    if not isinstance(receipt, dict):
        raise ApplyError("recovery receipt shape is invalid")
    if raw_receipt != canonical_json_bytes(receipt) + b"\n":
        raise ApplyError("recovery receipt bytes are not canonical")
    return receipt


def _collect_recovery_receipt(
    client: GitHubClient,
    *,
    pr: int,
    run_id: int,
    artifact_name: str,
    receipt_sha256: str,
    expected_conclusion: str = "failure",
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_name = _recovery_artifact_name(pr, run_id)
    if artifact_name != expected_name:
        raise ApplyError("recovery artifact name binding mismatch")
    run = client.get(f"/actions/runs/{run_id}")
    if not isinstance(run, Mapping):
        raise ApplyError("recovery source run shape is invalid")
    repository = run.get("repository")
    expected_run = {
        "id": run_id,
        "name": RECOVERY_WORKFLOW_NAME,
        "path": RECOVERY_WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": expected_conclusion,
        "head_branch": "main",
    }
    for field, value in expected_run.items():
        if run.get(field) != value:
            raise ApplyError(f"recovery source run binding mismatch: {field}")
    if (
        not isinstance(repository, Mapping)
        or repository.get("full_name") != client.repository
    ):
        raise ApplyError("recovery source run repository mismatch")
    run_head = exact_sha(run.get("head_sha"), "recovery-run-head")
    matches: list[Mapping[str, Any]] = []
    for page in range(1, 11):
        payload = client.get(
            f"/actions/runs/{run_id}/artifacts?per_page=100&page={page}"
        )
        values = payload.get("artifacts") if isinstance(payload, Mapping) else None
        if not isinstance(values, list):
            raise ApplyError("recovery artifact listing shape is invalid")
        matches.extend(
            item
            for item in values
            if isinstance(item, Mapping) and item.get("name") == artifact_name
        )
        if len(values) < 100:
            break
    else:
        raise ApplyError("recovery artifact pagination bound exceeded")
    if len(matches) != 1:
        raise ApplyError("recovery artifact is missing or ambiguous")
    artifact = matches[0]
    artifact_run = artifact.get("workflow_run")
    if (
        artifact.get("expired") is True
        or not isinstance(artifact.get("id"), int)
        or not isinstance(artifact.get("size_in_bytes"), int)
        or int(artifact["size_in_bytes"]) <= 0
        or int(artifact["size_in_bytes"]) > MAX_RECOVERY_ARTIFACT_BYTES
        or not isinstance(artifact_run, Mapping)
        or artifact_run.get("id") != run_id
        or artifact_run.get("head_branch") != "main"
        or artifact_run.get("head_sha") != run_head
    ):
        raise ApplyError("recovery artifact provenance mismatch")
    raw_zip = client.request(
        "GET",
        f"/actions/artifacts/{int(artifact['id'])}/zip",
        accept="application/vnd.github+json",
        raw=True,
    )
    if not isinstance(raw_zip, bytes):
        raise ApplyError("recovery artifact download shape is invalid")
    run_with_artifact = dict(run)
    run_with_artifact["validated_artifact"] = dict(artifact)
    return run_with_artifact, _extract_recovery_receipt(raw_zip, receipt_sha256)


def _validate_recovery_receipt(
    receipt: Mapping[str, Any],
    *,
    repository: str,
    pr: int,
    merge_sha: str,
    run_head_sha: str,
    authorization_comment_id: int,
    expected_operation: str,
    goal: Mapping[str, Any],
) -> None:
    expected_apply_count = 2 if goal.get("profile") == WBC0027_GOAL_PROFILE else 1
    expected = {
        "schema": APPLY_RECEIPT_SCHEMA,
        "state": "done",
        "operation_id": expected_operation,
        "repository": repository,
        "pull_request": pr,
        "merge_sha": merge_sha,
        "deployed_sha": merge_sha,
        "authorization_comment_id": authorization_comment_id,
        "apply_count": expected_apply_count,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ApplyError(f"recovery receipt binding mismatch: {field}")
    if merge_sha != run_head_sha:
        raise ApplyError("recovery source run head is not the exact merge SHA")
    if receipt.get("goal") != dict(goal):
        raise ApplyError("recovery receipt goal binding mismatch")
    derived_operation = operation_id(
        repository,
        pr,
        authorization_comment_id,
        goal,
    )
    if derived_operation != expected_operation:
        raise ApplyError("recovery operation derivation mismatch")
    release_operation = str(receipt.get("release_operation_id") or "")
    if re.fullmatch(r"release-v2-[0-9a-f]{32}", release_operation) is None:
        raise ApplyError("recovery release operation id is invalid")
    evidence = receipt.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ApplyError("recovery receipt evidence is missing")
    expected_evidence = {
        "state": "done",
        "reason": "reconciled",
        "apply_count": expected_apply_count,
    }
    for field, value in expected_evidence.items():
        if evidence.get(field) != value:
            raise ApplyError(f"recovery receipt evidence mismatch: {field}")
    if goal.get("profile") == WBC0027_GOAL_PROFILE:
        predecessor = receipt.get("superseded_predecessor_evidence")
        if not (
            isinstance(predecessor, Mapping)
            and predecessor.get("binding") == WBC0027_PREDECESSOR_BINDING
            and predecessor.get("state") == "blocked"
            and predecessor.get("apply_count") == 0
            and predecessor.get("product_lifecycle") == "missing"
            and predecessor.get("economics_lifecycle") == "missing"
            and predecessor.get("private_manifests_reusable") is False
            and predecessor.get("terminal_operation_reusable") is False
        ):
            raise ApplyError("WBC0027 terminal predecessor evidence is invalid")
        if not (
            evidence.get("product_submit_count") == 1
            and evidence.get("economics_submit_count") == 1
            and evidence.get("product_state") == "applied"
            and evidence.get("economics_state") == "applied"
        ):
            raise ApplyError("WBC0027 recovery phase states are invalid")
        candidates = {
            "product": evidence.get("product_candidate"),
            "economics": evidence.get("economics_candidate"),
        }
        for phase in ("product", "economics"):
            candidate = candidates[phase]
            apply_evidence = evidence.get(f"{phase}_apply")
            readback_evidence = evidence.get(f"{phase}_readback")
            apply_result = (
                apply_evidence.get("result")
                if isinstance(apply_evidence, Mapping)
                else None
            )
            readback = (
                readback_evidence.get("result")
                if isinstance(readback_evidence, Mapping)
                else None
            )
            attempts = (evidence.get("qualification_attempts") or {}).get(phase)
            apply_transport_ambiguous = bool(
                isinstance(apply_evidence, Mapping)
                and apply_evidence.get("transport_ambiguous") is True
                and apply_evidence.get("return_code") is None
            )
            apply_confirmed = bool(
                isinstance(apply_evidence, Mapping)
                and apply_evidence.get("return_code") == 0
                and apply_evidence.get("transport_ambiguous") is False
                and isinstance(apply_result, Mapping)
                and apply_result.get("status") == "applied"
                and apply_result.get("phase") == phase
                and apply_result.get("phase_operation_id")
                == (
                    candidate.get("phase_operation_id")
                    if isinstance(candidate, Mapping)
                    else None
                )
                and apply_result.get("production_mutation_submit_count") == 1
            )
            if not (
                isinstance(candidate, Mapping)
                and re.fullmatch(
                    r"recovery_[0-9a-f]{32}",
                    str(candidate.get("phase_operation_id") or ""),
                )
                and re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    str(candidate.get("material_qualification_digest") or ""),
                )
                and (apply_confirmed or apply_transport_ambiguous)
                and isinstance(readback_evidence, Mapping)
                and readback_evidence.get("return_code") == 0
                and readback_evidence.get("transport_ambiguous") is False
                and isinstance(readback, Mapping)
                and readback.get("status") == "reconciled"
                and readback.get("query_only") is True
                and readback.get("database_written") is False
                and readback.get("phase") == phase
                and readback.get("goal_operation_id") == expected_operation
                and readback.get("phase_operation_id")
                == candidate.get("phase_operation_id")
                and readback.get("deployed_sha") == merge_sha
                and isinstance(attempts, list)
                and len(attempts) >= 2
                and attempts[-1].get("qualification_state") == "qualified"
                and attempts[-2].get("material_qualification_digest")
                == attempts[-1].get("material_qualification_digest")
            ):
                raise ApplyError(f"WBC0027 {phase} recovery evidence is invalid")
        return
    qualified = evidence.get("qualified_manifest")
    apply_evidence = evidence.get("apply")
    readback_evidence = evidence.get("readback")
    if not all(
        isinstance(item, Mapping)
        for item in (qualified, apply_evidence, readback_evidence)
    ):
        raise ApplyError("recovery receipt terminal evidence is incomplete")
    readback = readback_evidence.get("result")
    manifest_sha = qualified.get("sha256")
    apply_result = apply_evidence.get("result")
    if goal["profile"] == WARM_ARCHIVE_GOAL_PROFILE:
        readback_job = readback.get("job") if isinstance(readback, Mapping) else None
        mutation_scope = (
            readback.get("mutation_scope_reconciliation")
            if isinstance(readback, Mapping)
            else None
        )
        apply_request = (
            readback_job.get("request") if isinstance(readback_job, Mapping) else None
        )
        if (
            readback_evidence.get("return_code") != 0
            or readback_evidence.get("transport_ambiguous") is not False
            or not isinstance(readback, Mapping)
            or readback.get("status") != "reconciled"
            or readback.get("query_only") is not True
            or readback.get("deployed_sha") != merge_sha
            or readback.get("source_count") != goal["expected_source_count"]
            or readback.get("source_absent_count") != goal["expected_source_count"]
            or readback.get("archive_count") != goal["expected_archive_count"]
            or readback.get("manifest_count") != goal["expected_manifest_count"]
            or readback.get("raw_unlink_count") != goal["expected_unlink_count"]
            or readback.get("reclaimed_allocated_bytes")
            != goal["expected_reclaimed_allocated_bytes"]
            or readback.get("manifest_sha256") != manifest_sha
            or readback.get("exact_manifest_apply_receipt_count") != 1
            or readback.get("root_minimum_passed") is not True
            or readback.get("backup_capacity_guard_passed") is not True
            or readback.get("services_healthy") is not True
            or readback.get("non_target_preserved") is not True
            or not isinstance(mutation_scope, Mapping)
            or mutation_scope.get("exact") is not True
            or mutation_scope.get("non_target_unlink_move_write_count") != 0
            or readback.get("promo_action_count") != 0
            or readback.get("business_data_mutation_count") != 0
            or not isinstance(manifest_sha, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha) is None
            or not isinstance(apply_request, Mapping)
            or apply_request.get("manifest_sha256") != manifest_sha
            or apply_request.get("operation") != "warm-archive-apply"
        ):
            raise ApplyError("recovery receipt warm archive proof is inconsistent")
        return
    if (
        readback_evidence.get("return_code") != 0
        or readback_evidence.get("transport_ambiguous") is not False
        or not isinstance(readback, Mapping)
        or readback.get("status") != "reconciled"
        or readback.get("mode") != "query-only-readback"
        or readback.get("query_only") is not True
        or readback.get("database_written") is not False
        or readback.get("deployed_sha") != merge_sha
        or readback.get("inserted_capture_count")
        != goal["expected_inserted_capture_count"]
        or readback.get("inserted_component_count")
        != goal["expected_inserted_component_count"]
        or readback.get("inserted_finalization_count")
        != goal["expected_inserted_finalization_count"]
        or readback.get("visible_history_date_count") != goal["date_count"]
        or readback.get("visible_history_quality")
        != {
            "full": goal["expected_full_date_count"],
            "partial": goal["expected_partial_date_count"],
            "unavailable": 0,
        }
        or readback.get("exact_manifest_apply_receipt_count") != 1
        or readback.get("total_inventory_history_apply_receipt_count") != 1
        or readback.get("non_target_preserved") is not True
    ):
        raise ApplyError("recovery receipt readback is not exact reconciled proof")
    if (
        not isinstance(manifest_sha, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha) is None
        or not isinstance(apply_result, Mapping)
        or apply_result.get("manifest_sha256") != manifest_sha
        or readback.get("manifest_sha256") != manifest_sha
        or apply_result.get("status") != "reconciled"
        or apply_result.get("database_written") is not True
        or apply_result.get("non_target_preserved") is not True
    ):
        raise ApplyError("recovery receipt apply evidence is inconsistent")


def _comment_payload(comment: Mapping[str, Any], operation: str) -> dict[str, Any]:
    body = str(comment.get("body") or "")
    if body.count(marker(operation)) != 1 or body.count("```json") != 1:
        raise ApplyError("existing recovery comment shape is ambiguous")
    try:
        payload_text = body.split("```json", 1)[1].split("```", 1)[0]
        payload = json.loads(payload_text)
    except (IndexError, json.JSONDecodeError) as exc:
        raise ApplyError("existing recovery comment JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ApplyError("existing recovery comment payload is invalid")
    return payload


def _recovery_comment_body(
    receipt: Mapping[str, Any],
    *,
    run_id: int,
    artifact_name: str,
    receipt_sha256: str,
) -> str:
    del run_id
    return _compact_apply_comment_body(
        receipt,
        artifact_name=artifact_name,
        receipt_sha256=receipt_sha256,
        receipt_size_bytes=len(canonical_json_bytes(receipt) + b"\n"),
    )


def _comment_matches_receipt(
    comment: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any],
    operation: str,
    artifact_name: str,
    receipt_sha256: str,
) -> bool:
    if not is_actions_bot_comment(comment):
        return False
    payload = _comment_payload(comment, operation)
    if payload == dict(receipt):
        return True
    artifact = payload.get("artifact") or {}
    full_receipt = payload.get("full_receipt") or {}
    return bool(
        payload.get("schema") == APPLY_COMMENT_SUMMARY_SCHEMA
        and payload.get("state") == receipt.get("state")
        and payload.get("operation_id") == operation
        and payload.get("apply_count") == receipt.get("apply_count")
        and artifact.get("name") == artifact_name
        and artifact.get("file") == RECOVERY_ARTIFACT_FILE
        and artifact.get("sha256") == "sha256:" + receipt_sha256
        and full_receipt.get("schema") == receipt.get("schema")
        and full_receipt.get("sha256") == "sha256:" + receipt_sha256
    )


def _run_receipt_recovery(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> int:
    if args.source_run_id is None or args.source_run_id <= 0:
        raise ApplyError("receipt-recovery mode requires a positive source run id")
    if not args.source_artifact_name:
        raise ApplyError("receipt-recovery mode requires a source artifact name")
    if re.fullmatch(r"[0-9a-f]{64}", str(args.source_receipt_sha256 or "")) is None:
        raise ApplyError("receipt-recovery mode requires an exact receipt SHA-256")
    if not args.operation_id:
        raise ApplyError("receipt-recovery mode requires an exact operation id")
    run, receipt = _collect_recovery_receipt(
        client,
        pr=args.pr,
        run_id=args.source_run_id,
        artifact_name=args.source_artifact_name,
        receipt_sha256=args.source_receipt_sha256,
    )
    merge_sha = exact_sha(pr.get("merge_commit_sha"), "pr-merge")
    authorization = client.get(f"/issues/comments/{args.authorization_comment_id}")
    goal = validate_authorization(
        authorization,
        repository=args.repository,
        pr=args.pr,
    )
    configured_profiles = {
        item.strip()
        for item in os.environ.get("WB_CORE_SCOPE_GOAL_PROFILE_ALLOWLIST", "").split(",")
        if item.strip()
    }
    if configured_profiles and goal["profile"] not in configured_profiles:
        raise ApplyError("scope-goal profile is not workflow-allowlisted")
    _validate_recovery_receipt(
        receipt,
        repository=args.repository,
        pr=args.pr,
        merge_sha=merge_sha,
        run_head_sha=exact_sha(run.get("head_sha"), "recovery-run-head"),
        authorization_comment_id=args.authorization_comment_id,
        expected_operation=args.operation_id,
        goal=goal,
    )
    authorization_body = str(authorization.get("body") or "").strip()
    if receipt.get("authorization_body_sha256") != digest(
        authorization_body.encode("utf-8")
    ):
        raise ApplyError("recovery authorization body digest mismatch")
    parse_release_receipt(
        comments,
        pr=args.pr,
        release_operation=str(receipt["release_operation_id"]),
        merge_sha=merge_sha,
    )
    # Persist the already-verified immutable source before attempting publication.
    # A comment transport failure therefore remains recoverable without touching
    # production or trusting a reconstructed payload.
    _write_receipt(args.output, receipt)
    body = _recovery_comment_body(
        receipt,
        run_id=args.source_run_id,
        artifact_name=args.source_artifact_name,
        receipt_sha256=args.source_receipt_sha256,
    )
    marked = [
        item
        for item in comments
        if marker(args.operation_id) in str(item.get("body") or "")
    ]
    if len(marked) > 1:
        raise ApplyError("duplicate or ambiguous durable apply receipt")
    if marked:
        existing = marked[0]
        if not _comment_matches_receipt(
            existing,
            receipt=receipt,
            operation=args.operation_id,
            artifact_name=args.source_artifact_name,
            receipt_sha256=args.source_receipt_sha256,
        ):
            raise ApplyError("existing durable apply receipt does not match source")
        publication_state = "already_terminal"
        published = existing
    else:
        published = client.post(f"/issues/{args.pr}/comments", {"body": body})
        if (
            not isinstance(published, Mapping)
            or not is_actions_bot_comment(published)
            or published.get("body") != body
        ):
            raise ApplyError("recovered receipt publication response mismatch")
        readback_comments = list_comments(client, args.pr)
        readback_marked = [
            item
            for item in readback_comments
            if marker(args.operation_id) in str(item.get("body") or "")
        ]
        if len(readback_marked) != 1 or not _comment_matches_receipt(
            readback_marked[0],
            receipt=receipt,
            operation=args.operation_id,
            artifact_name=args.source_artifact_name,
            receipt_sha256=args.source_receipt_sha256,
        ):
            raise ApplyError("recovered receipt publication readback mismatch")
        published = readback_marked[0]
        publication_state = "published"
    comment_id = published.get("id")
    if not isinstance(comment_id, int) or comment_id <= 0:
        raise ApplyError("recovered receipt comment id is invalid")
    print(
        json.dumps(
            {
                "state": publication_state,
                "operation_id": args.operation_id,
                "source_run_id": args.source_run_id,
                "source_artifact_name": args.source_artifact_name,
                "source_receipt_sha256": args.source_receipt_sha256,
                "comment_id": comment_id,
                "comment_body_sha256": digest(str(published["body"]).encode("utf-8")),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _warm_reconciliation_artifact_name(pr: int, run_id: int) -> str:
    return f"root-warm-archive-reconciliation-pr-{pr}-run-{run_id}"


def _validate_warm_reconciliation_source_receipt(
    receipt: Mapping[str, Any],
    *,
    repository: str,
    pr: int,
    merge_sha: str,
    authorization_comment_id: int,
    expected_operation: str,
    goal: Mapping[str, Any],
) -> dict[str, Any]:
    expected = {
        "schema": APPLY_RECEIPT_SCHEMA,
        "state": "blocked",
        "operation_id": expected_operation,
        "repository": repository,
        "pull_request": pr,
        "merge_sha": merge_sha,
        "deployed_sha": merge_sha,
        "authorization_comment_id": authorization_comment_id,
        "apply_count": 1,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ApplyError(f"warm reconciliation source mismatch: {field}")
    if (
        receipt.get("goal") != dict(goal)
        or goal.get("profile") != WARM_ARCHIVE_GOAL_PROFILE
        or goal.get("task") != "WBC0008"
        or goal.get("target_id") != "wb_core_eu_hosted_runtime_active"
        or goal.get("expected_source_count") != 6
        or goal.get("expected_archive_count") != 6
        or goal.get("expected_manifest_count") != 6
        or goal.get("expected_unlink_count") != 6
        or goal.get("expected_reclaimed_allocated_bytes") != 27_591_725_056
        or goal.get("root_minimum_after_bytes") != 25 * 1024**3
        or goal.get("max_mutation_submits") != 1
    ):
        raise ApplyError("warm reconciliation source goal binding mismatch")
    if (
        operation_id(repository, pr, authorization_comment_id, goal)
        != expected_operation
    ):
        raise ApplyError("warm reconciliation source operation derivation mismatch")
    readiness = receipt.get("warm_archive_readiness")
    evidence = receipt.get("evidence")
    if (
        not isinstance(readiness, Mapping)
        or readiness.get("schema") != WARM_READINESS_RECEIPT_SCHEMA
        or readiness.get("state") != "ready"
        or readiness.get("query_only") is not True
        or readiness.get("database_written") is not False
        or readiness.get("goal_operation_id") != expected_operation
        or readiness.get("deployed_sha") != merge_sha
        or not isinstance(evidence, Mapping)
        or evidence.get("state") != "blocked"
        or evidence.get("reason") != "post-submit-readback-not-reconciled"
        or evidence.get("apply_count") != 1
    ):
        raise ApplyError("warm reconciliation source terminal state/reason is invalid")
    qualified = evidence.get("qualified_manifest")
    apply_evidence = evidence.get("apply")
    readback_evidence = evidence.get("readback")
    if not all(
        isinstance(item, Mapping)
        for item in (qualified, apply_evidence, readback_evidence)
    ):
        raise ApplyError("warm reconciliation source evidence is incomplete")
    manifest_path = str(qualified.get("path") or "")
    manifest_sha = _require_fingerprint(qualified.get("sha256"), "source manifest")
    apply_result = apply_evidence.get("result")
    readback = readback_evidence.get("result")
    if (
        apply_evidence.get("return_code") != 0
        or apply_evidence.get("transport_ambiguous") is not False
        or not isinstance(apply_result, Mapping)
        or apply_result.get("job_id") is None
        or apply_result.get("status") != "queued"
        or apply_result.get("terminal") is not False
        or apply_result.get("submit_idempotent") is not False
        or not isinstance(apply_result.get("request"), Mapping)
        or apply_result["request"].get("operation") != "warm-archive-apply"
        or apply_result["request"].get("manifest") != manifest_path
        or apply_result["request"].get("manifest_sha256") != manifest_sha
        or apply_result["request"].get("goal_operation_id") != expected_operation
        or apply_result["request"].get("deployed_sha") != merge_sha
    ):
        raise ApplyError("warm reconciliation source submit evidence is invalid")
    job_id = str(apply_result["job_id"])
    job = readback.get("job") if isinstance(readback, Mapping) else None
    job_request = job.get("request") if isinstance(job, Mapping) else None
    job_result = job.get("result") if isinstance(job, Mapping) else None
    mutation_scope = (
        readback.get("mutation_scope_reconciliation")
        if isinstance(readback, Mapping)
        else None
    )
    if (
        readback_evidence.get("return_code") != 0
        or readback_evidence.get("transport_ambiguous") is not False
        or not isinstance(readback, Mapping)
        or readback.get("status") != "blocked"
        or readback.get("query_only") is not True
        or readback.get("deployed_sha") != merge_sha
        or readback.get("operation_id") != expected_operation
        or readback.get("manifest_sha256") != manifest_sha
        or readback.get("source_count") != 6
        or readback.get("source_absent_count") != 6
        or readback.get("archive_count") != 6
        or readback.get("manifest_count") != 6
        or readback.get("raw_unlink_count") != 6
        or readback.get("reclaimed_allocated_bytes") != 27_591_725_056
        or readback.get("root_minimum_passed") is not True
        or readback.get("backup_capacity_guard_passed") is not False
        or readback.get("services_healthy") is not True
        or readback.get("non_target_preserved") is not True
        or readback.get("promo_action_count") != 0
        or readback.get("business_data_mutation_count") != 0
        or readback.get("exact_manifest_apply_receipt_count") != 1
        or not isinstance(mutation_scope, Mapping)
        or mutation_scope.get("exact") is not True
        or mutation_scope.get("non_target_unlink_move_write_count") != 0
        or not isinstance(job, Mapping)
        or job.get("job_id") != job_id
        or job.get("status") != "succeeded"
        or job.get("terminal") is not True
        or job.get("attempt") != 1
        or not isinstance(job_request, Mapping)
        or job_request.get("operation") != "warm-archive-apply"
        or job_request.get("goal_operation_id") != expected_operation
        or job_request.get("manifest") != manifest_path
        or job_request.get("manifest_sha256") != manifest_sha
        or job_request.get("deployed_sha") != merge_sha
        or not isinstance(job_result, Mapping)
        or job_result.get("status") != "complete"
        or job_result.get("operation_id") != expected_operation
        or job_result.get("manifest_path") != manifest_path
        or job_result.get("manifest_sha256") != manifest_sha
        or job_result.get("mutation_submit_count") != 1
        or job_result.get("raw_unlink_count") != 6
        or job_result.get("reclaimed_allocated_bytes") != 27_591_725_056
        or job_result.get("promo_action_count") != 0
        or job_result.get("business_data_mutation_count") != 0
        or payload_digest(job_result) != str(job.get("result_digest") or "")
    ):
        raise ApplyError("warm reconciliation source terminal readback is inconsistent")
    request_digest = _require_fingerprint(
        job.get("request_digest"), "source job request"
    )
    result_digest = _require_fingerprint(job.get("result_digest"), "source job result")
    return {
        "readiness_id": str(readiness.get("readiness_id") or ""),
        "job_id": job_id,
        "job_request_digest": request_digest,
        "job_result_digest": result_digest,
        "manifest_path": manifest_path,
        "manifest_sha256": manifest_sha,
        "expected_reclaimed_allocated_bytes": 27_591_725_056,
        "required_backup_floor_bytes": int(goal["required_backup_floor_bytes"]),
        "release_operation_id": str(receipt.get("release_operation_id") or ""),
    }


def _require_fingerprint(value: Any, label: str) -> str:
    text = str(value or "")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", text) is None:
        raise ApplyError(f"{label} digest is invalid")
    return text


def _validate_source_blocked_marker(
    comments: list[Mapping[str, Any]],
    *,
    comment_id: int,
    operation: str,
    artifact_name: str,
    receipt_sha256: str,
    job_id: str,
) -> dict[str, Any]:
    matches = [item for item in comments if item.get("id") == comment_id]
    if len(matches) != 1 or not is_actions_bot_comment(matches[0]):
        raise ApplyError("exact blocked marker comment is missing or foreign")
    marked = [
        item for item in comments if marker(operation) in str(item.get("body") or "")
    ]
    if len(marked) != 1 or marked[0].get("id") != comment_id:
        raise ApplyError("source blocked marker is duplicate or ambiguous")
    payload = _comment_payload(matches[0], operation)
    artifact = payload.get("artifact") or {}
    job = payload.get("job") or {}
    if (
        payload.get("schema") != APPLY_COMMENT_SUMMARY_SCHEMA
        or payload.get("state") != "blocked"
        or payload.get("reason") != "post-submit-readback-not-reconciled"
        or payload.get("operation_id") != operation
        or payload.get("apply_count") != 1
        or artifact.get("name") != artifact_name
        or artifact.get("file") != RECOVERY_ARTIFACT_FILE
        or artifact.get("sha256") != "sha256:" + receipt_sha256
        or job.get("job_id") != job_id
        or job.get("status") != "succeeded"
        or job.get("terminal") is not True
        or job.get("attempt") != 1
    ):
        raise ApplyError("source blocked marker binding is invalid")
    return dict(payload)


def _parse_reconciliation_comment_payload(
    comment: Mapping[str, Any], *, expected_marker: str
) -> dict[str, Any]:
    body = str(comment.get("body") or "")
    if (
        body.count(expected_marker) != 1
        or body.count("```json") != 1
        or not is_actions_bot_comment(comment)
    ):
        raise ApplyError("reconciliation marker is malformed or foreign")
    try:
        payload = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ApplyError("reconciliation marker JSON is malformed") from exc
    if not isinstance(payload, dict):
        raise ApplyError("reconciliation marker payload is not an object")
    return payload


def _validate_legacy_warm_reconciliation_a01(
    *,
    client: GitHubClient,
    comments: list[Mapping[str, Any]],
    args: argparse.Namespace,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        args.prior_reconciliation_run_id != WARM_RECONCILIATION_A01_RUN_ID
        or args.prior_reconciliation_artifact_id != WARM_RECONCILIATION_A01_ARTIFACT_ID
        or args.prior_reconciliation_artifact_name
        != WARM_RECONCILIATION_A01_ARTIFACT_NAME
        or args.prior_reconciliation_receipt_sha256
        != WARM_RECONCILIATION_A01_RECEIPT_SHA256
        or args.prior_reconciliation_comment_id != WARM_RECONCILIATION_A01_COMMENT_ID
    ):
        raise ApplyError("legacy reconciliation a01 immutable identity drifted")
    matches = [
        item
        for item in comments
        if item.get("id") == args.prior_reconciliation_comment_id
    ]
    if len(matches) != 1:
        raise ApplyError("exact legacy reconciliation a01 marker is missing")
    operation = str(source["operation_id"])
    marker_text = warm_reconciliation_marker(operation)
    payload = _parse_reconciliation_comment_payload(
        matches[0], expected_marker=marker_text
    )
    artifact = payload.get("artifact")
    legacy_release = payload.get("reconciliation_release")
    terminal_facts = payload.get("terminal_facts")
    if (
        set(payload)
        != {
            "artifact",
            "evidence_digest",
            "operation_id",
            "production_mutation_count",
            "query_only",
            "reason",
            "reconciliation_release",
            "schema",
            "source",
            "state",
            "terminal_disposition",
            "terminal_facts",
        }
        or payload.get("schema") != LEGACY_WARM_RECONCILIATION_SUMMARY_SCHEMA
        or payload.get("state") != "blocked"
        or payload.get("reason") != "query-only-reconciliation-not-proven"
        or payload.get("terminal_disposition") != "blocked"
        or payload.get("query_only") is not True
        or payload.get("production_mutation_count") != 0
        or payload.get("operation_id") != operation
        or payload_digest(payload) != WARM_RECONCILIATION_A01_MARKER_DIGEST
        or payload.get("source") != source
        or not isinstance(artifact, Mapping)
        or artifact.get("name") != args.prior_reconciliation_artifact_name
        or artifact.get("file") != WARM_RECONCILIATION_ARTIFACT_FILE
        or artifact.get("retention_days") != 90
        or artifact.get("sha256")
        != "sha256:" + str(args.prior_reconciliation_receipt_sha256)
        or not isinstance(legacy_release, Mapping)
        or set(legacy_release)
        != {
            "deployed_sha",
            "merge_sha",
            "plan_hash",
            "probe_source_sha256",
            "pull_request",
            "release_kind",
            "release_operation_id",
            "workflow_run_id",
        }
        or legacy_release.get("release_kind") != "repo_only"
        or legacy_release.get("pull_request") != WARM_RECONCILIATION_A01_RELEASE_PR
        or legacy_release.get("merge_sha") != WARM_RECONCILIATION_A01_RELEASE_MERGE_SHA
        or legacy_release.get("deployed_sha") is not None
        or not isinstance(terminal_facts, Mapping)
        or not terminal_facts
        or any(value is not None for value in terminal_facts.values())
    ):
        raise ApplyError("legacy reconciliation a01 marker binding is invalid")
    expected_artifact_name = _warm_reconciliation_artifact_name(
        int(source["pull_request"]), int(args.prior_reconciliation_run_id)
    )
    if artifact.get("name") != expected_artifact_name:
        raise ApplyError("legacy reconciliation a01 artifact/run binding differs")
    legacy_pr_number = int(legacy_release.get("pull_request") or 0)
    legacy_pr = client.get(f"/pulls/{legacy_pr_number}")
    if not isinstance(legacy_pr, Mapping) or legacy_pr.get("merged") is not True:
        raise ApplyError("legacy reconciliation a01 release PR is not merged")
    legacy_merge_sha = exact_sha(
        legacy_pr.get("merge_commit_sha"), "legacy-reconciliation-merge"
    )
    if legacy_merge_sha != legacy_release.get("merge_sha"):
        raise ApplyError("legacy reconciliation a01 release merge drifted")
    legacy_comments = list_comments(client, legacy_pr_number)
    parsed_release = parse_repo_only_release_receipt(
        legacy_comments,
        pr=legacy_pr_number,
        release_operation=str(legacy_release.get("release_operation_id") or ""),
        merge_sha=legacy_merge_sha,
    )
    if parsed_release.get("workflow_run_id") != legacy_release.get(
        "workflow_run_id"
    ) or parsed_release.get("plan_hash") != legacy_release.get("plan_hash"):
        raise ApplyError("legacy reconciliation a01 release receipt drifted")
    verified = _verify_uploaded_warm_reconciliation_artifact(
        client,
        run_id=int(args.prior_reconciliation_run_id),
        artifact_name=str(args.prior_reconciliation_artifact_name),
        receipt_sha256=str(args.prior_reconciliation_receipt_sha256),
        code_sha=legacy_merge_sha,
    )
    metadata = verified.get("metadata")
    receipt = verified.get("receipt")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("id") != args.prior_reconciliation_artifact_id
        or metadata.get("digest") != WARM_RECONCILIATION_A01_ARCHIVE_DIGEST
        or not isinstance(receipt, Mapping)
        or set(receipt)
        != {
            "evidence_digest",
            "probe",
            "production_mutation_count",
            "query_only",
            "reason",
            "reconciliation_release",
            "schema",
            "source",
            "state",
            "terminal_disposition",
        }
        or receipt.get("schema") != LEGACY_WARM_RECONCILIATION_RECEIPT_SCHEMA
        or receipt.get("state") != "blocked"
        or receipt.get("reason") != "query-only-reconciliation-not-proven"
        or receipt.get("terminal_disposition") != "blocked"
        or receipt.get("query_only") is not True
        or receipt.get("production_mutation_count") != 0
        or receipt.get("source") != source
        or receipt.get("reconciliation_release") != legacy_release
        or receipt.get("evidence_digest") != payload.get("evidence_digest")
        or receipt.get("evidence_digest")
        != payload_digest(
            {key: value for key, value in receipt.items() if key != "evidence_digest"}
        )
    ):
        raise ApplyError("legacy reconciliation a01 artifact binding is invalid")
    probe_evidence = receipt.get("probe")
    probe_result = (
        probe_evidence.get("result") if isinstance(probe_evidence, Mapping) else None
    )
    error = probe_result.get("error") if isinstance(probe_result, Mapping) else None
    if (
        not isinstance(probe_evidence, Mapping)
        or probe_evidence.get("return_code") != 0
        or probe_evidence.get("transport_ambiguous") is not False
        or probe_evidence.get("stdin_sha256")
        != legacy_release.get("probe_source_sha256")
        or not isinstance(probe_result, Mapping)
        or probe_result.get("schema")
        != "wb-core.root-warm-archive-reconciliation-probe/v1"
        or probe_result.get("status") != "blocked"
        or probe_result.get("query_only") is not True
        or probe_result.get("production_mutation_count") != 0
        or not isinstance(error, Mapping)
        or error.get("type") != "ProbeError"
        or error.get("message")
        != (
            "systemd timer/service pair is unhealthy: "
            "wb-core-sheet-vitrina-refresh.timer"
        )
        or probe_result.get("evidence_digest")
        != payload_digest(
            {
                key: value
                for key, value in probe_result.items()
                if key != "evidence_digest"
            }
        )
    ):
        raise ApplyError(
            "legacy reconciliation a01 blocker is not the exact timer predicate"
        )
    canonical_receipt = canonical_json_bytes(receipt) + b"\n"
    if (
        int(artifact.get("size_bytes") or 0) != len(canonical_receipt)
        or digest(canonical_receipt) != args.prior_reconciliation_receipt_sha256
    ):
        raise ApplyError(
            "legacy reconciliation a01 marker artifact size/digest drifted"
        )
    return {
        "attempt": "a01",
        "legacy": True,
        "state": "blocked",
        "reason": "query-only-reconciliation-not-proven",
        "run_id": int(args.prior_reconciliation_run_id),
        "artifact_id": int(args.prior_reconciliation_artifact_id),
        "artifact_name": str(args.prior_reconciliation_artifact_name),
        "artifact_sha256": "sha256:" + str(args.prior_reconciliation_receipt_sha256),
        "marker_comment_id": int(args.prior_reconciliation_comment_id),
        "marker_digest": payload_digest(payload),
        "evidence_digest": str(receipt["evidence_digest"]),
        "production_mutation_count": 0,
        "operation_id": operation,
        "job_id": str(source["job_id"]),
        "reconciliation_release": dict(legacy_release),
    }


def _validate_exhausted_warm_reconciliation_a02(
    *,
    client: GitHubClient,
    comments: list[Mapping[str, Any]],
    args: argparse.Namespace,
    source: Mapping[str, Any],
    prior_a01: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        args.prior_reconciliation_a02_run_id != WARM_RECONCILIATION_A02_RUN_ID
        or args.prior_reconciliation_a02_artifact_id
        != WARM_RECONCILIATION_A02_ARTIFACT_ID
        or args.prior_reconciliation_a02_artifact_name
        != WARM_RECONCILIATION_A02_ARTIFACT_NAME
        or args.prior_reconciliation_a02_receipt_sha256
        != WARM_RECONCILIATION_A02_RECEIPT_SHA256
        or args.prior_reconciliation_a02_comment_id
        != WARM_RECONCILIATION_A02_COMMENT_ID
    ):
        raise ApplyError("exhausted legacy reconciliation a02 identity drifted")
    matches = [
        item
        for item in comments
        if item.get("id") == args.prior_reconciliation_a02_comment_id
    ]
    if len(matches) != 1:
        raise ApplyError("exact exhausted legacy reconciliation a02 marker is missing")
    operation = str(source["operation_id"])
    payload = _parse_reconciliation_comment_payload(
        matches[0], expected_marker=warm_reconciliation_marker(operation, "a02")
    )
    artifact = payload.get("artifact")
    release = payload.get("reconciliation_release")
    sequence = payload.get("reconciliation_sequence")
    if (
        set(payload)
        != {
            "artifact",
            "attempt",
            "evidence_digest",
            "operation_id",
            "production_mutation_count",
            "query_only",
            "reason",
            "reconciliation_release",
            "reconciliation_sequence",
            "schema",
            "source",
            "state",
            "terminal_disposition",
            "terminal_facts",
        }
        or payload.get("schema") != LEGACY_WARM_RECONCILIATION_A02_SUMMARY_SCHEMA
        or payload.get("attempt") != "a02"
        or payload.get("state") != "blocked"
        or payload.get("reason") != "query-only-reconciliation-not-proven"
        or payload.get("terminal_disposition") != "blocked"
        or payload.get("query_only") is not True
        or payload.get("production_mutation_count") != 0
        or payload.get("operation_id") != operation
        or payload.get("source") != source
        or payload_digest(payload) != WARM_RECONCILIATION_A02_MARKER_DIGEST
        or not isinstance(artifact, Mapping)
        or artifact.get("name") != WARM_RECONCILIATION_A02_ARTIFACT_NAME
        or artifact.get("file") != WARM_RECONCILIATION_ARTIFACT_FILE
        or artifact.get("retention_days") != 90
        or artifact.get("sha256") != "sha256:" + WARM_RECONCILIATION_A02_RECEIPT_SHA256
        or not isinstance(release, Mapping)
        or release.get("release_kind") != "repo_only"
        or release.get("deployed_sha") is not None
        or release.get("pull_request") != WARM_RECONCILIATION_A02_RELEASE_PR
        or release.get("merge_sha") != WARM_RECONCILIATION_A02_RELEASE_MERGE_SHA
        or not isinstance(sequence, Mapping)
        or sequence.get("schema") != LEGACY_WARM_RECONCILIATION_SEQUENCE_SCHEMA
        or sequence.get("attempt") != "a02"
        or sequence.get("maximum_attempt") != "a02"
        or sequence.get("sequence_exhausted_after_attempt") is not True
        or sequence.get("prior_attempt") != prior_a01
    ):
        raise ApplyError("exhausted legacy reconciliation a02 marker is invalid")
    release_pr = client.get(f"/pulls/{WARM_RECONCILIATION_A02_RELEASE_PR}")
    if (
        not isinstance(release_pr, Mapping)
        or release_pr.get("merged") is not True
        or exact_sha(release_pr.get("merge_commit_sha"), "legacy-a02-release-merge")
        != WARM_RECONCILIATION_A02_RELEASE_MERGE_SHA
    ):
        raise ApplyError("legacy a02 repo-only release PR drifted")
    release_comments = list_comments(client, WARM_RECONCILIATION_A02_RELEASE_PR)
    release_receipt = parse_repo_only_release_receipt(
        release_comments,
        pr=WARM_RECONCILIATION_A02_RELEASE_PR,
        release_operation=str(release.get("release_operation_id") or ""),
        merge_sha=WARM_RECONCILIATION_A02_RELEASE_MERGE_SHA,
    )
    if release_receipt.get("workflow_run_id") != release.get(
        "workflow_run_id"
    ) or release_receipt.get("plan_hash") != release.get("plan_hash"):
        raise ApplyError("legacy a02 release receipt drifted")
    verified = _verify_uploaded_warm_reconciliation_artifact(
        client,
        run_id=WARM_RECONCILIATION_A02_RUN_ID,
        artifact_name=WARM_RECONCILIATION_A02_ARTIFACT_NAME,
        receipt_sha256=WARM_RECONCILIATION_A02_RECEIPT_SHA256,
        code_sha=WARM_RECONCILIATION_A02_RELEASE_MERGE_SHA,
    )
    metadata = verified.get("metadata")
    receipt = verified.get("receipt")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("id") != WARM_RECONCILIATION_A02_ARTIFACT_ID
        or metadata.get("digest") != WARM_RECONCILIATION_A02_ARCHIVE_DIGEST
        or not isinstance(receipt, Mapping)
        or set(receipt)
        != {
            "attempt",
            "evidence_digest",
            "probe",
            "production_mutation_count",
            "query_only",
            "reason",
            "reconciliation_release",
            "reconciliation_sequence",
            "schema",
            "source",
            "state",
            "terminal_disposition",
        }
        or receipt.get("schema") != LEGACY_WARM_RECONCILIATION_A02_RECEIPT_SCHEMA
        or receipt.get("attempt") != "a02"
        or receipt.get("state") != "blocked"
        or receipt.get("reason") != "query-only-reconciliation-not-proven"
        or receipt.get("terminal_disposition") != "blocked"
        or receipt.get("query_only") is not True
        or receipt.get("production_mutation_count") != 0
        or receipt.get("source") != source
        or receipt.get("reconciliation_release") != release
        or receipt.get("reconciliation_sequence") != sequence
        or receipt.get("evidence_digest") != payload.get("evidence_digest")
        or receipt.get("evidence_digest")
        != payload_digest(
            {key: value for key, value in receipt.items() if key != "evidence_digest"}
        )
    ):
        raise ApplyError("exhausted legacy reconciliation a02 artifact is invalid")
    probe_evidence = receipt.get("probe")
    probe_result = (
        probe_evidence.get("result") if isinstance(probe_evidence, Mapping) else None
    )
    error = probe_result.get("error") if isinstance(probe_result, Mapping) else None
    gate = (
        probe_result.get("systemd_service_gate")
        if isinstance(probe_result, Mapping)
        else None
    )
    pairs = gate.get("pairs") if isinstance(gate, Mapping) else None
    units = gate.get("units") if isinstance(gate, Mapping) else None
    raw_by_name = {
        str(row.get("name")): row.get("raw")
        for row in units or []
        if isinstance(row, Mapping) and isinstance(row.get("raw"), Mapping)
    }
    canary_timer = raw_by_name.get("wb-core-sheet-vitrina-canary-restore.timer")
    canary_owner = raw_by_name.get("wb-core-sheet-vitrina-canary-restore.service")
    root_owner = raw_by_name.get("wb-core-root-storage-policy.service")
    timer_rows = [
        raw
        for name, raw in raw_by_name.items()
        if name.endswith(".timer") and isinstance(raw, Mapping)
    ]
    if (
        not isinstance(probe_evidence, Mapping)
        or probe_evidence.get("return_code") != 0
        or probe_evidence.get("transport_ambiguous") is not False
        or not isinstance(probe_result, Mapping)
        or probe_result.get("schema")
        != "wb-core.root-warm-archive-reconciliation-probe/v2"
        or probe_result.get("status") != "blocked"
        or probe_result.get("query_only") is not True
        or probe_result.get("production_mutation_count") != 0
        or not isinstance(error, Mapping)
        or error.get("type") != "SystemdGateError"
        or error.get("message") != "systemd 27/12 paired health is not proven"
        or not isinstance(gate, Mapping)
        or gate.get("healthy") is not False
        or gate.get("unit_count") != 27
        or gate.get("pair_count") != 12
        or gate.get("failing_pair_count") != 12
        or not isinstance(pairs, list)
        or len(pairs) != 12
        or not all(
            isinstance(pair, Mapping)
            and pair.get("classification") == "failed_or_unknown_pair"
            and pair.get("timer_reason_codes") == ["required_properties_missing"]
            for pair in pairs
        )
        or len(raw_by_name) != 27
        or len(timer_rows) != 12
        or not all(
            "MainPID" not in set(row.get("ObservedProperties") or [])
            and "ExecMainStatus" not in set(row.get("ObservedProperties") or [])
            for row in timer_rows
        )
        or not isinstance(root_owner, Mapping)
        or root_owner.get("UnitFileState") != "disabled"
        or not isinstance(canary_timer, Mapping)
        or canary_timer.get("ActiveState") != "active"
        or canary_timer.get("SubState") != "running"
        or not isinstance(canary_owner, Mapping)
        or canary_owner.get("ActiveState") != "activating"
        or canary_owner.get("SubState") != "start"
        or canary_owner.get("MainPID") != "593451"
        or canary_owner.get("ExecMainStatus") != "0"
        or probe_result.get("evidence_digest")
        != payload_digest(
            {
                key: value
                for key, value in probe_result.items()
                if key != "evidence_digest"
            }
        )
    ):
        raise ApplyError("legacy a02 classifier defect evidence is not exact")
    canonical_receipt = canonical_json_bytes(receipt) + b"\n"
    if (
        int(artifact.get("size_bytes") or 0) != len(canonical_receipt)
        or digest(canonical_receipt) != WARM_RECONCILIATION_A02_RECEIPT_SHA256
    ):
        raise ApplyError("legacy a02 receipt size/digest drifted")
    return {
        "attempt": "a02",
        "generation": "legacy-v1",
        "state": "blocked",
        "reason": "query-only-reconciliation-not-proven",
        "run_id": WARM_RECONCILIATION_A02_RUN_ID,
        "artifact_id": WARM_RECONCILIATION_A02_ARTIFACT_ID,
        "artifact_name": WARM_RECONCILIATION_A02_ARTIFACT_NAME,
        "artifact_archive_digest": WARM_RECONCILIATION_A02_ARCHIVE_DIGEST,
        "artifact_receipt_sha256": "sha256:" + WARM_RECONCILIATION_A02_RECEIPT_SHA256,
        "marker_comment_id": WARM_RECONCILIATION_A02_COMMENT_ID,
        "marker_digest": WARM_RECONCILIATION_A02_MARKER_DIGEST,
        "evidence_digest": str(receipt["evidence_digest"]),
        "production_mutation_count": 0,
        "operation_id": operation,
        "job_id": str(source["job_id"]),
        "sequence": dict(sequence),
        "reconciliation_release": dict(release),
    }


def _warm_reconciliation_context(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    source_pr: Mapping[str, Any],
    source_comments: list[Mapping[str, Any]],
) -> dict[str, Any]:
    required = {
        "source_run_id": args.source_run_id,
        "source_artifact_name": args.source_artifact_name,
        "source_receipt_sha256": args.source_receipt_sha256,
        "operation_id": args.operation_id,
        "blocked_comment_id": args.blocked_comment_id,
        "reconciliation_pr": args.reconciliation_pr,
        "reconciliation_release_operation_id": args.reconciliation_release_operation_id,
        "reconciliation_attempt": args.reconciliation_attempt,
        "prior_reconciliation_run_id": args.prior_reconciliation_run_id,
        "prior_reconciliation_artifact_id": args.prior_reconciliation_artifact_id,
        "prior_reconciliation_artifact_name": args.prior_reconciliation_artifact_name,
        "prior_reconciliation_receipt_sha256": args.prior_reconciliation_receipt_sha256,
        "prior_reconciliation_comment_id": args.prior_reconciliation_comment_id,
        "prior_reconciliation_a02_run_id": args.prior_reconciliation_a02_run_id,
        "prior_reconciliation_a02_artifact_id": args.prior_reconciliation_a02_artifact_id,
        "prior_reconciliation_a02_artifact_name": args.prior_reconciliation_a02_artifact_name,
        "prior_reconciliation_a02_receipt_sha256": args.prior_reconciliation_a02_receipt_sha256,
        "prior_reconciliation_a02_comment_id": args.prior_reconciliation_a02_comment_id,
    }
    missing = sorted(field for field, value in required.items() if not value)
    if missing:
        raise ApplyError(
            "warm archive receipt reconciliation inputs are missing: "
            + ", ".join(missing)
        )
    if (
        int(args.source_run_id) <= 0
        or int(args.blocked_comment_id) <= 0
        or int(args.reconciliation_pr) <= 0
        or int(args.prior_reconciliation_run_id) <= 0
        or int(args.prior_reconciliation_artifact_id) <= 0
        or int(args.prior_reconciliation_comment_id) <= 0
        or int(args.prior_reconciliation_a02_run_id) <= 0
        or int(args.prior_reconciliation_a02_artifact_id) <= 0
        or int(args.prior_reconciliation_a02_comment_id) <= 0
    ):
        raise ApplyError("warm archive receipt reconciliation ids are invalid")
    if re.fullmatch(r"[0-9a-f]{64}", str(args.source_receipt_sha256)) is None:
        raise ApplyError("source receipt SHA-256 is invalid")
    if (
        args.reconciliation_attempt != WARM_RECONCILIATION_ATTEMPT
        or re.fullmatch(r"[0-9a-f]{64}", str(args.prior_reconciliation_receipt_sha256))
        is None
        or re.fullmatch(
            r"[0-9a-f]{64}",
            str(args.prior_reconciliation_a02_receipt_sha256),
        )
        is None
    ):
        raise ApplyError(
            "only the exact code-delta reconciliation generation v2-a01 is valid"
        )
    if (
        args.pr != WARM_RECONCILIATION_SOURCE_PR
        or args.source_run_id != WARM_RECONCILIATION_SOURCE_RUN_ID
        or args.source_artifact_name != WARM_RECONCILIATION_SOURCE_ARTIFACT_NAME
        or args.source_receipt_sha256 != WARM_RECONCILIATION_SOURCE_RECEIPT_SHA256
        or args.authorization_comment_id
        != WARM_RECONCILIATION_SOURCE_AUTHORIZATION_COMMENT_ID
        or args.blocked_comment_id != WARM_RECONCILIATION_SOURCE_BLOCKED_COMMENT_ID
        or args.operation_id != WARM_RECONCILIATION_SOURCE_OPERATION_ID
    ):
        raise ApplyError("completed exact-six source lineage drifted")
    source_merge_sha = exact_sha(source_pr.get("merge_commit_sha"), "source-pr-merge")
    if source_merge_sha != WARM_RECONCILIATION_SOURCE_DEPLOYED_SHA:
        raise ApplyError("completed exact-six source merge SHA drifted")
    run, source_receipt = _collect_recovery_receipt(
        client,
        pr=args.pr,
        run_id=args.source_run_id,
        artifact_name=args.source_artifact_name,
        receipt_sha256=args.source_receipt_sha256,
        expected_conclusion="success",
    )
    if exact_sha(run.get("head_sha"), "source-run-head") != source_merge_sha:
        raise ApplyError("source run head is not the exact source PR merge")
    authorization = client.get(f"/issues/comments/{args.authorization_comment_id}")
    goal = validate_authorization(
        authorization,
        repository=args.repository,
        pr=args.pr,
    )
    authorization_body = str(authorization.get("body") or "").strip()
    if source_receipt.get("authorization_body_sha256") != digest(
        authorization_body.encode("utf-8")
    ):
        raise ApplyError("source authorization body digest mismatch")
    source_binding = _validate_warm_reconciliation_source_receipt(
        source_receipt,
        repository=args.repository,
        pr=args.pr,
        merge_sha=source_merge_sha,
        authorization_comment_id=args.authorization_comment_id,
        expected_operation=str(args.operation_id),
        goal=goal,
    )
    if source_binding.get("job_id") != WARM_RECONCILIATION_SOURCE_JOB_ID:
        raise ApplyError("completed exact-six source operation/job binding drifted")
    parse_release_receipt(
        source_comments,
        pr=args.pr,
        release_operation=source_binding["release_operation_id"],
        merge_sha=source_merge_sha,
    )
    readiness = parse_warm_readiness_receipt(
        source_comments,
        repository=args.repository,
        pr=args.pr,
        release_operation=source_binding["release_operation_id"],
        merge_sha=source_merge_sha,
        authorization_comment_id=args.authorization_comment_id,
        goal_operation_id=str(args.operation_id),
    )
    if readiness.get("readiness_id") != source_binding["readiness_id"]:
        raise ApplyError("source readiness binding drifted")
    blocked_marker = _validate_source_blocked_marker(
        source_comments,
        comment_id=args.blocked_comment_id,
        operation=str(args.operation_id),
        artifact_name=str(args.source_artifact_name),
        receipt_sha256=str(args.source_receipt_sha256),
        job_id=source_binding["job_id"],
    )

    reconciliation_pr = client.get(f"/pulls/{args.reconciliation_pr}")
    if (
        not isinstance(reconciliation_pr, Mapping)
        or reconciliation_pr.get("merged") is not True
    ):
        raise ApplyError("reconciliation release PR is not merged")
    reconciliation_merge_sha = exact_sha(
        reconciliation_pr.get("merge_commit_sha"), "reconciliation-pr-merge"
    )
    code_sha = exact_sha(os.environ.get("GITHUB_SHA"), "reconciliation-code")
    if code_sha != reconciliation_merge_sha:
        raise ApplyError(
            "trusted reconciliation checkout is not the exact release merge"
        )
    reconciliation_comments = list_comments(client, args.reconciliation_pr)
    reconciliation_release = parse_repo_only_release_receipt(
        reconciliation_comments,
        pr=args.reconciliation_pr,
        release_operation=args.reconciliation_release_operation_id,
        merge_sha=reconciliation_merge_sha,
    )
    artifact = run.get("validated_artifact")
    if not isinstance(artifact, Mapping):
        raise ApplyError("source artifact provenance is missing")
    if (
        artifact.get("id") != WARM_RECONCILIATION_SOURCE_ARTIFACT_ID
        or artifact.get("digest") != WARM_RECONCILIATION_SOURCE_ARCHIVE_DIGEST
    ):
        raise ApplyError("source artifact immutable identity drifted")
    probe_source = (
        ROOT / "apps" / "wbc0008_warm_archive_receipt_reconciliation_probe.py"
    ).read_bytes()
    context = {
        "source": {
            "pull_request": args.pr,
            "run_id": args.source_run_id,
            "run_head_sha": source_merge_sha,
            "artifact_id": artifact.get("id"),
            "artifact_name": args.source_artifact_name,
            "receipt_file": RECOVERY_ARTIFACT_FILE,
            "receipt_sha256": "sha256:" + str(args.source_receipt_sha256),
            "receipt_state": "blocked",
            "receipt_reason": "post-submit-readback-not-reconciled",
            "authorization_comment_id": args.authorization_comment_id,
            "authorization_body_sha256": source_receipt.get(
                "authorization_body_sha256"
            ),
            "blocked_comment_id": args.blocked_comment_id,
            "blocked_comment_digest": payload_digest(blocked_marker),
            "release_operation_id": source_binding["release_operation_id"],
            "deployed_sha": source_merge_sha,
            "readiness_id": source_binding["readiness_id"],
            "operation_id": args.operation_id,
            "job_id": source_binding["job_id"],
            "job_request_digest": source_binding["job_request_digest"],
            "job_result_digest": source_binding["job_result_digest"],
            "manifest_path": source_binding["manifest_path"],
            "manifest_sha256": source_binding["manifest_sha256"],
            "expected_reclaimed_allocated_bytes": source_binding[
                "expected_reclaimed_allocated_bytes"
            ],
            "required_backup_floor_bytes": source_binding[
                "required_backup_floor_bytes"
            ],
        },
        "reconciliation_release": {
            "pull_request": args.reconciliation_pr,
            "release_operation_id": args.reconciliation_release_operation_id,
            "release_kind": "repo_only",
            "merge_sha": reconciliation_merge_sha,
            "workflow_run_id": reconciliation_release.get("workflow_run_id"),
            "plan_hash": reconciliation_release.get("plan_hash"),
            "deployed_sha": None,
            "probe_source_sha256": "sha256:" + digest(probe_source),
        },
    }
    prior_a01 = _validate_legacy_warm_reconciliation_a01(
        client=client,
        comments=source_comments,
        args=args,
        source=context["source"],
    )
    prior_a02 = _validate_exhausted_warm_reconciliation_a02(
        client=client,
        comments=source_comments,
        args=args,
        source=context["source"],
        prior_a01=prior_a01,
    )
    if (
        context["reconciliation_release"]["merge_sha"]
        in {
            prior_a01["reconciliation_release"]["merge_sha"],
            prior_a02["reconciliation_release"]["merge_sha"],
        }
        or context["reconciliation_release"]["pull_request"]
        in {
            prior_a01["reconciliation_release"]["pull_request"],
            prior_a02["reconciliation_release"]["pull_request"],
        }
        or context["reconciliation_release"]["probe_source_sha256"]
        == prior_a02["reconciliation_release"]["probe_source_sha256"]
    ):
        raise ApplyError(
            "v2-a01 requires a distinct repo-only classifier code-delta release"
        )
    prior_a01_lineage = {
        **{key: value for key, value in prior_a01.items() if key != "artifact_sha256"},
        "artifact_archive_digest": WARM_RECONCILIATION_A01_ARCHIVE_DIGEST,
        "artifact_receipt_sha256": prior_a01["artifact_sha256"],
        "generation": "legacy-v1",
    }
    generation_material = {
        "generation": "v2",
        "operation_id": context["source"]["operation_id"],
        "job_id": context["source"]["job_id"],
        "source_receipt_sha256": context["source"]["receipt_sha256"],
        "source_artifact_archive_digest": (WARM_RECONCILIATION_SOURCE_ARCHIVE_DIGEST),
        "prior_attempts": [prior_a01_lineage, prior_a02],
        "repo_only_release": context["reconciliation_release"],
    }
    generation_id = (
        "root-warm-archive-reconciliation-v2-"
        + digest(canonical_json_bytes(generation_material))[:32]
    )
    attempt_binding = {
        "generation_id": generation_id,
        "attempt": WARM_RECONCILIATION_ATTEMPT,
        "operation_id": context["source"]["operation_id"],
        "job_id": context["source"]["job_id"],
        "source_receipt_sha256": context["source"]["receipt_sha256"],
        "source_artifact_archive_digest": (WARM_RECONCILIATION_SOURCE_ARCHIVE_DIGEST),
        "prior_attempts": [prior_a01_lineage, prior_a02],
        "reconciliation_release": context["reconciliation_release"],
    }
    context["reconciliation_generation"] = {
        "schema": WARM_RECONCILIATION_GENERATION_SCHEMA,
        "generation": "v2",
        "generation_id": generation_id,
        "attempt": WARM_RECONCILIATION_ATTEMPT,
        "code_delta_required": True,
        "legacy_generation_exhausted": True,
        "source_artifact_archive_digest": (WARM_RECONCILIATION_SOURCE_ARCHIVE_DIGEST),
        "source_receipt_sha256": context["source"]["receipt_sha256"],
        "prior_attempts": [prior_a01_lineage, prior_a02],
        "repo_only_release": context["reconciliation_release"],
        "attempt_binding_digest": payload_digest(attempt_binding),
        "maximum_attempt": WARM_RECONCILIATION_ATTEMPT,
        "generation_exhausted_after_attempt": True,
    }
    return context


def _existing_warm_reconciliation_marker(
    comments: list[Mapping[str, Any]],
    *,
    context: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    operation = str(context["source"]["operation_id"])
    all_markers = [
        item
        for item in comments
        if WARM_RECONCILIATION_MARKER in str(item.get("body") or "")
    ]
    prior_ids = {
        int(item["marker_comment_id"])
        for item in context["reconciliation_generation"]["prior_attempts"]
    }
    if prior_ids != {
        WARM_RECONCILIATION_A01_COMMENT_ID,
        WARM_RECONCILIATION_A02_COMMENT_ID,
    } or any(
        len([item for item in all_markers if item.get("id") == prior_id]) != 1
        for prior_id in prior_ids
    ):
        raise ApplyError(
            "legacy reconciliation a01/a02 markers are missing or duplicate"
        )
    remaining = [item for item in all_markers if item.get("id") not in prior_ids]
    if not remaining:
        return None
    if len(remaining) != 1:
        raise ApplyError("reconciliation v2-a01 marker is duplicate or ambiguous")
    comment = remaining[0]
    payload = _parse_reconciliation_comment_payload(
        comment,
        expected_marker=warm_reconciliation_marker(
            operation, WARM_RECONCILIATION_ATTEMPT
        ),
    )
    source = payload.get("source") if isinstance(payload, Mapping) else None
    release = (
        payload.get("reconciliation_release") if isinstance(payload, Mapping) else None
    )
    generation = (
        payload.get("reconciliation_generation")
        if isinstance(payload, Mapping)
        else None
    )
    artifact = payload.get("artifact") if isinstance(payload, Mapping) else None
    if (
        set(payload)
        != {
            "artifact",
            "attempt",
            "evidence_digest",
            "operation_id",
            "production_mutation_count",
            "query_only",
            "reason",
            "reconciliation_release",
            "reconciliation_generation",
            "schema",
            "source",
            "state",
            "terminal_disposition",
            "terminal_facts",
        }
        or payload.get("schema") != WARM_RECONCILIATION_SUMMARY_SCHEMA
        or payload.get("state") not in {"done", "blocked"}
        or payload.get("attempt") != WARM_RECONCILIATION_ATTEMPT
        or payload.get("operation_id") != operation
        or payload.get("query_only") is not True
        or payload.get("production_mutation_count") != 0
        or source != context.get("source")
        or release != context.get("reconciliation_release")
        or generation != context.get("reconciliation_generation")
        or not isinstance(artifact, Mapping)
        or re.fullmatch(
            rf"root-warm-archive-reconciliation-pr-{int(context['source']['pull_request'])}-run-[1-9][0-9]*",
            str(artifact.get("name") or ""),
        )
        is None
        or artifact.get("file") != WARM_RECONCILIATION_ARTIFACT_FILE
        or artifact.get("retention_days") != 90
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("sha256") or ""))
        is None
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(payload.get("evidence_digest") or "")
        )
        is None
    ):
        raise ApplyError("preexisting reconciliation marker binding differs")
    if payload.get("state") == "done" and (
        payload.get("terminal_disposition") != "done/reconciled_existing_operation"
        or payload.get("reason") != "reconciled-existing-terminal-operation"
    ):
        raise ApplyError("preexisting reconciliation done marker is inconsistent")
    if payload.get("state") == "blocked" and (
        payload.get("terminal_disposition") != "blocked"
        or payload.get("reason") != "query-only-reconciliation-not-proven"
    ):
        raise ApplyError("preexisting reconciliation blocked marker is inconsistent")
    run_match = re.fullmatch(
        r"root-warm-archive-reconciliation-pr-[1-9][0-9]*-run-([1-9][0-9]*)",
        str(artifact["name"]),
    )
    if run_match is None:
        raise ApplyError("preexisting reconciliation artifact run is invalid")
    verified = _verify_uploaded_warm_reconciliation_artifact(
        context["client"],
        run_id=int(run_match.group(1)),
        artifact_name=str(artifact["name"]),
        receipt_sha256=str(artifact["sha256"])[len("sha256:") :],
        code_sha=str(context["reconciliation_release"]["merge_sha"]),
    )
    receipt = verified.get("receipt")
    if (
        not isinstance(receipt, Mapping)
        or set(receipt)
        != {
            "attempt",
            "evidence_digest",
            "probe",
            "production_mutation_count",
            "query_only",
            "reason",
            "reconciliation_release",
            "reconciliation_generation",
            "schema",
            "source",
            "state",
            "terminal_disposition",
        }
        or receipt.get("schema") != WARM_RECONCILIATION_RECEIPT_SCHEMA
        or receipt.get("state") != payload.get("state")
        or receipt.get("reason") != payload.get("reason")
        or receipt.get("attempt") != WARM_RECONCILIATION_ATTEMPT
        or receipt.get("terminal_disposition") != payload.get("terminal_disposition")
        or receipt.get("source") != context.get("source")
        or receipt.get("reconciliation_release")
        != context.get("reconciliation_release")
        or receipt.get("reconciliation_generation")
        != context.get("reconciliation_generation")
        or receipt.get("evidence_digest") != payload.get("evidence_digest")
        or receipt.get("production_mutation_count") != 0
        or (
            receipt.get("state") == "done"
            and not _valid_warm_reconciliation_probe(
                (receipt.get("probe") or {}).get("result"), context=context
            )
        )
        or receipt.get("evidence_digest")
        != payload_digest(
            {key: value for key, value in receipt.items() if key != "evidence_digest"}
        )
    ):
        raise ApplyError("preexisting reconciliation artifact digest differs")
    canonical_receipt = canonical_json_bytes(receipt) + b"\n"
    if artifact.get("size_bytes") != len(canonical_receipt) or artifact.get(
        "sha256"
    ) != "sha256:" + digest(canonical_receipt):
        raise ApplyError("preexisting reconciliation marker artifact metadata differs")
    return comment


def _write_github_output(path: str, values: Mapping[str, Any]) -> None:
    output = Path(path)
    with output.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(
                f"{key}={str(value).lower() if isinstance(value, bool) else value}\n"
            )


def _run_warm_reconciliation_preflight(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> int:
    context = _warm_reconciliation_context(
        args=args,
        client=client,
        source_pr=pr,
        source_comments=comments,
    )
    context["client"] = client
    existing = _existing_warm_reconciliation_marker(comments, context=context)
    if existing is None:
        state = "ready_for_probe"
        probe_required = True
        comment_id = 0
    else:
        state = "already_terminal"
        probe_required = False
        comment_id = int(existing.get("id") or 0)
        if comment_id <= 0:
            raise ApplyError("preexisting reconciliation comment id is invalid")
    receipt = {
        "schema": WARM_RECONCILIATION_RECEIPT_SCHEMA,
        "state": state,
        "attempt": WARM_RECONCILIATION_ATTEMPT,
        "query_only": True,
        "production_mutation_count": 0,
        "source": context["source"],
        "reconciliation_release": context["reconciliation_release"],
        "reconciliation_generation": context["reconciliation_generation"],
        "comment_id": comment_id,
    }
    _write_receipt(args.output, receipt)
    if args.github_output:
        _write_github_output(
            args.github_output,
            {
                "probe_required": probe_required,
                "state": state,
                "comment_id": comment_id,
            },
        )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def _warm_reconciliation_probe_command(
    *, target: Mapping[str, Any], binding: Mapping[str, Any]
) -> list[str]:
    raw = canonical_json_bytes(binding)
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    shell = (
        "set -eu; umask 077; cd /; "
        "export PYTHONDONTWRITEBYTECODE=1; "
        "exec python3 - " + shlex.quote(encoded)
    )
    if any(
        token in shell
        for token in (
            "systemctl start",
            "systemctl restart",
            "readback_batch",
            "warm-archive-apply",
            "storage_recovery_sanitation_job.py",
            "sqlite3",
        )
    ):
        raise ApplyError("reconciliation remote command contains a forbidden action")
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def _query_only_probe_evidence(
    command: list[str], probe_source: bytes
) -> dict[str, Any]:
    if len(command) < 2 or command[0] != "ssh":
        raise ApplyError("reconciliation probe is not one canonical SSH command")
    try:
        result = subprocess.run(
            command,
            input=probe_source,
            capture_output=True,
            timeout=900.0,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "return_code": None,
            "transport_ambiguous": True,
            "command_sha256": digest(canonical_json_bytes(command)),
            "stdin_sha256": "sha256:" + digest(probe_source),
            "stdout_sha256": "sha256:" + digest(exc.stdout or b""),
            "stderr_sha256": "sha256:" + digest(exc.stderr or b""),
            "result": None,
            "error": "query-only reconciliation probe timed out",
        }
    stdout = result.stdout or b""
    stderr = result.stderr or b""
    payload: Any = None
    try:
        payload = json.loads(stdout.decode("utf-8")) if stdout.strip() else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    return {
        "return_code": result.returncode,
        "transport_ambiguous": result.returncode == 255,
        "command_sha256": digest(canonical_json_bytes(command)),
        "stdin_sha256": "sha256:" + digest(probe_source),
        "stdout_sha256": "sha256:" + digest(stdout),
        "stderr_sha256": "sha256:" + digest(stderr),
        "stderr_excerpt": stderr.decode("utf-8", errors="replace")[:2000],
        "result": payload,
    }


def _valid_warm_reconciliation_probe(
    payload: Any, *, context: Mapping[str, Any]
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    source = context["source"]
    archive = payload.get("archive_reconciliation")
    capacity = payload.get("capacity_reconciliation")
    finance = payload.get("finance_reconciliation")
    monitor = payload.get("natural_root_monitor")
    services = payload.get("systemd_service_gate")
    canonical_contract = (
        services.get("canonical_contract") if isinstance(services, Mapping) else None
    )
    canonical_gate = (
        services.get("canonical_gate") if isinstance(services, Mapping) else None
    )
    service_resamples = (
        services.get("pair_resample_evidence")
        if isinstance(services, Mapping)
        else None
    )
    next_trigger_observations = (
        services.get("timer_next_trigger_observations")
        if isinstance(services, Mapping)
        else None
    )
    non_target = payload.get("non_target_reconciliation")
    journald = payload.get("journald_reconciliation")
    actions = payload.get("remote_action_counts")
    return bool(
        payload.get("schema") == "wb-core.root-warm-archive-reconciliation-probe/v3"
        and payload.get("status") == "reconciled"
        and payload.get("query_only") is True
        and payload.get("pythondontwritebytecode") is True
        and payload.get("operation_id") == source["operation_id"]
        and payload.get("job_id") == source["job_id"]
        and payload.get("deployed_sha") == source["deployed_sha"]
        and payload.get("manifest_path") == source["manifest_path"]
        and payload.get("manifest_sha256") == source["manifest_sha256"]
        and payload.get("production_mutation_count") == 0
        and payload.get("mutation_submit_count_observed") == 1
        and payload.get("promo_action_count") == 0
        and payload.get("business_data_mutation_count") == 0
        and payload.get("active_sanitation_job_count") == 0
        and payload.get("held_lock_count") == 0
        and isinstance(archive, Mapping)
        and archive.get("source_absent_count") == 6
        and archive.get("destination_object_count") == 12
        and archive.get("archive_count") == 6
        and archive.get("manifest_count") == 6
        and archive.get("foreign_object_count") == 0
        and archive.get("temporary_object_count") == 0
        and archive.get("partial_object_count") == 0
        and archive.get("pending_object_count") == 0
        and archive.get("raw_unlink_count") == 6
        and archive.get("reclaimed_allocated_bytes")
        == source["expected_reclaimed_allocated_bytes"]
        and isinstance(archive.get("archives"), list)
        and len(archive["archives"]) == 6
        and isinstance(capacity, Mapping)
        and capacity.get("sample_count") == 3
        and capacity.get("root_stable") is True
        and capacity.get("backup_stable") is True
        and int(capacity.get("root_min_available_bytes") or 0) >= 25 * 1024**3
        and int(capacity.get("backup_min_available_bytes") or 0)
        >= source["required_backup_floor_bytes"]
        and isinstance(finance, Mapping)
        and finance.get("healthy") is True
        and finance.get("required_available_floor_bytes")
        == source["required_backup_floor_bytes"]
        and isinstance(monitor, Mapping)
        and monitor.get("fresh") is True
        and monitor.get("normal") is True
        and isinstance(services, Mapping)
        and services.get("healthy") is True
        and services.get("schema") == "wb-core.systemd-canonical-health-gate/v2"
        and services.get("classification") == "healthy"
        and services.get("unit_count") == 27
        and services.get("pair_count") == 12
        and services.get("failing_pair_count") == 0
        and services.get("failing_persistent_service_count") == 0
        and isinstance(canonical_contract, Mapping)
        and canonical_contract.get("deployed_sha")
        == WARM_RECONCILIATION_SOURCE_DEPLOYED_SHA
        and canonical_contract.get("module_sha256")
        == WARM_RECONCILIATION_CANONICAL_MODULE_SHA256
        and canonical_contract.get("archive_contract")
        == "root_storage_warm_archive_wbc0008_006_v7"
        and canonical_contract.get("service_names_digest")
        == WARM_RECONCILIATION_SERVICE_NAMES_DIGEST
        and isinstance(canonical_contract.get("service_names"), list)
        and len(canonical_contract["service_names"]) == 27
        and isinstance(canonical_contract.get("timer_service_pairs"), list)
        and len(canonical_contract["timer_service_pairs"]) == 12
        and canonical_contract["timer_service_pairs"]
        == [
            [name, name.removesuffix(".timer") + ".service"]
            for name in canonical_contract["service_names"]
            if isinstance(name, str) and name.endswith(".timer")
        ]
        and set(canonical_contract.get("query_only_symbols") or [])
        == {
            "PERSISTENT_SERVICE_NAMES",
            "SERVICE_NAMES",
            "SYSTEMD_PAIR_RESAMPLE_INTERVAL_SECONDS",
            "SYSTEMD_PAIR_RESAMPLE_MAX_ATTEMPTS",
            "SYSTEMD_PAIR_RESAMPLE_MAX_SECONDS",
            "TIMER_SERVICE_PAIRS",
            "_systemd_service_gate_with_resample",
            "_systemd_snapshot",
            "_systemd_unit_row",
        }
        and isinstance(canonical_gate, Mapping)
        and canonical_gate.get("healthy") is True
        and canonical_gate.get("observed_unit_count") == 27
        and canonical_gate.get("observed_pair_count") == 12
        and services.get("canonical_gate_digest") == payload_digest(canonical_gate)
        and isinstance(service_resamples, Mapping)
        and 0 <= int(service_resamples.get("attempt_count") or 0) <= 3
        and service_resamples.get("max_attempts") == 3
        and float(service_resamples.get("max_seconds") or 0) == 5.0
        and isinstance(service_resamples.get("samples"), list)
        and isinstance(next_trigger_observations, list)
        and len(next_trigger_observations) == 12
        and {
            str(row.get("timer_name"))
            for row in next_trigger_observations
            if isinstance(row, Mapping)
        }
        == {
            str(pair[0])
            for pair in canonical_contract["timer_service_pairs"]
            if isinstance(pair, list) and len(pair) == 2
        }
        and services.get("timer_next_trigger_observations_digest")
        == payload_digest(next_trigger_observations)
        and isinstance(services.get("pairs"), list)
        and len(services["pairs"]) == 12
        and all(
            isinstance(pair, Mapping)
            and pair.get("healthy") is True
            and pair.get("classification")
            in {
                "waiting_with_inactive_success_owner",
                "trigger_in_progress_with_active_owner",
            }
            for pair in services["pairs"]
        )
        and services.get("gate_digest")
        == payload_digest(
            {key: value for key, value in services.items() if key != "gate_digest"}
        )
        and isinstance(non_target, Mapping)
        and non_target.get("preserved") is True
        and isinstance(journald, Mapping)
        and journald.get("preserved") is True
        and isinstance(actions, Mapping)
        and set(actions) == WARM_RECONCILIATION_ZERO_ACTIONS
        and set(actions.values()) == {0}
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(payload.get("evidence_digest") or "")
        )
        is not None
        and payload.get("evidence_digest")
        == payload_digest(
            {key: value for key, value in payload.items() if key != "evidence_digest"}
        )
    )


def _run_warm_reconciliation_collect(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> int:
    context = _warm_reconciliation_context(
        args=args,
        client=client,
        source_pr=pr,
        source_comments=comments,
    )
    context["client"] = client
    if _existing_warm_reconciliation_marker(comments, context=context) is not None:
        raise ApplyError(
            "reconciliation operation is already terminal; probe suppressed"
        )
    probe_path = ROOT / "apps" / "wbc0008_warm_archive_receipt_reconciliation_probe.py"
    probe_source = probe_path.read_bytes()
    if (
        not probe_source.startswith(b"#!/usr/bin/env python3\n")
        or b"from apps.root_storage_warm_archive import (" not in probe_source
        or any(
            symbol.encode("utf-8") not in probe_source
            for symbol in (
                "SERVICE_NAMES",
                "_systemd_snapshot",
                "_systemd_service_gate_with_resample",
                "_systemd_unit_row",
            )
        )
        or b"def _classify_timer_service_pair" in probe_source
        or b"def _classify_persistent_service" in probe_source
        or b"def _systemd_observation" in probe_source
        or b"readback_batch(" in probe_source
        or b"storage_recovery_sanitation_job.py" in probe_source
        or b'systemctl", "start' in probe_source
        or b'systemctl", "restart' in probe_source
        or b"import sqlite3" in probe_source
        or b"import tempfile" in probe_source
        or b"import fcntl" in probe_source
        or b".unlink(" in probe_source
        or b".write_bytes(" in probe_source
        or b".write_text(" in probe_source
        or b"os.replace(" in probe_source
        or b"os.rename(" in probe_source
    ):
        raise ApplyError("trusted reconciliation probe contains a forbidden primitive")
    source = context["source"]
    probe_binding = {
        key: source[key]
        for key in (
            "operation_id",
            "job_id",
            "deployed_sha",
            "manifest_path",
            "manifest_sha256",
            "job_request_digest",
            "job_result_digest",
            "expected_reclaimed_allocated_bytes",
            "required_backup_floor_bytes",
        )
    }
    target = _canonical_target()
    with tempfile.TemporaryDirectory(
        prefix="wb-core-warm-reconciliation-ssh-"
    ) as directory:
        configure_deploy_environment(Path(directory))
        probe_evidence = _query_only_probe_evidence(
            _warm_reconciliation_probe_command(target=target, binding=probe_binding),
            probe_source,
        )
    reconciled = bool(
        probe_evidence.get("return_code") == 0
        and probe_evidence.get("transport_ambiguous") is False
        and _valid_warm_reconciliation_probe(
            probe_evidence.get("result"), context=context
        )
    )
    receipt_without_digest: dict[str, Any] = {
        "schema": WARM_RECONCILIATION_RECEIPT_SCHEMA,
        "state": "done" if reconciled else "blocked",
        "attempt": WARM_RECONCILIATION_ATTEMPT,
        "reason": (
            "reconciled-existing-terminal-operation"
            if reconciled
            else "query-only-reconciliation-not-proven"
        ),
        "terminal_disposition": (
            "done/reconciled_existing_operation" if reconciled else "blocked"
        ),
        "query_only": True,
        "production_mutation_count": 0,
        "source": context["source"],
        "reconciliation_release": context["reconciliation_release"],
        "reconciliation_generation": context["reconciliation_generation"],
        "probe": probe_evidence,
    }
    receipt_without_digest["evidence_digest"] = payload_digest(receipt_without_digest)
    _write_receipt(args.output, receipt_without_digest)
    print(json.dumps(receipt_without_digest, ensure_ascii=False, sort_keys=True))
    return 0


def _extract_warm_reconciliation_artifact(
    raw_zip: bytes, *, expected_sha256: str
) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if (
                len(files) != 1
                or files[0].filename != WARM_RECONCILIATION_ARTIFACT_FILE
                or files[0].file_size <= 0
                or files[0].file_size > MAX_WARM_RECONCILIATION_ARTIFACT_BYTES
            ):
                raise ApplyError("reconciliation artifact shape is invalid")
            raw = archive.read(files[0])
    except zipfile.BadZipFile as exc:
        raise ApplyError("reconciliation artifact ZIP is invalid") from exc
    if digest(raw) != expected_sha256:
        raise ApplyError("reconciliation artifact digest mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplyError("reconciliation artifact JSON is invalid") from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise ApplyError("reconciliation artifact bytes are not canonical")
    return payload


def _verify_uploaded_warm_reconciliation_artifact(
    client: GitHubClient,
    *,
    run_id: int,
    artifact_name: str,
    receipt_sha256: str,
    code_sha: str,
) -> dict[str, Any]:
    matches: list[Mapping[str, Any]] = []
    for attempt in range(6):
        payload = client.get(f"/actions/runs/{run_id}/artifacts?per_page=100")
        values = payload.get("artifacts") if isinstance(payload, Mapping) else None
        if not isinstance(values, list):
            raise ApplyError("reconciliation artifact listing is invalid")
        matches = [
            item
            for item in values
            if isinstance(item, Mapping) and item.get("name") == artifact_name
        ]
        if matches or attempt == 5:
            break
        time.sleep(2.0)
    if len(matches) != 1:
        raise ApplyError("uploaded reconciliation artifact is missing or ambiguous")
    artifact = matches[0]
    workflow_run = artifact.get("workflow_run")
    if (
        artifact.get("expired") is True
        or not isinstance(artifact.get("id"), int)
        or not isinstance(workflow_run, Mapping)
        or workflow_run.get("id") != run_id
        or workflow_run.get("head_branch") != "main"
        or workflow_run.get("head_sha") != code_sha
    ):
        raise ApplyError("uploaded reconciliation artifact provenance mismatch")
    raw_zip = client.request(
        "GET",
        f"/actions/artifacts/{int(artifact['id'])}/zip",
        accept="application/vnd.github+json",
        raw=True,
    )
    if not isinstance(raw_zip, bytes):
        raise ApplyError("uploaded reconciliation artifact download failed")
    receipt = _extract_warm_reconciliation_artifact(
        raw_zip, expected_sha256=receipt_sha256
    )
    return {"metadata": dict(artifact), "receipt": receipt}


def _warm_reconciliation_comment_body(
    receipt: Mapping[str, Any],
    *,
    artifact_name: str,
    receipt_sha256: str,
    receipt_size_bytes: int,
) -> str:
    probe = receipt.get("probe") or {}
    result = probe.get("result") or {}
    archive = result.get("archive_reconciliation") or {}
    capacity = result.get("capacity_reconciliation") or {}
    finance = result.get("finance_reconciliation") or {}
    services = result.get("systemd_service_gate") or {}
    summary = {
        "schema": WARM_RECONCILIATION_SUMMARY_SCHEMA,
        "state": receipt.get("state"),
        "attempt": receipt.get("attempt"),
        "reason": receipt.get("reason"),
        "terminal_disposition": receipt.get("terminal_disposition"),
        "operation_id": receipt.get("source", {}).get("operation_id"),
        "query_only": True,
        "production_mutation_count": 0,
        "source": receipt.get("source"),
        "reconciliation_release": receipt.get("reconciliation_release"),
        "reconciliation_generation": receipt.get("reconciliation_generation"),
        "evidence_digest": receipt.get("evidence_digest"),
        "terminal_facts": {
            "job_status": (result.get("job_evidence") or {}).get("status"),
            "job_attempt": (result.get("job_evidence") or {}).get("attempt"),
            "journal_file_sha256": (result.get("job_evidence") or {}).get(
                "journal_file_sha256"
            ),
            "source_absent_count": archive.get("source_absent_count"),
            "destination_object_count": archive.get("destination_object_count"),
            "archive_count": archive.get("archive_count"),
            "manifest_count": archive.get("manifest_count"),
            "raw_unlink_count": archive.get("raw_unlink_count"),
            "reclaimed_allocated_bytes": archive.get("reclaimed_allocated_bytes"),
            "root_min_available_bytes": capacity.get("root_min_available_bytes"),
            "backup_min_available_bytes": capacity.get("backup_min_available_bytes"),
            "finance_floor_bytes": finance.get("required_available_floor_bytes"),
            "service_unit_count": services.get("unit_count"),
            "service_pair_count": services.get("pair_count"),
            "natural_monitor_normal": (result.get("natural_root_monitor") or {}).get(
                "normal"
            ),
            "non_target_preserved": (result.get("non_target_reconciliation") or {}).get(
                "preserved"
            ),
            "journald_preserved": (result.get("journald_reconciliation") or {}).get(
                "preserved"
            ),
            "promo_action_count": result.get("promo_action_count"),
            "business_data_mutation_count": result.get("business_data_mutation_count"),
        },
        "artifact": {
            "name": artifact_name,
            "file": WARM_RECONCILIATION_ARTIFACT_FILE,
            "sha256": "sha256:" + receipt_sha256,
            "size_bytes": receipt_size_bytes,
            "retention_days": 90,
        },
    }
    body = (
        warm_reconciliation_marker(
            str(summary["operation_id"]), WARM_RECONCILIATION_ATTEMPT
        )
        + "\nCompact terminal supersession marker for the existing WBC0008 operation; full canonical evidence was uploaded first."
        + "\n```json\n"
        + json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n```"
    )
    if len(body.encode("utf-8")) >= MAX_GITHUB_COMMENT_BYTES:
        raise ApplyError("compact reconciliation marker exceeds GitHub limit")
    return body


def _run_warm_reconciliation_publish(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> int:
    context = _warm_reconciliation_context(
        args=args,
        client=client,
        source_pr=pr,
        source_comments=comments,
    )
    context["client"] = client
    if _existing_warm_reconciliation_marker(comments, context=context) is not None:
        raise ApplyError("reconciliation marker became terminal before publication")
    raw = args.output.read_bytes()
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplyError("reconciliation receipt is unreadable") from exc
    if (
        not isinstance(receipt, dict)
        or raw != canonical_json_bytes(receipt) + b"\n"
        or receipt.get("schema") != WARM_RECONCILIATION_RECEIPT_SCHEMA
        or receipt.get("state") not in {"done", "blocked"}
        or receipt.get("attempt") != WARM_RECONCILIATION_ATTEMPT
        or receipt.get("query_only") is not True
        or receipt.get("production_mutation_count") != 0
        or receipt.get("source") != context["source"]
        or receipt.get("reconciliation_release") != context["reconciliation_release"]
        or receipt.get("reconciliation_generation")
        != context["reconciliation_generation"]
        or receipt.get("evidence_digest")
        != payload_digest(
            {key: value for key, value in receipt.items() if key != "evidence_digest"}
        )
    ):
        raise ApplyError("reconciliation receipt contract is invalid")
    if receipt.get("state") == "done" and (
        receipt.get("reason") != "reconciled-existing-terminal-operation"
        or receipt.get("terminal_disposition") != "done/reconciled_existing_operation"
        or not _valid_warm_reconciliation_probe(
            (receipt.get("probe") or {}).get("result"), context=context
        )
    ):
        raise ApplyError("done reconciliation receipt lacks exact terminal proof")
    if receipt.get("state") == "blocked" and (
        receipt.get("reason") != "query-only-reconciliation-not-proven"
        or receipt.get("terminal_disposition") != "blocked"
    ):
        raise ApplyError("blocked reconciliation receipt binding is invalid")
    run_id = int(os.environ.get("GITHUB_RUN_ID") or 0)
    if run_id <= 0:
        raise ApplyError("reconciliation publication lacks workflow run identity")
    artifact_name = _warm_reconciliation_artifact_name(args.pr, run_id)
    receipt_sha256 = digest(raw)
    _verify_uploaded_warm_reconciliation_artifact(
        client,
        run_id=run_id,
        artifact_name=artifact_name,
        receipt_sha256=receipt_sha256,
        code_sha=context["reconciliation_release"]["merge_sha"],
    )
    body = _warm_reconciliation_comment_body(
        receipt,
        artifact_name=artifact_name,
        receipt_sha256=receipt_sha256,
        receipt_size_bytes=len(raw),
    )
    published = client.post(f"/issues/{args.pr}/comments", {"body": body})
    if (
        not isinstance(published, Mapping)
        or not is_actions_bot_comment(published)
        or published.get("body") != body
        or not isinstance(published.get("id"), int)
    ):
        raise ApplyError("reconciliation marker publication response mismatch")
    readback_comments = list_comments(client, args.pr)
    readback = _existing_warm_reconciliation_marker(readback_comments, context=context)
    if readback is None or readback.get("id") != published.get("id"):
        raise ApplyError("reconciliation marker publication readback mismatch")
    print(
        json.dumps(
            {
                "state": receipt["state"],
                "terminal_disposition": receipt["terminal_disposition"],
                "operation_id": context["source"]["operation_id"],
                "run_id": run_id,
                "artifact_name": artifact_name,
                "artifact_sha256": "sha256:" + receipt_sha256,
                "evidence_digest": receipt["evidence_digest"],
                "comment_id": published["id"],
                "production_mutation_count": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _run_warm_reconciliation_mode(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> int:
    if args.authorization_comment_id <= 0:
        raise ApplyError("warm archive reconciliation requires authorization comment")
    if args.reconciliation_phase == "preflight":
        return _run_warm_reconciliation_preflight(
            args=args, client=client, pr=pr, comments=comments
        )
    if args.reconciliation_phase == "collect":
        return _run_warm_reconciliation_collect(
            args=args, client=client, pr=pr, comments=comments
        )
    if args.reconciliation_phase == "publish":
        return _run_warm_reconciliation_publish(
            args=args, client=client, pr=pr, comments=comments
        )
    raise ApplyError("warm archive reconciliation phase is invalid")


def _wbc0027_reconciliation_marker(operation: str) -> str:
    return f"<!-- {WBC0027_RECONCILIATION_MARKER} operation={operation} -->"


def _wbc0027_reconciliation_artifact_name(run_id: int) -> str:
    return (
        "wbc0027-existing-operation-reconciliation-pr-1129-run-"
        + str(run_id)
    )


def _wbc0027_release_comment_payloads(
    comments: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    marker_re = re.compile(
        rf"<!-- {re.escape(RECEIPT_MARKER)} "
        r"operation=(release-v2-[0-9a-f]{32}) -->"
    )
    payloads: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if RECEIPT_MARKER not in body or not is_actions_bot_comment(comment):
            continue
        markers = marker_re.findall(body)
        if len(markers) != 1 or "```json" not in body:
            raise ApplyError("WBC0027 trusted release marker is malformed")
        try:
            payload = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
        except (IndexError, json.JSONDecodeError) as exc:
            raise ApplyError("WBC0027 trusted release receipt is malformed") from exc
        if not isinstance(payload, dict) or payload.get("operation_id") != markers[0]:
            raise ApplyError("WBC0027 trusted release marker binding is invalid")
        payloads.append(payload)
    return payloads


def _wbc0027_trusted_release(
    client: GitHubClient,
    *,
    pr_number: int,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
    merge_sha: str,
    release_kind: str,
    operation: str | None,
) -> dict[str, Any]:
    base = pr.get("base") or {}
    head = pr.get("head") or {}
    base_repo = base.get("repo") or {}
    head_repo = head.get("repo") or {}
    if not (
        isinstance(base, Mapping)
        and isinstance(head, Mapping)
        and isinstance(base_repo, Mapping)
        and isinstance(head_repo, Mapping)
        and pr.get("number") == pr_number
        and pr.get("merged") is True
        and pr.get("draft") is False
        and str(pr.get("state") or "") == "closed"
        and exact_sha(pr.get("merge_commit_sha"), "trusted-release-merge")
        == merge_sha
        and base.get("ref") == "main"
        and base_repo.get("full_name") == CANONICAL_REPOSITORY
        and head_repo.get("full_name") == CANONICAL_REPOSITORY
    ):
        raise ApplyError("WBC0027 trusted release PR binding is invalid")
    base_sha = exact_sha(base.get("sha"), "trusted-release-base")
    head_sha = exact_sha(head.get("sha"), "trusted-release-head")
    matches: list[dict[str, Any]] = []
    for payload in _wbc0027_release_comment_payloads(comments):
        workflow_run_id = payload.get("workflow_run_id")
        plan_hash = str(payload.get("plan_hash") or "")
        payload_operation = str(payload.get("operation_id") or "")
        valid = bool(
            payload.get("schema") == RELEASE_RECEIPT_SCHEMA
            and payload.get("state") == "done"
            and payload.get("repository") == CANONICAL_REPOSITORY
            and payload.get("pull_request") == pr_number
            and payload.get("base_sha") == base_sha
            and payload.get("head_sha") == head_sha
            and payload.get("merge_sha") == merge_sha
            and payload.get("release_kind") == release_kind
            and payload.get("manifest") is None
            and payload.get("reason_codes") == []
            and isinstance(workflow_run_id, int)
            and not isinstance(workflow_run_id, bool)
            and workflow_run_id > 0
            and re.fullmatch(r"[0-9a-f]{64}", plan_hash) is not None
            and payload_operation
            == release_operation_id(
                CANONICAL_REPOSITORY,
                workflow_run_id,
                pr_number,
                head_sha,
                plan_hash,
            )
            and (
                payload.get("deployed_sha") == merge_sha
                if release_kind == "live_runtime"
                else payload.get("deployed_sha") is None
            )
        )
        if operation is not None and payload_operation != operation:
            continue
        if not valid:
            raise ApplyError("WBC0027 trusted release receipt binding is invalid")
        matches.append(payload)
    if len(matches) != 1:
        raise ApplyError("WBC0027 trusted release receipt is missing or ambiguous")
    receipt = matches[0]
    gate = client.get(f"/actions/runs/{int(receipt['workflow_run_id'])}")
    if not isinstance(gate, Mapping):
        raise ApplyError("WBC0027 exact PR Gate response is invalid")
    repository = gate.get("repository")
    head_repository = gate.get("head_repository")
    if not (
        isinstance(gate, Mapping)
        and isinstance(repository, Mapping)
        and isinstance(head_repository, Mapping)
        and gate.get("id") == receipt["workflow_run_id"]
        and gate.get("name") == "PR Gate"
        and gate.get("path") == ".github/workflows/pr-gate.yml"
        and gate.get("event") == "pull_request"
        and gate.get("status") == "completed"
        and gate.get("conclusion") == "success"
        and gate.get("run_attempt") == 1
        and gate.get("head_sha") == head_sha
        and repository.get("full_name") == CANONICAL_REPOSITORY
        and head_repository.get("full_name") == CANONICAL_REPOSITORY
    ):
        raise ApplyError("WBC0027 exact PR Gate provenance is invalid")
    return {
        "pull_request": pr_number,
        "operation_id": str(receipt["operation_id"]),
        "state": "done",
        "release_kind": release_kind,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_sha": merge_sha,
        "deployed_sha": receipt.get("deployed_sha"),
        "workflow_run_id": int(receipt["workflow_run_id"]),
        "plan_hash": str(receipt["plan_hash"]),
        "pr_gate": {
            "workflow_run_id": int(receipt["workflow_run_id"]),
            "workflow": ".github/workflows/pr-gate.yml",
            "event": "pull_request",
            "run_attempt": 1,
            "head_sha": head_sha,
            "conclusion": "success",
        },
    }


def _git_checkout_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return exact_sha(completed.stdout.strip(), "trusted-checkout-head")


def _git_blob_binding(commit_sha: str, path: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "ls-tree", "--full-tree", commit_sha, "--", path],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    rows = [row for row in completed.stdout.splitlines() if row]
    if len(rows) != 1 or "\t" not in rows[0]:
        raise ApplyError(f"WBC0027 source binding path is missing: {path}")
    metadata, observed_path = rows[0].split("\t", 1)
    parts = metadata.split()
    if (
        observed_path != path
        or len(parts) != 3
        or parts[0] not in {"100644", "100755"}
        or parts[1] != "blob"
        or re.fullmatch(r"[0-9a-f]{40}", parts[2]) is None
    ):
        raise ApplyError(f"WBC0027 source binding path is invalid: {path}")
    size = subprocess.run(
        ["git", "cat-file", "-s", parts[2]],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not size.isdigit():
        raise ApplyError(f"WBC0027 source binding size is invalid: {path}")
    return {
        "path": path,
        "mode": parts[0],
        "git_blob_sha": parts[2],
        "size_bytes": int(size),
    }


def _wbc0027_runtime_source_integrity(
    *, deployed_sha: str, bridge_sha: str
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in WBC0027_RUNTIME_SOURCE_PATHS:
        deployed = _git_blob_binding(deployed_sha, path)
        bridge = (
            deployed
            if bridge_sha == deployed_sha
            else _git_blob_binding(bridge_sha, path)
        )
        if (
            bridge["git_blob_sha"] != deployed["git_blob_sha"]
            or bridge["mode"] != deployed["mode"]
            or bridge["size_bytes"] != deployed["size_bytes"]
        ):
            raise ApplyError(
                "WBC0027 reconciliation runtime source changed after live release: "
                + path
            )
        rows.append(
            {
                "path": path,
                "mode": str(deployed["mode"]),
                "git_blob_sha": str(deployed["git_blob_sha"]),
                "size_bytes": int(deployed["size_bytes"]),
            }
        )
    payload = {
        "contract_name": WBC0027_RUNTIME_SOURCE_BINDING_CONTRACT,
        "deployed_sha": deployed_sha,
        "bridge_sha": bridge_sha,
        "comparison": (
            "direct_deployed_checkout"
            if bridge_sha == deployed_sha
            else "byte_identical_repo_only_bridge"
        ),
        "paths": rows,
    }
    return {**payload, "binding_digest": payload_digest(payload)}


def _wbc0027_workflow_bridge(
    *,
    client: GitHubClient,
    deployed_release: Mapping[str, Any],
) -> dict[str, Any]:
    event = str(os.environ.get("GITHUB_EVENT_NAME") or "")
    ref = str(os.environ.get("GITHUB_REF") or "")
    repository_name = str(os.environ.get("GITHUB_REPOSITORY") or "")
    run_attempt = str(os.environ.get("GITHUB_RUN_ATTEMPT") or "")
    run_id = int(os.environ.get("GITHUB_RUN_ID") or 0)
    bridge_sha = exact_sha(os.environ.get("GITHUB_SHA"), "workflow-bridge")
    if not (
        event == "workflow_dispatch"
        and ref == "refs/heads/main"
        and repository_name == CANONICAL_REPOSITORY
        and run_attempt == "1"
        and run_id > 0
        and _git_checkout_head() == bridge_sha
    ):
        raise ApplyError("WBC0027 workflow bridge is not an exact main dispatch")
    dispatch_run = client.get(f"/actions/runs/{run_id}")
    if not isinstance(dispatch_run, Mapping):
        raise ApplyError("WBC0027 workflow dispatch response is invalid")
    dispatch_repository = dispatch_run.get("repository")
    dispatch_head_repository = dispatch_run.get("head_repository")
    dispatch_terminal_valid = bool(
        (
            dispatch_run.get("status") == "in_progress"
            and dispatch_run.get("conclusion") is None
        )
        or (
            dispatch_run.get("status") == "completed"
            and dispatch_run.get("conclusion") == "success"
        )
    )
    if not (
        isinstance(dispatch_run, Mapping)
        and isinstance(dispatch_repository, Mapping)
        and isinstance(dispatch_head_repository, Mapping)
        and dispatch_run.get("id") == run_id
        and dispatch_run.get("name") == "Production Apply Runner"
        and dispatch_run.get("path") == PRODUCTION_APPLY_WORKFLOW_PATH
        and dispatch_run.get("event") == "workflow_dispatch"
        and dispatch_run.get("head_branch") == "main"
        and dispatch_run.get("head_sha") == bridge_sha
        and dispatch_run.get("run_attempt") == 1
        and dispatch_terminal_valid
        and dispatch_repository.get("full_name") == CANONICAL_REPOSITORY
        and dispatch_head_repository.get("full_name") == CANONICAL_REPOSITORY
    ):
        raise ApplyError("WBC0027 workflow dispatch provenance is invalid")
    associated = client.get(f"/commits/{bridge_sha}/pulls?per_page=100")
    if not isinstance(associated, list):
        raise ApplyError("WBC0027 workflow bridge PR lookup is invalid")
    associated_numbers = [
        item.get("number")
        for item in associated
        if isinstance(item, Mapping)
        and isinstance(item.get("number"), int)
        and not isinstance(item.get("number"), bool)
        and item.get("merged_at")
        and item.get("merge_commit_sha") == bridge_sha
        and (item.get("base") or {}).get("ref") == "main"
    ]
    if len(associated_numbers) != 1:
        raise ApplyError("WBC0027 workflow bridge PR is missing or ambiguous")
    bridge_pr_number = int(associated_numbers[0])
    bridge_pr = client.get(f"/pulls/{bridge_pr_number}")
    deployed_sha = exact_sha(
        deployed_release.get("deployed_sha"), "reconciliation-deployed"
    )
    expected_kind = "live_runtime" if bridge_sha == deployed_sha else "repo_only"
    if bridge_sha == deployed_sha:
        if bridge_pr_number != int(deployed_release["pull_request"]):
            raise ApplyError("WBC0027 direct workflow bridge PR drifted")
        bridge_release = dict(deployed_release)
    else:
        bridge_release = _wbc0027_trusted_release(
            client,
            pr_number=bridge_pr_number,
            pr=bridge_pr,
            comments=list_comments(client, bridge_pr_number),
            merge_sha=bridge_sha,
            release_kind=expected_kind,
            operation=None,
        )
    if bridge_release.get("release_kind") != expected_kind:
        raise ApplyError("WBC0027 workflow bridge release kind is invalid")
    if bridge_sha == deployed_sha:
        ancestry = {
            "status": "identical",
            "merge_base_sha": deployed_sha,
            "ahead_by": 0,
            "behind_by": 0,
            "commit_count": 0,
        }
    else:
        compare = client.get(f"/compare/{deployed_sha}...{bridge_sha}")
        merge_base = compare.get("merge_base_commit") or {}
        commits = compare.get("commits") or []
        commit_shas = [
            item.get("sha") for item in commits if isinstance(item, Mapping)
        ]
        if not (
            isinstance(compare, Mapping)
            and isinstance(merge_base, Mapping)
            and isinstance(commits, list)
            and compare.get("status") == "ahead"
            and isinstance(compare.get("ahead_by"), int)
            and not isinstance(compare.get("ahead_by"), bool)
            and int(compare["ahead_by"]) > 0
            and compare.get("behind_by") == 0
            and merge_base.get("sha") == deployed_sha
            and len(commit_shas) == int(compare["ahead_by"])
            and all(
                re.fullmatch(r"[0-9a-f]{40}", str(sha or "")) is not None
                for sha in commit_shas
            )
            and commit_shas[-1:] == [bridge_sha]
        ):
            raise ApplyError("WBC0027 workflow bridge ancestry is invalid")
        ancestry_payload = {
            "deployed_sha": deployed_sha,
            "bridge_sha": bridge_sha,
            "commit_shas": commit_shas,
        }
        ancestry = {
            "status": "ahead",
            "merge_base_sha": deployed_sha,
            "ahead_by": int(compare["ahead_by"]),
            "behind_by": 0,
            "commit_count": len(commit_shas),
            "commit_digest": payload_digest(ancestry_payload),
        }
    workflow = _git_blob_binding(bridge_sha, PRODUCTION_APPLY_WORKFLOW_PATH)
    checkout_workflow = _git_blob_binding("HEAD", PRODUCTION_APPLY_WORKFLOW_PATH)
    if workflow != checkout_workflow:
        raise ApplyError("WBC0027 checked-out workflow bytes are not exact main")
    source_integrity = _wbc0027_runtime_source_integrity(
        deployed_sha=deployed_sha,
        bridge_sha=bridge_sha,
    )
    return {
        "pull_request": bridge_pr_number,
        "operation_id": str(bridge_release["operation_id"]),
        "state": str(bridge_release["state"]),
        "release_kind": expected_kind,
        "merge_sha": bridge_sha,
        "deployed_sha": bridge_release.get("deployed_sha"),
        "workflow_run_id": int(bridge_release["workflow_run_id"]),
        "plan_hash": str(bridge_release["plan_hash"]),
        "pr_gate": dict(bridge_release["pr_gate"]),
        "dispatch": {
            "workflow_run_id": run_id,
            "workflow": PRODUCTION_APPLY_WORKFLOW_PATH,
            "event": event,
            "ref": ref,
            "run_attempt": 1,
            "head_sha": bridge_sha,
        },
        "workflow_blob": workflow,
        "ancestry": ancestry,
        "runtime_source_integrity": source_integrity,
    }


def _validate_wbc0027_reconciliation_source_receipt(
    receipt: Mapping[str, Any],
    *,
    goal: Mapping[str, Any],
) -> dict[str, Any]:
    source = WBC0027_RECONCILIATION_SOURCE
    expected = {
        "schema": APPLY_RECEIPT_SCHEMA,
        "state": "blocked",
        "operation_id": source["operation_id"],
        "repository": CANONICAL_REPOSITORY,
        "pull_request": source["pull_request"],
        "release_operation_id": source["release_operation_id"],
        "merge_sha": source["deployed_sha"],
        "deployed_sha": source["deployed_sha"],
        "authorization_comment_id": source["authorization_comment_id"],
        "authorization_body_sha256": source["authorization_body_sha256"],
        "apply_count": 1,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ApplyError(
                f"WBC0027 reconciliation source receipt drifted: {field}"
            )
    if receipt.get("goal") != dict(goal):
        raise ApplyError("WBC0027 reconciliation source goal drifted")
    evidence = receipt.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ApplyError("WBC0027 reconciliation source evidence is missing")
    expected_evidence = {
        "state": "blocked",
        "reason": "wbc0027-economics-query-only-readback-not-reconciled",
        "apply_count": 1,
        "product_state": "applied",
        "economics_state": "not_applied",
        "product_submit_count": 1,
        "economics_submit_count": 0,
    }
    for field, value in expected_evidence.items():
        if evidence.get(field) != value:
            raise ApplyError(
                f"WBC0027 reconciliation source phase evidence drifted: {field}"
            )
    product_candidate = evidence.get("product_candidate")
    economics_candidate = evidence.get("economics_candidate")
    if not isinstance(product_candidate, Mapping) or not isinstance(
        economics_candidate, Mapping
    ):
        raise ApplyError("WBC0027 reconciliation source candidates are missing")
    expected_economics = {
        "manifest_path": source["economics_manifest_path"],
        "manifest_sha256": source["economics_manifest_sha256"],
        "phase_fingerprint": source["economics_phase_fingerprint"],
        "phase_operation_id": source["economics_phase_operation_id"],
        "storage_generation": source["storage_generation"],
    }
    for field, value in expected_economics.items():
        if economics_candidate.get(field) != value:
            raise ApplyError(
                f"WBC0027 reconciliation economics candidate drifted: {field}"
            )
    if (
        product_candidate.get("phase_operation_id")
        != source["product_phase_operation_id"]
        or product_candidate.get("storage_generation")
        != source["storage_generation"]
    ):
        raise ApplyError("WBC0027 reconciliation product candidate drifted")
    product_apply = evidence.get("product_apply")
    economics_apply = evidence.get("economics_apply")
    economics_readback = evidence.get("economics_readback")
    product_result = (
        (product_apply.get("result") or {})
        if isinstance(product_apply, Mapping)
        else {}
    )
    economics_result = (
        (economics_apply.get("result") or {})
        if isinstance(economics_apply, Mapping)
        else {}
    )
    readback_result = (
        (economics_readback.get("result") or {})
        if isinstance(economics_readback, Mapping)
        else {}
    )
    product_capital = readback_result.get("product_capital") or {}
    hard_non_target = readback_result.get("hard_non_target") or {}
    if not (
        isinstance(product_result, Mapping)
        and product_result.get("status") == "applied"
        and product_result.get("phase_operation_id")
        == source["product_phase_operation_id"]
        and product_result.get("database_written") is True
        and product_result.get("production_mutation_submit_count") == 1
        and isinstance(economics_result, Mapping)
        and economics_result.get("status") == "not_applied"
        and economics_result.get("error_type") == "RecoveryPolicyError"
        and economics_result.get("error")
        == "non-target digest changed after mutation"
        and economics_result.get("database_written") is False
        and economics_result.get("production_mutation_submit_count") == 0
        and isinstance(readback_result, Mapping)
        and readback_result.get("status") == "pending_reconciliation"
        and readback_result.get("recovery_lifecycle") == "quarantined"
        and readback_result.get("economics_target_exact") is True
        and readback_result.get("functional_economics_missing")
        == {"2026-08-26": 12, "2026-08-29": 0}
        and isinstance(product_capital, Mapping)
        and product_capital.get("status") == "published_exact"
        and int(product_capital.get("mismatch_count") or 0) == 0
        and isinstance(hard_non_target, Mapping)
        and hard_non_target.get("all_exact") is True
    ):
        raise ApplyError(
            "WBC0027 source does not prove the exact after-COMMIT false quarantine"
        )
    return {
        "product_candidate": dict(product_candidate),
        "economics_candidate": dict(economics_candidate),
    }


def _wbc0027_reconciliation_context(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    source_pr: Mapping[str, Any],
    source_comments: list[Mapping[str, Any]],
) -> dict[str, Any]:
    source = WBC0027_RECONCILIATION_SOURCE
    required = {
        "source_run_id": args.source_run_id,
        "source_artifact_id": args.source_artifact_id,
        "source_artifact_name": args.source_artifact_name,
        "source_receipt_sha256": args.source_receipt_sha256,
        "authorization_comment_id": args.authorization_comment_id,
        "blocked_comment_id": args.blocked_comment_id,
        "operation_id": args.operation_id,
        "reconciliation_pr": args.reconciliation_pr,
        "reconciliation_release_operation_id": args.reconciliation_release_operation_id,
    }
    missing = sorted(field for field, value in required.items() if not value)
    if missing:
        raise ApplyError(
            "WBC0027 receipt reconciliation inputs are missing: "
            + ", ".join(missing)
        )
    exact_inputs = {
        "pr": args.pr,
        "source_run_id": args.source_run_id,
        "source_artifact_id": args.source_artifact_id,
        "source_artifact_name": args.source_artifact_name,
        "source_receipt_sha256": args.source_receipt_sha256,
        "authorization_comment_id": args.authorization_comment_id,
        "blocked_comment_id": args.blocked_comment_id,
        "operation_id": args.operation_id,
    }
    expected_inputs = {
        "pr": source["pull_request"],
        "source_run_id": source["run_id"],
        "source_artifact_id": source["artifact_id"],
        "source_artifact_name": source["artifact_name"],
        "source_receipt_sha256": source["receipt_sha256"],
        "authorization_comment_id": source["authorization_comment_id"],
        "blocked_comment_id": source["blocked_comment_id"],
        "operation_id": source["operation_id"],
    }
    if exact_inputs != expected_inputs:
        raise ApplyError("WBC0027 exact source lineage input drifted")
    source_merge_sha = exact_sha(source_pr.get("merge_commit_sha"), "source-pr-merge")
    if source_merge_sha != source["deployed_sha"]:
        raise ApplyError("WBC0027 exact source PR merge drifted")
    run, receipt = _collect_recovery_receipt(
        client,
        pr=args.pr,
        run_id=int(args.source_run_id),
        artifact_name=str(args.source_artifact_name),
        receipt_sha256=str(args.source_receipt_sha256),
        expected_conclusion="success",
    )
    artifact = run.get("validated_artifact")
    if not isinstance(artifact, Mapping) or (
        artifact.get("id") != source["artifact_id"]
        or artifact.get("digest") != source["artifact_archive_digest"]
    ):
        raise ApplyError("WBC0027 source artifact immutable identity drifted")
    authorization = client.get(
        f"/issues/comments/{int(args.authorization_comment_id)}"
    )
    goal = validate_authorization(
        authorization,
        repository=args.repository,
        pr=args.pr,
    )
    authorization_body = str(authorization.get("body") or "").strip()
    if (
        digest(authorization_body.encode("utf-8"))
        != source["authorization_body_sha256"]
        or operation_id(
            args.repository, args.pr, int(args.authorization_comment_id), goal
        )
        != source["operation_id"]
    ):
        raise ApplyError("WBC0027 source authorization bytes drifted")
    candidate_binding = _validate_wbc0027_reconciliation_source_receipt(
        receipt, goal=goal
    )
    parse_release_receipt(
        source_comments,
        pr=args.pr,
        release_operation=str(source["release_operation_id"]),
        merge_sha=source_merge_sha,
    )
    source_markers = [
        item
        for item in source_comments
        if marker(str(source["operation_id"])) in str(item.get("body") or "")
    ]
    if len(source_markers) != 1 or source_markers[0].get("id") != source[
        "blocked_comment_id"
    ]:
        raise ApplyError("WBC0027 source blocked marker is missing or ambiguous")
    blocked_comment = source_markers[0]
    if not is_actions_bot_comment(blocked_comment):
        raise ApplyError("WBC0027 source blocked marker author is foreign")
    blocked_payload = _comment_payload(
        blocked_comment, str(source["operation_id"])
    )
    blocked_artifact = blocked_payload.get("artifact") or {}
    if not (
        blocked_payload.get("schema") == APPLY_COMMENT_SUMMARY_SCHEMA
        and blocked_payload.get("state") == "blocked"
        and blocked_payload.get("operation_id") == source["operation_id"]
        and blocked_payload.get("pull_request") == source["pull_request"]
        and blocked_payload.get("deployed_sha") == source["deployed_sha"]
        and blocked_payload.get("reason")
        == "wbc0027-economics-query-only-readback-not-reconciled"
        and blocked_payload.get("apply_count") == 1
        and isinstance(blocked_artifact, Mapping)
        and blocked_artifact.get("name") == source["artifact_name"]
        and blocked_artifact.get("sha256")
        == "sha256:" + str(source["receipt_sha256"])
    ):
        raise ApplyError("WBC0027 source blocked marker binding drifted")
    correction_pr = client.get(f"/pulls/{int(args.reconciliation_pr)}")
    if not isinstance(correction_pr, Mapping) or correction_pr.get("merged") is not True:
        raise ApplyError("WBC0027 reconciliation release PR is not merged")
    correction_sha = exact_sha(
        correction_pr.get("merge_commit_sha"), "reconciliation-pr-merge"
    )
    if correction_sha == source_merge_sha:
        raise ApplyError("WBC0027 reconciliation release did not change deployed code")
    correction_comments = list_comments(client, int(args.reconciliation_pr))
    release = _wbc0027_trusted_release(
        client,
        pr_number=int(args.reconciliation_pr),
        pr=correction_pr,
        comments=correction_comments,
        merge_sha=correction_sha,
        release_kind="live_runtime",
        operation=str(args.reconciliation_release_operation_id),
    )
    workflow_bridge = _wbc0027_workflow_bridge(
        client=client,
        deployed_release=release,
    )
    if release.get("deployed_sha") != correction_sha:
        raise ApplyError("WBC0027 reconciliation deployed SHA binding is invalid")
    parse_release_receipt(
        correction_comments,
        pr=int(args.reconciliation_pr),
        release_operation=str(args.reconciliation_release_operation_id),
        merge_sha=correction_sha,
    )
    return {
        "source": {
            "pull_request": source["pull_request"],
            "run_id": source["run_id"],
            "run_head_sha": source_merge_sha,
            "artifact_id": source["artifact_id"],
            "artifact_name": source["artifact_name"],
            "artifact_archive_digest": source["artifact_archive_digest"],
            "receipt_file": RECOVERY_ARTIFACT_FILE,
            "receipt_sha256": "sha256:" + str(source["receipt_sha256"]),
            "blocked_comment_id": source["blocked_comment_id"],
            "blocked_comment_digest": payload_digest(blocked_payload),
            "authorization_comment_id": source["authorization_comment_id"],
            "authorization_body_sha256": source["authorization_body_sha256"],
            "authorization_reference": (
                f"github:{args.repository}:pr:{args.pr}:comment:"
                f"{args.authorization_comment_id}:sha256:"
                f"{source['authorization_body_sha256']}"
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
            "material_qualification_digest": candidate_binding[
                "economics_candidate"
            ]["material_qualification_digest"],
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
        "reconciliation_release": release,
        "workflow_bridge": workflow_bridge,
    }


def _wbc0027_finalize_remote_command(
    *, target: Mapping[str, Any], context: Mapping[str, Any]
) -> list[str]:
    source = context["source"]
    release = context["reconciliation_release"]
    target_dir = str(target["target_dir"])
    evidence_dir = str(
        storage_destination_root("production_apply_evidence")
        / "production-goals"
        / str(source["operation_id"])
    )
    parts = [
        "python3",
        f"{target_dir}/apps/wbc0027_capital_recovery.py",
        "--runtime-dir",
        "/opt/wb-core-runtime/state",
        "--deployed-sha-file",
        f"{target_dir}/.wb-core-runtime-sha",
        "--expected-deployed-sha",
        str(release["merge_sha"]),
        "--profile",
        WBC0027_GOAL_PROFILE,
        "--target-id",
        CANONICAL_PRODUCTION_TARGET_ID,
        "--operation-id",
        str(source["operation_id"]),
        "--evidence-dir",
        evidence_dir,
        "finalize-only",
        "--source-deployed-sha",
        str(source["deployed_sha"]),
        "--source-manifest",
        str(source["economics_manifest_path"]),
        "--source-manifest-sha256",
        str(source["economics_manifest_sha256"]),
        "--source-phase-operation-id",
        str(source["economics_phase_operation_id"]),
        "--source-phase-fingerprint",
        str(source["economics_phase_fingerprint"]),
        "--source-storage-generation-json",
        json.dumps(
            source["storage_generation"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "--source-run-id",
        str(source["run_id"]),
        "--source-artifact-id",
        str(source["artifact_id"]),
        "--source-artifact-name",
        str(source["artifact_name"]),
        "--source-receipt-sha256",
        str(source["receipt_sha256"]),
        "--source-comment-id",
        str(source["blocked_comment_id"]),
        "--reconciliation-pr",
        str(release["pull_request"]),
        "--reconciliation-release-operation-id",
        str(release["operation_id"]),
        "--authorization-reference",
        str(source["authorization_reference"]),
        "--no-create",
    ]
    shell = (
        "set -eu; umask 077; test -d "
        + shlex.quote(evidence_dir)
        + '; test "$(stat -c %a '
        + shlex.quote(evidence_dir)
        + ')" = 700; cd '
        + shlex.quote(target_dir)
        + "; export PYTHONPATH="
        + shlex.quote(target_dir)
        + "; "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def _valid_wbc0027_finalize_result(
    result: Any, *, context: Mapping[str, Any]
) -> bool:
    if not isinstance(result, Mapping):
        return False
    source = context["source"]
    release = context["reconciliation_release"]
    source_apply = result.get("source_apply") or {}
    result_release = result.get("reconciliation_release") or {}
    source_row = result.get("source_recovery_row") or {}
    product = result.get("product_capital") or {}
    hard = result.get("hard_non_target") or {}
    source_transaction = result.get("source_transaction") or {}
    source_raw = source_transaction.get("source_raw_non_target") or {}
    source_target_rows = source_transaction.get("target_rows") or []
    source_write_set = source_transaction.get("write_set") or {}
    source_ordering = source_transaction.get("ordering") or {}
    source_code = source_transaction.get("source_code") or {}
    temporal_drift = result.get("temporal_non_target_drift") or {}
    protected = result.get("protected_invariant") or {}
    hashes = result.get("current_target_hashes")
    temporal_changed = bool(
        temporal_drift.get("current_ready_row_count")
        != source["economics_source_ready_row_count"]
        or temporal_drift.get("current_raw_non_target_row_count")
        != source["economics_source_raw_non_target_row_count"]
        or temporal_drift.get("current_raw_non_target_digest")
        != source["economics_source_raw_non_target_digest"]
    )
    derived_added = temporal_drift.get("derived_added_rows")
    observed_late = temporal_drift.get("observed_late_ordinary_rows")
    derived_added_valid = bool(
        isinstance(derived_added, list)
        and all(
            isinstance(item, Mapping)
            and isinstance(item.get("identity"), list)
            and len(item["identity"]) == 3
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("plan_sha256") or ""))
            is not None
            for item in derived_added
        )
        and (
            (
                temporal_drift.get("diff_derivation")
                == "unique_added_row_from_source_raw_aggregate"
                and len(derived_added) == 1
            )
            or (
                temporal_drift.get("diff_derivation")
                in {
                    "not_applicable",
                    "not_derivable_from_source_aggregate_digest",
                }
                and not derived_added
            )
        )
    )
    observed_late_valid = bool(
        isinstance(observed_late, list)
        and all(
            isinstance(item, Mapping)
            and isinstance(item.get("identity"), list)
            and len(item["identity"]) == 3
            and str(item["identity"][1]) >= "2026-08-30"
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("plan_sha256") or ""))
            is not None
            for item in observed_late
        )
    )
    return bool(
        result.get("contract_name")
        == "wbc0027_existing_operation_reconciliation/v1"
        and result.get("runtime_source_binding_contract")
        == WBC0027_RUNTIME_SOURCE_BINDING_CONTRACT
        and result.get("status") == "reconciled_existing_operation"
        and result.get("qualification_status") == "qualified"
        and result.get("repeat_disposition") == "already_qualifiable"
        and result.get("terminal_disposition")
        == "supersede_false_quarantine_receipt"
        and result.get("profile") == WBC0027_GOAL_PROFILE
        and result.get("target_id") == CANONICAL_PRODUCTION_TARGET_ID
        and result.get("goal_operation_id") == source["operation_id"]
        and result.get("product_phase_operation_id")
        == source["product_phase_operation_id"]
        and result.get("economics_phase_operation_id")
        == source["economics_phase_operation_id"]
        and result.get("source_deployed_sha") == source["deployed_sha"]
        and result.get("reconciliation_deployed_sha") == release["merge_sha"]
        and result.get("storage_generation") == source["storage_generation"]
        and isinstance(source_apply, Mapping)
        and source_apply.get("run_id") == source["run_id"]
        and source_apply.get("artifact_id") == source["artifact_id"]
        and source_apply.get("artifact_name") == source["artifact_name"]
        and source_apply.get("receipt_sha256") == source["receipt_sha256"]
        and source_apply.get("comment_id") == source["blocked_comment_id"]
        and source_apply.get("authorization_reference")
        == source["authorization_reference"]
        and result_release
        == {
            "pull_request": release["pull_request"],
            "operation_id": release["operation_id"],
        }
        and isinstance(source_row, Mapping)
        and source_row.get("operation_id")
        == source["economics_phase_operation_id"]
        and source_row.get("lifecycle") == "quarantined"
        and source_row.get("quarantine_reason")
        == "non_target_digest_drift_after_mutation"
        and source_row.get("after_digest") == source["economics_target_after_digest"]
        and source_row.get("non_target_digest")
        == source["economics_source_raw_non_target_digest"]
        and result.get("undo_row_count") == 3
        and hashes == source["economics_target_after_hashes"]
        and result.get("target_before_digest")
        == source["economics_target_before_digest"]
        and result.get("target_after_digest")
        == source["economics_target_after_digest"]
        and result.get("current_target_digest")
        == source["economics_target_after_digest"]
        and isinstance(source_transaction, Mapping)
        and source_transaction.get("contract_name")
        == "wbc0027_source_economics_transaction_legacy_adapter/v1"
        and source_transaction.get("source_ready_row_count")
        == source["economics_source_ready_row_count"]
        and source_transaction.get("source_semantic_components_reconstructable")
        is False
        and "source_semantic_non_target" not in source_transaction
        and source_transaction.get("source_adapter_rehearsal_digest")
        == source["source_adapter_rehearsal_digest"]
        and isinstance(source_raw, Mapping)
        and source_raw.get("contract_name")
        == "wbc0027_legacy_raw_non_target_aggregate/v1"
        and source_raw.get("row_count")
        == source["economics_source_raw_non_target_row_count"]
        and source_raw.get("digest") == source["economics_source_raw_non_target_digest"]
        and source_raw.get("binding") == "exact_source_manifest_and_recovery_row"
        and isinstance(source_target_rows, list)
        and [item.get("identity") for item in source_target_rows]
        == source["economics_target_identities"]
        and [item.get("before_sha256") for item in source_target_rows]
        == source["economics_target_before_hashes"]
        and [item.get("planned_after_sha256") for item in source_target_rows]
        == source["economics_target_after_hashes"]
        and [item.get("changed_cell_count") for item in source_target_rows]
        == source["economics_target_changed_cell_counts"]
        and [item.get("target_removed_before_digest") for item in source_target_rows]
        == source["economics_target_removed_digests"]
        and [
            item.get("target_removed_planned_after_digest")
            for item in source_target_rows
        ]
        == source["economics_target_removed_digests"]
        and source_write_set
        == {
            "row_count": 3,
            "cell_count": 472,
            "undo_row_count": 3,
            "undo_rows_verified": True,
            "undo_artifact_verified": True,
            "expected_after_image_count": 3,
        }
        and source_ordering.get("cas_before_images_verified") is True
        and source_ordering.get("exact_after_readback_verified") is True
        and source_ordering.get("source_code_semantic_before_after_equal") is True
        and source_ordering.get("source_code_commit_before_retain") is True
        and source_ordering.get("exact_retain_mismatch_caused_quarantine") is True
        and source_ordering.get("mutation_running_transition_index") == 4
        and source_ordering.get("quarantine_after_commit_index") == 5
        and isinstance(source_code, Mapping)
        and source_code.get("contract_name") == "wbc0027_source_economics_code_order/v1"
        and source_code.get("deployed_sha") == source["deployed_sha"]
        and source_code.get("phase_contract") == source["source_phase_contract"]
        and source_code.get("immutable_order")
        == [
            "before_image_cas",
            "exact_after_readback",
            "semantic_non_target_equality",
            "commit",
            "retain",
        ]
        and isinstance(temporal_drift, Mapping)
        and temporal_drift.get("contract_name")
        == "wbc0027_temporal_non_target_drift/v1"
        and temporal_drift.get("classification")
        in {"later_non_target_evolution", "no_later_non_target_evolution"}
        and temporal_drift.get("changed") is temporal_changed
        and temporal_drift.get("classification")
        == (
            "later_non_target_evolution"
            if temporal_changed
            else "no_later_non_target_evolution"
        )
        and temporal_drift.get("source_ready_row_count")
        == source["economics_source_ready_row_count"]
        and isinstance(temporal_drift.get("current_ready_row_count"), int)
        and temporal_drift.get("current_ready_row_count") >= 3
        and temporal_drift.get("source_raw_non_target_row_count")
        == source["economics_source_raw_non_target_row_count"]
        and isinstance(temporal_drift.get("current_raw_non_target_row_count"), int)
        and temporal_drift.get("current_raw_non_target_row_count") >= 0
        and temporal_drift.get("source_target_row_count") == 3
        and temporal_drift.get("current_target_row_count") == 3
        and temporal_drift.get("source_semantic_components_available") is False
        and temporal_drift.get("source_semantic_reconstruction_permitted") is False
        and isinstance(temporal_drift.get("current_component_digests"), Mapping)
        and set(temporal_drift["current_component_digests"])
        == {"identities", "semantic_payloads", "rows"}
        and temporal_drift.get("equality_gate") is False
        and temporal_drift.get("effect") == "receipt_evidence_only_not_target_approval"
        and derived_added_valid
        and observed_late_valid
        and all(
            re.fullmatch(r"sha256:[0-9a-f]{64}", str(value or ""))
            for value in (
                temporal_drift.get("current_semantic_digest"),
                temporal_drift.get("source_raw_non_target_digest"),
                temporal_drift.get("current_raw_non_target_digest"),
                *temporal_drift["current_component_digests"].values(),
            )
        )
        and isinstance(protected, Mapping)
        and protected.get("nm_id") == 428853741
        and protected.get("unit_cost_rub") == "117.537167"
        and isinstance(result.get("evidence_blocked"), list)
        and len(result.get("evidence_blocked")) == 12
        and isinstance(product, Mapping)
        and product.get("status") == "published_exact"
        and int(product.get("scope_count") or 0) == 1152
        and int(product.get("cell_count") or 0) == 24192
        and int(product.get("mismatch_count") or 0) == 0
        and isinstance(hard, Mapping)
        and hard.get("all_exact") is True
        and hard.get("from_date") == "2026-08-30"
        and int(hard.get("observed_date_count") or 0) >= 1
        and result.get("functional_economics_missing")
        == {"2026-08-26": 12, "2026-08-29": 0}
        and result.get("query_only") is True
        and result.get("database_written") is False
        and result.get("production_mutation_count") == 0
        and result.get("product_replay_count") == 0
        and result.get("economics_replay_count") == 0
    )


def _extract_wbc0027_reconciliation_artifact(
    raw_zip: bytes, *, expected_sha256: str
) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if (
                len(files) != 1
                or files[0].filename != WBC0027_RECONCILIATION_ARTIFACT_FILE
                or files[0].file_size <= 0
                or files[0].file_size > MAX_RECOVERY_ARTIFACT_BYTES
            ):
                raise ApplyError("WBC0027 reconciliation artifact shape is invalid")
            raw = archive.read(files[0])
    except zipfile.BadZipFile as exc:
        raise ApplyError("WBC0027 reconciliation artifact ZIP is invalid") from exc
    if digest(raw) != expected_sha256:
        raise ApplyError("WBC0027 reconciliation artifact digest mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplyError("WBC0027 reconciliation artifact JSON is invalid") from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise ApplyError("WBC0027 reconciliation artifact bytes are not canonical")
    return payload


def _verify_uploaded_wbc0027_reconciliation_artifact(
    client: GitHubClient,
    *,
    run_id: int,
    artifact_name: str,
    receipt_sha256: str,
    code_sha: str,
) -> dict[str, Any]:
    matches: list[Mapping[str, Any]] = []
    for attempt in range(6):
        payload = client.get(f"/actions/runs/{run_id}/artifacts?per_page=100")
        values = payload.get("artifacts") if isinstance(payload, Mapping) else None
        if not isinstance(values, list):
            raise ApplyError("WBC0027 reconciliation artifact listing is invalid")
        matches = [
            item
            for item in values
            if isinstance(item, Mapping) and item.get("name") == artifact_name
        ]
        if matches or attempt == 5:
            break
        time.sleep(2.0)
    if len(matches) != 1:
        raise ApplyError("uploaded WBC0027 reconciliation artifact is ambiguous")
    artifact = matches[0]
    workflow_run = artifact.get("workflow_run")
    if (
        artifact.get("expired") is True
        or not isinstance(artifact.get("id"), int)
        or not isinstance(workflow_run, Mapping)
        or workflow_run.get("id") != run_id
        or workflow_run.get("head_branch") != "main"
        or workflow_run.get("head_sha") != code_sha
    ):
        raise ApplyError("uploaded WBC0027 artifact provenance mismatch")
    raw_zip = client.request(
        "GET",
        f"/actions/artifacts/{int(artifact['id'])}/zip",
        accept="application/vnd.github+json",
        raw=True,
    )
    if not isinstance(raw_zip, bytes):
        raise ApplyError("uploaded WBC0027 artifact download failed")
    return {
        "metadata": dict(artifact),
        "receipt": _extract_wbc0027_reconciliation_artifact(
            raw_zip, expected_sha256=receipt_sha256
        ),
    }


def _validate_wbc0027_reconciliation_receipt(
    receipt: Any, *, context: Mapping[str, Any]
) -> bool:
    if not isinstance(receipt, Mapping):
        return False
    return bool(
        receipt.get("schema") == WBC0027_RECONCILIATION_RECEIPT_SCHEMA
        and receipt.get("state") == "done"
        and receipt.get("terminal_disposition")
        == "done/reconciled_existing_operation"
        and receipt.get("query_only") is True
        and receipt.get("database_written") is False
        and receipt.get("production_mutation_count") == 0
        and receipt.get("product_replay_count") == 0
        and receipt.get("economics_replay_count") == 0
        and receipt.get("source") == context["source"]
        and receipt.get("reconciliation_release")
        == context["reconciliation_release"]
        and receipt.get("workflow_bridge") == context["workflow_bridge"]
        and _valid_wbc0027_finalize_result(
            (receipt.get("probe") or {}).get("result"), context=context
        )
        and receipt.get("evidence_digest")
        == payload_digest(
            {key: value for key, value in receipt.items() if key != "evidence_digest"}
        )
    )


def _existing_wbc0027_reconciliation_marker(
    comments: list[Mapping[str, Any]],
    *,
    context: Mapping[str, Any],
    client: GitHubClient,
) -> Mapping[str, Any] | None:
    markers = [
        item
        for item in comments
        if WBC0027_RECONCILIATION_MARKER in str(item.get("body") or "")
    ]
    if not markers:
        return None
    if len(markers) != 1:
        raise ApplyError("WBC0027 reconciliation marker is duplicate or ambiguous")
    marker_comment = markers[0]
    payload = _parse_reconciliation_comment_payload(
        marker_comment,
        expected_marker=_wbc0027_reconciliation_marker(
            str(context["source"]["operation_id"])
        ),
    )
    artifact = payload.get("artifact") or {}
    if not (
        payload.get("schema") == WBC0027_RECONCILIATION_SUMMARY_SCHEMA
        and payload.get("state") == "done"
        and payload.get("terminal_disposition")
        == "done/reconciled_existing_operation"
        and payload.get("query_only") is True
        and payload.get("production_mutation_count") == 0
        and payload.get("product_replay_count") == 0
        and payload.get("economics_replay_count") == 0
        and payload.get("source") == context["source"]
        and payload.get("reconciliation_release")
        == context["reconciliation_release"]
        and payload.get("workflow_bridge") == context["workflow_bridge"]
        and isinstance(artifact, Mapping)
        and artifact.get("file") == WBC0027_RECONCILIATION_ARTIFACT_FILE
        and artifact.get("retention_days") == 90
        and re.fullmatch(
            r"wbc0027-existing-operation-reconciliation-pr-1129-run-([1-9][0-9]*)",
            str(artifact.get("name") or ""),
        )
        is not None
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(artifact.get("sha256") or "")
        )
        is not None
    ):
        raise ApplyError("preexisting WBC0027 reconciliation marker drifted")
    run_id = int(str(artifact["name"]).rsplit("-", 1)[1])
    verified = _verify_uploaded_wbc0027_reconciliation_artifact(
        client,
        run_id=run_id,
        artifact_name=str(artifact["name"]),
        receipt_sha256=str(artifact["sha256"])[len("sha256:") :],
        code_sha=str(context["workflow_bridge"]["merge_sha"]),
    )
    receipt = verified["receipt"]
    if not _validate_wbc0027_reconciliation_receipt(receipt, context=context):
        raise ApplyError("preexisting WBC0027 reconciliation receipt drifted")
    raw = canonical_json_bytes(receipt) + b"\n"
    if (
        artifact.get("size_bytes") != len(raw)
        or artifact.get("sha256") != "sha256:" + digest(raw)
    ):
        raise ApplyError("preexisting WBC0027 artifact metadata drifted")
    return marker_comment


def _run_wbc0027_reconciliation_preflight(
    *, args: argparse.Namespace, client: GitHubClient, pr: Mapping[str, Any], comments: list[Mapping[str, Any]]
) -> int:
    context = _wbc0027_reconciliation_context(
        args=args, client=client, source_pr=pr, source_comments=comments
    )
    existing = _existing_wbc0027_reconciliation_marker(
        comments, context=context, client=client
    )
    receipt = {
        "schema": WBC0027_RECONCILIATION_RECEIPT_SCHEMA,
        "state": "already_terminal" if existing is not None else "ready_for_probe",
        "query_only": True,
        "database_written": False,
        "production_mutation_count": 0,
        "product_replay_count": 0,
        "economics_replay_count": 0,
        "source": context["source"],
        "reconciliation_release": context["reconciliation_release"],
        "workflow_bridge": context["workflow_bridge"],
        "comment_id": int(existing.get("id") or 0) if existing is not None else 0,
    }
    _write_receipt(args.output, receipt)
    if args.github_output:
        _write_github_output(
            args.github_output,
            {
                "probe_required": existing is None,
                "state": receipt["state"],
                "comment_id": receipt["comment_id"],
            },
        )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def _run_wbc0027_reconciliation_collect(
    *, args: argparse.Namespace, client: GitHubClient, pr: Mapping[str, Any], comments: list[Mapping[str, Any]]
) -> int:
    context = _wbc0027_reconciliation_context(
        args=args, client=client, source_pr=pr, source_comments=comments
    )
    if _existing_wbc0027_reconciliation_marker(
        comments, context=context, client=client
    ) is not None:
        raise ApplyError("WBC0027 reconciliation became terminal before collect")
    target = _canonical_target()
    with tempfile.TemporaryDirectory(prefix="wbc0027-reconciliation-") as directory:
        configure_deploy_environment(Path(directory))
        probe = command_evidence(
            _wbc0027_finalize_remote_command(target=target, context=context),
            timeout_seconds=900.0,
        )
    if not (
        probe.get("return_code") == 0
        and probe.get("transport_ambiguous") is False
        and _valid_wbc0027_finalize_result(probe.get("result"), context=context)
    ):
        raise ApplyError("WBC0027 query-only reconciliation proof is not exact")
    receipt: dict[str, Any] = {
        "schema": WBC0027_RECONCILIATION_RECEIPT_SCHEMA,
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
        "probe": probe,
    }
    receipt["evidence_digest"] = payload_digest(receipt)
    _write_receipt(args.output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def _run_wbc0027_reconciliation_publish(
    *, args: argparse.Namespace, client: GitHubClient, pr: Mapping[str, Any], comments: list[Mapping[str, Any]]
) -> int:
    context = _wbc0027_reconciliation_context(
        args=args, client=client, source_pr=pr, source_comments=comments
    )
    if _existing_wbc0027_reconciliation_marker(
        comments, context=context, client=client
    ) is not None:
        raise ApplyError("WBC0027 reconciliation became terminal before publish")
    raw = args.output.read_bytes()
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplyError("WBC0027 reconciliation receipt is unreadable") from exc
    if (
        raw != canonical_json_bytes(receipt) + b"\n"
        or not _validate_wbc0027_reconciliation_receipt(receipt, context=context)
    ):
        raise ApplyError("WBC0027 reconciliation receipt contract is invalid")
    run_id = int(os.environ.get("GITHUB_RUN_ID") or 0)
    if run_id <= 0:
        raise ApplyError("WBC0027 reconciliation publication lacks run identity")
    artifact_name = _wbc0027_reconciliation_artifact_name(run_id)
    _verify_uploaded_wbc0027_reconciliation_artifact(
        client,
        run_id=run_id,
        artifact_name=artifact_name,
        receipt_sha256=digest(raw),
        code_sha=str(context["workflow_bridge"]["merge_sha"]),
    )
    result = receipt["probe"]["result"]
    summary = {
        "schema": WBC0027_RECONCILIATION_SUMMARY_SCHEMA,
        "state": "done",
        "terminal_disposition": "done/reconciled_existing_operation",
        "query_only": True,
        "production_mutation_count": 0,
        "product_replay_count": 0,
        "economics_replay_count": 0,
        "source": context["source"],
        "reconciliation_release": context["reconciliation_release"],
        "workflow_bridge": context["workflow_bridge"],
        "qualification": {
            "source_recovery_row_digest": result["source_recovery_row_digest"],
            "transition_digest": result["transition_digest"],
            "undo_digest": result["undo_digest"],
            "source_raw_non_target_digest": result["source_transaction"][
                "source_raw_non_target"
            ]["digest"],
            "source_adapter_rehearsal_digest": result["source_transaction"][
                "source_adapter_rehearsal_digest"
            ],
            "current_semantic_non_target_digest": result["temporal_non_target_drift"][
                "current_semantic_digest"
            ],
            "temporal_non_target_drift": result["temporal_non_target_drift"],
            "current_target_digest": result["current_target_digest"],
            "current_target_hashes": result["current_target_hashes"],
            "functional_economics_missing": result[
                "functional_economics_missing"
            ],
        },
        "evidence_digest": receipt["evidence_digest"],
        "artifact": {
            "name": artifact_name,
            "file": WBC0027_RECONCILIATION_ARTIFACT_FILE,
            "sha256": "sha256:" + digest(raw),
            "size_bytes": len(raw),
            "retention_days": 90,
        },
    }
    body = (
        _wbc0027_reconciliation_marker(str(context["source"]["operation_id"]))
        + "\nCompact immutable supersession receipt for the already committed WBC0027 economics operation; no business data was replayed."
        + "\n```json\n"
        + json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n```"
    )
    if len(body.encode("utf-8")) >= MAX_GITHUB_COMMENT_BYTES:
        raise ApplyError("WBC0027 reconciliation marker exceeds GitHub limit")
    published = client.post(f"/issues/{args.pr}/comments", {"body": body})
    if not isinstance(published, Mapping) or published.get("body") != body:
        raise ApplyError("WBC0027 reconciliation marker publication mismatch")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def _run_wbc0027_reconciliation_mode(
    *, args: argparse.Namespace, client: GitHubClient, pr: Mapping[str, Any], comments: list[Mapping[str, Any]]
) -> int:
    if args.reconciliation_phase == "preflight":
        return _run_wbc0027_reconciliation_preflight(
            args=args, client=client, pr=pr, comments=comments
        )
    if args.reconciliation_phase == "collect":
        return _run_wbc0027_reconciliation_collect(
            args=args, client=client, pr=pr, comments=comments
        )
    if args.reconciliation_phase == "publish":
        return _run_wbc0027_reconciliation_publish(
            args=args, client=client, pr=pr, comments=comments
        )
    raise ApplyError("WBC0027 reconciliation phase is invalid")


def _load_legacy_manifest(path: Path, expected_sha: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if digest(raw) != expected_sha:
        raise ApplyError("manifest digest mismatch")
    manifest = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(manifest, dict)
        or validate_production_manifest(manifest)["valid"] is not True
    ):
        raise ApplyError("manifest contract invalid")
    return manifest


def _run_legacy_commands(manifest: Mapping[str, Any]) -> dict[str, Any]:
    commands = manifest["commands"]
    dry_run = command_evidence(list(commands["dry_run"]))
    if dry_run["return_code"] != 0:
        return {"state": "blocked", "apply_count": 0, "dry_run": dry_run}
    apply_result = command_evidence(list(commands["apply"]))
    readback = command_evidence(list(commands["readback"]))
    reconcile = command_evidence(list(commands["reconcile"]))
    complete = all(
        item["return_code"] == 0 for item in (apply_result, readback, reconcile)
    )
    return {
        "state": "done" if complete else "blocked",
        "apply_count": 1,
        "dry_run": dry_run,
        "apply": apply_result,
        "readback": readback,
        "reconcile": reconcile,
    }


def _run_legacy_commands_with_deploy_environment(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Materialize the canonical hosted SSH identity for exact-manifest commands."""

    environment_names = (
        "WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE",
        "WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS",
    )
    previous = {name: os.environ.get(name) for name in environment_names}
    try:
        with tempfile.TemporaryDirectory(prefix="production-apply-deploy-") as directory:
            configure_deploy_environment(Path(directory))
            return _run_legacy_commands(manifest)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _run_legacy_mode(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> int:
    merge_sha = exact_sha(args.merge_sha, "merge")
    deployed_sha = exact_sha(args.deployed_sha, "deployed")
    if deployed_sha != merge_sha:
        raise ApplyError("deployed SHA must equal exact merge SHA")
    if exact_sha(pr.get("merge_commit_sha"), "pr-merge") != merge_sha:
        raise ApplyError("PR merge binding mismatch")
    if re.fullmatch(r"[0-9a-f]{64}", str(args.manifest_sha256 or "")) is None:
        raise ApplyError("manifest SHA-256 is invalid")
    operation = str(args.operation_id or "")
    prior = [
        item
        for item in comments
        if marker(operation) in str(item.get("body") or "")
        and is_actions_bot_comment(item)
    ]
    if prior:
        if len(prior) != 1:
            raise ApplyError("duplicate or ambiguous durable apply receipt")
        receipt = {
            "schema": APPLY_RECEIPT_SCHEMA,
            "state": "already_terminal",
            "operation_id": operation,
            "pull_request": args.pr,
        }
        _write_receipt(args.output, receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    release_receipt = parse_legacy_release_receipt(
        comments,
        pr=args.pr,
        merge_sha=merge_sha,
        manifest_sha=str(args.manifest_sha256),
        operation=operation,
    )
    authorization = client.get(f"/issues/comments/{args.authorization_comment_id}")
    validate_legacy_authorization(
        authorization,
        pr=args.pr,
        merge_sha=merge_sha,
        deployed_sha=deployed_sha,
        manifest_sha=str(args.manifest_sha256),
        operation=operation,
    )
    subprocess.run(
        ["git", "fetch", "--no-tags", "origin", merge_sha], cwd=ROOT, check=True
    )
    subprocess.run(["git", "checkout", "--detach", merge_sha], cwd=ROOT, check=True)
    binding = release_receipt["manifest"]
    manifest_path = (ROOT / str(binding["path"])).resolve()
    if ROOT not in manifest_path.parents:
        raise ApplyError("manifest path escapes repository")
    manifest = _load_legacy_manifest(manifest_path, str(args.manifest_sha256))
    if manifest.get("operation_id") != operation:
        raise ApplyError("manifest operation id mismatch")
    result = _run_legacy_commands_with_deploy_environment(manifest)
    approval_body = str(authorization.get("body") or "").strip()
    receipt = {
        "schema": APPLY_RECEIPT_SCHEMA,
        "state": result["state"],
        "operation_id": operation,
        "repository": args.repository,
        "pull_request": args.pr,
        "merge_sha": merge_sha,
        "deployed_sha": deployed_sha,
        "manifest_sha256": str(args.manifest_sha256),
        "authorization_comment_id": args.authorization_comment_id,
        "authorization_body_sha256": digest(approval_body.encode("utf-8")),
        "apply_count": result["apply_count"],
        "evidence": result,
    }
    _write_receipt(args.output, receipt)
    run_id = int(os.environ.get("GITHUB_RUN_ID") or 0)
    if run_id <= 0:
        raise ApplyError("production apply publication lacks GitHub run identity")
    _publish_compact_apply_receipt(
        client,
        pr=args.pr,
        receipt=receipt,
        receipt_path=args.output,
        artifact_name=_receipt_artifact_name(args.pr, run_id),
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


def _run_warm_readiness_mode(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> int:
    if not args.release_operation_id:
        raise ApplyError("warm archive readiness requires --release-operation-id")
    if args.authorization_comment_id <= 0:
        raise ApplyError("warm archive readiness requires --authorization-comment-id")
    merge_sha = exact_sha(pr.get("merge_commit_sha"), "pr-merge")
    release_receipt = parse_release_receipt(
        comments,
        pr=args.pr,
        release_operation=args.release_operation_id,
        merge_sha=merge_sha,
    )
    mount_probe = parse_warm_mount_probe_receipt(
        comments,
        repository=args.repository,
        pr=args.pr,
        release_operation=args.release_operation_id,
        merge_sha=merge_sha,
    )
    authorization = client.get(f"/issues/comments/{args.authorization_comment_id}")
    try:
        probe_created_at = datetime.fromisoformat(
            str(mount_probe["comment_created_at"]).replace("Z", "+00:00")
        )
        authorization_created_at = datetime.fromisoformat(
            str(authorization.get("created_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ApplyError("warm archive owner/probe chronology is invalid") from exc
    if (
        authorization.get("id") != args.authorization_comment_id
        or str(authorization.get("author_association") or "").upper() != "OWNER"
        or args.authorization_comment_id <= int(mount_probe["comment_id"])
        or authorization_created_at <= probe_created_at
    ):
        raise ApplyError(
            "warm archive readiness requires a fresh OWNER binding after the exact worker probe"
        )
    goal = validate_authorization(
        authorization,
        repository=args.repository,
        pr=args.pr,
    )
    if goal.get("profile") != WARM_ARCHIVE_GOAL_PROFILE:
        raise ApplyError("warm archive readiness authorization profile is invalid")
    goal_operation_id = operation_id(
        args.repository,
        args.pr,
        args.authorization_comment_id,
        goal,
    )
    attempts = _collect_warm_readiness_attempts(
        comments,
        repository=args.repository,
        pr=args.pr,
        release_operation=args.release_operation_id,
        merge_sha=merge_sha,
        authorization_comment_id=args.authorization_comment_id,
        goal_operation_id=goal_operation_id,
    )
    ready = [payload for payload in attempts.values() if payload["state"] == "ready"]
    if ready:
        receipt = {
            **parse_warm_readiness_receipt(
                comments,
                repository=args.repository,
                pr=args.pr,
                release_operation=args.release_operation_id,
                merge_sha=merge_sha,
                authorization_comment_id=args.authorization_comment_id,
                goal_operation_id=goal_operation_id,
            ),
            "idempotent": True,
        }
        _write_receipt(args.output, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    if len(attempts) >= MAX_WARM_READINESS_ATTEMPTS:
        receipt = {
            "schema": WARM_READINESS_RECEIPT_SCHEMA,
            "state": "blocked",
            "reason": "bounded-readiness-sequence-exhausted",
            "query_only": True,
            "database_written": False,
            "repository": args.repository,
            "pull_request": args.pr,
            "release_operation_id": release_receipt["operation_id"],
            "authorization_comment_id": args.authorization_comment_id,
            "goal_operation_id": goal_operation_id,
            "mount_probe_job_id": mount_probe["job_id"],
            "mount_probe_evidence_digest": mount_probe["evidence_digest"],
            "mount_probe_artifact": mount_probe["artifact"],
            "mount_probe_comment_id": mount_probe["comment_id"],
            "merge_sha": merge_sha,
            "deployed_sha": merge_sha,
            "attempt_count": len(attempts),
            "readiness_ids": [
                attempts[number]["readiness_id"] for number in sorted(attempts)
            ],
            "production_command_count": 0,
            "comment_publication_count": 0,
        }
        _write_receipt(args.output, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0

    attempt = len(attempts) + 1
    readiness = warm_readiness_id(
        args.repository,
        args.pr,
        args.release_operation_id,
        args.authorization_comment_id,
        goal_operation_id,
        attempt,
    )

    subprocess.run(
        ["git", "fetch", "--no-tags", "origin", merge_sha], cwd=ROOT, check=True
    )
    subprocess.run(["git", "checkout", "--detach", merge_sha], cwd=ROOT, check=True)
    target = _canonical_target()
    with tempfile.TemporaryDirectory(
        prefix="wb-core-warm-archive-readiness-"
    ) as directory:
        configure_deploy_environment(Path(directory))
        evidence = command_evidence(
            _warm_readiness_remote_command(
                target=target, merge_sha=merge_sha, readiness_id=readiness
            ),
            timeout_seconds=14_400.0,
        )
    payload = evidence.get("result")
    systemd_service_gate = (
        payload.get("systemd_service_gate") if isinstance(payload, Mapping) else None
    )
    valid_payload = bool(
        evidence.get("return_code") == 0
        and isinstance(payload, Mapping)
        and payload.get("status") in {"ready", "blocked"}
        and payload.get("query_only") is True
        and payload.get("database_written") is False
        and payload.get("readiness_id") == readiness
        and payload.get("deployed_sha") == merge_sha
        and _valid_warm_systemd_service_gate(
            systemd_service_gate, require_healthy=payload.get("status") == "ready"
        )
        and (
            payload.get("status") != "ready"
            or (
                payload.get("source_count") == 6
                and payload.get("capacity_guard_passed") is True
                and payload.get("root_minimum_after_bytes") == 25 * 1024**3
            )
        )
    )
    if not valid_payload:
        state = "blocked"
        reason = "readiness-transport-or-contract-failed"
        payload = None
    else:
        state = str(payload["status"])
        reason = (
            "bounded-readiness-clean"
            if state == "ready"
            else str(payload.get("reason") or "bounded-readiness-blocked")
        )
    receipt: dict[str, Any] = {
        "schema": WARM_READINESS_RECEIPT_SCHEMA,
        "state": state,
        "reason": reason,
        "attempt": attempt,
        "query_only": True,
        "database_written": False,
        "readiness_id": readiness,
        "repository": args.repository,
        "pull_request": args.pr,
        "release_operation_id": release_receipt["operation_id"],
        "authorization_comment_id": args.authorization_comment_id,
        "goal_operation_id": goal_operation_id,
        "mount_probe_job_id": mount_probe["job_id"],
        "mount_probe_evidence_digest": mount_probe["evidence_digest"],
        "mount_probe_artifact": mount_probe["artifact"],
        "mount_probe_comment_id": mount_probe["comment_id"],
        "merge_sha": merge_sha,
        "deployed_sha": merge_sha,
        "evidence": evidence,
    }
    if isinstance(payload, Mapping):
        receipt.update(
            {
                "projection_manifest_path": payload.get("projection_manifest_path"),
                "projection_manifest_sha256": payload.get("projection_manifest_sha256"),
                "material_qualification_digest": payload.get(
                    "material_qualification_digest"
                ),
                "material_partition": payload.get("material_partition"),
                "immutable_non_target_digest": payload.get(
                    "immutable_non_target_digest"
                ),
                "mutable_canonical_topology_digest": payload.get(
                    "mutable_canonical_topology_digest"
                ),
                "mutable_canonical_observations": payload.get(
                    "mutable_canonical_observations"
                ),
                "mutable_safety_predicates": payload.get("mutable_safety_predicates"),
                "expected_reclaimed_allocated_bytes": payload.get(
                    "expected_reclaimed_allocated_bytes"
                ),
                "required_backup_floor_bytes": payload.get(
                    "required_backup_floor_bytes"
                ),
                "root_minimum_after_bytes": payload.get("root_minimum_after_bytes"),
                "systemd_service_gate": payload.get("systemd_service_gate"),
                "component_diff": payload.get("component_diff"),
                "callback": _readiness_callback_summary(payload.get("callback", [])),
            }
        )
    if state == "ready":
        for field in (
            "projection_manifest_path",
            "projection_manifest_sha256",
            "material_qualification_digest",
            "material_partition",
            "immutable_non_target_digest",
            "mutable_canonical_topology_digest",
            "mutable_safety_predicates",
        ):
            if not receipt.get(field):
                raise ApplyError(f"ready warm archive receipt lacks {field}")
    _write_receipt(args.output, receipt)
    run_id = int(os.environ.get("GITHUB_RUN_ID") or 0)
    if run_id <= 0:
        raise ApplyError("warm archive readiness publication lacks GitHub run identity")
    raw_receipt = args.output.read_bytes()
    body = _compact_warm_readiness_comment_body(
        receipt,
        artifact_name=_warm_readiness_artifact_name(args.pr, run_id),
        receipt_sha256=digest(raw_receipt),
        receipt_size_bytes=len(raw_receipt),
    )
    published = client.post(f"/issues/{args.pr}/comments", {"body": body})
    if not isinstance(published, Mapping) or published.get("body") != body:
        raise ApplyError("warm archive readiness publication response mismatch")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def _run_warm_mount_probe_mode(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> int:
    if not args.release_operation_id:
        raise ApplyError("warm archive mount probe requires --release-operation-id")
    merge_sha = exact_sha(pr.get("merge_commit_sha"), "pr-merge")
    release_receipt = parse_release_receipt(
        comments,
        pr=args.pr,
        release_operation=args.release_operation_id,
        merge_sha=merge_sha,
    )
    job_id = warm_mount_probe_job_id(
        args.repository,
        args.pr,
        args.release_operation_id,
        merge_sha,
    )
    marker_text = warm_mount_probe_marker(job_id)
    existing = [
        item
        for item in comments
        if marker_text in str(item.get("body") or "") and is_actions_bot_comment(item)
    ]
    if len(existing) > 1:
        raise ApplyError("duplicate warm archive mount probe receipts")
    if existing:
        body = str(existing[0].get("body") or "")
        try:
            payload = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
        except (IndexError, json.JSONDecodeError) as exc:
            raise ApplyError(
                "existing warm archive mount probe receipt is invalid"
            ) from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != WARM_MOUNT_PROBE_RECEIPT_SCHEMA
            or payload.get("job_id") != job_id
            or payload.get("deployed_sha") != merge_sha
            or payload.get("state") != "observed"
        ):
            raise ApplyError("existing warm archive mount probe binding drifted")
        receipt = {**dict(payload), "idempotent": True, "production_probe_count": 0}
        _write_receipt(args.output, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0

    subprocess.run(
        ["git", "fetch", "--no-tags", "origin", merge_sha], cwd=ROOT, check=True
    )
    subprocess.run(["git", "checkout", "--detach", merge_sha], cwd=ROOT, check=True)
    target = _canonical_target()
    with tempfile.TemporaryDirectory(prefix="wb-core-warm-mount-probe-") as directory:
        configure_deploy_environment(Path(directory))
        submit = command_evidence(
            _warm_mount_probe_submit_remote_command(
                target=target,
                merge_sha=merge_sha,
                job_id=job_id,
            ),
            timeout_seconds=120.0,
        )
        status_attempts: list[dict[str, Any]] = []
        final_status: Mapping[str, Any] | None = None
        for _attempt in range(90):
            status = command_evidence(
                _warm_mount_probe_status_remote_command(
                    target=target,
                    merge_sha=merge_sha,
                    job_id=job_id,
                ),
                timeout_seconds=60.0,
            )
            status_attempts.append(
                {
                    "command_sha256": status.get("command_sha256"),
                    "return_code": status.get("return_code"),
                    "stdout_sha256": status.get("stdout_sha256"),
                    "stderr_sha256": status.get("stderr_sha256"),
                    "transport_ambiguous": status.get("transport_ambiguous"),
                }
            )
            result = status.get("result")
            if (
                status.get("return_code") == 0
                and isinstance(result, Mapping)
                and result.get("terminal") is True
            ):
                final_status = result
                break
            time.sleep(2)
    probe = (
        final_status.get("result")
        if isinstance(final_status, Mapping)
        and isinstance(final_status.get("result"), Mapping)
        else None
    )
    valid = bool(
        isinstance(final_status, Mapping)
        and final_status.get("status") == "succeeded"
        and final_status.get("terminal") is True
        and isinstance(final_status.get("request"), Mapping)
        and final_status["request"].get("job_id") == job_id
        and final_status["request"].get("deployed_sha") == merge_sha
        and final_status["request"].get("operation") == "warm-archive-mount-probe"
        and isinstance(probe, Mapping)
        and probe.get("schema") == "wb-core.root-warm-archive-mount-probe/v1"
        and probe.get("status") == "observed"
        and probe.get("query_only") is True
        and probe.get("database_written") is False
        and probe.get("archive_mutation_count") == 0
        and probe.get("source_unlink_count") == 0
        and probe.get("service_restart_count") == 0
        and probe.get("timer_change_count") == 0
        and probe.get("job_id") == job_id
        and probe.get("deployed_sha") == merge_sha
        and probe.get("path_count") == 3
        and isinstance(probe.get("paths"), list)
        and all(isinstance(item, Mapping) for item in probe["paths"])
        and {item.get("filesystem_role") for item in probe["paths"]}
        == {"root", "backup", "generation"}
        and any(
            item.get("filesystem_role") == "backup"
            and int(item.get("raw_candidate_count") or 0) > 1
            for item in probe["paths"]
        )
        and all(
            isinstance(item, Mapping)
            and int(item.get("raw_candidate_count") or 0) >= 1
            and isinstance(item.get("raw_mount_candidates"), list)
            and len(item["raw_mount_candidates"]) == int(item["raw_candidate_count"])
            and all(
                isinstance(candidate, Mapping)
                and isinstance(candidate.get("raw_line"), str)
                and bool(candidate.get("raw_line"))
                for candidate in item["raw_mount_candidates"]
            )
            and isinstance(item.get("candidate_proofs"), list)
            and len(item["candidate_proofs"]) == int(item["raw_candidate_count"])
            for item in probe["paths"]
        )
    )
    state = "observed" if valid else "blocked"
    receipt: dict[str, Any] = {
        "schema": WARM_MOUNT_PROBE_RECEIPT_SCHEMA,
        "state": state,
        "reason": (
            "exact-worker-mount-candidates-observed"
            if valid
            else "mount-probe-transport-or-contract-failed"
        ),
        "query_only": True,
        "database_written": False,
        "production_probe_count": 1,
        "job_id": job_id,
        "repository": args.repository,
        "pull_request": args.pr,
        "release_operation_id": release_receipt["operation_id"],
        "merge_sha": merge_sha,
        "deployed_sha": merge_sha,
        "submit": submit,
        "status_attempt_count": len(status_attempts),
        "status_attempts": status_attempts,
        "final_status": final_status,
        "probe": probe,
    }
    _write_receipt(args.output, receipt)
    run_id = int(os.environ.get("GITHUB_RUN_ID") or 0)
    if run_id <= 0:
        raise ApplyError(
            "warm archive mount probe publication lacks GitHub run identity"
        )
    raw_receipt = args.output.read_bytes()
    compact_paths = [
        {
            "filesystem_role": item.get("filesystem_role"),
            "target": item.get("target"),
            "semantic_identity_digest": item.get("semantic_identity_digest"),
            "raw_candidate_count": item.get("raw_candidate_count"),
            "raw_candidates_digest": item.get("raw_candidates_digest"),
        }
        for item in (probe or {}).get("paths") or []
        if isinstance(item, Mapping)
    ]
    compact = {
        key: receipt.get(key)
        for key in (
            "schema",
            "state",
            "reason",
            "query_only",
            "database_written",
            "production_probe_count",
            "job_id",
            "repository",
            "pull_request",
            "release_operation_id",
            "merge_sha",
            "deployed_sha",
            "status_attempt_count",
        )
    }
    compact.update(
        {
            "paths": compact_paths,
            "worker": (probe or {}).get("worker"),
            "evidence_digest": (probe or {}).get("evidence_digest"),
            "artifact": {
                "name": f"root-warm-archive-mount-probe-pr-{args.pr}-run-{run_id}",
                "file": "root-warm-archive-mount-probe-receipt.json",
                "sha256": "sha256:" + digest(raw_receipt),
                "size_bytes": len(raw_receipt),
                "retention_days": 90,
            },
        }
    )
    body = (
        marker_text
        + "\nCompact terminal exact-worker query-only mount probe; full raw candidates are in the bound Actions artifact."
        + "\n```json\n"
        + json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n```"
    )
    if len(body.encode("utf-8")) >= MAX_GITHUB_COMMENT_BYTES:
        raise ApplyError(
            "compact warm archive mount probe comment exceeds GitHub limit"
        )
    published = client.post(f"/issues/{args.pr}/comments", {"body": body})
    if not isinstance(published, Mapping) or published.get("body") != body:
        raise ApplyError("warm archive mount probe publication response mismatch")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--authorization-mode",
        choices=(
            "scope-goal",
            "exact-manifest",
            "receipt-recovery",
            "warm-archive-readiness",
            "warm-archive-mount-probe",
            "warm-archive-receipt-reconciliation",
            "wbc0027-receipt-reconciliation",
        ),
        default="scope-goal",
    )
    parser.add_argument("--repository", default=CANONICAL_REPOSITORY)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--release-operation-id")
    parser.add_argument("--authorization-comment-id", type=int, default=0)
    parser.add_argument("--merge-sha")
    parser.add_argument("--deployed-sha")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--operation-id")
    parser.add_argument("--source-run-id", type=int)
    parser.add_argument("--source-artifact-id", type=int, default=0)
    parser.add_argument("--source-artifact-name")
    parser.add_argument("--source-receipt-sha256")
    parser.add_argument("--blocked-comment-id", type=int, default=0)
    parser.add_argument("--reconciliation-pr", type=int, default=0)
    parser.add_argument("--reconciliation-release-operation-id")
    parser.add_argument("--reconciliation-attempt")
    parser.add_argument("--prior-reconciliation-run-id", type=int, default=0)
    parser.add_argument("--prior-reconciliation-artifact-id", type=int, default=0)
    parser.add_argument("--prior-reconciliation-artifact-name")
    parser.add_argument("--prior-reconciliation-receipt-sha256")
    parser.add_argument("--prior-reconciliation-comment-id", type=int, default=0)
    parser.add_argument("--prior-reconciliation-a02-run-id", type=int, default=0)
    parser.add_argument("--prior-reconciliation-a02-artifact-id", type=int, default=0)
    parser.add_argument("--prior-reconciliation-a02-artifact-name")
    parser.add_argument("--prior-reconciliation-a02-receipt-sha256")
    parser.add_argument("--prior-reconciliation-a02-comment-id", type=int, default=0)
    parser.add_argument(
        "--reconciliation-phase",
        choices=("preflight", "collect", "publish"),
        default="preflight",
    )
    parser.add_argument("--github-output")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    client = GitHubClient(args.repository, token)
    pr = client.get(f"/pulls/{args.pr}")
    if pr.get("merged") is not True:
        raise ApplyError("pull request is not merged")
    comments = list_comments(client, args.pr)
    if args.authorization_mode == "wbc0027-receipt-reconciliation":
        return _run_wbc0027_reconciliation_mode(
            args=args,
            client=client,
            pr=pr,
            comments=comments,
        )
    if args.authorization_mode == "warm-archive-receipt-reconciliation":
        return _run_warm_reconciliation_mode(
            args=args,
            client=client,
            pr=pr,
            comments=comments,
        )
    if args.authorization_mode == "warm-archive-mount-probe":
        return _run_warm_mount_probe_mode(
            args=args,
            client=client,
            pr=pr,
            comments=comments,
        )
    if args.authorization_mode == "warm-archive-readiness":
        return _run_warm_readiness_mode(
            args=args,
            client=client,
            pr=pr,
            comments=comments,
        )
    if args.authorization_mode == "receipt-recovery":
        if args.authorization_comment_id <= 0:
            raise ApplyError("receipt recovery requires --authorization-comment-id")
        return _run_receipt_recovery(
            args=args,
            client=client,
            pr=pr,
            comments=comments,
        )
    if args.authorization_mode == "exact-manifest":
        if args.authorization_comment_id <= 0:
            raise ApplyError("exact-manifest mode requires --authorization-comment-id")
        required = {
            "merge_sha": args.merge_sha,
            "deployed_sha": args.deployed_sha,
            "manifest_sha256": args.manifest_sha256,
            "operation_id": args.operation_id,
        }
        missing = sorted(field for field, value in required.items() if not value)
        if missing:
            raise ApplyError(
                "exact-manifest mode inputs are missing: " + ", ".join(missing)
            )
        return _run_legacy_mode(
            args=args,
            client=client,
            pr=pr,
            comments=comments,
        )
    if not args.release_operation_id:
        raise ApplyError("scope-goal mode requires --release-operation-id")
    if args.authorization_comment_id <= 0:
        raise ApplyError("scope-goal mode requires --authorization-comment-id")
    merge_sha = exact_sha(pr.get("merge_commit_sha"), "pr-merge")
    release_receipt = parse_release_receipt(
        comments,
        pr=args.pr,
        release_operation=args.release_operation_id,
        merge_sha=merge_sha,
    )
    authorization = client.get(f"/issues/comments/{args.authorization_comment_id}")
    goal = validate_authorization(
        authorization,
        repository=args.repository,
        pr=args.pr,
    )
    configured_profiles = {
        item.strip()
        for item in os.environ.get("WB_CORE_SCOPE_GOAL_PROFILE_ALLOWLIST", "").split(",")
        if item.strip()
    }
    if configured_profiles and goal["profile"] not in configured_profiles:
        raise ApplyError("scope-goal profile is not workflow-allowlisted")
    operation = operation_id(
        args.repository,
        args.pr,
        args.authorization_comment_id,
        goal,
    )
    predecessor_binding = goal.get("supersedes_terminal_predecessor")
    if (
        goal["profile"] == WBC0027_GOAL_PROFILE
        and isinstance(predecessor_binding, Mapping)
        and operation == predecessor_binding.get("goal_operation_id")
    ):
        raise ApplyError("WBC0027 correction operation reused terminal predecessor")
    warm_readiness = (
        parse_warm_readiness_receipt(
            comments,
            repository=args.repository,
            pr=args.pr,
            release_operation=args.release_operation_id,
            merge_sha=merge_sha,
            authorization_comment_id=args.authorization_comment_id,
            goal_operation_id=operation,
        )
        if goal["profile"] == WARM_ARCHIVE_GOAL_PROFILE
        else None
    )
    prior = [
        item
        for item in comments
        if marker(operation) in str(item.get("body") or "")
        and is_actions_bot_comment(item)
    ]
    if prior:
        if len(prior) != 1:
            raise ApplyError("duplicate or ambiguous durable apply receipt")
        receipt = {
            "schema": APPLY_RECEIPT_SCHEMA,
            "state": "already_terminal",
            "operation_id": operation,
            "pull_request": args.pr,
        }
        _write_receipt(args.output, receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 0

    predecessor_evidence = (
        _collect_wbc0027_predecessor_evidence(client, goal)
        if goal["profile"] == WBC0027_GOAL_PROFILE
        else None
    )

    subprocess.run(
        ["git", "fetch", "--no-tags", "origin", merge_sha], cwd=ROOT, check=True
    )
    subprocess.run(["git", "checkout", "--detach", merge_sha], cwd=ROOT, check=True)
    target = _canonical_target()
    approval_body = str(authorization.get("body") or "").strip()
    approval_reference = (
        f"github:{args.repository}:pr:{args.pr}:comment:{args.authorization_comment_id}:"
        f"sha256:{digest(approval_body.encode('utf-8'))}"
    )
    with tempfile.TemporaryDirectory(prefix="wb-core-production-goal-") as directory:
        configure_deploy_environment(Path(directory))
        result = (
            run_wbc0027_goal(
                target=target,
                merge_sha=merge_sha,
                goal=goal,
                operation=operation,
                approval_reference=approval_reference,
            )
            if goal["profile"] == WBC0027_GOAL_PROFILE
            else run_wbc0013_goal(
                target=target,
                merge_sha=merge_sha,
                goal=goal,
                operation=operation,
                approval_reference=approval_reference,
            )
            if goal["profile"] == WBC0013_GOAL_PROFILE
            else run_historical_cost_goal(
                target=target,
                merge_sha=merge_sha,
                goal=goal,
                operation=operation,
                approval_reference=approval_reference,
            )
            if goal["profile"] == HISTORICAL_COST_GOAL_PROFILE
            else run_historical_missing_goal(
                target=target,
                merge_sha=merge_sha,
                goal=goal,
                operation=operation,
                approval_reference=approval_reference,
            )
            if goal["profile"] == HISTORICAL_MISSING_REPAIR_GOAL_PROFILE
            else run_dynamic_goal(
                target=target,
                merge_sha=merge_sha,
                goal=goal,
                operation=operation,
                approval_reference=approval_reference,
                warm_readiness=warm_readiness,
            )
        )
    receipt = {
        "schema": APPLY_RECEIPT_SCHEMA,
        "state": result["state"],
        "operation_id": operation,
        "repository": args.repository,
        "pull_request": args.pr,
        "release_operation_id": release_receipt["operation_id"],
        "merge_sha": merge_sha,
        "deployed_sha": merge_sha,
        "authorization_comment_id": args.authorization_comment_id,
        "authorization_body_sha256": digest(approval_body.encode("utf-8")),
        "goal": goal,
        "warm_archive_readiness": warm_readiness,
        **(
            {"superseded_predecessor_evidence": predecessor_evidence}
            if predecessor_evidence is not None
            else {}
        ),
        "apply_count": result["apply_count"],
        "evidence": result,
    }
    _write_receipt(args.output, receipt)
    run_id = int(os.environ.get("GITHUB_RUN_ID") or 0)
    if run_id <= 0:
        raise ApplyError("production apply publication lacks GitHub run identity")
    _publish_compact_apply_receipt(
        client,
        pr=args.pr,
        receipt=receipt,
        receipt_path=args.output,
        artifact_name=_receipt_artifact_name(args.pr, run_id),
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
