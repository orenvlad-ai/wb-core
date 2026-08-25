#!/usr/bin/env python3
"""Deterministic smoke tests for one-shot Release Runner admission and action guards."""

from __future__ import annotations

import copy
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import github_release_runner as runner
from ci.test_planner import canonical_json_bytes


BASE = "a" * 40
HEAD = "b" * 40
MERGE = "c" * 40


def plan() -> dict:
    value = {
        "schema": "wb-core.test-plan/v2",
        "protocol_version": 2,
        "cutover_epoch": runner.PROTOCOL_V2_CUTOVER_EPOCH,
        "pull_request": 1041,
        "base_sha": BASE,
        "head_sha": HEAD,
        "registry": {},
        "changed_paths": [],
        "changed_paths_digest": "d" * 64,
        "unknown_paths": [],
        "selected_suites": ["release_safety"],
        "groups": ["release"],
        "execution": {},
        "release_plan": {"kind": "repo_only", "valid": True, "deploy_required": False, "production_apply_required": False, "manifest": None},
        "reason_codes": [],
    }
    value["plan_hash"] = runner.sha256(canonical_json_bytes(value))
    return value


def workflow() -> dict:
    return {
        "name": "PR Gate",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "head_sha": HEAD,
        "pull_requests": [{"number": 1041}],
    }


def pull_request() -> dict:
    return {
        "number": 1041,
        "state": "open",
        "draft": False,
        "mergeable": True,
        "merged": False,
        "merge_commit_sha": None,
        "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "orenvlad-ai/wb-core"}},
        "head": {"ref": "codex/change", "sha": HEAD, "repo": {"full_name": "orenvlad-ai/wb-core"}},
    }


class FakeClient:
    def __init__(self) -> None:
        self.repository = "orenvlad-ai/wb-core"
        self.pr = pull_request()
        self.merge_body = None

    def put(self, path: str, body: dict) -> dict:
        assert path == "/pulls/1041/merge"
        self.merge_body = body
        self.pr["merged"] = True
        self.pr["state"] = "closed"
        self.pr["merge_commit_sha"] = MERGE
        return {"merged": True, "sha": MERGE}

    def get(self, path: str) -> dict:
        assert path == "/pulls/1041"
        return copy.deepcopy(self.pr)


class ArtifactClient:
    def __init__(self, artifact_plan: dict) -> None:
        self.artifact_plan = artifact_plan
        self.download_request: dict[str, object] = {}

    def get(self, path: str) -> dict:
        if path == "/actions/runs/99":
            return workflow()
        assert path == "/actions/runs/99/artifacts?per_page=100"
        return {
            "artifacts": [
                {
                    "id": 7,
                    "name": "test-plan-1041-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "expired": False,
                }
            ]
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        accept: str,
        raw: bool,
    ) -> bytes:
        self.download_request = {
            "method": method,
            "path": path,
            "accept": accept,
            "raw": raw,
        }
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("test-plan.json", canonical_json_bytes(self.artifact_plan))
        return payload.getvalue()


def main() -> None:
    golden = plan()
    artifact_client = ArtifactClient(golden)
    collected_run, collected_artifact, collected_plan = runner.collect_workflow_plan(
        artifact_client, 99
    )
    assert collected_run == workflow()
    assert collected_artifact["id"] == 7
    assert collected_plan == golden
    assert artifact_client.download_request == {
        "method": "GET",
        "path": "/actions/artifacts/7/zip",
        "accept": "application/vnd.github+json",
        "raw": True,
    }

    reasons = runner.admission_reasons(
        repository="orenvlad-ai/wb-core",
        run=workflow(),
        pr=pull_request(),
        artifact_plan=golden,
        recomputed_plan=copy.deepcopy(golden),
        trusted_main_sha=BASE,
    )
    assert reasons == []

    dispatch = workflow()
    dispatch["event"] = "workflow_dispatch"
    assert "workflow-provenance-not-pull-request" in runner.admission_reasons(
        repository="orenvlad-ai/wb-core",
        run=dispatch,
        pr=pull_request(),
        artifact_plan=golden,
        recomputed_plan=golden,
        trusted_main_sha=BASE,
    )

    drifted = copy.deepcopy(golden)
    drifted["head_sha"] = "e" * 40
    drifted.pop("plan_hash")
    drifted["plan_hash"] = runner.sha256(canonical_json_bytes(drifted))
    drift_reasons = runner.admission_reasons(
        repository="orenvlad-ai/wb-core",
        run=workflow(),
        pr=pull_request(),
        artifact_plan=drifted,
        recomputed_plan=golden,
        trusted_main_sha=BASE,
    )
    assert "plan-head-mismatch" in drift_reasons
    assert "plan-recomputation-mismatch" in drift_reasons
    assert runner.classify_blocked_state(drift_reasons) == "superseded"

    client = FakeClient()
    assert runner.merge_exact(client, 1041, HEAD) == MERGE
    assert client.merge_body == {"sha": HEAD, "merge_method": "squash"}

    operation = runner.operation_id("orenvlad-ai/wb-core", 99, 1041, HEAD, golden["plan_hash"])
    comment = {
        "body": runner.receipt_marker(operation) + "\n{}",
        "user": {"login": "github-actions[bot]"},
    }
    assert runner.matching_receipts([comment], operation) == [comment]
    assert len(runner.matching_receipts([comment, comment], operation)) == 2
    assert runner.matching_receipts(
        [{**comment, "user": {"login": "contributor"}}], operation
    ) == []

    receipt = runner.make_receipt(
        state="done",
        operation=operation,
        repository="orenvlad-ai/wb-core",
        workflow_run_id=99,
        pr=1041,
        base_sha=BASE,
        head_sha=HEAD,
        plan_hash=golden["plan_hash"],
        release_kind="repo_only",
        merge_sha=MERGE,
    )
    assert json.loads(canonical_json_bytes(receipt))["state"] == "done"
    assert runner.route_kind(golden) == ("repo_only", False)

    source = (ROOT / "apps/github_release_runner.py").read_text(encoding="utf-8")
    for forbidden in (
        "dispatch" + "-next",
        "schedule" + ":",
        "release" + ":ready",
        "task" + ":standard",
        "scope" + ":repo-only",
    ):
        assert forbidden not in source
    assert "time.sleep" not in source
    print("github_release_runner_smoke: ok")


if __name__ == "__main__":
    main()
