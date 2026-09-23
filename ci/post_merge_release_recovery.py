#!/usr/bin/env python3
"""Fail-closed continuation for a proven post-merge deploy tail.

The runner never merges, copies code, or installs runtime dependencies.  Its
storage tail never restarts services; one exact b9 case resumes the canonical
post-dependency activation stages only after strict receipt, prestate, and
phase evidence.  Recovery code may be newer only when intervening commits are
classified repo-only by the current trusted check map.
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
import textwrap
import zipfile
from collections.abc import Mapping
from enum import Enum
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
REMOTE_DIAGNOSTIC_SCHEMA = "wb-core.release-recovery-remote-diagnostic/v1"
REMOTE_DIAGNOSTIC_STAGES = frozenset(
    {"metadata", "services", "health", "process-env", "pilot-env", "finance", "nginx", "ss"}
)
EXPECTED_SELECTIVE_RUN_ID = 35779532714
EXPECTED_SELECTIVE_ORIGINAL_BASE_SHA = "e5b62aae8ed1a709253d62cf4ff556720414f714"
EXPECTED_SELECTIVE_PREVIOUS_DEPLOYED_SHA = "ae2d7f2f309cc84f0d5b1b9bb9b7be3161bec329"
EXPECTED_SELECTIVE_GATE_RUN_ID = 35779448242
EXPECTED_SELECTIVE_HEAD_SHA = "2aee46a3967e0ae1deaae2ed152d496dff9644c3"
EXPECTED_SELECTIVE_MERGE_SHA = "b9b709805b9eb1da9d417f3b8028dd22e4ccb1d6"


class RecoveryCase(str, Enum):
    STORAGE_TAIL = "storage-tail"
    SELECTIVE_B9_ACTIVATION = "normal-b9-activation-tail"
    REGISTRY_PRECHECK_ACTIVATION = "normal-registry-precheck-activation-tail"


def recovery_case(release_run_id: int) -> RecoveryCase:
    if release_run_id == EXPECTED_SELECTIVE_RUN_ID:
        return RecoveryCase.SELECTIVE_B9_ACTIVATION
    return RecoveryCase.STORAGE_TAIL


def normal_activation_tail_case(case: RecoveryCase) -> bool:
    return case in {
        RecoveryCase.SELECTIVE_B9_ACTIVATION,
        RecoveryCase.REGISTRY_PRECHECK_ACTIVATION,
    }


def _registry_precheck_failure_matches(text: str, gate_run_id: int) -> bool:
    required = (
        "deploy_current_checkout",
        "wb_autoanswers_activation.py prepare-deploy",
        "registry_state_before = unit_state(registry_service)",
        "RuntimeError: systemd quiesce service state is invalid: wb-core-registry-http.service",
        "returned non-zero exit status 1",
        f'--workflow-run-id "{gate_run_id}"',
    )
    return all(item in text for item in required)


REMOTE_DIAGNOSTIC_CATEGORIES = frozenset(
    {
        "system-exit",
        "subprocess",
        "http",
        "url",
        "json",
        "file-not-found",
        "permission",
        "timeout",
        "os",
        "generic",
    }
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


def _validate_original_receipt(value: Mapping[str, Any], *, case: RecoveryCase = RecoveryCase.STORAGE_TAIL) -> dict[str, Any]:
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
    if case is RecoveryCase.SELECTIVE_B9_ACTIVATION and (
        normalized["base_sha"] != EXPECTED_SELECTIVE_ORIGINAL_BASE_SHA
        or normalized["merge_sha"] != EXPECTED_SELECTIVE_MERGE_SHA
    ):
        raise RecoveryError("original-receipt-not-exact-selective-b9")
    return normalized


def _prove_failed_stage(raw_log: bytes, gate_run_id: int, job_name: str, *, case: RecoveryCase = RecoveryCase.STORAGE_TAIL) -> dict[str, Any]:
    text = raw_log.decode("utf-8", errors="replace")
    if case is RecoveryCase.SELECTIVE_B9_ACTIVATION:
        required = ("deploy_current_checkout", "wb_autoanswers_activation.py prepare-deploy",
                    "wb-core-autoanswers-worker.service", "returned non-zero exit status 1",
                    f'--workflow-run-id "{gate_run_id}"')
        if any(item not in text for item in required):
            raise RecoveryError("failed-stage-not-autoanswers-drain-exit1")
        failures = re.findall(r"subprocess\.CalledProcessError: Command .*?returned non-zero exit status (\d+)", text, re.DOTALL)
        if failures != ["1"] or "exit status 255" in text:
            raise RecoveryError("failed-stage-not-definite-single-exit1")
        return {"job_name": job_name, "job_log_sha256": digest(raw_log),
                "stage": "autoanswers-prepare-deploy-drain", "exit_status": 1,
                "unit": "wb-core-autoanswers-worker.service"}
    if case is RecoveryCase.REGISTRY_PRECHECK_ACTIVATION:
        if not _registry_precheck_failure_matches(text, gate_run_id):
            raise RecoveryError("failed-stage-not-registry-precheck-exit1")
        failures = re.findall(r"(?:subprocess\.)?CalledProcessError:.*?returned non-zero exit status (\d+)", text, re.DOTALL)
        if failures != ["1"] or "exit status 255" in text:
            raise RecoveryError("failed-stage-not-definite-single-exit1")
        return {"job_name": job_name, "job_log_sha256": digest(raw_log),
                "stage": "autoanswers-prepare-deploy-registry-precheck", "exit_status": 1,
                "unit": "wb-core-registry-http.service"}
    required = ("deploy_current_checkout", 'run_stage("readback", root_storage_commands["status_artifact_readback"])',
                "apps/root_storage_policy.py", "status-readback", "returned non-zero exit status 3",
                f'--workflow-run-id "{gate_run_id}"')
    if any(item not in text for item in required):
        raise RecoveryError("failed-stage-not-root-storage-readback-exit3")
    failures = re.findall(r"subprocess\.CalledProcessError: Command .*?returned non-zero exit status (\d+)", text, re.DOTALL)
    if failures != ["3"] or "exit status 255" in text:
        raise RecoveryError("failed-stage-not-definite-single-exit3")
    return {"job_name": job_name, "job_log_sha256": digest(raw_log),
            "stage": "root-storage-status-artifact-readback", "exit_status": 3}


def collect_evidence(client: release.GitHub, release_run_id: int) -> dict[str, Any]:
    case = recovery_case(release_run_id)
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
        _json_file(_zip_files(raw_receipt, "original-receipt-artifact"), "release-receipt.json", "original-receipt"), case=case
    )
    gate_id = int(original["gate_run_id"])
    if case is RecoveryCase.SELECTIVE_B9_ACTIVATION and (gate_id != EXPECTED_SELECTIVE_GATE_RUN_ID or original["head_sha"] != EXPECTED_SELECTIVE_HEAD_SHA):
        raise RecoveryError("selective-original-gate-or-head-not-exact")
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
    if case is RecoveryCase.STORAGE_TAIL and _registry_precheck_failure_matches(
        raw_log.decode("utf-8", errors="replace"), gate_id
    ):
        case = RecoveryCase.REGISTRY_PRECHECK_ACTIVATION
    failure = _prove_failed_stage(raw_log, gate_id, str(deployed_job["name"]), case=case)
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
        "recovery_case": case.value,
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
    if current == merge:
        return {
            "trusted_main_sha": current,
            "target_merge_sha": merge,
            "intervening_proof": "exact-target-no-intervening-commit",
            "intervening_paths": [],
            "intervening_plan_sha256": None,
            "intervening_release_kind": "exact_target",
            "intervening_commits": [],
        }
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


def _remote_failure_reason(returncode: int, stderr: str) -> str:
    fallback = f"remote-readback-failed-{returncode}"
    candidate = stderr.strip()
    if not candidate or len(candidate) > 512 or "\n" in candidate:
        return fallback
    try:
        diagnostic = json.loads(candidate)
    except json.JSONDecodeError:
        return fallback
    if not isinstance(diagnostic, dict):
        return fallback
    required = {"schema", "stage", "exception_category"}
    numeric_fields = {"guard_status", "subprocess_returncode", "http_status", "errno"}
    if not required <= diagnostic.keys() or not diagnostic.keys() <= required | numeric_fields:
        return fallback
    if diagnostic["schema"] != REMOTE_DIAGNOSTIC_SCHEMA:
        return fallback
    stage = diagnostic["stage"]
    exception_category = diagnostic["exception_category"]
    if not isinstance(stage, str) or stage not in REMOTE_DIAGNOSTIC_STAGES:
        return fallback
    if not isinstance(exception_category, str) or exception_category not in REMOTE_DIAGNOSTIC_CATEGORIES:
        return fallback
    optional = [name for name in numeric_fields if name in diagnostic]
    if len(optional) > 1:
        return fallback
    suffix = ""
    if optional:
        name = optional[0]
        value = diagnostic[name]
        bounds = {
            "guard_status": (1, 255),
            "subprocess_returncode": (-255, 255),
            "http_status": (100, 599),
            "errno": (1, 4095),
        }
        if isinstance(value, bool) or not isinstance(value, int) or not bounds[name][0] <= value <= bounds[name][1]:
            return fallback
        expected_field = {
            "system-exit": "guard_status",
            "subprocess": "subprocess_returncode",
            "http": "http_status",
            "os": "errno",
            "file-not-found": "errno",
            "permission": "errno",
            "timeout": "errno",
        }.get(exception_category)
        if name != expected_field:
            return fallback
        suffix = f"-{name}-{value}"
    canonical = json.dumps(diagnostic, sort_keys=True, separators=(",", ":"))
    if candidate != canonical:
        return fallback
    return f"{fallback}-stage-{stage}-{exception_category}{suffix}"


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
        raise RecoveryError(_remote_failure_reason(result.returncode, result.stderr))
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RecoveryError("remote-readback-invalid") from exc
    if not isinstance(value, dict):
        raise RecoveryError("remote-readback-invalid")
    return value


def _finance_pilot_validator_source() -> str:
    """Return the exact validator embedded in the remote prestate program."""

    return """
