#!/usr/bin/env python3
"""Deterministic smoke coverage for task-scoped one-submit production apply."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import production_apply_runner as apply


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
        payload: dict[str, object] = {
            "status": "ready",
            "phase": phase,
            "deployed_sha": MERGE_SHA,
            "query_only": True,
            "database_written": False,
            "manifest_path": f"{base}/wbc0013-{phase}-plan-20260828T120000Z.json",
            "manifest_sha256": "sha256:" + phase * 64,
            "material_qualification_digest": "sha256:"
            + ("1" if phase == "a" else "2") * 64,
            "file_mode": "0600",
            "barrier_inactive": True,
            "target_generation_bound": True,
            "timer_change_count": 0,
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
            "code": "dense_qualification_blocked",
            "message": "fixture qualification blocked",
            "predicate": "dense_a.apply_allowed_after_exact_qualification",
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
        "code": "dense_qualification_blocked",
        "message": "fixture qualification blocked",
        "predicate": "dense_a.apply_allowed_after_exact_qualification",
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


def main() -> None:
    _exercise_compact_oversized_blocked_receipt()
    _exercise_worker_mount_probe()
    _exercise_wbc0013_two_phase_runner()
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
    legacy_result = apply._run_legacy_commands(
        {
            "commands": {
                "dry_run": [sys.executable, "-c", "print('{}')"],
                "apply": [sys.executable, "-c", "print('{}')"],
                "readback": [sys.executable, "-c", "print('{}')"],
                "reconcile": [sys.executable, "-c", "print('{}')"],
            }
        }
    )
    assert legacy_result["state"] == "done"
    assert legacy_result["apply_count"] == 1

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
    assert "pull-requests: read" not in workflow
    apply_job, recovery_and_reconciliation = workflow.split("\n  recover_receipt:\n", 1)
    recovery_job, reconciliation_job = recovery_and_reconciliation.split(
        "\n  warm_archive_receipt_reconciliation:\n", 1
    )
    assert "pull-requests: write" in apply_job
    assert "--authorization-mode warm-archive-readiness" in apply_job
    assert "--authorization-mode warm-archive-mount-probe" in apply_job
    assert "--authorization-comment-id" in apply_job
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
        "--prior-reconciliation-run-id",
        "--prior-reconciliation-artifact-id",
        "--prior-reconciliation-artifact-name",
        "--prior-reconciliation-receipt-sha256",
        "--prior-reconciliation-comment-id",
    ):
        assert reconciliation_job.count(exact_a02_input) == 3
    assert reconciliation_job.index(
        "Upload full immutable reconciliation evidence first"
    ) < (reconciliation_job.index("publish one compact supersession marker"))
    assert reconciliation_job.count("Execute one bounded query-only SSH probe") == 1
    assert "pip install" not in reconciliation_job
    print("production_apply_runner_smoke: ok")


if __name__ == "__main__":
    main()
