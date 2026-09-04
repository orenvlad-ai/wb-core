#!/usr/bin/env python3
"""Trusted one-shot PR merge and exact-SHA release runner.

Only the checked-out merge commit can become a deployed runtime.
"""

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

from ci.select_checks import canonical_bytes, verify_plan  # noqa: E402


REPOSITORY = "orenvlad-ai/wb-core"
WORKFLOW_NAME = "PR Gate"
WORKFLOW_PATH = ".github/workflows/pr-gate.yml"
PLAN_PREFIX = "check-plan-"
RECEIPT_SCHEMA = "wb-core.release-receipt/v3"
RECEIPT_MARKER = "wb-core-release-receipt"
SHA_RE = re.compile(r"[0-9a-f]{40}")


class RunnerError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def exact_sha(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if SHA_RE.fullmatch(normalized) is None:
        raise RunnerError(f"{label}-invalid")
    return normalized


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def operation_id(run_id: int, pr: int, base: str, head: str, plan_hash: str) -> str:
    return "release-v3-" + sha256(
        canonical_bytes({"run": run_id, "pr": pr, "base": base, "head": head, "plan": plan_hash})
    )[:32]


def _origin(url: str) -> tuple[str, str, int] | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _origin(req.full_url) != _origin(newurl):
            for name in ("Authorization", "Proxy-Authorization", "Cookie"):
                redirected.remove_header(name)
        return redirected


class GitHub:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.base = f"https://api.github.com/repos/{repository}"
        self.token = token

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None, *, raw: bool = False) -> Any:
        payload = None if body is None else canonical_bytes(body)
        request = urllib.request.Request(
            path if path.startswith("https://") else self.base + path,
            method=method,
            data=payload,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "wb-core-release-runner-v3",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        try:
            with urllib.request.build_opener(_SafeRedirect()).open(request, timeout=30) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise RunnerError(f"github-http-{exc.code}:{detail}") from exc
        except urllib.error.URLError as exc:
            raise RunnerError(f"github-transport:{exc.reason}") from exc
        if raw:
            return data
        return json.loads(data) if data else None

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        return self.request("POST", path, body)

    def put(self, path: str, body: Mapping[str, Any]) -> Any:
        return self.request("PUT", path, body)


def trusted_main_sha() -> str:
    value = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, text=True, capture_output=True
    ).stdout
    return exact_sha(value, "trusted-main")


