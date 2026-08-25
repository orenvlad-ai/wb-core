#!/usr/bin/env python3
"""Deterministic smoke coverage for task-scoped one-submit production apply."""

from __future__ import annotations

import argparse
import io
import json
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
RECOVERY_RELEASE_OPERATION = "release-v2-" + "1" * 32
AUTH_BODY = (
    "/wb-core authorize-goal-v1 task WBC0006 profile inventory-history-backfill "
    "target wb_core_eu_hosted_runtime_active dates 2026-03-01..2026-08-24 "
    "captures 177 components 18054 finalizations 177 full-days 172 partial-days 5"
)


def authorization(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
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
            "\n```json\n"
            + json.dumps(payload)
            + "\n```"
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
            "/opt/wb-core-runtime/state/private-evidence/production-goals/"
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


def _run_dynamic_sequence(sequence: list[dict[str, object]]) -> dict[str, object]:
    original = apply.command_evidence
    original_sleep = apply.time.sleep

    def fake(_command: list[str], *, timeout_seconds: float = 3600.0) -> dict[str, object]:
        del timeout_seconds
        assert sequence
        return sequence.pop(0)

    apply.command_evidence = fake
    apply.time.sleep = lambda _seconds: None
    try:
        goal = apply.validate_authorization(
            authorization(), repository="orenvlad-ai/wb-core", pr=1050
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
        )
    finally:
        apply.command_evidence = original
        apply.time.sleep = original_sleep


def main() -> None:
    goal = apply.validate_authorization(
        authorization(), repository="orenvlad-ai/wb-core", pr=1050
    )
    assert goal["date_count"] == 177
    assert goal["max_mutation_submits"] == 1
    assert goal["max_pre_submit_regenerations"] == 3
    for invalid in (
        authorization(author_association="CONTRIBUTOR"),
        authorization(body=AUTH_BODY.replace("full-days 172", "full-days 171")),
        authorization(body=AUTH_BODY.replace("2026-08-24", "2026-08-23")),
        authorization(issue_url="https://api.github.com/repos/orenvlad-ai/wb-core/issues/1051"),
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
    assert [item["qualification_state"] for item in success["qualification_attempts"]] == [
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
        with tempfile.TemporaryDirectory(prefix="production-receipt-recovery-smoke-") as directory:
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
            assert json.loads(recovery_args.output.read_text(encoding="utf-8")) == recovered_receipt
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
            raise AssertionError("non-done or wrongly bound recovery receipt must fail closed")
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
    with tempfile.TemporaryDirectory(prefix="production-receipt-duplicate-smoke-") as directory:
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
    apply_job, recovery_job = workflow.split("\n  recover_receipt:\n", 1)
    assert "pull-requests: write" in apply_job
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
    print("production_apply_runner_smoke: ok")


if __name__ == "__main__":
    main()
