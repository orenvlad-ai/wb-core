#!/usr/bin/env python3
"""Trusted one-shot PR merge and exact-SHA release runner.

Only the checked-out merge commit can become a deployed runtime. Release
readback is bounded to exact identity, enabled services and one public surface.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
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
from ci.loopback_listener_proof import remote_source  # noqa: E402


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


def bootstrap_deploy_operation(merge: str) -> str:
    """Recover the OLD trusted Runner's v3 identity after its merge checkout.

    Only immutable successful Gate evidence and exact merged PR/parent are
    admitted. Six bounded read-only GitHub requests; no merge or runtime write.
    This never renames an owner or permits a manual release-context fallback.
    """
    merge = exact_sha(merge, 'bootstrap-merge')
    raw_pr = os.environ.get('WB_CORE_RELEASE_PR', '').strip()
    head = exact_sha(os.environ.get('WB_CORE_RELEASE_HEAD'), 'bootstrap-head')
    if not raw_pr.isdigit() or int(raw_pr) <= 0:
        raise RunnerError('bootstrap-pr-invalid')
    pr_number = int(raw_pr)
    if (os.environ.get('GITHUB_EVENT_NAME') != 'workflow_run'
            or os.environ.get('GITHUB_REPOSITORY') != REPOSITORY
            or os.environ.get('GITHUB_WORKFLOW') != 'Release Runner'
            or not os.environ.get('GITHUB_TOKEN')):
        raise RunnerError('bootstrap-workflow-context-invalid')
    try:
        path = Path(os.environ['GITHUB_EVENT_PATH'])
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('event exceeds bound')
        event = json.loads(path.read_text())
        triggered = event['workflow_run']
        run_id = triggered['id']
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            raise ValueError('invalid run')
    except (KeyError, OSError, ValueError, TypeError):
        raise RunnerError('bootstrap-event-invalid') from None
    if event.get('repository', {}).get('full_name') != REPOSITORY:
        raise RunnerError('bootstrap-event-repository-mismatch')
    client = GitHub(REPOSITORY, os.environ['GITHUB_TOKEN'])
    try:
        run, plan = collect_plan(client, run_id)
        jobs_ok = successful_jobs(client, run_id)
        pr = client.get(f'/pulls/{pr_number}')
        commit = client.get(f'/git/commits/{merge}')
    except Exception:
        # Existing API errors may include provider response bodies. Keep the
        # bootstrap refusal small and never expose token/event payloads.
        raise RunnerError('bootstrap-github-evidence-unavailable') from None
    for evidence in (triggered, run):
        if (evidence.get('id') != run_id or evidence.get('name') != WORKFLOW_NAME
                or evidence.get('path') != WORKFLOW_PATH
                or evidence.get('event') != 'pull_request' or evidence.get('run_attempt') != 1
                or evidence.get('status') != 'completed' or evidence.get('conclusion') != 'success'
                or evidence.get('repository', {}).get('full_name') != REPOSITORY
                or exact_sha(evidence.get('head_sha'), 'bootstrap-gate-head') != head):
            raise RunnerError('bootstrap-gate-binding-invalid')
        # GitHub removes the run's PR links after merge. The exact checked plan,
        # env PR/head and merged PR/parent below are the authoritative binding.
        # An empty list is legitimate; a nonempty conflicting list never is.
        links = evidence.get('pull_requests')
        if not isinstance(links, list) or (links and workflow_pr(evidence) != pr_number):
            raise RunnerError('bootstrap-gate-pr-links-invalid')
    base = exact_sha(plan.get('base_sha'), 'bootstrap-plan-base')
    if (not jobs_ok or plan.get('pull_request') != pr_number or plan.get('head_sha') != head
            or plan.get('release_kind') != 'live_runtime'
            or pr.get('number') != pr_number or pr.get('merged') is not True
            or exact_sha(pr.get('merge_commit_sha'), 'bootstrap-pr-merge') != merge
            or exact_sha(pr.get('head', {}).get('sha'), 'bootstrap-pr-head') != head
            or pr.get('head', {}).get('repo', {}).get('full_name') != REPOSITORY
            or pr.get('base', {}).get('ref') != 'main'
            or pr.get('base', {}).get('repo', {}).get('full_name') != REPOSITORY
            or commit.get('sha') != merge
            or [parent.get('sha') for parent in commit.get('parents', [])] != [base]
            or trusted_main_sha() != merge):
        raise RunnerError('bootstrap-merge-plan-binding-invalid')
    return operation_id(run_id, pr_number, base, head, plan['plan_sha256'])


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


def recompute_plan(pr: int, base: str, head: str, *, trusted_base: str | None = None) -> dict[str, Any]:
    if trusted_main_sha() != (trusted_base or base):
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


def documentation_only_diff(before: str, after: str) -> None:
    """Allow only ordinary, non-executable Markdown; inspect both entry modes."""
    raw = subprocess.run(
        ["git", "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", before, after],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout.split(b"\0")
    if raw.pop() != b"" or len(raw) % 2:
        raise RunnerError("documentation-diff-invalid")
    for metadata, raw_path in zip(raw[::2], raw[1::2]):
        fields = metadata.split()
        path = raw_path.decode("utf-8")
        if (len(fields) != 5 or fields[0] not in {b":100644", b":000000"}
                or fields[1] not in {b"100644", b"000000"}
                or not (path == "AGENTS.md" or (path.startswith("docs/") and path.endswith(".md")))):
            raise RunnerError("main-change-requires-new-gate")


def documentation_merge_tree(tested_base: str, base: str, head: str) -> str:
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", tested_base, base], cwd=ROOT)
    if ancestor.returncode != 0:
        raise RunnerError("tested-base-not-ancestor")
    documentation_only_diff(tested_base, base)
    merged = subprocess.run(["git", "merge-tree", "--write-tree", base, head],
                            cwd=ROOT, capture_output=True, text=True)
    if merged.returncode != 0:
        raise RunnerError("documentation-merge-conflict")
    tree = exact_sha(merged.stdout.splitlines()[0], "admitted-merge-tree")
    # A docs rebase may not alter any candidate code, dependency or CI entry.
    documentation_only_diff(head, tree)
    return tree


def admit(client: GitHub, run_id: int) -> tuple[dict[str, Any], dict[str, Any], int, str, str]:
    run, plan = collect_plan(client, run_id)
    pr_number = workflow_pr(run)
    pr = client.get(f"/pulls/{pr_number}")
    base = exact_sha(pr.get("base", {}).get("sha"), "pr-base")
    head = exact_sha(pr.get("head", {}).get("sha"), "pr-head")
    tested_base = exact_sha(plan.get("base_sha"), "tested-base")
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
    if plan.get("pull_request") != pr_number or plan.get("head_sha") != head:
        reasons.append("plan-binding-invalid")
    if not successful_jobs(client, run_id):
        reasons.append("gate-jobs-invalid")
    merge_tree = None
    if not reasons:
        try:
            if tested_base != base:
                # No runtime or selector change can enter via an untested main.
                merge_tree = documentation_merge_tree(tested_base, base, head)
                expected = recompute_plan(pr_number, tested_base, head, trusted_base=base)
            else:
                expected = recompute_plan(pr_number, base, head)
            if canonical_bytes(expected) != canonical_bytes(plan):
                reasons.append("plan-recomputation-mismatch")
            elif tested_base != base:
                current = recompute_plan(pr_number, base, head)
                semantic = lambda value: {k: v for k, v in value.items() if k not in {"base_sha", "plan_sha256"}}
                if canonical_bytes(semantic(current)) != canonical_bytes(semantic(plan)):
                    reasons.append("current-plan-mismatch")
        except Exception as exc:
            reasons.append(exc.reason if isinstance(exc, RunnerError) else "plan-recomputation-failed")
    if reasons:
        raise RunnerError(",".join(sorted(set(reasons))))
    return {**run, "admitted_merge_tree": merge_tree}, plan, pr_number, base, head


def merge_exact(client: GitHub, pr: int, base: str, head: str, *, expected_tree: str | None = None) -> str:
    if expected_tree is not None:
        current_pr = client.get(f"/pulls/{pr}")
        current_main = client.get("/git/ref/heads/main")
        if (current_pr.get("state") != "open" or current_pr.get("draft") is not False
                or current_pr.get("mergeable") is not True
                or current_pr.get("base", {}).get("sha") != base
                or current_pr.get("base", {}).get("ref") != "main"
                or current_pr.get("base", {}).get("repo", {}).get("full_name") != client.repository
                or current_pr.get("head", {}).get("sha") != head
                or current_pr.get("head", {}).get("repo", {}).get("full_name") != client.repository
                or current_main.get("object", {}).get("sha") != base):
            raise RunnerError("premerge-identity-drift")
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
    if expected_tree is not None and commit.get("tree", {}).get("sha") != expected_tree:
        raise RunnerError("merge-tree-mismatch")
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


def runtime_readback_payload(target: Mapping[str, Any], merge: str) -> dict[str, Any]:
    services = sorted(
        str(unit.get("name") or "")
        for unit in target.get("managed_systemd_units") or []
        if isinstance(unit, Mapping)
        and unit.get("enable") is True
        and str(unit.get("name") or "").endswith(".service")
    )
    payload = {
        "expected_commit": exact_sha(merge, "deploy-readback"),
        "target_dir": str(target.get("target_dir") or "").rstrip("/"),
        "main_service": str(target.get("service_name") or "").strip(),
        "main_loopback": str(target.get("loopback_base_url") or "").rstrip("/"),
        "services": services,
        "urls": [
            str(target.get("login_health_loopback_base_url") or target.get("loopback_base_url") or "").rstrip("/") + "/login",
            str(target.get("public_base_url") or "").rstrip("/") + "/login",
        ],
    }
    main_url = urllib.parse.urlparse(payload["main_loopback"])
    if (not payload["target_dir"].startswith("/") or not services
            or payload["main_service"] not in services
            or main_url.scheme != "http" or main_url.hostname != "127.0.0.1"
            or not main_url.port):
        raise RunnerError("deploy-readback-contract-invalid")
    if any(not url.startswith(("http://", "https://")) for url in payload["urls"]):
        raise RunnerError("deploy-readback-contract-invalid")
    return payload


def runtime_readback(target: Mapping[str, Any], merge: str) -> None:
    destination = str(target.get("ssh_destination") or "").strip()
    identity = os.environ.get("WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE", "").strip()
    options = os.environ.get("WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS", "").strip()
    if not destination or not identity or not options:
        raise RunnerError("deploy-readback-contract-invalid")
    payload = runtime_readback_payload(target, merge)
    script = f"""
