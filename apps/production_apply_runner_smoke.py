#!/usr/bin/env python3
"""Deterministic smoke coverage for task-scoped one-submit production apply."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import production_apply_runner as apply


MERGE_SHA = "a" * 40
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
    print("production_apply_runner_smoke: ok")


if __name__ == "__main__":
    main()
