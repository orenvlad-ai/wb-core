#!/usr/bin/env python3
"""One-shot protocol-v2 GitHub Release Runner: admit once, act once, receipt once."""

from __future__ import annotations

import argparse
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
    PLAN_SCHEMA,
    canonical_json_bytes,
    verify_plan,
)


RECEIPT_SCHEMA = "wb-core.release-receipt/v2"
WORKFLOW_NAME = "PR Gate"
ARTIFACT_PREFIX = "test-plan-"
RECEIPT_MARKER = "wb-core-release-receipt"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TERMINAL_STATES = {"done", "awaiting_apply", "blocked", "superseded"}


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
    recomputed_plan: Mapping[str, Any],
    trusted_main_sha: str,
) -> list[str]:
    reasons: list[str] = []
    head = exact_sha(pr.get("head", {}).get("sha"), "pr-head")
    base = exact_sha(pr.get("base", {}).get("sha"), "pr-base")
    if repository != CANONICAL_REPOSITORY:
        reasons.append("repository-not-canonical")
    if run.get("name") != WORKFLOW_NAME:
        reasons.append("workflow-name-mismatch")
    if run.get("event") != "pull_request":
        reasons.append("workflow-provenance-not-pull-request")
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
    if canonical_json_bytes(artifact_plan) != canonical_json_bytes(recomputed_plan):
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
    subprocess.run(
        ["git", "fetch", "--no-tags", "origin", f"pull/{pr}/head"],
        cwd=ROOT,
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="wb-core-release-plan-") as directory:
        output = Path(directory) / "test-plan.json"
        subprocess.run(
            [
                sys.executable,
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
            check=True,
        )
        plan = json.loads(output.read_text(encoding="utf-8"))
    verify_plan(plan)
    return plan


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


def deploy_exact(pr: int, head_sha: str, merge_sha: str, temp_dir: Path) -> str:
    configure_deploy_environment(temp_dir)
    evidence = temp_dir / "deploy-evidence.json"
    env = os.environ.copy()
    env["WB_CORE_RELEASE_PR"] = str(pr)
    env["WB_CORE_RELEASE_HEAD"] = head_sha
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


def read_manifest(binding: Mapping[str, Any], merge_sha: str) -> dict[str, Any]:
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
    return {"path": path, "sha256": expected, "operation_id": manifest["operation_id"]}


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
    recomputed = recompute_plan(pr_number, base_sha, head_sha)
    reasons = admission_reasons(
        repository=client.repository,
        run=run,
        pr=pr,
        artifact_plan=artifact_plan,
        recomputed_plan=recomputed,
        trusted_main_sha=trusted,
    )
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
        if kind in {"live_runtime", "production_mutation"}:
            with tempfile.TemporaryDirectory(prefix="wb-core-deploy-") as directory:
                deployed_sha = deploy_exact(pr_number, head_sha, merge_sha, Path(directory))
        if kind == "production_mutation":
            binding = artifact_plan["release_plan"].get("manifest")
            if not isinstance(binding, Mapping):
                raise RunnerError("production-manifest-binding-missing")
            manifest = read_manifest(binding, merge_sha)
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
