#!/usr/bin/env python3
"""One-shot protocol-v2 GitHub Release Runner: admit once, act once, receipt once."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.release_protocol import (  # noqa: E402
    CANONICAL_REPOSITORY,
    PROTOCOL_V2_CUTOVER_EPOCH,
    validate_production_manifest,
)
from ci.test_planner import (  # noqa: E402
    GROUP_HARNESS_PATH,
    PLANNER_PATH,
    PLAN_SCHEMA,
    PR_GATE_WORKFLOW_PATH,
    canonical_json_bytes,
    verify_plan,
)


RECEIPT_SCHEMA = "wb-core.release-receipt/v2"
WORKFLOW_NAME = "PR Gate"
ARTIFACT_PREFIX = "test-plan-"
RECEIPT_MARKER = "wb-core-release-receipt"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TERMINAL_STATES = {"done", "awaiting_apply", "blocked", "superseded"}
RECOVERY_SCRATCH_MANIFEST_CONTRACT = (
    "wbc0035_recovery_scratch_bootstrap_passport/v1"
)
RECOVERY_SCRATCH_RELEASE_BRIDGE_CONTRACT = (
    "wbc0035_recovery_scratch_release_bridge/v1"
)
TRANSITION_WORKFLOW_NAME = "WBC Transition Validator"
TRANSITION_WORKFLOW_PATH = ".github/workflows/wbc-transition-validator.yml"
TRANSITION_JOB_NAME = "transition-validator"
TRANSITION_ARTIFACT_PREFIX = "wbc-transition-validator-"
TRANSITION_RECEIPT_SCHEMA = "wb-core.transition-validator/v1"
TRANSITION_PROTECTED_PATHS = {
    ".github/workflows/pr-gate.yml",
    ".github/workflows/release-runner.yml",
    "apps/github_release_runner.py",
}


class RunnerError(RuntimeError):
    def __init__(self, reason: str, *, state: str = "blocked") -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state


def _url_origin(url: str) -> tuple[str, str, int] | None:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not hostname:
        return None
    return (scheme, hostname, parsed.port or (443 if scheme == "https" else 80))


class _AuthSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep GitHub auth on-origin and strip it from signed storage redirects."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        source_origin = _url_origin(req.full_url)
        target_origin = _url_origin(newurl)
        if source_origin is None or target_origin is None or source_origin != target_origin:
            for header in ("Authorization", "Proxy-Authorization", "Cookie"):
                redirected.remove_header(header)
        return redirected


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def exact_sha(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if SHA_RE.fullmatch(normalized) is None:
        raise RunnerError(f"{field}-invalid")
    return normalized


def operation_id(repository: str, workflow_run_id: int, pr: int, head: str, plan_hash: str) -> str:
    material = canonical_json_bytes(
        {
            "repository": repository,
            "workflow_run_id": workflow_run_id,
            "pull_request": pr,
            "head_sha": head,
            "plan_hash": plan_hash,
        }
    )
    return "release-v2-" + sha256(material)[:32]


class GitHubClient:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.token = token
        self.base = f"https://api.github.com/repos/{repository}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
        raw: bool = False,
    ) -> Any:
        url = path if path.startswith("https://") else self.base + path
        data = None if body is None else canonical_json_bytes(body)
        request = urllib.request.Request(
            url,
            method=method,
            data=data,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "wb-core-release-runner-v2",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            opener = urllib.request.build_opener(_AuthSafeRedirectHandler())
            with opener.open(request, timeout=30) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RunnerError(f"github-http-{exc.code}:{detail}") from exc
        except urllib.error.URLError as exc:
            raise RunnerError(f"github-transport:{exc.reason}") from exc
        if raw:
            return payload
        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        return self.request("POST", path, body=body)

    def put(self, path: str, body: Mapping[str, Any]) -> Any:
        return self.request("PUT", path, body=body)


def extract_plan(zip_bytes: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = [name for name in archive.namelist() if name.rstrip("/") == "test-plan.json"]
            if names != ["test-plan.json"]:
                raise RunnerError("test-plan-artifact-shape-invalid")
            raw = archive.read(names[0])
    except zipfile.BadZipFile as exc:
        raise RunnerError("test-plan-artifact-zip-invalid") from exc
    try:
        plan = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("test-plan-json-invalid") from exc
    if not isinstance(plan, dict):
        raise RunnerError("test-plan-shape-invalid")
    try:
        verify_plan(plan)
    except Exception as exc:
        raise RunnerError("test-plan-hash-invalid") from exc
    return plan


def collect_workflow_plan(client: GitHubClient, workflow_run_id: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    run = client.get(f"/actions/runs/{workflow_run_id}")
    artifacts = client.get(f"/actions/runs/{workflow_run_id}/artifacts?per_page=100")
    values = [
        item
        for item in (artifacts.get("artifacts") if isinstance(artifacts, Mapping) else []) or []
        if str(item.get("name") or "").startswith(ARTIFACT_PREFIX)
        and item.get("expired") is not True
    ]
    if len(values) != 1:
        raise RunnerError("test-plan-artifact-count-invalid")
    artifact = values[0]
    raw_zip = client.request(
        "GET",
        f"/actions/artifacts/{int(artifact['id'])}/zip",
        accept="application/vnd.github+json",
        raw=True,
    )
    return run, artifact, extract_plan(raw_zip)


def extract_transition_receipt(zip_bytes: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = [name for name in archive.namelist() if name.rstrip("/") == "wbc-transition-receipt.json"]
            if names != ["wbc-transition-receipt.json"]:
                raise RunnerError("transition-receipt-artifact-shape-invalid")
            raw = archive.read(names[0])
    except zipfile.BadZipFile as exc:
        raise RunnerError("transition-receipt-artifact-zip-invalid") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("transition-receipt-json-invalid") from exc
    if not isinstance(value, dict):
        raise RunnerError("transition-receipt-shape-invalid")
    return value


def pull_request_paths(client: GitHubClient, pr_number: int) -> set[str]:
    paths: set[str] = set()
    for page in range(1, 31):
        payload = client.get(f"/pulls/{pr_number}/files?per_page=100&page={page}")
        if not isinstance(payload, list):
            raise RunnerError("transition-pr-files-shape-invalid")
        for item in payload:
            if not isinstance(item, Mapping) or not isinstance(item.get("filename"), str):
                raise RunnerError("transition-pr-file-invalid")
            paths.add(str(item["filename"]))
        if len(payload) < 100:
            return paths
    raise RunnerError("transition-pr-files-pagination-bound-exceeded")


def transition_receipt_reasons(
    client: GitHubClient, *, pr_number: int, base_sha: str, head_sha: str
) -> list[str]:
    protected = sorted(TRANSITION_PROTECTED_PATHS.intersection(pull_request_paths(client, pr_number)))
    if not protected:
        return []
    payload = client.get(f"/commits/{head_sha}/check-runs?filter=latest&per_page=100")
    checks = payload.get("check_runs") if isinstance(payload, Mapping) else None
    if not isinstance(checks, list):
        return ["transition-checks-shape-invalid"]
    matches = [
        item
        for item in checks
        if isinstance(item, Mapping)
        and item.get("name") == TRANSITION_JOB_NAME
        and item.get("app", {}).get("slug") == "github-actions"
    ]
    if len(matches) != 1:
        return ["transition-check-count-invalid"]
    check = matches[0]
    if check.get("status") != "completed" or check.get("conclusion") != "success":
        return ["transition-check-not-successful"]
    details = str(check.get("details_url") or "")
    match = re.search(r"/actions/runs/(\d+)/job/", details)
    if match is None:
        return ["transition-check-run-id-missing"]
    run_id = int(match.group(1))
    run = client.get(f"/actions/runs/{run_id}")
    run_prs = run.get("pull_requests") if isinstance(run, Mapping) else None
    run_numbers = [item.get("number") for item in run_prs if isinstance(item, Mapping)] if isinstance(run_prs, list) else []
    reasons: list[str] = []
    if run.get("name") != TRANSITION_WORKFLOW_NAME or run.get("path") != TRANSITION_WORKFLOW_PATH:
        reasons.append("transition-workflow-identity-invalid")
    if run.get("event") != "pull_request" or run.get("run_attempt") != 1:
        reasons.append("transition-workflow-provenance-invalid")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        reasons.append("transition-workflow-not-successful")
    if exact_sha(run.get("head_sha"), "transition-workflow-head") != head_sha:
        reasons.append("transition-workflow-head-mismatch")
    if run_numbers != [pr_number]:
        reasons.append("transition-workflow-pr-mismatch")
    artifacts_payload = client.get(f"/actions/runs/{run_id}/artifacts?per_page=100")
    artifacts = artifacts_payload.get("artifacts") if isinstance(artifacts_payload, Mapping) else None
    expected_name = f"{TRANSITION_ARTIFACT_PREFIX}{pr_number}-{head_sha}"
    values = [
        item for item in artifacts or []
        if isinstance(item, Mapping) and item.get("name") == expected_name and item.get("expired") is not True
    ]
    if len(values) != 1:
        return reasons + ["transition-receipt-artifact-count-invalid"]
    raw_zip = client.request(
        "GET",
        f"/actions/artifacts/{int(values[0]['id'])}/zip",
        accept="application/vnd.github+json",
        raw=True,
    )
    receipt = extract_transition_receipt(raw_zip)
    if receipt.get("schema") != TRANSITION_RECEIPT_SCHEMA or receipt.get("result") != "success":
        reasons.append("transition-receipt-result-invalid")
    if receipt.get("pull_request") != pr_number:
        reasons.append("transition-receipt-pr-mismatch")
    if receipt.get("base_sha") != base_sha or receipt.get("head_sha") != head_sha:
        reasons.append("transition-receipt-sha-mismatch")
    if sorted(receipt.get("protected_paths") or []) != protected:
        reasons.append("transition-receipt-paths-mismatch")
    if re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("validator_sha256") or "")) is None:
        reasons.append("transition-receipt-validator-invalid")
    return reasons


def collect_workflow_jobs(
    client: GitHubClient, workflow_run_id: int
) -> list[Mapping[str, Any]]:
    jobs: list[Mapping[str, Any]] = []
    for page in range(1, 101):
        payload = client.get(
            f"/actions/runs/{workflow_run_id}/jobs?filter=latest&per_page=100&page={page}"
        )
        values = payload.get("jobs") if isinstance(payload, Mapping) else None
        if not isinstance(values, list):
            raise RunnerError("workflow-jobs-shape-invalid")
        jobs.extend(item for item in values if isinstance(item, Mapping))
        if len(values) < 100:
            return jobs
    raise RunnerError("workflow-jobs-pagination-bound-exceeded")


def workflow_job_reasons(
    jobs: list[Mapping[str, Any]], artifact_plan: Mapping[str, Any]
) -> list[str]:
    groups = artifact_plan.get("groups")
    if not isinstance(groups, list) or any(
        not isinstance(group, str) or not group for group in groups
    ):
        return ["plan-groups-invalid"]
    expected = {
        "Fast core checks",
        "Deterministic impact plan",
        "pr-gate",
        *(f"Selected group · {group}" for group in groups),
    }
    actual_names = [str(job.get("name") or "") for job in jobs]
    reasons: list[str] = []
    if len(actual_names) != len(set(actual_names)):
        reasons.append("workflow-job-names-duplicate")
    if set(actual_names) != expected:
        reasons.append("workflow-job-set-mismatch")
    if any(
        job.get("status") != "completed" or job.get("conclusion") != "success"
        for job in jobs
    ):
        reasons.append("workflow-job-not-successful")
    return reasons


def workflow_pull_request(run: Mapping[str, Any]) -> int:
    prs = run.get("pull_requests")
    if not isinstance(prs, list) or len(prs) != 1:
        raise RunnerError("workflow-pr-binding-ambiguous")
    number = prs[0].get("number")
    if not isinstance(number, int) or number <= 0:
        raise RunnerError("workflow-pr-number-invalid")
    return number


def admission_reasons(
    *,
    repository: str,
    run: Mapping[str, Any],
    pr: Mapping[str, Any],
    artifact_plan: Mapping[str, Any],
    recomputed_plan: Mapping[str, Any] | None,
    trusted_main_sha: str,
) -> list[str]:
    reasons: list[str] = []
    head = exact_sha(pr.get("head", {}).get("sha"), "pr-head")
    base = exact_sha(pr.get("base", {}).get("sha"), "pr-base")
    if repository != CANONICAL_REPOSITORY:
        reasons.append("repository-not-canonical")
    if run.get("name") != WORKFLOW_NAME:
        reasons.append("workflow-name-mismatch")
    if run.get("path") != PR_GATE_WORKFLOW_PATH:
        reasons.append("workflow-path-mismatch")
    if run.get("event") != "pull_request":
        reasons.append("workflow-provenance-not-pull-request")
    if run.get("run_attempt") != 1:
        reasons.append("workflow-run-attempt-not-one")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        reasons.append("workflow-not-successful")
    if exact_sha(run.get("head_sha"), "workflow-head") != head:
        reasons.append("workflow-head-mismatch")
    if pr.get("state") != "open":
        reasons.append("pr-not-open")
    if pr.get("draft") is not False:
        reasons.append("pr-draft")
    if pr.get("base", {}).get("ref") != "main":
        reasons.append("base-not-main")
    if pr.get("base", {}).get("repo", {}).get("full_name") != repository:
        reasons.append("base-repository-mismatch")
    if pr.get("head", {}).get("repo", {}).get("full_name") != repository:
        reasons.append("head-repository-mismatch")
    if base != trusted_main_sha:
        reasons.append("base-main-drift")
    if pr.get("mergeable") is not True:
        reasons.append("mergeability-not-true")
    if artifact_plan.get("schema") != PLAN_SCHEMA or artifact_plan.get("protocol_version") != 2:
        reasons.append("plan-protocol-unsupported")
    if artifact_plan.get("cutover_epoch") != PROTOCOL_V2_CUTOVER_EPOCH:
        reasons.append("plan-cutover-epoch-mismatch")
    if artifact_plan.get("pull_request") != pr.get("number"):
        reasons.append("plan-pr-mismatch")
    if artifact_plan.get("base_sha") != base:
        reasons.append("plan-base-mismatch")
    if artifact_plan.get("head_sha") != head:
        reasons.append("plan-head-mismatch")
    planner = artifact_plan.get("planner")
    if (
        not isinstance(planner, Mapping)
        or planner.get("path") != PLANNER_PATH
        or planner.get("execution_sha") != base
        or re.fullmatch(r"[0-9a-f]{64}", str(planner.get("blob_sha256") or "")) is None
    ):
        reasons.append("plan-planner-provenance-invalid")
    group_harness = artifact_plan.get("group_harness")
    if (
        not isinstance(group_harness, Mapping)
        or group_harness.get("path") != GROUP_HARNESS_PATH
        or group_harness.get("execution_sha") != base
        or group_harness.get("candidate_worktree_sha") != head
        or re.fullmatch(
            r"[0-9a-f]{64}", str(group_harness.get("blob_sha256") or "")
        )
        is None
    ):
        reasons.append("plan-group-harness-provenance-invalid")
    if recomputed_plan is not None and canonical_json_bytes(artifact_plan) != canonical_json_bytes(recomputed_plan):
        reasons.append("plan-recomputation-mismatch")
    release_plan = artifact_plan.get("release_plan")
    if not isinstance(release_plan, Mapping) or release_plan.get("valid") is not True:
        reasons.append("release-plan-invalid")
    elif release_plan.get("kind") not in {"repo_only", "live_runtime", "production_mutation"}:
        reasons.append("release-kind-invalid")
    return reasons


def classify_blocked_state(reasons: list[str]) -> str:
    superseded = {
        "pr-not-open",
        "base-main-drift",
        "workflow-head-mismatch",
        "plan-base-mismatch",
        "plan-head-mismatch",
        "plan-recomputation-mismatch",
        "plan-planner-provenance-invalid",
        "plan-group-harness-provenance-invalid",
        "planner-base-checkout-mismatch",
        "pull-ref-head-mismatch",
    }
    return "superseded" if set(reasons) & superseded else "blocked"


def make_receipt(
    *,
    state: str,
    operation: str,
    repository: str,
    workflow_run_id: int,
    pr: int,
    base_sha: str,
    head_sha: str,
    plan_hash: str,
    release_kind: str,
    reason_codes: list[str] | None = None,
    merge_sha: str | None = None,
    deployed_sha: str | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if state not in TERMINAL_STATES | {"already_terminal"}:
        raise ValueError(f"unsupported receipt state: {state}")
    return {
        "schema": RECEIPT_SCHEMA,
        "state": state,
        "operation_id": operation,
        "repository": repository,
        "workflow_run_id": workflow_run_id,
        "pull_request": pr,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "plan_hash": plan_hash,
        "release_kind": release_kind,
        "merge_sha": merge_sha,
        "deployed_sha": deployed_sha,
        "manifest": dict(manifest) if manifest else None,
        "reason_codes": sorted(set(reason_codes or [])),
    }


def receipt_marker(operation: str) -> str:
    return f"<!-- {RECEIPT_MARKER} operation={operation} -->"


def is_actions_bot_comment(comment: Mapping[str, Any]) -> bool:
    user = comment.get("user")
    return isinstance(user, Mapping) and user.get("login") == "github-actions[bot]"


def matching_receipts(comments: list[Mapping[str, Any]], operation: str) -> list[Mapping[str, Any]]:
    marker = receipt_marker(operation)
    return [
        comment
        for comment in comments
        if marker in str(comment.get("body") or "")
        and is_actions_bot_comment(comment)
    ]


def list_comments(client: GitHubClient, pr: int) -> list[Mapping[str, Any]]:
    comments: list[Mapping[str, Any]] = []
    for page in range(1, 101):
        values = client.get(f"/issues/{pr}/comments?per_page=100&page={page}")
        if not isinstance(values, list):
            raise RunnerError("issue-comments-shape-invalid")
        comments.extend(item for item in values if isinstance(item, Mapping))
        if len(values) < 100:
            return comments
    raise RunnerError("issue-comments-pagination-bound-exceeded")


def post_receipt(client: GitHubClient, pr: int, receipt: Mapping[str, Any]) -> None:
    operation = str(receipt["operation_id"])
    comments = list_comments(client, pr)
    existing = matching_receipts(comments, operation)
    if existing:
        raise RunnerError("duplicate-or-ambiguous-receipt")
    body = (
        receipt_marker(operation)
        + "\nProtocol-v2 one-shot release receipt:\n```json\n"
        + json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n```"
    )
    client.post(f"/issues/{pr}/comments", {"body": body})


def write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(receipt) + b"\n")


def trusted_main_sha() -> str:
    return exact_sha(
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        ).stdout,
        "trusted-main",
    )


def ensure_epoch_ancestry(base_sha: str) -> None:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", PROTOCOL_V2_CUTOVER_EPOCH, base_sha],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise RunnerError("base-before-protocol-v2-epoch", state="superseded")


def recompute_plan(pr: int, base_sha: str, head_sha: str) -> dict[str, Any]:
    if trusted_main_sha() != base_sha:
        raise RunnerError("planner-base-checkout-mismatch", state="superseded")
    fetch = subprocess.run(
        [
            "git",
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "origin",
            f"+refs/pull/{pr}/head:refs/remotes/origin/release-plan-head",
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if fetch.returncode != 0:
        raise RunnerError("pull-ref-head-fetch-failed")
    resolved_head = exact_sha(
        subprocess.run(
            ["git", "rev-parse", "refs/remotes/origin/release-plan-head^{commit}"],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        ).stdout,
        "pull-ref-head",
    )
    if resolved_head != head_sha:
        raise RunnerError("pull-ref-head-mismatch", state="superseded")
    # Changes to the PR/release boundary are admitted only by the separate
    # base-owned transition receipt checked in run_once.
    with tempfile.TemporaryDirectory(prefix="wb-core-release-plan-") as directory:
        output = Path(directory) / "test-plan.json"
        env = os.environ.copy()
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "ci/test_planner.py",
                "--pr",
                str(pr),
                "--base",
                base_sha,
                "--head",
                head_sha,
                "--output",
                str(output),
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
        plan = json.loads(output.read_text(encoding="utf-8"))
    verify_plan(plan)
    return plan


def require_unchanged_pr_gate_workflow(
    base_sha: str, head_sha: str, *, root: Path = ROOT
) -> None:
    """Require the trusted PR workflow blob to be identical in base and head."""

    base_workflow = subprocess.run(
        ["git", "show", f"{base_sha}:{PR_GATE_WORKFLOW_PATH}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    head_workflow = subprocess.run(
        ["git", "show", f"{head_sha}:{PR_GATE_WORKFLOW_PATH}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if base_workflow.returncode != 0 or head_workflow.returncode != 0:
        raise RunnerError("pr-gate-workflow-blob-unavailable")
    if base_workflow.stdout != head_workflow.stdout:
        raise RunnerError("pr-gate-workflow-change-requires-staged-bootstrap")


def merge_exact(client: GitHubClient, pr: int, head_sha: str) -> str:
    try:
        result = client.put(
            f"/pulls/{pr}/merge",
            {"sha": head_sha, "merge_method": "squash"},
        )
    except RunnerError:
        readback = client.get(f"/pulls/{pr}")
        if readback.get("merged") is True and readback.get("head", {}).get("sha") == head_sha:
            return exact_sha(readback.get("merge_commit_sha"), "merge-readback")
        raise
    if not isinstance(result, Mapping) or result.get("merged") is not True:
        raise RunnerError("expected-head-merge-rejected")
    merge_sha = exact_sha(result.get("sha"), "merge-result")
    readback = client.get(f"/pulls/{pr}")
    if (
        readback.get("merged") is not True
        or exact_sha(readback.get("merge_commit_sha"), "merge-readback") != merge_sha
        or exact_sha(readback.get("head", {}).get("sha"), "merged-head-readback") != head_sha
    ):
        raise RunnerError("exact-merge-readback-mismatch")
    return merge_sha


def checkout_merge(merge_sha: str) -> None:
    subprocess.run(["git", "fetch", "--no-tags", "origin", merge_sha], cwd=ROOT, check=True)
    subprocess.run(["git", "checkout", "--detach", merge_sha], cwd=ROOT, check=True)
    if trusted_main_sha() != merge_sha:
        raise RunnerError("merge-checkout-mismatch")


def configure_deploy_environment(temp_dir: Path) -> None:
    key = os.environ.get("WB_CORE_DEPLOY_SSH_KEY", "")
    known_hosts = os.environ.get("WB_CORE_DEPLOY_KNOWN_HOSTS", "")
    if not key.strip() or not known_hosts.strip():
        raise RunnerError("deploy-credentials-missing")
    target = json.loads(
        (ROOT / "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json").read_text(encoding="utf-8")
    )
    host = str(target.get("host_ip") or "").strip()
    if not host:
        raise RunnerError("deploy-target-host-missing")
    key_path = temp_dir / "deploy-key"
    known_hosts_path = temp_dir / "known-hosts"
    key_path.write_text(key, encoding="utf-8")
    known_hosts_path.write_text(known_hosts, encoding="utf-8")
    key_path.chmod(0o600)
    known_hosts_path.chmod(0o600)
    os.environ["WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE"] = str(key_path)
    os.environ["WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS"] = (
        f"-o HostName={host} -o User=root -o IdentitiesOnly=yes "
        f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_hosts_path}"
    )


def deploy_exact(
    pr: int,
    head_sha: str,
    merge_sha: str,
    temp_dir: Path,
    *,
    recovery_scratch_release_bridge: Mapping[str, Any] | None = None,
) -> str:
    configure_deploy_environment(temp_dir)
    evidence = temp_dir / "deploy-evidence.json"
    env = os.environ.copy()
    env["WB_CORE_RELEASE_PR"] = str(pr)
    env["WB_CORE_RELEASE_HEAD"] = head_sha
    if recovery_scratch_release_bridge is not None:
        env["WB_CORE_RECOVERY_SCRATCH_RELEASE_BRIDGE"] = base64.b64encode(
            canonical_json_bytes(recovery_scratch_release_bridge)
        ).decode("ascii")
    subprocess.run(
        [
            sys.executable,
            "apps/registry_upload_http_entrypoint_hosted_runtime.py",
            "deploy-and-verify",
            "--output",
            str(evidence),
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    if payload.get("ok") is not True or trusted_main_sha() != merge_sha:
        raise RunnerError("exact-sha-deploy-verify-failed")
    return merge_sha


def read_manifest_payload(
    binding: Mapping[str, Any], merge_sha: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = str(binding.get("path") or "")
    expected = str(binding.get("sha256") or "")
    manifest_path = (ROOT / path).resolve()
    if ROOT not in manifest_path.parents or not manifest_path.is_file():
        raise RunnerError("production-manifest-path-invalid")
    raw = manifest_path.read_bytes()
    if sha256(raw) != expected:
        raise RunnerError("production-manifest-digest-mismatch")
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise RunnerError("production-manifest-shape-invalid")
    validation = validate_production_manifest(manifest)
    if validation["valid"] is not True:
        raise RunnerError("production-manifest-contract-invalid")
    if str(manifest.get("merge_sha") or "").lower() not in {"", merge_sha}:
        raise RunnerError("production-manifest-merge-binding-mismatch")
    return (
        {"path": path, "sha256": expected, "operation_id": manifest["operation_id"]},
        manifest,
    )


def read_manifest(binding: Mapping[str, Any], merge_sha: str) -> dict[str, Any]:
    compact, _manifest = read_manifest_payload(binding, merge_sha)
    return compact


def build_recovery_scratch_release_bridge(
    binding: Mapping[str, Any],
    manifest: Mapping[str, Any],
    merge_sha: str,
) -> dict[str, Any] | None:
    if manifest.get("contract") != RECOVERY_SCRATCH_MANIFEST_CONTRACT:
        return None
    target_file = ROOT / (
        "artifacts/registry_upload_http_entrypoint/input/"
        "hosted_runtime_target__europe_api.json"
    )
    target_contract = json.loads(target_file.read_text(encoding="utf-8"))
    scratch = dict(target_contract.get("recovery_scratch_filesystem") or {})
    if not scratch:
        return None
    expected_target = {
        "parent_device": "/dev/sdd",
        "parent_device_by_id": scratch.get("parent_device_by_id"),
        "partition_device_by_id": scratch.get("partition_device_by_id"),
        "parent_serial": scratch.get("parent_serial"),
        "parent_model": scratch.get("parent_model"),
        "parent_size_bytes": scratch.get("parent_size_bytes"),
        "parent_major_minor": scratch.get("parent_major_minor"),
        "parent_hctl": scratch.get("parent_hctl"),
        "blank": True,
        "layout": "single-gpt-partition-ext4",
        "filesystem_uuid": scratch.get("filesystem_uuid"),
        "mountpoint": scratch.get("path"),
        "mount_options": scratch.get("required_mount_options"),
        "directory_mode": 0o700,
        "minimum_available_bytes": 35_157_336_064,
    }
    expected_preconditions = {
        "contract": RECOVERY_SCRATCH_RELEASE_BRIDGE_CONTRACT,
        "barrier": {
            "active": True,
            "phase": "acquiring",
            "hold_confirmed": False,
            "window_id": "wbc0027-s047-live-last-good-freeze-v2-896b02c0",
            "plan_fingerprint": "sha256:0d680ca758c1699fe2a9025b01d71f0fa4f8c5bcf7555a7945b5b930cdc5285f",
            "maintenance_phase": "abort_quiescing",
            "owner_policy_revision": 59,
            "all_business_timers_paused": True,
        },
        "finance": {
            "only_allowed_blocker": "retained backup exceeded RPO age",
            "retained_backup_id": "finance-backup-459a091d48326c9be224",
            "canonical_source_bytes": 26_567_401_472,
            "next_replacement_required_bytes": 35_224_444_928,
            "capacity_basis": "canonical_current_split_source_size_plus_copy_overhead_plus_hard_reserve",
            "next_replacement_capacity": True,
        },
        "non_target_filesystem_uuids": {
            "root": "d77f6a25-e90f-4292-a85d-9bcc1cecf9e2",
            "backup": "bd3d563f-e5ea-4e4a-a76a-be45e7f94ec0",
            "generation": "284b3362-b890-431d-a7da-7f0fcd2ee0a6",
        },
    }
    if (
        manifest.get("operation_id") != "wbc0035-026-recovery-scratch-a01"
        or manifest.get("target_id") != "wb_core_eu_hosted_runtime_active"
        or manifest.get("deployed_sha_contract") != "exact-merge-sha"
        or manifest.get("target") != expected_target
        or manifest.get("release_bridge") != expected_preconditions
        or str(binding.get("path") or "")
        != "release/production-mutations/wbc0035_recovery_scratch_bootstrap.json"
        or re.fullmatch(r"[0-9a-f]{64}", str(binding.get("sha256") or ""))
        is None
    ):
        raise RunnerError("recovery-scratch-release-bridge-invalid")
    return {
        "contract": RECOVERY_SCRATCH_RELEASE_BRIDGE_CONTRACT,
        "manifest_path": str(binding["path"]),
        "manifest_sha256": str(binding["sha256"]),
        "target_id": str(manifest["target_id"]),
        "operation_id": str(manifest["operation_id"]),
        "release_sha": exact_sha(merge_sha, "recovery-scratch-release"),
        "target": expected_target,
        "preconditions": expected_preconditions,
    }


def route_kind(plan: Mapping[str, Any]) -> tuple[str, bool]:
    verify_plan(plan)
    release = plan.get("release_plan")
    if not isinstance(release, Mapping) or release.get("valid") is not True:
        return "production_mutation", True
    kind = str(release.get("kind") or "")
    if kind not in {"repo_only", "live_runtime", "production_mutation"}:
        return "production_mutation", True
    return kind, kind != "repo_only"


def run_once(client: GitHubClient, workflow_run_id: int, output: Path) -> dict[str, Any]:
    run, _artifact, artifact_plan = collect_workflow_plan(client, workflow_run_id)
    jobs = collect_workflow_jobs(client, workflow_run_id)
    pr_number = workflow_pull_request(run)
    pr = client.get(f"/pulls/{pr_number}")
    head_sha = exact_sha(pr.get("head", {}).get("sha"), "pr-head")
    base_sha = exact_sha(pr.get("base", {}).get("sha"), "pr-base")
    plan_hash = str(artifact_plan.get("plan_hash") or "")
    kind, _deploy_required = route_kind(artifact_plan)
    operation = operation_id(client.repository, workflow_run_id, pr_number, head_sha, plan_hash)
    existing = matching_receipts(list_comments(client, pr_number), operation)
    if existing:
        if len(existing) != 1:
            raise RunnerError("duplicate-or-ambiguous-action")
        receipt = make_receipt(
            state="already_terminal",
            operation=operation,
            repository=client.repository,
            workflow_run_id=workflow_run_id,
            pr=pr_number,
            base_sha=base_sha,
            head_sha=head_sha,
            plan_hash=plan_hash,
            release_kind=kind,
            reason_codes=["exact-operation-receipt-already-exists"],
        )
        write_receipt(output, receipt)
        return receipt

    trusted = trusted_main_sha()
    reasons = admission_reasons(
        repository=client.repository,
        run=run,
        pr=pr,
        artifact_plan=artifact_plan,
        recomputed_plan=None,
        trusted_main_sha=trusted,
    )
    reasons.extend(workflow_job_reasons(jobs, artifact_plan))
    try:
        reasons.extend(
            transition_receipt_reasons(
                client,
                pr_number=pr_number,
                base_sha=base_sha,
                head_sha=head_sha,
            )
        )
    except RunnerError as exc:
        reasons.append(exc.reason)
    if not reasons:
        try:
            recomputed = recompute_plan(pr_number, base_sha, head_sha)
        except RunnerError as exc:
            reasons.append(exc.reason)
        else:
            if canonical_json_bytes(artifact_plan) != canonical_json_bytes(recomputed):
                reasons.append("plan-recomputation-mismatch")
    try:
        ensure_epoch_ancestry(base_sha)
    except RunnerError as exc:
        reasons.append(exc.reason)
    if reasons:
        receipt = make_receipt(
            state=classify_blocked_state(reasons),
            operation=operation,
            repository=client.repository,
            workflow_run_id=workflow_run_id,
            pr=pr_number,
            base_sha=base_sha,
            head_sha=head_sha,
            plan_hash=plan_hash,
            release_kind=kind,
            reason_codes=reasons,
        )
        write_receipt(output, receipt)
        post_receipt(client, pr_number, receipt)
        return receipt

    merge_sha: str | None = None
    deployed_sha: str | None = None
    manifest: Mapping[str, Any] | None = None
    state = "blocked"
    action_reasons: list[str] = []
    try:
        merge_sha = merge_exact(client, pr_number, head_sha)
        checkout_merge(merge_sha)
        manifest_payload: Mapping[str, Any] | None = None
        release_bridge: Mapping[str, Any] | None = None
        if kind == "production_mutation":
            binding = artifact_plan["release_plan"].get("manifest")
            if not isinstance(binding, Mapping):
                raise RunnerError("production-manifest-binding-missing")
            manifest, manifest_payload = read_manifest_payload(binding, merge_sha)
            release_bridge = build_recovery_scratch_release_bridge(
                binding, manifest_payload, merge_sha
            )
        if kind in {"live_runtime", "production_mutation"}:
            with tempfile.TemporaryDirectory(prefix="wb-core-deploy-") as directory:
                deployed_sha = deploy_exact(
                    pr_number,
                    head_sha,
                    merge_sha,
                    Path(directory),
                    recovery_scratch_release_bridge=release_bridge,
                )
        if kind == "production_mutation":
            state = "awaiting_apply"
        else:
            state = "done"
    except Exception as exc:
        action_reasons.append(exc.reason if isinstance(exc, RunnerError) else f"action-failed:{type(exc).__name__}")
        state = "blocked"

    receipt = make_receipt(
        state=state,
        operation=operation,
        repository=client.repository,
        workflow_run_id=workflow_run_id,
        pr=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        plan_hash=plan_hash,
        release_kind=kind,
        reason_codes=action_reasons,
        merge_sha=merge_sha,
        deployed_sha=deployed_sha,
        manifest=manifest,
    )
    write_receipt(output, receipt)
    post_receipt(client, pr_number, receipt)
    return receipt


def _write_output(path: Path | None, values: Mapping[str, str]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("route", "run"))
    parser.add_argument("--repository", default=CANONICAL_REPOSITORY)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    client = GitHubClient(args.repository, token)
    if args.command == "route":
        try:
            _run, _artifact, plan = collect_workflow_plan(client, args.workflow_run_id)
            kind, deploy_required = route_kind(plan)
            route_error = ""
        except Exception as exc:
            kind, deploy_required = "production_mutation", True
            route_error = type(exc).__name__
        _write_output(
            args.github_output,
            {
                "release_kind": kind,
                "deploy_required": str(deploy_required).lower(),
                "route_error": route_error,
            },
        )
        print(json.dumps({"release_kind": kind, "deploy_required": deploy_required, "route_error": route_error}, sort_keys=True))
        return 0
    if args.output is None:
        raise SystemExit("--output is required for run")
    receipt = run_once(client, args.workflow_run_id, args.output)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["state"] in {"done", "awaiting_apply", "already_terminal", "blocked", "superseded"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