def _extract_plan(raw_zip: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            names = [name for name in archive.namelist() if name.rstrip("/") == "check-plan.json"]
            if names != ["check-plan.json"]:
                raise RunnerError("plan-artifact-shape-invalid")
            plan = json.loads(archive.read(names[0]))
    except (zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise RunnerError("plan-artifact-invalid") from exc
    if not isinstance(plan, dict):
        raise RunnerError("plan-shape-invalid")
    verify_plan(plan)
    return plan


def collect_plan(client: GitHub, run_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    run = client.get(f"/actions/runs/{run_id}")
    artifacts = client.get(f"/actions/runs/{run_id}/artifacts?per_page=100")
    values = [
        item for item in (artifacts.get("artifacts") or [])
        if str(item.get("name") or "").startswith(PLAN_PREFIX) and item.get("expired") is not True
    ]
    if len(values) != 1:
        raise RunnerError("plan-artifact-count-invalid")
    raw = client.request("GET", f"/actions/artifacts/{int(values[0]['id'])}/zip", raw=True)
    return run, _extract_plan(raw)


def workflow_pr(run: Mapping[str, Any]) -> int:
    values = run.get("pull_requests")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0].get("number"), int):
        raise RunnerError("workflow-pr-binding-invalid")
    return int(values[0]["number"])


def successful_jobs(client: GitHub, run_id: int) -> bool:
    payload = client.get(f"/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    jobs = payload.get("jobs") if isinstance(payload, Mapping) else None
    if not isinstance(jobs, list):
        return False
    expected = {"Core", "Plan", "Checks", "pr-gate"}
    return {str(job.get("name") or "") for job in jobs} == expected and all(
        job.get("status") == "completed" and job.get("conclusion") == "success" for job in jobs
    )


def recompute_plan(pr: int, base: str, head: str) -> dict[str, Any]:
    if trusted_main_sha() != base:
        raise RunnerError("base-main-drift")
    subprocess.run(
        ["git", "fetch", "--no-tags", "--no-recurse-submodules", "origin", f"+refs/pull/{pr}/head:refs/remotes/origin/release-head"],
        cwd=ROOT,
        check=True,
    )
    resolved = subprocess.run(
        ["git", "rev-parse", "refs/remotes/origin/release-head^{commit}"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if exact_sha(resolved, "pull-head") != head:
        raise RunnerError("pull-head-drift")
    with tempfile.TemporaryDirectory(prefix="wb-core-plan-") as directory:
        output = Path(directory) / "check-plan.json"
        subprocess.run(
            [sys.executable, "-I", "ci/select_checks.py", "--pr", str(pr), "--base", base, "--head", head, "--output", str(output)],
            cwd=ROOT,
            check=True,
        )
        result = json.loads(output.read_text(encoding="utf-8"))
    verify_plan(result)
    return result


def admit(client: GitHub, run_id: int) -> tuple[dict[str, Any], dict[str, Any], int, str, str]:
    run, plan = collect_plan(client, run_id)
    pr_number = workflow_pr(run)
    pr = client.get(f"/pulls/{pr_number}")
    base = exact_sha(pr.get("base", {}).get("sha"), "pr-base")
    head = exact_sha(pr.get("head", {}).get("sha"), "pr-head")
    reasons: list[str] = []
    if client.repository != REPOSITORY:
        reasons.append("repository-mismatch")
    if run.get("name") != WORKFLOW_NAME or run.get("path") != WORKFLOW_PATH:
        reasons.append("workflow-mismatch")
    if run.get("event") != "pull_request" or run.get("run_attempt") != 1:
        reasons.append("workflow-provenance-invalid")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        reasons.append("workflow-not-successful")
    if exact_sha(run.get("head_sha"), "workflow-head") != head:
        reasons.append("workflow-head-mismatch")
    if pr.get("state") != "open" or pr.get("draft") is not False:
        reasons.append("pr-not-ready")
    if pr.get("base", {}).get("ref") != "main" or pr.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY:
        reasons.append("base-invalid")
    if pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY:
        reasons.append("head-repository-invalid")
    if base != trusted_main_sha():
        reasons.append("base-main-drift")
    if pr.get("mergeable") is not True:
        reasons.append("pr-not-mergeable")
    if plan.get("pull_request") != pr_number or plan.get("base_sha") != base or plan.get("head_sha") != head:
        reasons.append("plan-binding-invalid")
    if not successful_jobs(client, run_id):
        reasons.append("gate-jobs-invalid")
    if not reasons:
        try:
            expected = recompute_plan(pr_number, base, head)
            if canonical_bytes(expected) != canonical_bytes(plan):
                reasons.append("plan-recomputation-mismatch")
        except Exception as exc:
            reasons.append(exc.reason if isinstance(exc, RunnerError) else "plan-recomputation-failed")
    if reasons:
        raise RunnerError(",".join(sorted(set(reasons))))
    return run, plan, pr_number, base, head


def merge_exact(client: GitHub, pr: int, base: str, head: str) -> str:
    try:
        result = client.put(f"/pulls/{pr}/merge", {"sha": head, "merge_method": "squash"})
    except RunnerError:
        readback = client.get(f"/pulls/{pr}")
        if readback.get("merged") is True and readback.get("head", {}).get("sha") == head:
            merge = exact_sha(readback.get("merge_commit_sha"), "merge-readback")
        else:
            raise
    else:
        if not isinstance(result, Mapping) or result.get("merged") is not True:
            raise RunnerError("expected-head-merge-rejected")
        merge = exact_sha(result.get("sha"), "merge-result")
    commit = client.get(f"/git/commits/{merge}")
    parents = commit.get("parents") if isinstance(commit, Mapping) else None
    if not isinstance(parents, list) or [item.get("sha") for item in parents] != [base]:
        raise RunnerError("merge-parent-mismatch")
    ref = client.get("/git/ref/heads/main")
    if exact_sha(ref.get("object", {}).get("sha"), "main-ref") != merge:
        raise RunnerError("main-ref-mismatch")
    return merge


def checkout_merge(merge: str) -> None:
    subprocess.run(["git", "fetch", "--no-tags", "origin", merge], cwd=ROOT, check=True)
    subprocess.run(["git", "checkout", "--detach", merge], cwd=ROOT, check=True)
    if trusted_main_sha() != merge:
        raise RunnerError("merge-checkout-mismatch")


def configure_ssh(directory: Path) -> None:
    """Write credential material byte-for-byte into protected temporary files."""
    key = os.environ.get("WB_CORE_DEPLOY_SSH_KEY", "")
    known_hosts = os.environ.get("WB_CORE_DEPLOY_KNOWN_HOSTS", "")
    if not key.strip() or not known_hosts.strip():
        raise RunnerError("deploy-credentials-missing")
    target = json.loads((ROOT / "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json").read_text())
    host = str(target.get("host_ip") or "").strip()
    if not host:
        raise RunnerError("deploy-target-missing")
    key_path = directory / "key"
    hosts_path = directory / "known-hosts"
    key_path.write_text(key, encoding="utf-8")
    hosts_path.write_text(known_hosts, encoding="utf-8")
    key_path.chmod(0o600)
    hosts_path.chmod(0o600)
    os.environ["WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE"] = str(key_path)
    os.environ["WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS"] = (
        f"-o HostName={host} -o User=root -o IdentitiesOnly=yes "
        f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={hosts_path}"
    )


def deploy_exact(pr: int, head: str, merge: str) -> str:
    with tempfile.TemporaryDirectory(prefix="wb-core-deploy-") as directory:
        configure_ssh(Path(directory))
        evidence = Path(directory) / "evidence.json"
        env = os.environ.copy()
        env["WB_CORE_RELEASE_PR"] = str(pr)
        env["WB_CORE_RELEASE_HEAD"] = head
        subprocess.run(
            [sys.executable, "apps/registry_upload_http_entrypoint_hosted_runtime.py", "deploy-and-verify", "--output", str(evidence)],
            cwd=ROOT,
            env=env,
            check=True,
        )
        payload = json.loads(evidence.read_text())
    if payload.get("ok") is not True or trusted_main_sha() != merge:
        raise RunnerError("deploy-readback-failed")
    return merge


def receipt(*, state: str, run_id: int, pr: int, base: str, head: str, plan: Mapping[str, Any], merge: str | None = None, deployed: str | None = None, reason: str | None = None) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "state": state,
        "operation_id": operation_id(run_id, pr, base, head, str(plan.get("plan_sha256") or "")),
        "pull_request": pr,
        "gate_run_id": run_id,
        "base_sha": base,
        "head_sha": head,
        "merge_sha": merge,
        "deployed_sha": deployed,
        "release_kind": plan.get("release_kind"),
        "reason": reason,
    }


def comments(client: GitHub, pr: int) -> list[Mapping[str, Any]]:
    values = client.get(f"/issues/{pr}/comments?per_page=100")
    return [item for item in values if isinstance(item, Mapping)] if isinstance(values, list) else []


def publish(client: GitHub, data: Mapping[str, Any]) -> None:
    marker = f"<!-- {RECEIPT_MARKER} operation={data['operation_id']} -->"
    if any(marker in str(item.get("body") or "") for item in comments(client, int(data["pull_request"]))):
        raise RunnerError("operation-already-terminal")
    client.post(
        f"/issues/{data['pull_request']}/comments",
        {"body": marker + "\n```json\n" + json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n```"},
    )


def write_receipt(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(data) + b"\n")


def _write_outputs(path: Path | None, values: Mapping[str, str]) -> None:
    if path:
        with path.open("a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def run(client: GitHub, run_id: int, output: Path) -> dict[str, Any]:
    try:
        _run, plan, pr, base, head = admit(client, run_id)
    except RunnerError:
        raise
    merge: str | None = None
    deployed: str | None = None
    try:
        merge = merge_exact(client, pr, base, head)
        checkout_merge(merge)
        kind = str(plan["release_kind"])
        if kind == "live_runtime":
            deployed = deploy_exact(pr, head, merge)
        data = receipt(state="done", run_id=run_id, pr=pr, base=base, head=head, plan=plan, merge=merge, deployed=deployed)
    except Exception as exc:
        reason = exc.reason if isinstance(exc, RunnerError) else f"{type(exc).__name__}"
        data = receipt(state="blocked", run_id=run_id, pr=pr, base=base, head=head, plan=plan, merge=merge, deployed=deployed, reason=reason)
    write_receipt(output, data)
    publish(client, data)
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("route", "run"))
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    client = GitHub(args.repository, token)
    if args.command == "route":
        _run, plan, _pr, _base, _head = admit(client, args.workflow_run_id)
        kind = str(plan["release_kind"])
        _write_outputs(args.github_output, {"release_kind": kind, "deploy_required": str(kind != "repo_only").lower()})
        print(json.dumps({"release_kind": kind, "deploy_required": kind != "repo_only"}, sort_keys=True))
        return 0
    if args.output is None:
        raise SystemExit("--output is required")
    data = run(client, args.workflow_run_id, args.output)
    print(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return 0 if data["state"] == "done" else 2


if __name__ == "__main__":
    raise SystemExit(main())