import json
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path

{remote_source()}
expected = {payload!r}
root = Path(expected["target_dir"])
commit = (root / ".wb-core-runtime-sha").read_text(encoding="utf-8").strip()
metadata = json.loads((root / ".wb-core-deploy.json").read_text(encoding="utf-8"))
if commit != expected["expected_commit"]:
    raise SystemExit(2)
if metadata.get("commit") != commit or metadata.get("deployment_complete") is not True:
    raise SystemExit(3)
for service in expected["services"]:
    subprocess.run(["systemctl", "is-active", "--quiet", service], check=True)
pid = int(subprocess.run(
    ["systemctl", "show", "--property=MainPID", "--value", expected["main_service"]],
    check=True, text=True, capture_output=True,
).stdout.strip())
if pid <= 0:
    raise SystemExit(5)
port = urllib.parse.urlparse(expected["main_loopback"]).port
if not owned_loopback_listener(pid, port):
    raise SystemExit(5)
for url in expected["urls"]:
    with urllib.request.urlopen(url, timeout=10) as response:
        if response.status != 200:
            raise SystemExit(4)
print(json.dumps({{"commit": commit, "services": expected["services"], "urls": expected["urls"]}}, sort_keys=True))
"""
    command = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
        "-i", identity,
        *shlex.split(options),
        destination,
        "python3", "-",
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(
                command,
                input=script,
                text=True,
                capture_output=True,
                timeout=35,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            try:
                observed = json.loads(result.stdout)
            except json.JSONDecodeError:
                observed = {}
            if observed.get("commit") == merge:
                return
        if attempt < 2:
            time.sleep(5)
    raise RunnerError("deploy-readback-failed")


def prepare_deploy_ownership(operation: str) -> None:
    """Bounded pre-merge claim; no timer stop, drain wait, sync or restart."""
    from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
    from packages.application.business_data_deploy_protection import remote_shell
    with tempfile.TemporaryDirectory(prefix="wb-core-premerge-") as directory:
        configure_ssh(Path(directory))
        target = hosted.load_hosted_runtime_target(hosted.resolve_target_file())
        shell = remote_shell('claim', app_dir=target.target_dir,
            runtime_dir=target.runtime_env['REGISTRY_UPLOAD_RUNTIME_DIR'],
            env_file=target.environment_file, operation=operation)
        subprocess.run(hosted._remote_shell_command(target, shell), timeout=120, check=True)


def deploy_exact(pr: int, head: str, merge: str, *, operation: str) -> str:
    with tempfile.TemporaryDirectory(prefix="wb-core-deploy-") as directory:
        configure_ssh(Path(directory))
        env = os.environ.copy()
        env["WB_CORE_RELEASE_PR"] = str(pr)
        env["WB_CORE_RELEASE_HEAD"] = head
        env["WB_CORE_RELEASE_OPERATION_ID"] = operation
        env['WB_CORE_RELEASE_DEFER_DEPLOY_OWNER_FINISH'] = 'true'
        subprocess.run(
            [sys.executable, "apps/registry_upload_http_entrypoint_hosted_runtime.py", "deploy"],
            cwd=ROOT,
            env=env,
            check=True,
        )
        target = json.loads(
            (ROOT / "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json").read_text()
        )
        runtime_readback(target, merge)
        from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
        from packages.application.business_data_deploy_protection import remote_shell
        finish = remote_shell('finish', app_dir=target['target_dir'],
            runtime_dir=target['runtime_env']['REGISTRY_UPLOAD_RUNTIME_DIR'],
            env_file=target['environment_file'], operation=operation, expected_sha=merge)
        subprocess.run(hosted._remote_shell_command(hosted.load_hosted_runtime_target(), finish),
                       timeout=120, check=True)
    if trusted_main_sha() != merge:
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
        "tested_base_sha": plan.get("base_sha", base),
        "gate_plan_sha256": plan.get("plan_sha256"),
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
        kind = str(plan["release_kind"])
        operation = operation_id(run_id, pr, base, head, str(plan.get('plan_sha256') or ''))
        if kind == 'live_runtime':
            prepare_deploy_ownership(operation)
        tree = _run.get("admitted_merge_tree")
        merge = (merge_exact(client, pr, base, head, expected_tree=tree) if tree is not None
                 else merge_exact(client, pr, base, head))
        checkout_merge(merge)
        if kind == "live_runtime":
            deployed = deploy_exact(pr, head, merge, operation=operation)
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
