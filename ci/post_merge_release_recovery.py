#!/usr/bin/env python3
"""Fail-closed continuation for a proven post-merge deploy tail.

The runner never merges, copies code, installs runtime dependencies, or
restarts services.  It accepts only a failed canonical Release Runner whose
trusted log proves the root-storage artifact readback exited 3 after the exact
merge was deployed.  Recovery code may be newer than that runtime only when
the complete intervening diff is classified repo-only by the current trusted
check map.
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
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import github_release_runner as release  # noqa: E402
from ci.select_checks import build_plan_from_paths, canonical_bytes, git_file_exists  # noqa: E402


REPOSITORY = "orenvlad-ai/wb-core"
RELEASE_WORKFLOW = "Release Runner"
RELEASE_WORKFLOW_PATH = ".github/workflows/release-runner.yml"
RECOVERY_SCHEMA = "wb-core.post-merge-release-recovery/v1"
CLAIM_MARKER = "wb-core-release-recovery-claim"
RECEIPT_MARKER = "wb-core-release-recovery-receipt"
TARGET_FILE = ROOT / "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json"
RUNTIME_GUARD_PATHS = (
    "apps/registry_upload_http_entrypoint_hosted_runtime.py",
    "apps/change_registry_observer.py",
    "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json",
    "artifacts/registry_upload_http_entrypoint/root_storage_policy_v1.json",
)
FINANCE_FLAGS = (
    "FINANCE_LIQUIDITY_ENABLED",
    "FINANCE_LIQUIDITY_READ_ENABLED",
    "FINANCE_LIQUIDITY_WRITE_ENABLED",
)


class RecoveryError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def exact_sha(value: Any, label: str) -> str:
    try:
        return release.exact_sha(value, label)
    except release.RunnerError as exc:
        raise RecoveryError(exc.reason) from exc


def _zip_files(raw: bytes, label: str) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if not names or len(names) > 100:
                raise RecoveryError(f"{label}-shape-invalid")
            return {name: archive.read(name) for name in names}
    except zipfile.BadZipFile as exc:
        raise RecoveryError(f"{label}-invalid") from exc


def _json_file(files: Mapping[str, bytes], basename: str, label: str) -> dict[str, Any]:
    matches = [raw for name, raw in files.items() if Path(name).name == basename]
    if len(matches) != 1:
        raise RecoveryError(f"{label}-count-invalid")
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise RecoveryError(f"{label}-invalid") from exc
    if not isinstance(value, dict):
        raise RecoveryError(f"{label}-shape-invalid")
    return value


def recovery_operation_id(release_run_id: int, original: Mapping[str, Any]) -> str:
    identity = {
        "release_run_id": int(release_run_id),
        "gate_run_id": int(original["gate_run_id"]),
        "pull_request": int(original["pull_request"]),
        "base_sha": original["base_sha"],
        "head_sha": original["head_sha"],
        "merge_sha": original["merge_sha"],
        "original_operation_id": original["operation_id"],
    }
    return "release-recovery-v1-" + digest(canonical_bytes(identity))[:32]


def _comment_payload(body: str, marker: str) -> dict[str, Any] | None:
    if marker not in body:
        return None
    match = re.search(r"```json\s*(\{.*?\})\s*```", body, re.DOTALL)
    if match is None:
        raise RecoveryError("recovery-comment-invalid")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RecoveryError("recovery-comment-invalid") from exc
    if not isinstance(value, dict):
        raise RecoveryError("recovery-comment-invalid")
    return value


def _matching_comments(client: release.GitHub, pr: int, marker: str) -> list[dict[str, Any]]:
    values: list[Mapping[str, Any]] = []
    for page in range(1, 11):
        payload = client.get(f"/issues/{pr}/comments?per_page=100&page={page}")
        if not isinstance(payload, list) or any(not isinstance(item, Mapping) for item in payload):
            raise RecoveryError("recovery-comments-invalid")
        values.extend(payload)
        if len(payload) < 100:
            break
    else:
        raise RecoveryError("recovery-comments-pagination-unbounded")
    found: list[dict[str, Any]] = []
    for item in values:
        parsed = _comment_payload(str(item.get("body") or ""), marker)
        if parsed is not None:
            found.append(parsed)
    return found


def _validate_original_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema": release.RECEIPT_SCHEMA,
        "state": "blocked",
        "reason": "CalledProcessError",
        "release_kind": "live_runtime",
        "deployed_sha": None,
    }
    if any(value.get(key) != expected for key, expected in required.items()):
        raise RecoveryError("original-receipt-not-eligible")
    normalized = dict(value)
    normalized["pull_request"] = int(value.get("pull_request") or 0)
    normalized["gate_run_id"] = int(value.get("gate_run_id") or 0)
    if normalized["pull_request"] <= 0 or normalized["gate_run_id"] <= 0:
        raise RecoveryError("original-receipt-identity-invalid")
    for key in ("base_sha", "head_sha", "merge_sha"):
        normalized[key] = exact_sha(value.get(key), key.replace("_", "-"))
    operation = str(value.get("operation_id") or "")
    if not operation.startswith("release-v3-"):
        raise RecoveryError("original-operation-invalid")
    normalized["operation_id"] = operation
    return normalized


def _prove_failed_stage(raw_log: bytes, gate_run_id: int, job_name: str) -> dict[str, Any]:
    text = raw_log.decode("utf-8", errors="replace")
    required = (
        "deploy_current_checkout",
        'run_stage("readback", root_storage_commands["status_artifact_readback"])',
        "apps/root_storage_policy.py",
        "status-readback",
        "returned non-zero exit status 3",
        f'--workflow-run-id "{gate_run_id}"',
    )
    if any(item not in text for item in required):
        raise RecoveryError("failed-stage-not-root-storage-readback-exit3")
    command_failures = re.findall(
        r"subprocess\.CalledProcessError: Command .*?returned non-zero exit status (\d+)",
        text,
        re.DOTALL,
    )
    if command_failures != ["3"] or "exit status 255" in text:
        raise RecoveryError("failed-stage-not-definite-single-exit3")
    return {
        "job_name": job_name,
        "job_log_sha256": digest(raw_log),
        "stage": "root-storage-status-artifact-readback",
        "exit_status": 3,
    }


def collect_evidence(client: release.GitHub, release_run_id: int) -> dict[str, Any]:
    run = client.get(f"/actions/runs/{release_run_id}")
    reasons: list[str] = []
    if client.repository != REPOSITORY:
        reasons.append("repository-mismatch")
    if run.get("name") != RELEASE_WORKFLOW or run.get("path") != RELEASE_WORKFLOW_PATH:
        reasons.append("release-workflow-mismatch")
    if run.get("event") != "workflow_run" or run.get("run_attempt") != 1:
        reasons.append("release-provenance-invalid")
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        reasons.append("release-not-failed")
    if reasons:
        raise RecoveryError(",".join(sorted(reasons)))

    artifacts = client.get(f"/actions/runs/{release_run_id}/artifacts?per_page=100")
    candidates = [
        item
        for item in (artifacts.get("artifacts") or [])
        if str(item.get("name") or "").startswith("release-receipt-")
        and item.get("expired") is not True
    ]
    if len(candidates) != 1:
        raise RecoveryError("original-receipt-artifact-count-invalid")
    artifact = candidates[0]
    raw_receipt = client.request(
        "GET", f"/actions/artifacts/{int(artifact['id'])}/zip", raw=True
    )
    original = _validate_original_receipt(
        _json_file(_zip_files(raw_receipt, "original-receipt-artifact"), "release-receipt.json", "original-receipt")
    )
    gate_id = int(original["gate_run_id"])
    if artifact.get("name") != f"release-receipt-{gate_id}":
        raise RecoveryError("original-receipt-artifact-binding-invalid")

    jobs_payload = client.get(f"/actions/runs/{release_run_id}/jobs?filter=latest&per_page=100")
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, Mapping) else None
    deployed_jobs = [
        item
        for item in (jobs or [])
        if isinstance(item, Mapping) and item.get("name") == "One-shot deployed release"
    ]
    if len(deployed_jobs) != 1:
        raise RecoveryError("release-deployed-job-count-invalid")
    deployed_job = deployed_jobs[0]
    failed_steps = [
        step
        for step in (deployed_job.get("steps") or [])
        if isinstance(step, Mapping) and step.get("conclusion") == "failure"
    ]
    if (
        deployed_job.get("status") != "completed"
        or deployed_job.get("conclusion") != "failure"
        or [step.get("name") for step in failed_steps]
        != ["Admit once, merge expected head once, deploy exact merge once, emit one receipt"]
    ):
        raise RecoveryError("release-deployed-job-shape-invalid")
    raw_log = client.request("GET", f"/actions/jobs/{int(deployed_job['id'])}/logs", raw=True)
    failure = _prove_failed_stage(raw_log, gate_id, str(deployed_job["name"]))
    if exact_sha(run.get("head_sha"), "release-run-head") != original["base_sha"]:
        raise RecoveryError("release-run-trusted-source-mismatch")
    gate_run, gate_plan = release.collect_plan(client, gate_id)
    if (
        gate_run.get("name") != release.WORKFLOW_NAME
        or gate_run.get("path") != release.WORKFLOW_PATH
        or gate_run.get("event") != "pull_request"
        or gate_run.get("run_attempt") != 1
        or gate_run.get("status") != "completed"
        or gate_run.get("conclusion") != "success"
        or not release.successful_jobs(client, gate_id)
    ):
        raise RecoveryError("original-gate-invalid")
    if (
        gate_plan.get("pull_request") != original["pull_request"]
        or gate_plan.get("base_sha") != original["base_sha"]
        or gate_plan.get("head_sha") != original["head_sha"]
        or gate_plan.get("release_kind") != "live_runtime"
        or exact_sha(gate_run.get("head_sha"), "gate-head") != original["head_sha"]
    ):
        raise RecoveryError("original-gate-binding-invalid")
    expected_operation = release.operation_id(
        gate_id,
        int(original["pull_request"]),
        str(original["base_sha"]),
        str(original["head_sha"]),
        str(gate_plan["plan_sha256"]),
    )
    if original["operation_id"] != expected_operation:
        raise RecoveryError("original-operation-binding-invalid")

    pr = client.get(f"/pulls/{original['pull_request']}")
    if (
        pr.get("merged") is not True
        or pr.get("state") != "closed"
        or pr.get("base", {}).get("ref") != "main"
        or pr.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY
        or pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
        or exact_sha(pr.get("head", {}).get("sha"), "pr-head") != original["head_sha"]
        or exact_sha(pr.get("merge_commit_sha"), "pr-merge") != original["merge_sha"]
    ):
        raise RecoveryError("merged-pr-binding-invalid")
    commit = client.get(f"/git/commits/{original['merge_sha']}")
    parents = commit.get("parents") if isinstance(commit, Mapping) else None
    if not isinstance(parents, list) or [item.get("sha") for item in parents] != [original["base_sha"]]:
        raise RecoveryError("merge-parent-binding-invalid")

    original_marker = f"<!-- {release.RECEIPT_MARKER} operation={original['operation_id']} -->"
    original_comments = _matching_comments(client, original["pull_request"], original_marker)
    if len(original_comments) != 1 or canonical_bytes(original_comments[0]) != canonical_bytes(original):
        raise RecoveryError("original-blocked-receipt-comment-invalid")
    return {
        "release_run_id": int(release_run_id),
        "release_run_head_sha": exact_sha(run.get("head_sha"), "release-run-head"),
        "original_receipt": original,
        "original_receipt_sha256": digest(canonical_bytes(original)),
        "gate_plan": gate_plan,
        "failure": failure,
    }


def _git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=check, text=True, capture_output=True
    )


def prove_repo_only_descendant(client: release.GitHub, original: Mapping[str, Any]) -> dict[str, Any]:
    merge = str(original["merge_sha"])
    current = exact_sha(_git(["rev-parse", "HEAD"]).stdout, "trusted-main")
    ref = client.get("/git/ref/heads/main")
    if exact_sha(ref.get("object", {}).get("sha"), "main-ref") != current:
        raise RecoveryError("trusted-main-remote-drift")
    if _git(["merge-base", "--is-ancestor", merge, current], check=False).returncode != 0:
        raise RecoveryError("trusted-main-not-target-descendant")
    commits = sorted(
        filter(None, _git(["rev-list", "--reverse", "--ancestry-path", f"{merge}..{current}"]).stdout.splitlines())
    )
    commit_proofs: list[dict[str, Any]] = []
    for commit in commits:
        commit = exact_sha(commit, "intervening-commit")
        parent_fields = _git(["show", "-s", "--format=%P", commit]).stdout.split()
        if len(parent_fields) != 1:
            raise RecoveryError("intervening-history-not-linear")
        parent = exact_sha(parent_fields[0], "intervening-parent")
        commit_paths = sorted(
            filter(None, _git(["diff", "--name-only", f"{parent}..{commit}"]).stdout.splitlines())
        )
        commit_plan = build_plan_from_paths(
            pull_request=int(original["pull_request"]),
            base=parent,
            head=commit,
            paths=commit_paths,
            file_exists=git_file_exists,
        )
        if not commit_paths or commit_plan.get("release_kind") != "repo_only":
            raise RecoveryError("intervening-commit-not-repo-only")
        commit_proofs.append(
            {
                "commit": commit,
                "parent": parent,
                "plan_sha256": commit_plan["plan_sha256"],
                "paths": commit_paths,
            }
        )
    paths = sorted(filter(None, _git(["diff", "--name-only", f"{merge}..{current}"]).stdout.splitlines()))
    plan = build_plan_from_paths(
        pull_request=int(original["pull_request"]),
        base=merge,
        head=current,
        paths=paths,
        file_exists=git_file_exists,
    )
    if not paths or plan.get("release_kind") != "repo_only":
        raise RecoveryError("intervening-diff-not-repo-only")
    guarded = _git(["diff", "--name-only", f"{merge}..{current}", "--", *RUNTIME_GUARD_PATHS]).stdout
    if guarded.strip():
        raise RecoveryError("runtime-contract-drift")
    return {
        "trusted_main_sha": current,
        "target_merge_sha": merge,
        "intervening_paths": paths,
        "intervening_plan_sha256": plan["plan_sha256"],
        "intervening_release_kind": plan["release_kind"],
        "intervening_commits": commit_proofs,
    }


def _remote_python_command(target: Any) -> list[str]:
    from apps import registry_upload_http_entrypoint_hosted_runtime as hosted

    return [*hosted._remote_shell_command(target, "python3 -")]


def _run_remote_json(target: Any, script: str) -> dict[str, Any]:
    result = subprocess.run(
        _remote_python_command(target),
        input=script,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        raise RecoveryError(f"remote-readback-failed-{result.returncode}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RecoveryError("remote-readback-invalid") from exc
    if not isinstance(value, dict):
        raise RecoveryError("remote-readback-invalid")
    return value


def _finance_off_validator_source() -> str:
    """Return the exact validator embedded in the remote prestate program."""

    return """
