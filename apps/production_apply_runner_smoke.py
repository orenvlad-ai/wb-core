#!/usr/bin/env python3
"""Deterministic smoke coverage for default-off one-shot production apply."""

from __future__ import annotations

import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import production_apply_runner as apply
from apps.release_protocol import validate_production_manifest


def manifest() -> dict:
    return {
        "schema": "wb-core.production-apply-manifest/v2",
        "operation_id": "op-1",
        "target_id": "wb_core_eu_hosted_runtime_active",
        "deployed_sha_contract": "exact-merge-sha",
        "dry_run_default": True,
        "explicit_apply": True,
        "bounded_scope": True,
        "pre_change_digest": True,
        "backup_evidence": True,
        "expected_affected_records": True,
        "non_target_invariants": True,
        "idempotency_or_recovery": True,
        "post_apply_readback": True,
        "reconciliation": True,
        "pre_change_digest_value": "sha256:" + "a" * 64,
        "backup_evidence_value": "backup://evidence/immutable-id",
        "expected_affected_record_count": 1,
        "non_target_invariant_ids": ["unrelated-rows-byte-stable"],
        "recovery_contract": {"mode": "idempotent", "id": "op-1-repeat-readback"},
        "query_only_manifest_readback": True,
        "commands": {
            "dry_run": [sys.executable, "-c", "print('dry')"],
            "apply": [sys.executable, "-c", "print('apply')"],
            "readback": [sys.executable, "-c", "print('readback')"],
            "reconcile": [sys.executable, "-c", "print('reconcile')"],
        },
    }


def main() -> None:
    value = manifest()
    assert validate_production_manifest(value)["valid"] is True
    result = apply.run_commands(value)
    assert result["state"] == "done"
    assert result["apply_count"] == 1
    blocked = manifest()
    blocked["commands"]["dry_run"] = [sys.executable, "-c", "raise SystemExit(2)"]
    result = apply.run_commands(blocked)
    assert result["state"] == "blocked"
    assert result["apply_count"] == 0

    comment = {
        "author_association": "OWNER",
        "body": "/wb-core apply-v2 pr 1041 merge " + "a" * 40 + " deployed " + "a" * 40 + " manifest sha256:" + "b" * 64 + " operation op-1",
    }
    apply.validate_authorization(
        comment,
        pr=1041,
        merge_sha="a" * 40,
        deployed_sha="a" * 40,
        manifest_sha="b" * 64,
        operation="op-1",
    )
    try:
        apply.validate_authorization(
            {**comment, "author_association": "CONTRIBUTOR"},
            pr=1041,
            merge_sha="a" * 40,
            deployed_sha="a" * 40,
            manifest_sha="b" * 64,
            operation="op-1",
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("unauthorized apply comment must fail closed")

    release_payload = {
        "state": "awaiting_apply",
        "operation_id": "op-1",
        "merge_sha": "a" * 40,
        "manifest": {"sha256": "b" * 64, "operation_id": "op-1"},
    }
    release_comment = {
        "user": {"login": "github-actions[bot]"},
        "body": "<!-- wb-core-release-receipt operation=op-1 -->\n```json\n"
        + json.dumps(release_payload)
        + "\n```",
    }
    assert apply.parse_release_receipt(
        [release_comment], merge_sha="a" * 40, manifest_sha="b" * 64, operation="op-1"
    ) == release_payload
    try:
        apply.parse_release_receipt(
            [{**release_comment, "user": {"login": "contributor"}}],
            merge_sha="a" * 40,
            manifest_sha="b" * 64,
            operation="op-1",
        )
    except apply.ApplyError:
        pass
    else:
        raise AssertionError("untrusted awaiting-apply receipt must fail closed")
    print("production_apply_runner_smoke: ok")


if __name__ == "__main__":
    main()