def require_finance_pilot(flags, pilot_returncode, pilot_stdout, legacy_returncode, legacy_stdout):
    if any(
        str(values.get(source) or '').strip().strip('\"\\\'') != '1'
        for values in flags.values()
        for source in ('main_process', 'pilot_process', 'pilot_source')
    ):
        raise SystemExit(23)
    def properties(returncode, stdout, exit_code):
        if returncode != 0:
            raise SystemExit(exit_code)
        result = {}
        for line in stdout.splitlines():
            if not line or '=' not in line:
                raise SystemExit(exit_code)
            key, value = line.split('=', 1)
            result[key] = value
        return result
    pilot = properties(pilot_returncode, pilot_stdout, 24)
    if pilot != {'LoadState': 'loaded', 'UnitFileState': 'enabled', 'ActiveState': 'active', 'SubState': 'running'}:
        raise SystemExit(24)
    legacy = properties(legacy_returncode, legacy_stdout, 30)
    if legacy != {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead'}:
        raise SystemExit(30)
"""


def _prestate_script(target: Any, merge: str) -> str:
    services = sorted(
        unit.name
        for unit in target.managed_systemd_units
        if unit.enable and unit.name.endswith(".service")
    )
    pilot_env = (
        target.target_dir.rstrip("/")
        + "/artifacts/finance_liquidity_cash/pilot/finance-liquidity-pilot.env"
    )
    expected = {
        "merge": merge,
        "target_dir": target.target_dir.rstrip("/"),
        "service": target.service_name,
        "services": services,
        "urls": [
            target.loopback_base_url.rstrip("/") + "/login",
            target.public_base_url.rstrip("/") + "/login",
        ],
        "pilot_store": "/opt/wb-core-runtime/state/finance-liquidity-pilot/finance-liquidity-pilot.sqlite3",
        "pilot_dir": "/opt/wb-core-runtime/state/finance-liquidity-pilot",
        "pilot_unit": "wb-core-finance-liquidity-pilot.service",
        "pilot_env": pilot_env,
        "legacy_store": "/opt/wb-core-runtime/state/finance-liquidity/finance-liquidity.sqlite3",
        "legacy_dir": "/opt/wb-core-runtime/state/finance-liquidity",
        "legacy_unit": "wb-core-finance-liquidity.service",
        "finance_port": 8767,
        "finance_flags": list(FINANCE_FLAGS),
        "pilot_env_values": {
            "FINANCE_LIQUIDITY_ENABLED": "1",
            "FINANCE_LIQUIDITY_READ_ENABLED": "1",
            "FINANCE_LIQUIDITY_WRITE_ENABLED": "1",
            "FINANCE_LIQUIDITY_ORIGIN": "https://api.selleros.pro",
            "FINANCE_LIQUIDITY_ACCESS_CONFIG": pilot_env.rsplit("/", 1)[0]
            + "/finance-liquidity-pilot-access.json",
        },
    }
    finance_validator = _finance_pilot_validator_source()
    body = f"""
root = Path(e['target_dir'])
runtime_sha = (root / '.wb-core-runtime-sha').read_text(encoding='utf-8').strip()
metadata_raw = (root / '.wb-core-deploy.json').read_bytes()
metadata = json.loads(metadata_raw)
if runtime_sha != e['merge'] or metadata.get('commit') != e['merge']:
    raise SystemExit(20)
stage = 'services'
pid = subprocess.run(['systemctl','show','--property','MainPID','--value',e['service']], check=True, text=True, capture_output=True).stdout.strip()
pilot_pid = subprocess.run(['systemctl','show','--property','MainPID','--value',e['pilot_unit']], check=True, text=True, capture_output=True).stdout.strip()
if not pid.isdigit() or int(pid) <= 0 or not pilot_pid.isdigit() or int(pilot_pid) <= 0:
    raise SystemExit(21)
for service in e['services']:
    subprocess.run(['systemctl','is-active','--quiet',service], check=True)
stage = 'health'
health = {{}}
for url in e['urls']:
    with urllib.request.urlopen(url, timeout=10) as response:
        health[url] = response.status
        if response.status != 200: raise SystemExit(22)
stage = 'process-env'
def finance_process_env(process_pid):
    values = {{}}
    for item in Path('/proc/' + process_pid + '/environ').read_bytes().split(b'\\0'):
        if b'=' in item:
            key, value = item.split(b'=', 1)
            decoded_key = key.decode(errors='replace')
            if decoded_key in e['finance_flags']:
                values[decoded_key] = value.decode(errors='replace')
    return values
main_proc_env = finance_process_env(pid)
pilot_proc_env = finance_process_env(pilot_pid)
stage = 'pilot-env'
pilot_source = {{}}
for line in Path(e['pilot_env']).read_text(encoding='utf-8').splitlines():
    line=line.strip()
    if line and not line.startswith('#') and '=' in line:
        key,value=line.split('=',1)
        pilot_source[key.strip()]=value.strip().strip('"\\'')
if pilot_source != e['pilot_env_values']:
    raise SystemExit(29)
stage = 'finance'
flags = {{name: {{'main_process': main_proc_env.get(name), 'pilot_process': pilot_proc_env.get(name), 'pilot_source': pilot_source.get(name)}} for name in e['finance_flags']}}
pilot_unit = subprocess.run(['systemctl','show','--property=LoadState','--property=UnitFileState','--property=ActiveState','--property=SubState',e['pilot_unit']], text=True, capture_output=True)
legacy_unit = subprocess.run(['systemctl','show','--property=LoadState','--property=ActiveState','--property=SubState',e['legacy_unit']], text=True, capture_output=True)
require_finance_pilot(flags, pilot_unit.returncode, pilot_unit.stdout, legacy_unit.returncode, legacy_unit.stdout)
def unit_environment_files(unit):
    unit_text = subprocess.run(['systemctl','cat',unit], check=True, text=True, capture_output=True).stdout
    return [line.strip().split('=', 1)[1].strip().lstrip('-') for line in unit_text.splitlines() if line.strip().startswith('EnvironmentFile=')]
for unit in (e['service'], e['pilot_unit']):
    if unit_environment_files(unit).count(e['pilot_env']) != 1:
        raise SystemExit(31)
pilot_store = Path(e['pilot_store'])
if (pilot_store.is_symlink() or not pilot_store.is_file() or pilot_store.resolve() != pilot_store or pilot_store.parent.is_symlink()):
    raise SystemExit(25)
if Path(e['legacy_store']).exists() or Path(e['legacy_dir']).exists():
    raise SystemExit(28)
stage = 'nginx'
nginx = subprocess.run(['nginx','-T'], text=True, capture_output=True)
if nginx.returncode != 0:
    raise SystemExit(26)
for route in ('/finance/', '/v1/finance/'):
    pattern = (r'location\\s+\\^~\\s+' + re.escape(route) + r'\\s*' + re.escape(chr(123)) + r'[^' + re.escape(chr(125)) + r']*?proxy_pass\\s+http://127\\.0\\.0\\.1:' + str(e['finance_port']) + r';')
    if len(re.findall(pattern, nginx.stdout, re.DOTALL)) != 1:
        raise SystemExit(26)
stage = 'ss'
listeners = subprocess.run(['ss','-ltn'], check=True, text=True, capture_output=True).stdout
bound = [line.split()[3] for line in listeners.splitlines()[1:] if len(line.split()) >= 4 and line.split()[3].rsplit(':',1)[-1] == str(e['finance_port'])]
if bound != ['127.0.0.1:' + str(e['finance_port'])]:
    raise SystemExit(27)
print(json.dumps({{
  'runtime_sha':runtime_sha,
  'metadata':metadata,
  'metadata_sha256':hashlib.sha256(metadata_raw).hexdigest(),
  'main_pid':int(pid),
  'pilot_pid':int(pilot_pid),
  'services':e['services'],
  'health':health,
  'finance_pilot':{{'flags':flags,'unit_active':True,'environment_bound':True,'store_present':True,'routes_bound':True,'loopback_listener':True,'legacy_unisolated_absent':True}},
}}, sort_keys=True))
"""
    return f"""
import hashlib, json, os, re, subprocess, sys, urllib.error, urllib.request
from pathlib import Path
{finance_validator}
e = {expected!r}
stage = 'metadata'
try:
{textwrap.indent(body.strip(), '    ')}
except BaseException as exc:
    if isinstance(exc, SystemExit):
        category = 'system-exit'
    elif isinstance(exc, subprocess.CalledProcessError):
        category = 'subprocess'
    elif isinstance(exc, urllib.error.HTTPError):
        category = 'http'
    elif isinstance(exc, urllib.error.URLError):
        category = 'url'
    elif isinstance(exc, json.JSONDecodeError):
        category = 'json'
    elif isinstance(exc, FileNotFoundError):
        category = 'file-not-found'
    elif isinstance(exc, PermissionError):
        category = 'permission'
    elif isinstance(exc, TimeoutError):
        category = 'timeout'
    elif isinstance(exc, OSError):
        category = 'os'
    else:
        category = 'generic'
    diagnostic = {{'schema': {REMOTE_DIAGNOSTIC_SCHEMA!r}, 'stage': stage, 'exception_category': category}}
    if isinstance(exc, SystemExit) and isinstance(exc.code, int):
        diagnostic['guard_status'] = exc.code
    elif isinstance(exc, subprocess.CalledProcessError):
        diagnostic['subprocess_returncode'] = int(exc.returncode)
    elif isinstance(exc, urllib.error.HTTPError):
        diagnostic['http_status'] = int(exc.code)
    elif isinstance(exc, OSError) and isinstance(exc.errno, int):
        diagnostic['errno'] = exc.errno
    print(json.dumps(diagnostic, sort_keys=True, separators=(',', ':')), file=sys.stderr)
    if isinstance(exc, SystemExit) and isinstance(exc.code, int):
        raise
    raise SystemExit(1)
"""

def _selective_b9_diff_proof() -> dict[str, Any]:
    allowed = {
        "apps/sheet_vitrina_v1_buyout_confirmation_recovery.py", "apps/sheet_vitrina_v1_buyout_confirmation_recovery_smoke.py",
        "apps/sheet_vitrina_v1_buyout_percent_smoke.py", "ci/checks.json", "ci/post_merge_release_recovery.py",
        "ci/post_merge_release_recovery_smoke.py", "docs/modules/08_MODULE__SALES_FUNNEL_HISTORY_BLOCK.md",
        "packages/application/calculation_parameters_v4.py", "packages/application/sheet_vitrina_v1_buyout_percent.py",
    }
    paths = sorted(filter(None, _git(["diff", "--name-only", f"{EXPECTED_SELECTIVE_PREVIOUS_DEPLOYED_SHA}..{EXPECTED_SELECTIVE_MERGE_SHA}"]).stdout.splitlines()))
    if set(paths) != allowed:
        raise RecoveryError("selective-b9-diff-not-exact")
    immutable = ("apps/wb_autoanswers", "packages/application/wb_autoanswers", "packages/adapters/wb_autoanswers",
                 "packages/node/wb_autoanswers", "artifacts/registry_upload_http_entrypoint/systemd/",
                 "apps/registry_upload_http_entrypoint_hosted_runtime.py",
                 "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json",
                 "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json")
    changed = [path for path in paths if path.startswith(immutable)]
    if changed:
        raise RecoveryError("selective-b9-immutable-closure-changed")
    return {"base_sha": EXPECTED_SELECTIVE_PREVIOUS_DEPLOYED_SHA, "merge_sha": EXPECTED_SELECTIVE_MERGE_SHA,
            "paths_sha256": digest(canonical_bytes(paths)), "immutable_paths_changed": changed}


def _selective_live_contract_script(target: Any) -> str:
    # Commands are read-only: file hashes, stable unit metadata, installed versions, and nginx config.
    prefixes = ("apps/wb_autoanswers", "packages/application/wb_autoanswers", "packages/adapters/wb_autoanswers", "packages/node/wb_autoanswers", "artifacts/registry_upload_http_entrypoint/systemd/")
    direct = ("apps/registry_upload_http_entrypoint_hosted_runtime.py", "apps/change_registry_observer.py", "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json", "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json")
    files = sorted(set(filter(None, _git(["ls-tree", "-r", "--name-only", EXPECTED_SELECTIVE_MERGE_SHA, "--", *prefixes, *direct]).stdout.splitlines())) | set(direct))
    expected = {"files": files, "units": [unit.name for unit in target.managed_systemd_units],
                "autoanswers_units": ["wb-core-autoanswers-worker.service", "wb-core-autoanswers-worker.timer", "wb-core-autoanswers-readonly-sync.service", "wb-core-autoanswers-readonly-sync.timer"]}
    blobs = {path: _git(["rev-parse", f"{EXPECTED_SELECTIVE_MERGE_SHA}:{path}"]).stdout.strip() for path in expected["files"]}
    return f'''import hashlib,json,subprocess
from importlib.metadata import version
from pathlib import Path
e={{"target":{target.target_dir!r},"unit_dir":{target.systemd_unit_directory!r},"blobs":{blobs!r},"units":{expected["units"]!r},"autoanswers_units":{expected["autoanswers_units"]!r}}}
def h(path): return subprocess.run(["git","hash-object",str(path)],check=True,text=True,capture_output=True).stdout.strip()
for rel,want in e["blobs"].items():
 p=Path(e["target"])/rel
 if not p.is_file() or h(p)!=want: raise SystemExit(41)
units={{}}
for name in e["units"]:
 p=Path(e["unit_dir"])/name
 rel="artifacts/registry_upload_http_entrypoint/systemd/"+name
 if not p.is_file() or h(p)!=e["blobs"].get(rel): raise SystemExit(42)
 if "@." in name:
  units[name]="template-file-hash-verified"
  continue
 props=subprocess.run(["systemctl","show",name,"--property=LoadState,UnitFileState,FragmentPath,DropInPaths","--no-page"],check=True,text=True,capture_output=True).stdout
 values={{line.split("=",1)[0]:line.split("=",1)[1] for line in props.splitlines() if "=" in line}}
 if name in e["autoanswers_units"] and values.get("DropInPaths", ""): raise SystemExit(44)
 units[name]=props
stable={{}}
expected_states={{"wb-core-autoanswers-worker.service":"static","wb-core-autoanswers-readonly-sync.service":"static","wb-core-autoanswers-worker.timer":"enabled","wb-core-autoanswers-readonly-sync.timer":"enabled"}}
for name in e["autoanswers_units"]:
 values={{line.split("=",1)[0]:line.split("=",1)[1] for line in units[name].splitlines() if "=" in line}}
 if values.get("DropInPaths", "") or values.get("UnitFileState") != expected_states[name]: raise SystemExit(45)
 stable[name]={{"load_state":values.get("LoadState"),"unit_file_state":values.get("UnitFileState"),"fragment_path":values.get("FragmentPath"),"drop_in_paths":values.get("DropInPaths","")}}
versions={{"system":subprocess.run(["python3","-c","from importlib.metadata import version; print(*(version(x) for x in ('apsw','openpyxl','xlrd','playwright','pypdf','reportlab')))"],check=True,text=True,capture_output=True).stdout.strip(),"web_bot":subprocess.run(["/opt/wb-web-bot/venv/bin/python","-c","from importlib.metadata import version; print(version('playwright'),version('psycopg2-binary'))"],check=True,text=True,capture_output=True).stdout.strip(),"wb_ai":subprocess.run(["/opt/wb-ai/venv/bin/python","-c","from importlib.metadata import version; print(*(version(x) for x in ('fastapi','uvicorn','psycopg2-binary','requests')))"],check=True,text=True,capture_output=True).stdout.strip(),"node":subprocess.run(["node","--version"],check=True,text=True,capture_output=True).stdout.strip(),"npm":subprocess.run(["npm","--version"],check=True,text=True,capture_output=True).stdout.strip()}}
expected_versions={{"system":"3.53.4.0 3.1.5 2.0.1 1.58.0 6.4.1 4.4.5","web_bot":"1.58.0 2.9.11","wb_ai":"0.129.1 0.41.0 2.9.11 2.32.5","node":"v22.21.1","npm":"10.9.4"}}
if versions != expected_versions: raise SystemExit(43)
nginx=subprocess.run(["nginx","-T"],check=True,text=True,capture_output=True).stdout.encode()
print(json.dumps({{"installed_unit_contract":units,"stable_autoanswers_units":stable,"dependency_versions":versions,"nginx_sha256":hashlib.sha256(nginx).hexdigest()}},sort_keys=True))'''


def collect_prestate(target: Any, merge: str, *, require_incomplete: bool, case: RecoveryCase = RecoveryCase.STORAGE_TAIL) -> dict[str, Any]:
    state = _run_remote_json(target, _prestate_script(target, merge))
    complete = state.get("metadata", {}).get("deployment_complete")
    if require_incomplete and complete is not False:
        raise RecoveryError("target-marker-not-incomplete")
    if not require_incomplete and complete is not True:
        raise RecoveryError("target-marker-not-complete")
    if case is RecoveryCase.SELECTIVE_B9_ACTIVATION:
        state["selective_live_contract"] = _run_remote_json(target, _selective_live_contract_script(target))
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
        "recovery_case": payload.get("recovery_case"),
        "selective_b9_diff": payload.get("selective_b9_diff"),
        "selective_previous_recovery": payload.get("selective_previous_recovery"),
        "prestate": payload["prestate"],
        "stages": payload["stages"],
        "forbidden_stages": payload["forbidden_stages"],
    }
    return digest(canonical_bytes(stable))


def build_stage_commands(
    target: Any, merge: str, metadata_sha: str, expected_main_pid: int,
    *, case: RecoveryCase = RecoveryCase.STORAGE_TAIL
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
    commands = {
        "root_storage_readback": root_storage,
        "status": status,
        "auth": auth,
        "activation": activation,
        "activation_readback": activation_readback,
        "completion": completion,
        "completion_input": completion_script,
    }
    if normal_activation_tail_case(case):
        if target.service_name != "wb-core-registry-http.service" or target.restart_command != "systemctl restart wb-core-registry-http.service":
            raise RecoveryError("normal-tail-restart-contract-invalid")
        managed = hosted._build_managed_systemd_commands(target)
        storage = hosted._build_root_storage_policy_commands(target)
        nginx = hosted._build_nginx_public_routes_command(target, target_file=TARGET_FILE, dry_run=False)
        required = {"prepare": hosted._build_autoanswers_prepare_deploy_command(target), "install": managed["install"], "daemon_reload": managed["daemon_reload"], "nginx": nginx, "restart": hosted._remote_shell_command(target, f"cd {shlex.quote(target.target_dir)} && {target.restart_command}"), "reconcile": managed["reconcile"], "barrier": managed["preflight"], "storage": storage["status"], "storage_readback": storage["status_artifact_readback"], "status": status, "auth": auth}
        if any(value is None for value in required.values()):
            raise RecoveryError("normal-tail-builder-contract-incomplete")
        commands["normal_activation_tail"] = required
    return commands


def _run_stage(command: list[str], *, input_text: str | None = None, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _must_succeed(name: str, command: list[str], *, input_text: str | None = None) -> dict[str, Any]:
    timeout = 720 if name == "autoanswers-prepare-deploy" else 180 if name in {"systemd-reconcile", "managed-service-status"} else 90
    result = _run_stage(command, input_text=input_text, timeout=timeout)
    if result.returncode != 0:
        raise RecoveryError(f"{name}-failed-{result.returncode}")
    return {"stage": name, "stdout_sha256": digest(result.stdout.encode())}


def _bounded_status_readback(command: list[str], *, sleep: Any = time.sleep) -> dict[str, Any]:
    # Exact hosted status-readback budget: 37 attempts × 5 seconds. Only this
    # read is retried; SSH transport ambiguity never triggers another mutation.
    attempts, retry_seconds = 37, 5.0
    for attempt in range(1, attempts + 1):
        result = _run_stage(command, timeout=180)
        if result.returncode == 0:
            return {"stage": "managed-service-status", "stdout_sha256": digest(result.stdout.encode()), "attempt": attempt}
        if result.returncode == 255:
            raise RecoveryError("managed-service-status-transport-ambiguous")
        if attempt < attempts:
            sleep(retry_seconds)
        else:
            raise RecoveryError(f"managed-service-status-failed-{result.returncode}")
    raise AssertionError("unreachable")

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


EXPECTED_SELECTIVE_PREVIOUS_RECOVERY_RUN_ID = 35779223543


def _selective_previous_recovery_proof(client: release.GitHub) -> dict[str, Any]:
    run = client.get(f"/actions/runs/{EXPECTED_SELECTIVE_PREVIOUS_RECOVERY_RUN_ID}")
    if (run.get("name"), run.get("path"), run.get("event"), run.get("status"), run.get("conclusion")) != (
        "Post-merge Release Recovery", ".github/workflows/release-recovery.yml", "workflow_dispatch", "completed", "success"
    ):
        raise RecoveryError("selective-previous-recovery-run-invalid")
    artifacts = client.get(f"/actions/runs/{EXPECTED_SELECTIVE_PREVIOUS_RECOVERY_RUN_ID}/artifacts?per_page=100")
    candidates = [item for item in (artifacts.get("artifacts") or []) if str(item.get("name") or "").startswith("release-recovery-") and item.get("expired") is not True]
    if len(candidates) != 1:
        raise RecoveryError("selective-previous-recovery-artifact-invalid")
    raw = client.request("GET", f"/actions/artifacts/{int(candidates[0]['id'])}/zip", raw=True)
    receipt = _json_file(_zip_files(raw, "selective-previous-recovery-artifact"), "recovery-receipt.json", "selective-previous-recovery-receipt")
    if receipt.get("schema") != RECOVERY_SCHEMA or receipt.get("state") != "complete" or receipt.get("source", {}).get("merge_sha") != EXPECTED_SELECTIVE_PREVIOUS_DEPLOYED_SHA:
        raise RecoveryError("selective-previous-recovery-receipt-invalid")
    return {"run_id": EXPECTED_SELECTIVE_PREVIOUS_RECOVERY_RUN_ID, "artifact_id": int(candidates[0]["id"]),
            "receipt_sha256": digest(canonical_bytes(receipt)), "source_merge_sha": EXPECTED_SELECTIVE_PREVIOUS_DEPLOYED_SHA}


def build_preview(client: release.GitHub, release_run_id: int, target: Any) -> dict[str, Any]:
    evidence = collect_evidence(client, release_run_id)
    original = evidence["original_receipt"]
    case = RecoveryCase(str(evidence["recovery_case"]))
    runner = prove_repo_only_descendant(client, original)
    prestate = collect_prestate(target, original["merge_sha"], require_incomplete=True, case=case)
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
        "recovery_case": case.value,
        "selective_b9_diff": _selective_b9_diff_proof() if case is RecoveryCase.SELECTIVE_B9_ACTIVATION else None,
        "selective_previous_recovery": _selective_previous_recovery_proof(client) if case is RecoveryCase.SELECTIVE_B9_ACTIVATION else None,
        "prestate": prestate,
        "stages": (["root-storage-status-artifact-readback", "managed-service-status", "auth-preflight", "change-registry-activation-exact-target", "deployment-metadata-cas-complete", "final-runtime-services-health-finance-pilot-readback"] if not normal_activation_tail_case(case) else ["auth-preflight", "root-storage-status", "systemd-barrier-preflight", "autoanswers-prepare-deploy", "systemd-install", "daemon-reload", "nginx", "registry-http-restart", "systemd-reconcile", "root-storage-readback", "managed-service-status", "auth-readback", "change-registry-activation-exact-target", "deployment-metadata-cas-complete", "final-runtime-services-health-finance-pilot-readback"]),
        "forbidden_stages": (["merge", "rsync", "dependencies", "systemd-install", "restart", "nginx"] if not normal_activation_tail_case(case) else ["merge", "rsync", "chown", "dependency-install"]),
    }
    result["preview_fingerprint"] = preview_fingerprint(result)
    return result


def _receipt_marker(operation: str) -> str:
    return f"<!-- {RECEIPT_MARKER} operation={operation} -->"


def _phase_marker(operation: str, phase: str) -> str:
    return f"<!-- wb-core-release-recovery-phase operation={operation} phase={phase} -->"


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
    case = RecoveryCase(str(evidence["recovery_case"]))
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
        final = collect_prestate(target, original["merge_sha"], require_incomplete=False, case=case)
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
        final = collect_prestate(target, original["merge_sha"], require_incomplete=False, case=case)
    except RecoveryError:
        if normal_activation_tail_case(case):
            phases = _matching_comments(client, pr, f"<!-- wb-core-release-recovery-phase operation={operation}")
            reason = (
                "selective-claim-incomplete-readback-only"
                if case is RecoveryCase.SELECTIVE_B9_ACTIVATION
                else "normal-claim-incomplete-readback-only"
            )
            return {"schema": RECOVERY_SCHEMA, "state": "blocked", "operation_id": operation,
                    "source": claim["source"], "preview_fingerprint": claim.get("preview_fingerprint"),
                    "reason": reason, "phase_evidence": phases,
                    "runner_readback": runner}
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
    case = RecoveryCase(str(preview.get("recovery_case") or RecoveryCase.STORAGE_TAIL.value))
    if _matching_comments(client, pr, receipt_marker) or _matching_comments(client, pr, claim_marker):
        raise RecoveryError("recovery-identity-already-claimed")

    commands = build_stage_commands(
        target, preview["source"]["merge_sha"], preview["prestate"]["metadata_sha256"], int(preview["prestate"]["main_pid"]), case=case
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
            target, preview["source"]["merge_sha"], require_incomplete=True, case=case
        )
        if canonical_bytes(fresh) != canonical_bytes(preview["prestate"]):
            raise RecoveryError("target-prestate-drift-after-claim")
        fresh_runner = prove_repo_only_descendant(client, preview["source"])
        if canonical_bytes(fresh_runner) != canonical_bytes(preview["runner"]):
            raise RecoveryError("trusted-main-drift-after-claim")
        if normal_activation_tail_case(case):
            if case is RecoveryCase.SELECTIVE_B9_ACTIVATION and canonical_bytes(
                _selective_b9_diff_proof()
            ) != canonical_bytes(preview["selective_b9_diff"]):
                raise RecoveryError("normal-tail-diff-drift-after-claim")
            tail = commands["normal_activation_tail"]
            phases = (("auth-preflight", tail["auth"]), ("root-storage-status", tail["storage"]), ("systemd-barrier-preflight", tail["barrier"]), ("autoanswers-prepare-deploy", tail["prepare"]), ("systemd-install", tail["install"]), ("daemon-reload", tail["daemon_reload"]), ("nginx", tail["nginx"]), ("registry-http-restart", tail["restart"]), ("systemd-reconcile", tail["reconcile"]), ("root-storage-status", tail["storage"]), ("root-storage-readback", tail["storage_readback"]), ("managed-service-status", tail["status"]), ("auth-readback", tail["auth"]))
            for ordinal, (phase, command) in enumerate(phases, start=1):
                phase_id = f"{ordinal:02d}-{phase}"
                marker = _phase_marker(operation, "before-" + phase_id)
                if _matching_comments(client, pr, marker):
                    raise RecoveryError("normal-tail-phase-already-recorded")
                _publish_once(client, pr, marker, {"schema": RECOVERY_SCHEMA, "state": "before", "operation_id": operation, "phase": phase_id, "preview_fingerprint": expected_fingerprint})
                stages.append(_bounded_status_readback(command) if phase == "managed-service-status" else _must_succeed(phase, command))
                _publish_once(client, pr, _phase_marker(operation, "after-" + phase_id), {"schema": RECOVERY_SCHEMA, "state": "after", "operation_id": operation, "phase": phase_id, "preview_fingerprint": expected_fingerprint})
            restarted = collect_prestate(target, preview["source"]["merge_sha"], require_incomplete=True, case=case)
            unchanged = dict(fresh); after = dict(restarted)
            for key in ("main_pid", "pilot_pid"):
                unchanged.pop(key, None); after.pop(key, None)
            if canonical_bytes(unchanged) != canonical_bytes(after) or int(restarted.get("main_pid") or 0) <= 0 or int(restarted["main_pid"]) == int(fresh["main_pid"]):
                raise RecoveryError("normal-tail-post-restart-drift")
            fresh = restarted
            commands = build_stage_commands(target, preview["source"]["merge_sha"], fresh["metadata_sha256"], int(fresh["main_pid"]), case=case)
        activation = _run_stage(commands["activation"])
        if activation.returncode != 0:
            readback = _run_stage(commands["activation_readback"])
            if readback.returncode != 0:
                raise RecoveryError(f"activation-ambiguous-{activation.returncode}")
            stages.append({"stage": "activation-readback", "stdout_sha256": digest(readback.stdout.encode())})
        else:
            stages.append({"stage": "activation", "stdout_sha256": digest(activation.stdout.encode())})
        before_completion = collect_prestate(
            target, preview["source"]["merge_sha"], require_incomplete=True, case=case
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
            final = collect_prestate(target, preview["source"]["merge_sha"], require_incomplete=False, case=case)
            stages.append({"stage": "completion-readback", "stdout_sha256": digest(canonical_bytes(final))})
        else:
            stages.append({"stage": "completion", "stdout_sha256": digest(completion.stdout.encode())})
        final = collect_prestate(target, preview["source"]["merge_sha"], require_incomplete=False, case=case)
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