def require_finance_off(flags, unit_returncode, unit_stdout):
    allowed = {None, '', '0', 'false', 'no', 'off'}
    for values in flags.values():
        for raw in values.values():
            normalized = None if raw is None else str(raw).strip().strip('"\\'').casefold()
            if normalized not in allowed:
                raise SystemExit(23)
    if unit_returncode != 0:
        raise SystemExit(24)
    properties = {}
    for line in unit_stdout.splitlines():
        if not line or '=' not in line:
            raise SystemExit(24)
        key, value = line.split('=', 1)
        properties[key] = value
    if properties != {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead'}:
        raise SystemExit(24)
"""


def _prestate_script(target: Any, merge: str) -> str:
    services = sorted(
        unit.name
        for unit in target.managed_systemd_units
        if unit.enable and unit.name.endswith(".service")
    )
    expected = {
        "merge": merge,
        "target_dir": target.target_dir.rstrip("/"),
        "service": target.service_name,
        "services": services,
        "environment_file": target.environment_file,
        "urls": [
            target.loopback_base_url.rstrip("/") + "/login",
            target.public_base_url.rstrip("/") + "/login",
        ],
        "finance_store": "/opt/wb-core-runtime/state/finance-liquidity/finance-liquidity.sqlite3",
        "finance_dir": "/opt/wb-core-runtime/state/finance-liquidity",
        "finance_unit": "wb-core-finance-liquidity.service",
        "finance_port": 8767,
        "finance_flags": list(FINANCE_FLAGS),
    }
    finance_validator = _finance_off_validator_source()
    return f"""
import hashlib, json, os, subprocess, urllib.request
from pathlib import Path
{finance_validator}
e = {expected!r}
root = Path(e['target_dir'])
runtime_sha = (root / '.wb-core-runtime-sha').read_text(encoding='utf-8').strip()
metadata_raw = (root / '.wb-core-deploy.json').read_bytes()
metadata = json.loads(metadata_raw)
if runtime_sha != e['merge'] or metadata.get('commit') != e['merge']:
    raise SystemExit(20)
pid = subprocess.run(['systemctl','show','--property','MainPID','--value',e['service']], check=True, text=True, capture_output=True).stdout.strip()
if not pid.isdigit() or int(pid) <= 0:
    raise SystemExit(21)
for service in e['services']:
    subprocess.run(['systemctl','is-active','--quiet',service], check=True)
health = {{}}
for url in e['urls']:
    with urllib.request.urlopen(url, timeout=10) as response:
        health[url] = response.status
        if response.status != 200: raise SystemExit(22)
proc_env = {{}}
for item in Path('/proc/' + pid + '/environ').read_bytes().split(b'\\0'):
    if b'=' in item:
        key, value = item.split(b'=', 1); proc_env[key.decode(errors='replace')] = value.decode(errors='replace')
source_env = {{}}
for line in Path(e['environment_file']).read_text(encoding='utf-8').splitlines():
    line=line.strip()
    if line and not line.startswith('#') and '=' in line:
        key,value=line.split('=',1); source_env[key.strip()]=value.strip().strip('"\\'')
flags = {{name: {{'process': proc_env.get(name), 'source': source_env.get(name)}} for name in e['finance_flags']}}
unit = subprocess.run(['systemctl','show','--property=LoadState','--property=ActiveState','--property=SubState',e['finance_unit']], text=True, capture_output=True)
require_finance_off(flags, unit.returncode, unit.stdout)
if Path(e['finance_store']).exists():
    raise SystemExit(25)
if Path(e['finance_dir']).exists():
    raise SystemExit(28)
nginx = subprocess.run(['nginx','-T'], text=True, capture_output=True)
if nginx.returncode != 0 or '/v1/finance/' in nginx.stdout or 'location ^~ /finance/' in nginx.stdout:
    raise SystemExit(26)
listeners = subprocess.run(['ss','-ltn'], check=True, text=True, capture_output=True).stdout
if any(line.split()[3].rsplit(':',1)[-1] == str(e['finance_port']) for line in listeners.splitlines()[1:] if len(line.split()) >= 4):
    raise SystemExit(27)
print(json.dumps({{
  'runtime_sha':runtime_sha,
  'metadata':metadata,
  'metadata_sha256':hashlib.sha256(metadata_raw).hexdigest(),
  'main_pid':int(pid),
  'services':e['services'],
  'health':health,
  'finance':{{'flags':flags,'unit_absent':True,'directory_absent':True,'store_absent':True,'routes_absent':True,'listener_absent':True}},
}}, sort_keys=True))
"""


def collect_prestate(target: Any, merge: str, *, require_incomplete: bool) -> dict[str, Any]:
    state = _run_remote_json(target, _prestate_script(target, merge))
    complete = state.get("metadata", {}).get("deployment_complete")
    if require_incomplete and complete is not False:
        raise RecoveryError("target-marker-not-incomplete")
    if not require_incomplete and complete is not True:
        raise RecoveryError("target-marker-not-complete")
    return state


def preview_fingerprint(payload: Mapping[str, Any]) -> str:
    stable = {
        "schema": payload["schema"],
        "release_run_id": payload["release_run_id"],
        "operation_id": payload["operation_id"],
        "source": payload["source"],
        "runner": payload["runner"],
        "target": payload["target"],
        "failure": payload["failure"],
        "prestate": payload["prestate"],
        "stages": payload["stages"],
        "forbidden_stages": payload["forbidden_stages"],
    }
    return digest(canonical_bytes(stable))


def build_stage_commands(
    target: Any, merge: str, metadata_sha: str, expected_main_pid: int
) -> dict[str, Any]:
    from apps import registry_upload_http_entrypoint_hosted_runtime as hosted

    root_storage = hosted._build_root_storage_policy_commands(target)["status_artifact_readback"]
    if not root_storage or not target.status_command:
        raise RecoveryError("recovery-tail-contract-incomplete")
    status = hosted._remote_shell_command(
        target, f"cd {shlex.quote(target.target_dir)} && {target.status_command}"
    )
    auth = hosted._build_auth_env_preflight_command(target)
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if str(target.runtime_env.get("CHANGE_REGISTRY_OBSERVER_ENABLED") or "").lower() not in {"1", "true", "yes", "on"}:
        raise RecoveryError("change-registry-activation-not-enabled")
    if not runtime_dir or not target.environment_file:
        raise RecoveryError("change-registry-contract-incomplete")
    unit = f"wb-core-change-registry-activation@{merge}.service"
    activation_readback_shell = (
        f"cd {shlex.quote(target.target_dir)} && python3 apps/change_registry_observer.py "
        f"--runtime-dir {shlex.quote(runtime_dir)} --env-file {shlex.quote(target.environment_file)} "
        f"activation-status --deployed-sha {shlex.quote(merge)}"
    )
    activation = hosted._remote_shell_command(
        target, f"set -eu; systemctl start {shlex.quote(unit)}; {activation_readback_shell}"
    )
    activation_readback = hosted._remote_shell_command(target, activation_readback_shell)
    completion_script = f"""
import hashlib, json, os, subprocess
from pathlib import Path
path=Path({str(target.target_dir.rstrip('/') + '/.wb-core-deploy.json')!r})
expected_sha={metadata_sha!r}; expected_commit={merge!r}; expected_pid={int(expected_main_pid)!r}
raw=path.read_bytes()
if hashlib.sha256(raw).hexdigest()!=expected_sha: raise SystemExit(31)
value=json.loads(raw)
if value.get('commit')!=expected_commit or value.get('deployment_complete') is not False: raise SystemExit(32)
runtime_sha=(path.parent/'.wb-core-runtime-sha').read_text(encoding='utf-8').strip()
pid=subprocess.run(['systemctl','show','--property','MainPID','--value',{target.service_name!r}],check=True,text=True,capture_output=True).stdout.strip()
if runtime_sha!=expected_commit or pid!=str(expected_pid): raise SystemExit(34)
before=dict(value); value['deployment_complete']=True
if value.get('deployed_at')!=before.get('deployed_at'): raise SystemExit(33)
new=(json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'))+'\\n').encode()
temp=path.with_name(path.name+'.recovery.tmp')
with temp.open('wb') as handle:
    handle.write(new); handle.flush(); os.fsync(handle.fileno())
os.replace(temp,path)
directory_fd=os.open(str(path.parent),os.O_RDONLY)
try: os.fsync(directory_fd)
finally: os.close(directory_fd)
print(json.dumps({{'before_sha256':expected_sha,'after_sha256':hashlib.sha256(new).hexdigest(),'commit':expected_commit,'deployed_at':value.get('deployed_at')}},sort_keys=True))
"""
    completion = _remote_python_command(target)
    return {
        "root_storage_readback": root_storage,
        "status": status,
        "auth": auth,
        "activation": activation,
        "activation_readback": activation_readback,
        "completion": completion,
        "completion_input": completion_script,
    }


def _run_stage(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )


def _must_succeed(name: str, command: list[str], *, input_text: str | None = None) -> dict[str, Any]:
    result = _run_stage(command, input_text=input_text)
    if result.returncode != 0:
        raise RecoveryError(f"{name}-failed-{result.returncode}")
    return {"stage": name, "stdout_sha256": digest(result.stdout.encode())}


def _publish_once(client: release.GitHub, pr: int, marker: str, payload: Mapping[str, Any]) -> None:
    existing = _matching_comments(client, pr, marker)
    if existing:
        if len(existing) == 1 and canonical_bytes(existing[0]) == canonical_bytes(payload):
            return
        raise RecoveryError("recovery-marker-conflict")
    body = marker + "\n```json\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n```"
    try:
        client.post(f"/issues/{pr}/comments", {"body": body})
    except Exception:
        readback = _matching_comments(client, pr, marker)
        if len(readback) != 1 or canonical_bytes(readback[0]) != canonical_bytes(payload):
            raise RecoveryError("recovery-comment-ambiguous")
    readback = _matching_comments(client, pr, marker)
    if len(readback) != 1 or canonical_bytes(readback[0]) != canonical_bytes(payload):
        raise RecoveryError("recovery-comment-readback-invalid")


def build_preview(client: release.GitHub, release_run_id: int, target: Any) -> dict[str, Any]:
    evidence = collect_evidence(client, release_run_id)
    original = evidence["original_receipt"]
    runner = prove_repo_only_descendant(client, original)
    prestate = collect_prestate(target, original["merge_sha"], require_incomplete=True)
    operation = recovery_operation_id(release_run_id, original)
    result = {
        "schema": RECOVERY_SCHEMA,
        "mode": "preview",
        "state": "reviewable",
        "release_run_id": release_run_id,
        "operation_id": operation,
        "source": {
            "pull_request": original["pull_request"],
            "gate_run_id": original["gate_run_id"],
            "base_sha": original["base_sha"],
            "head_sha": original["head_sha"],
            "merge_sha": original["merge_sha"],
            "original_operation_id": original["operation_id"],
            "original_receipt_sha256": evidence["original_receipt_sha256"],
            "gate_plan_sha256": evidence["gate_plan"]["plan_sha256"],
        },
        "runner": runner,
        "target": {"target_id": target.target_id, "ssh_destination": target.ssh_destination, "target_dir": target.target_dir},
        "failure": evidence["failure"],
        "prestate": prestate,
        "stages": [
            "root-storage-status-artifact-readback",
            "managed-service-status",
            "auth-preflight",
            "change-registry-activation-exact-target",
            "deployment-metadata-cas-complete",
            "final-runtime-services-health-finance-off-readback",
        ],
        "forbidden_stages": ["merge", "rsync", "dependencies", "systemd-install", "restart", "nginx"],
    }
    result["preview_fingerprint"] = preview_fingerprint(result)
    return result


def _receipt_marker(operation: str) -> str:
    return f"<!-- {RECEIPT_MARKER} operation={operation} -->"


def _claim_marker(operation: str) -> str:
    return f"<!-- {CLAIM_MARKER} operation={operation} -->"


def existing_recovery_readback(
    client: release.GitHub,
    release_run_id: int,
    expected_fingerprint: str,
    target: Any,
) -> dict[str, Any] | None:
    """Resolve a prior claim without constructing a new incomplete preview."""

    evidence = collect_evidence(client, release_run_id)
    original = evidence["original_receipt"]
    runner = prove_repo_only_descendant(client, original)
    operation = recovery_operation_id(release_run_id, original)
    pr = int(original["pull_request"])
    receipts = _matching_comments(client, pr, _receipt_marker(operation))
    claims = _matching_comments(client, pr, _claim_marker(operation))
    if receipts:
        if len(receipts) != 1 or receipts[0].get("state") != "complete":
            raise RecoveryError("recovery-receipt-invalid")
        receipt = receipts[0]
        if (
            receipt.get("operation_id") != operation
            or receipt.get("source", {}).get("merge_sha") != original["merge_sha"]
            or receipt.get("preview_fingerprint") != expected_fingerprint
            or len(claims) != 1
        ):
            raise RecoveryError("recovery-receipt-binding-invalid")
        final = collect_prestate(target, original["merge_sha"], require_incomplete=False)
        return {**receipt, "fresh_final_readback": final, "runner_readback": runner}
    if not claims:
        return None
    if len(claims) != 1:
        raise RecoveryError("recovery-claim-count-invalid")
    claim = claims[0]
    if (
        claim.get("state") != "claimed"
        or claim.get("operation_id") != operation
        or claim.get("source", {}).get("merge_sha") != original["merge_sha"]
        or claim.get("preview_fingerprint") != expected_fingerprint
    ):
        raise RecoveryError("recovery-claim-binding-invalid")
    try:
        final = collect_prestate(target, original["merge_sha"], require_incomplete=False)
    except RecoveryError:
        return {
            "schema": RECOVERY_SCHEMA,
            "state": "ambiguous",
            "operation_id": operation,
            "source": claim["source"],
            "preview_fingerprint": claim.get("preview_fingerprint"),
            "reason": "existing-claim-without-complete-readback",
            "runner_readback": runner,
        }
    completed = {
        "schema": RECOVERY_SCHEMA,
        "state": "complete",
        "operation_id": operation,
        "source": claim["source"],
        "preview_fingerprint": claim.get("preview_fingerprint"),
        "final_readback": final,
        "recovered_from_existing_claim": True,
    }
    _publish_once(client, pr, _receipt_marker(operation), completed)
    return completed


def apply_recovery(
    client: release.GitHub,
    preview: Mapping[str, Any],
    expected_fingerprint: str,
    target: Any,
) -> dict[str, Any]:
    if preview.get("preview_fingerprint") != expected_fingerprint:
        raise RecoveryError("preview-fingerprint-mismatch")
    operation = str(preview["operation_id"])
    pr = int(preview["source"]["pull_request"])
    receipt_marker = _receipt_marker(operation)
    claim_marker = _claim_marker(operation)
    if _matching_comments(client, pr, receipt_marker) or _matching_comments(client, pr, claim_marker):
        raise RecoveryError("recovery-identity-already-claimed")

    commands = build_stage_commands(
        target,
        preview["source"]["merge_sha"],
        preview["prestate"]["metadata_sha256"],
        int(preview["prestate"]["main_pid"]),
    )
    # These stages are read-only and precede the durable mutation claim.  A
    # stale artifact or failed service/auth check must not consume the identity.
    stages: list[dict[str, Any]] = [
        _must_succeed("root-storage-readback", commands["root_storage_readback"]),
        _must_succeed("status", commands["status"]),
        _must_succeed("auth", commands["auth"]),
    ]

    claim = {
        "schema": RECOVERY_SCHEMA,
        "state": "claimed",
        "operation_id": operation,
        "source": preview["source"],
        "preview_fingerprint": expected_fingerprint,
        "target": preview["target"],
    }
    _publish_once(client, pr, claim_marker, claim)

    try:
        # Repeat every target and source guard after the durable claim and
        # immediately before the first production mutation.  A changed PID,
        # metadata hash, or main ref invalidates the reviewed preview.
        fresh = collect_prestate(
            target, preview["source"]["merge_sha"], require_incomplete=True
        )
        if canonical_bytes(fresh) != canonical_bytes(preview["prestate"]):
            raise RecoveryError("target-prestate-drift-after-claim")
        fresh_runner = prove_repo_only_descendant(client, preview["source"])
        if canonical_bytes(fresh_runner) != canonical_bytes(preview["runner"]):
            raise RecoveryError("trusted-main-drift-after-claim")
        activation = _run_stage(commands["activation"])
        if activation.returncode != 0:
            readback = _run_stage(commands["activation_readback"])
            if readback.returncode != 0:
                raise RecoveryError(f"activation-ambiguous-{activation.returncode}")
            stages.append({"stage": "activation-readback", "stdout_sha256": digest(readback.stdout.encode())})
        else:
            stages.append({"stage": "activation", "stdout_sha256": digest(activation.stdout.encode())})
        before_completion = collect_prestate(
            target, preview["source"]["merge_sha"], require_incomplete=True
        )
        if canonical_bytes(before_completion) != canonical_bytes(fresh):
            raise RecoveryError("target-drift-before-completion")
        completion_runner = prove_repo_only_descendant(client, preview["source"])
        if canonical_bytes(completion_runner) != canonical_bytes(fresh_runner):
            raise RecoveryError("trusted-main-drift-before-completion")
        completion = _run_stage(commands["completion"], input_text=commands["completion_input"])
        if completion.returncode != 0:
            # Never repeat the CAS write.  Accept only an exact complete target
            # readback, otherwise leave the durable claim unresolved.
            final = collect_prestate(target, preview["source"]["merge_sha"], require_incomplete=False)
            stages.append({"stage": "completion-readback", "stdout_sha256": digest(canonical_bytes(final))})
        else:
            stages.append({"stage": "completion", "stdout_sha256": digest(completion.stdout.encode())})
        final = collect_prestate(target, preview["source"]["merge_sha"], require_incomplete=False)
    except Exception as exc:
        reason = exc.reason if isinstance(exc, RecoveryError) else type(exc).__name__
        return {
            "schema": RECOVERY_SCHEMA,
            "state": "ambiguous",
            "operation_id": operation,
            "source": preview["source"],
            "preview_fingerprint": expected_fingerprint,
            "reason": reason,
            "completed_stages": stages,
        }

    completed = {
        "schema": RECOVERY_SCHEMA,
        "state": "complete",
        "operation_id": operation,
        "source": preview["source"],
        "preview_fingerprint": expected_fingerprint,
        "completed_stages": stages,
        "final_readback": final,
        "recovered_from_existing_claim": False,
    }
    _publish_once(client, pr, receipt_marker, completed)
    return completed


def write_output(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(payload) + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "apply"))
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--release-run-id", required=True, type=int)
    parser.add_argument("--preview-fingerprint", default="")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    if args.command == "apply" and not re.fullmatch(r"[0-9a-f]{64}", args.preview_fingerprint):
        raise SystemExit("--preview-fingerprint is required for apply")
    client = release.GitHub(args.repository, token)
    from apps import registry_upload_http_entrypoint_hosted_runtime as hosted

    target = hosted.load_hosted_runtime_target(TARGET_FILE)
    with tempfile.TemporaryDirectory(prefix="wb-core-release-recovery-") as directory:
        release.configure_ssh(Path(directory))
        if args.command == "preview":
            result = build_preview(client, args.release_run_id, target)
        else:
            existing = existing_recovery_readback(
                client, args.release_run_id, args.preview_fingerprint, target
            )
            if existing is not None:
                result = existing
            else:
                preview = build_preview(client, args.release_run_id, target)
                result = apply_recovery(
                    client, preview, args.preview_fingerprint, target
                )
    write_output(args.output, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {"reviewable", "complete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
