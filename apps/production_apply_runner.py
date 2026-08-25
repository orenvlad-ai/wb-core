#!/usr/bin/env python3
"""One-submit production apply from a durable task-scoped authorization."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Mapping
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.github_release_runner import (  # noqa: E402
    GitHubClient,
    RECEIPT_MARKER,
    canonical_json_bytes,
    configure_deploy_environment,
    exact_sha,
    is_actions_bot_comment,
    list_comments,
)
from apps.release_protocol import (  # noqa: E402
    CANONICAL_PRODUCTION_TARGET_ID,
    CANONICAL_REPOSITORY,
    validate_production_manifest,
)


APPLY_RECEIPT_SCHEMA = "wb-core.production-apply-receipt/v3"
APPLY_MARKER = "wb-core-production-apply-receipt"
GOAL_PROFILE = "inventory-history-backfill"
MAX_QUALIFICATION_CANDIDATES = 4
RECOVERY_WORKFLOW_NAME = "Production Apply Runner"
RECOVERY_WORKFLOW_PATH = ".github/workflows/production-apply.yml"
RECOVERY_ARTIFACT_FILE = "production-apply-receipt.json"
MAX_RECOVERY_ARTIFACT_BYTES = 262_144
TARGET_FILE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "hosted_runtime_target__europe_api.json"
)
AUTH_RE = re.compile(
    r"^/wb-core authorize-goal-v1 task (?P<task>WBC[0-9]{4}) "
    r"profile (?P<profile>[a-z0-9-]{1,80}) "
    r"target (?P<target>[A-Za-z0-9._:-]{1,160}) "
    r"dates (?P<date_from>[0-9]{4}-[0-9]{2}-[0-9]{2})\.\."
    r"(?P<date_to>[0-9]{4}-[0-9]{2}-[0-9]{2}) "
    r"captures (?P<captures>[1-9][0-9]*) "
    r"components (?P<components>[1-9][0-9]*) "
    r"finalizations (?P<finalizations>[1-9][0-9]*) "
    r"full-days (?P<full_days>[0-9]+) partial-days (?P<partial_days>[0-9]+)$"
)
LEGACY_AUTH_RE = re.compile(
    r"^/wb-core apply-v2 pr (?P<pr>[1-9][0-9]*) merge (?P<merge>[0-9a-f]{40}) "
    r"deployed (?P<deployed>[0-9a-f]{40}) manifest sha256:(?P<manifest>[0-9a-f]{64}) "
    r"operation (?P<operation>[A-Za-z0-9._:-]{1,160})$"
)


class ApplyError(RuntimeError):
    pass


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def marker(operation: str) -> str:
    return f"<!-- {APPLY_MARKER} operation={operation} -->"


def parse_release_receipt(
    comments: list[Mapping[str, Any]],
    *,
    pr: int,
    release_operation: str,
    merge_sha: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if (
            f"<!-- {RECEIPT_MARKER} operation={release_operation} -->" not in body
            or "```json" not in body
            or not is_actions_bot_comment(comment)
        ):
            continue
        try:
            payload_text = body.split("```json", 1)[1].split("```", 1)[0]
            payload = json.loads(payload_text)
        except (IndexError, json.JSONDecodeError):
            continue
        if (
            payload.get("state") == "done"
            and payload.get("operation_id") == release_operation
            and payload.get("pull_request") == pr
            and payload.get("merge_sha") == merge_sha
            and payload.get("deployed_sha") == merge_sha
            and payload.get("release_kind") == "live_runtime"
        ):
            matches.append(payload)
    if len(matches) != 1:
        raise ApplyError("exact live-runtime release receipt is missing or ambiguous")
    return matches[0]


def parse_legacy_release_receipt(
    comments: list[Mapping[str, Any]],
    *,
    merge_sha: str,
    manifest_sha: str,
    operation: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if (
            f"<!-- {RECEIPT_MARKER} " not in body
            or "```json" not in body
            or not is_actions_bot_comment(comment)
        ):
            continue
        try:
            payload = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
        except (IndexError, json.JSONDecodeError):
            continue
        manifest = payload.get("manifest")
        if (
            payload.get("state") == "awaiting_apply"
            and payload.get("operation_id") == operation
            and payload.get("merge_sha") == merge_sha
            and isinstance(manifest, Mapping)
            and manifest.get("sha256") == manifest_sha
            and manifest.get("operation_id") == operation
        ):
            matches.append(payload)
    if len(matches) != 1:
        raise ApplyError("awaiting-apply receipt is missing or ambiguous")
    return matches[0]


def validate_legacy_authorization(
    comment: Mapping[str, Any],
    *,
    pr: int,
    merge_sha: str,
    deployed_sha: str,
    manifest_sha: str,
    operation: str,
) -> None:
    association = str(comment.get("author_association") or "").upper()
    if association not in {"OWNER", "MEMBER"}:
        raise ApplyError("apply authorization association is not OWNER or MEMBER")
    match = LEGACY_AUTH_RE.fullmatch(str(comment.get("body") or "").strip())
    if match is None:
        raise ApplyError("apply authorization body is not exact protocol-v2 syntax")
    expected = {
        "pr": str(pr),
        "merge": merge_sha,
        "deployed": deployed_sha,
        "manifest": manifest_sha,
        "operation": operation,
    }
    if match.groupdict() != expected:
        raise ApplyError("apply authorization binding mismatch")


def validate_authorization(
    comment: Mapping[str, Any],
    *,
    repository: str,
    pr: int,
) -> dict[str, Any]:
    association = str(comment.get("author_association") or "").upper()
    if association not in {"OWNER", "MEMBER"}:
        raise ApplyError("task authorization association is not OWNER or MEMBER")
    issue_url = str(comment.get("issue_url") or "")
    expected_suffix = f"/repos/{repository}/issues/{pr}"
    if not issue_url.endswith(expected_suffix):
        raise ApplyError("task authorization is not attached to the exact pull request")
    match = AUTH_RE.fullmatch(str(comment.get("body") or "").strip())
    if match is None:
        raise ApplyError("task authorization body is not exact goal-v1 syntax")
    raw = match.groupdict()
    if raw["profile"] != GOAL_PROFILE:
        raise ApplyError("task authorization profile is unsupported")
    if raw["target"] != CANONICAL_PRODUCTION_TARGET_ID:
        raise ApplyError("task authorization target is not canonical production")
    date_from = date.fromisoformat(raw["date_from"])
    date_to = date.fromisoformat(raw["date_to"])
    date_count = (date_to - date_from).days + 1
    if date_count <= 0 or date_count > 730:
        raise ApplyError("task authorization date scope is invalid")
    goal: dict[str, Any] = {
        "contract": "wb-core.production-goal-passport/v1",
        "task": raw["task"],
        "profile": raw["profile"],
        "target_id": raw["target"],
        "date_from": raw["date_from"],
        "date_to": raw["date_to"],
        "date_count": date_count,
        "expected_inserted_capture_count": int(raw["captures"]),
        "expected_inserted_component_count": int(raw["components"]),
        "expected_inserted_finalization_count": int(raw["finalizations"]),
        "expected_full_date_count": int(raw["full_days"]),
        "expected_partial_date_count": int(raw["partial_days"]),
        "max_mutation_submits": 1,
        "max_pre_submit_regenerations": MAX_QUALIFICATION_CANDIDATES - 1,
        "reversible": True,
    }
    if goal["expected_inserted_capture_count"] != date_count:
        raise ApplyError("task authorization capture count does not match date scope")
    if goal["expected_inserted_finalization_count"] != date_count:
        raise ApplyError("task authorization finalization count does not match date scope")
    if (
        goal["expected_full_date_count"] + goal["expected_partial_date_count"]
        != date_count
    ):
        raise ApplyError("task authorization quality partition does not match date scope")
    if goal["expected_inserted_component_count"] < date_count:
        raise ApplyError("task authorization component bound is invalid")
    return goal


def operation_id(repository: str, pr: int, comment_id: int, goal: Mapping[str, Any]) -> str:
    material = canonical_json_bytes(
        {
            "repository": repository,
            "pull_request": pr,
            "authorization_comment_id": comment_id,
            "goal": goal,
        }
    )
    return "production-goal-v1-" + digest(material)[:32]


def _canonical_target() -> dict[str, Any]:
    payload = json.loads(TARGET_FILE.read_text(encoding="utf-8"))
    expected = {
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
        "target_status": "active",
        "target_role": "primary_live",
        "target_lifecycle": "current_live",
        "ssh_destination": "wb-core-eu-root",
        "target_dir": "/opt/wb-core-runtime/app",
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ApplyError(f"canonical production target mismatch: {field}")
    return payload


def _ssh_command() -> list[str]:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=20",
    ]
    identity = os.environ.get("WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE", "").strip()
    if identity:
        command.extend(["-i", identity])
    options = os.environ.get("WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS", "").strip()
    if options:
        command.extend(shlex.split(options))
    return command


def _remote_command(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    goal: Mapping[str, Any],
    evidence_dir: str,
    mode: str,
    manifest_path: str = "",
    manifest_sha256: str = "",
    approval_reference: str = "",
) -> list[str]:
    if mode not in {"dry-run", "apply", "readback"}:
        raise ApplyError("unsupported remote production-goal mode")
    target_dir = str(target["target_dir"])
    parts = [
        "python3",
        f"{target_dir}/apps/sheet_vitrina_v1_inventory_history_backfill.py",
        "--runtime-dir",
        "/opt/wb-core-runtime/state",
        "--evidence-dir",
        evidence_dir,
        "--deployed-sha",
        merge_sha,
        "--deployed-sha-file",
        f"{target_dir}/.wb-core-runtime-sha",
        "--date-from",
        str(goal["date_from"]),
        "--date-to",
        str(goal["date_to"]),
    ]
    if mode in {"apply", "readback"}:
        normalized_manifest_path = posixpath.normpath(manifest_path)
        normalized_evidence_dir = posixpath.normpath(evidence_dir)
        if (
            normalized_manifest_path != manifest_path
            or posixpath.dirname(normalized_manifest_path)
            != normalized_evidence_dir
            or re.fullmatch(
                r"inventory-history-backfill-plan-[0-9]{8}T[0-9]{6}Z\.json",
                posixpath.basename(normalized_manifest_path),
            )
            is None
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha256)
        ):
            raise ApplyError("remote manifest binding escapes authorized evidence scope")
        parts.extend(
            [
                "--manifest",
                manifest_path,
                "--manifest-sha256",
                manifest_sha256,
            ]
        )
    if mode == "apply":
        if not approval_reference or len(approval_reference) > 500:
            raise ApplyError("task authorization reference is invalid")
        parts.extend(["--apply", "--approval-reference", approval_reference])
    elif mode == "readback":
        parts.append("--readback")
    evidence_setup = (
        "install -d -m 0700 " + shlex.quote(evidence_dir)
        if mode == "dry-run"
        else "test -d "
        + shlex.quote(evidence_dir)
        + " && test \"$(stat -c %a "
        + shlex.quote(evidence_dir)
        + ")\" = 700"
    )
    shell = (
        "set -eu; umask 077; "
        + evidence_setup
        + "; cd "
        + shlex.quote(target_dir)
        + "; "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return _ssh_command() + [str(target["ssh_destination"]), shell]


def command_evidence(command: list[str], *, timeout_seconds: float = 3600.0) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "command_sha256": digest(canonical_json_bytes(command)),
            "return_code": None,
            "transport_ambiguous": True,
            "error": type(exc).__name__,
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    return {
        "command_sha256": digest(canonical_json_bytes(command)),
        "return_code": result.returncode,
        "stdout_sha256": digest(result.stdout.encode("utf-8")),
        "stderr_sha256": digest(result.stderr.encode("utf-8")),
        "transport_ambiguous": False,
        "result": payload if isinstance(payload, Mapping) else None,
    }


def _validate_candidate(payload: Mapping[str, Any], goal: Mapping[str, Any]) -> None:
    expected = {
        "status": "ready",
        "date_from": goal["date_from"],
        "date_to": goal["date_to"],
        "date_count": goal["date_count"],
        "inserted_capture_count": goal["expected_inserted_capture_count"],
        "inserted_component_count": goal["expected_inserted_component_count"],
        "inserted_finalization_count": goal["expected_inserted_finalization_count"],
        "full_date_count": goal["expected_full_date_count"],
        "partial_date_count": goal["expected_partial_date_count"],
        "unavailable_date_count": 0,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ApplyError(f"dynamic manifest escaped authorized goal: {field}")
    if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("deployed_sha") or "")):
        raise ApplyError("dynamic manifest deployed SHA is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get("manifest_sha256") or "")):
        raise ApplyError("dynamic manifest digest is invalid")
    if not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        str(payload.get("material_qualification_digest") or ""),
    ):
        raise ApplyError("dynamic material qualification digest is invalid")


def run_dynamic_goal(
    *,
    target: Mapping[str, Any],
    merge_sha: str,
    goal: Mapping[str, Any],
    operation: str,
    approval_reference: str,
) -> dict[str, Any]:
    evidence_dir = f"/opt/wb-core-runtime/state/private-evidence/production-goals/{operation}"
    attempts: list[dict[str, Any]] = []
    previous_material_digest = ""
    candidate: Mapping[str, Any] | None = None
    for attempt in range(1, MAX_QUALIFICATION_CANDIDATES + 1):
        evidence = command_evidence(
            _remote_command(
                target=target,
                merge_sha=merge_sha,
                goal=goal,
                evidence_dir=evidence_dir,
                mode="dry-run",
            )
        )
        payload = evidence.get("result")
        if evidence.get("return_code") != 0 or not isinstance(payload, Mapping):
            return {
                "state": "blocked",
                "reason": "jit-material-preflight-failed",
                "apply_count": 0,
                "qualification_attempts": [*attempts, evidence],
            }
        try:
            _validate_candidate(payload, goal)
        except ApplyError as exc:
            return {
                "state": "blocked",
                "reason": str(exc),
                "apply_count": 0,
                "qualification_attempts": [*attempts, evidence],
            }
        if payload.get("deployed_sha") != merge_sha:
            raise ApplyError("dynamic manifest is not bound to exact deployed merge SHA")
        attempts.append(
            {
                **{key: value for key, value in evidence.items() if key != "result"},
                "attempt": attempt,
                "manifest_path": payload["manifest_path"],
                "manifest_sha256": payload["manifest_sha256"],
                "material_qualification_digest": payload[
                    "material_qualification_digest"
                ],
                "source_watermarks_digest": payload["source_watermarks_digest"],
                "target_history_digest": payload["target_history_digest"],
                "qualification_state": "candidate",
            }
        )
        current_material_digest = str(payload["material_qualification_digest"])
        if current_material_digest == previous_material_digest:
            attempts[-2]["qualification_state"] = "matching_witness"
            attempts[-1]["qualification_state"] = "qualified"
            candidate = payload
            break
        if len(attempts) > 1:
            attempts[-2]["qualification_state"] = "superseded_material_drift"
        previous_material_digest = current_material_digest
        if attempt < MAX_QUALIFICATION_CANDIDATES:
            time.sleep(1.1)
    if candidate is None:
        if attempts:
            attempts[-1]["qualification_state"] = "unstable_at_bound"
        return {
            "state": "blocked",
            "reason": "material-cas-did-not-survive-bounded-qualification",
            "apply_count": 0,
            "qualification_attempts": attempts,
        }

    manifest_path = str(candidate["manifest_path"])
    manifest_sha256 = str(candidate["manifest_sha256"])
    try:
        apply_command = _remote_command(
            target=target,
            merge_sha=merge_sha,
            goal=goal,
            evidence_dir=evidence_dir,
            mode="apply",
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            approval_reference=approval_reference,
        )
    except ApplyError as exc:
        return {
            "state": "blocked",
            "reason": str(exc),
            "apply_count": 0,
            "qualification_attempts": attempts,
        }
    apply_evidence = command_evidence(apply_command)
    # This is the single mutation submit boundary. It is never repeated,
    # including after a nonzero exit or ambiguous SSH transport.
    readback_evidence = command_evidence(
        _remote_command(
            target=target,
            merge_sha=merge_sha,
            goal=goal,
            evidence_dir=evidence_dir,
            mode="readback",
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
        )
    )
    readback = readback_evidence.get("result")
    reconciled = (
        readback_evidence.get("return_code") == 0
        and isinstance(readback, Mapping)
        and readback.get("status") == "reconciled"
        and readback.get("query_only") is True
        and readback.get("inserted_capture_count")
        == goal["expected_inserted_capture_count"]
        and readback.get("inserted_component_count")
        == goal["expected_inserted_component_count"]
        and readback.get("inserted_finalization_count")
        == goal["expected_inserted_finalization_count"]
        and readback.get("visible_history_date_count") == goal["date_count"]
        and readback.get("visible_history_quality")
        == {
            "full": goal["expected_full_date_count"],
            "partial": goal["expected_partial_date_count"],
            "unavailable": 0,
        }
        and readback.get("exact_manifest_apply_receipt_count") == 1
        and readback.get("total_inventory_history_apply_receipt_count") == 1
        and readback.get("non_target_preserved") is True
    )
    return {
        "state": "done" if reconciled else "blocked",
        "reason": "reconciled" if reconciled else "post-submit-readback-not-reconciled",
        "apply_count": 1,
        "qualification_attempts": attempts,
        "qualified_manifest": {
            "path": manifest_path,
            "sha256": manifest_sha256,
            "material_qualification_digest": candidate[
                "material_qualification_digest"
            ],
        },
        "apply": apply_evidence,
        "readback": readback_evidence,
    }


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(receipt) + b"\n")


def _recovery_artifact_name(pr: int, run_id: int) -> str:
    return f"production-apply-receipt-pr-{pr}-run-{run_id}"


def _extract_recovery_receipt(raw_zip: bytes, expected_sha256: str) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if len(files) != 1 or files[0].filename != RECOVERY_ARTIFACT_FILE:
                raise ApplyError("recovery artifact shape is invalid")
            if files[0].file_size <= 0 or files[0].file_size > MAX_RECOVERY_ARTIFACT_BYTES:
                raise ApplyError("recovery receipt file size is invalid")
            raw_receipt = archive.read(files[0])
    except zipfile.BadZipFile as exc:
        raise ApplyError("recovery artifact ZIP is invalid") from exc
    if digest(raw_receipt) != expected_sha256:
        raise ApplyError("recovery receipt digest mismatch")
    try:
        receipt = json.loads(raw_receipt.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplyError("recovery receipt JSON is invalid") from exc
    if not isinstance(receipt, dict):
        raise ApplyError("recovery receipt shape is invalid")
    if raw_receipt != canonical_json_bytes(receipt) + b"\n":
        raise ApplyError("recovery receipt bytes are not canonical")
    return receipt


def _collect_recovery_receipt(
    client: GitHubClient,
    *,
    pr: int,
    run_id: int,
    artifact_name: str,
    receipt_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_name = _recovery_artifact_name(pr, run_id)
    if artifact_name != expected_name:
        raise ApplyError("recovery artifact name binding mismatch")
    run = client.get(f"/actions/runs/{run_id}")
    if not isinstance(run, Mapping):
        raise ApplyError("recovery source run shape is invalid")
    repository = run.get("repository")
    expected_run = {
        "id": run_id,
        "name": RECOVERY_WORKFLOW_NAME,
        "path": RECOVERY_WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "failure",
        "head_branch": "main",
    }
    for field, value in expected_run.items():
        if run.get(field) != value:
            raise ApplyError(f"recovery source run binding mismatch: {field}")
    if not isinstance(repository, Mapping) or repository.get("full_name") != client.repository:
        raise ApplyError("recovery source run repository mismatch")
    run_head = exact_sha(run.get("head_sha"), "recovery-run-head")
    matches: list[Mapping[str, Any]] = []
    for page in range(1, 11):
        payload = client.get(
            f"/actions/runs/{run_id}/artifacts?per_page=100&page={page}"
        )
        values = payload.get("artifacts") if isinstance(payload, Mapping) else None
        if not isinstance(values, list):
            raise ApplyError("recovery artifact listing shape is invalid")
        matches.extend(
            item
            for item in values
            if isinstance(item, Mapping) and item.get("name") == artifact_name
        )
        if len(values) < 100:
            break
    else:
        raise ApplyError("recovery artifact pagination bound exceeded")
    if len(matches) != 1:
        raise ApplyError("recovery artifact is missing or ambiguous")
    artifact = matches[0]
    artifact_run = artifact.get("workflow_run")
    if (
        artifact.get("expired") is True
        or not isinstance(artifact.get("id"), int)
        or not isinstance(artifact.get("size_in_bytes"), int)
        or int(artifact["size_in_bytes"]) <= 0
        or int(artifact["size_in_bytes"]) > MAX_RECOVERY_ARTIFACT_BYTES
        or not isinstance(artifact_run, Mapping)
        or artifact_run.get("id") != run_id
        or artifact_run.get("head_branch") != "main"
        or artifact_run.get("head_sha") != run_head
    ):
        raise ApplyError("recovery artifact provenance mismatch")
    raw_zip = client.request(
        "GET",
        f"/actions/artifacts/{int(artifact['id'])}/zip",
        accept="application/vnd.github+json",
        raw=True,
    )
    if not isinstance(raw_zip, bytes):
        raise ApplyError("recovery artifact download shape is invalid")
    return dict(run), _extract_recovery_receipt(raw_zip, receipt_sha256)


def _validate_recovery_receipt(
    receipt: Mapping[str, Any],
    *,
    repository: str,
    pr: int,
    merge_sha: str,
    run_head_sha: str,
    authorization_comment_id: int,
    expected_operation: str,
    goal: Mapping[str, Any],
) -> None:
    expected = {
        "schema": APPLY_RECEIPT_SCHEMA,
        "state": "done",
        "operation_id": expected_operation,
        "repository": repository,
        "pull_request": pr,
        "merge_sha": merge_sha,
        "deployed_sha": merge_sha,
        "authorization_comment_id": authorization_comment_id,
        "apply_count": 1,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ApplyError(f"recovery receipt binding mismatch: {field}")
    if merge_sha != run_head_sha:
        raise ApplyError("recovery source run head is not the exact merge SHA")
    if receipt.get("goal") != dict(goal):
        raise ApplyError("recovery receipt goal binding mismatch")
    derived_operation = operation_id(
        repository,
        pr,
        authorization_comment_id,
        goal,
    )
    if derived_operation != expected_operation:
        raise ApplyError("recovery operation derivation mismatch")
    release_operation = str(receipt.get("release_operation_id") or "")
    if re.fullmatch(r"release-v2-[0-9a-f]{32}", release_operation) is None:
        raise ApplyError("recovery release operation id is invalid")
    evidence = receipt.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ApplyError("recovery receipt evidence is missing")
    expected_evidence = {
        "state": "done",
        "reason": "reconciled",
        "apply_count": 1,
    }
    for field, value in expected_evidence.items():
        if evidence.get(field) != value:
            raise ApplyError(f"recovery receipt evidence mismatch: {field}")
    qualified = evidence.get("qualified_manifest")
    apply_evidence = evidence.get("apply")
    readback_evidence = evidence.get("readback")
    if not all(
        isinstance(item, Mapping)
        for item in (qualified, apply_evidence, readback_evidence)
    ):
        raise ApplyError("recovery receipt terminal evidence is incomplete")
    readback = readback_evidence.get("result")
    if (
        readback_evidence.get("return_code") != 0
        or readback_evidence.get("transport_ambiguous") is not False
        or not isinstance(readback, Mapping)
        or readback.get("status") != "reconciled"
        or readback.get("mode") != "query-only-readback"
        or readback.get("query_only") is not True
        or readback.get("database_written") is not False
        or readback.get("deployed_sha") != merge_sha
        or readback.get("inserted_capture_count")
        != goal["expected_inserted_capture_count"]
        or readback.get("inserted_component_count")
        != goal["expected_inserted_component_count"]
        or readback.get("inserted_finalization_count")
        != goal["expected_inserted_finalization_count"]
        or readback.get("visible_history_date_count") != goal["date_count"]
        or readback.get("visible_history_quality")
        != {
            "full": goal["expected_full_date_count"],
            "partial": goal["expected_partial_date_count"],
            "unavailable": 0,
        }
        or readback.get("exact_manifest_apply_receipt_count") != 1
        or readback.get("total_inventory_history_apply_receipt_count") != 1
        or readback.get("non_target_preserved") is not True
    ):
        raise ApplyError("recovery receipt readback is not exact reconciled proof")
    manifest_sha = qualified.get("sha256")
    apply_result = apply_evidence.get("result")
    if (
        not isinstance(manifest_sha, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha) is None
        or not isinstance(apply_result, Mapping)
        or apply_result.get("manifest_sha256") != manifest_sha
        or readback.get("manifest_sha256") != manifest_sha
        or apply_result.get("status") != "reconciled"
        or apply_result.get("database_written") is not True
        or apply_result.get("non_target_preserved") is not True
    ):
        raise ApplyError("recovery receipt apply evidence is inconsistent")


def _comment_payload(comment: Mapping[str, Any], operation: str) -> dict[str, Any]:
    body = str(comment.get("body") or "")
    if body.count(marker(operation)) != 1 or body.count("```json") != 1:
        raise ApplyError("existing recovery comment shape is ambiguous")
    try:
        payload_text = body.split("```json", 1)[1].split("```", 1)[0]
        payload = json.loads(payload_text)
    except (IndexError, json.JSONDecodeError) as exc:
        raise ApplyError("existing recovery comment JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ApplyError("existing recovery comment payload is invalid")
    return payload


def _recovery_comment_body(
    receipt: Mapping[str, Any],
    *,
    run_id: int,
    artifact_name: str,
    receipt_sha256: str,
) -> str:
    operation = str(receipt["operation_id"])
    return (
        marker(operation)
        + "\nRecovered immutable task-scoped production apply receipt; no production command was executed."
        + f"\nSource Actions run: {run_id}; artifact: `{artifact_name}`; "
        + f"receipt SHA-256: `{receipt_sha256}`."
        + "\n```json\n"
        + json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n```"
    )


def _run_receipt_recovery(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> int:
    if args.source_run_id is None or args.source_run_id <= 0:
        raise ApplyError("receipt-recovery mode requires a positive source run id")
    if not args.source_artifact_name:
        raise ApplyError("receipt-recovery mode requires a source artifact name")
    if re.fullmatch(r"[0-9a-f]{64}", str(args.source_receipt_sha256 or "")) is None:
        raise ApplyError("receipt-recovery mode requires an exact receipt SHA-256")
    if not args.operation_id:
        raise ApplyError("receipt-recovery mode requires an exact operation id")
    run, receipt = _collect_recovery_receipt(
        client,
        pr=args.pr,
        run_id=args.source_run_id,
        artifact_name=args.source_artifact_name,
        receipt_sha256=args.source_receipt_sha256,
    )
    merge_sha = exact_sha(pr.get("merge_commit_sha"), "pr-merge")
    authorization = client.get(f"/issues/comments/{args.authorization_comment_id}")
    goal = validate_authorization(
        authorization,
        repository=args.repository,
        pr=args.pr,
    )
    _validate_recovery_receipt(
        receipt,
        repository=args.repository,
        pr=args.pr,
        merge_sha=merge_sha,
        run_head_sha=exact_sha(run.get("head_sha"), "recovery-run-head"),
        authorization_comment_id=args.authorization_comment_id,
        expected_operation=args.operation_id,
        goal=goal,
    )
    authorization_body = str(authorization.get("body") or "").strip()
    if receipt.get("authorization_body_sha256") != digest(
        authorization_body.encode("utf-8")
    ):
        raise ApplyError("recovery authorization body digest mismatch")
    parse_release_receipt(
        comments,
        pr=args.pr,
        release_operation=str(receipt["release_operation_id"]),
        merge_sha=merge_sha,
    )
    # Persist the already-verified immutable source before attempting publication.
    # A comment transport failure therefore remains recoverable without touching
    # production or trusting a reconstructed payload.
    _write_receipt(args.output, receipt)
    body = _recovery_comment_body(
        receipt,
        run_id=args.source_run_id,
        artifact_name=args.source_artifact_name,
        receipt_sha256=args.source_receipt_sha256,
    )
    marked = [
        item for item in comments if marker(args.operation_id) in str(item.get("body") or "")
    ]
    if len(marked) > 1:
        raise ApplyError("duplicate or ambiguous durable apply receipt")
    if marked:
        existing = marked[0]
        if (
            not is_actions_bot_comment(existing)
            or _comment_payload(existing, args.operation_id) != receipt
        ):
            raise ApplyError("existing durable apply receipt does not match source")
        publication_state = "already_terminal"
        published = existing
    else:
        published = client.post(f"/issues/{args.pr}/comments", {"body": body})
        if (
            not isinstance(published, Mapping)
            or not is_actions_bot_comment(published)
            or published.get("body") != body
        ):
            raise ApplyError("recovered receipt publication response mismatch")
        readback_comments = list_comments(client, args.pr)
        readback_marked = [
            item
            for item in readback_comments
            if marker(args.operation_id) in str(item.get("body") or "")
        ]
        if (
            len(readback_marked) != 1
            or not is_actions_bot_comment(readback_marked[0])
            or _comment_payload(readback_marked[0], args.operation_id) != receipt
        ):
            raise ApplyError("recovered receipt publication readback mismatch")
        published = readback_marked[0]
        publication_state = "published"
    comment_id = published.get("id")
    if not isinstance(comment_id, int) or comment_id <= 0:
        raise ApplyError("recovered receipt comment id is invalid")
    print(
        json.dumps(
            {
                "state": publication_state,
                "operation_id": args.operation_id,
                "source_run_id": args.source_run_id,
                "source_artifact_name": args.source_artifact_name,
                "source_receipt_sha256": args.source_receipt_sha256,
                "comment_id": comment_id,
                "comment_body_sha256": digest(str(published["body"]).encode("utf-8")),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _load_legacy_manifest(path: Path, expected_sha: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if digest(raw) != expected_sha:
        raise ApplyError("manifest digest mismatch")
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict) or validate_production_manifest(manifest)["valid"] is not True:
        raise ApplyError("manifest contract invalid")
    return manifest


def _run_legacy_commands(manifest: Mapping[str, Any]) -> dict[str, Any]:
    commands = manifest["commands"]
    dry_run = command_evidence(list(commands["dry_run"]))
    if dry_run["return_code"] != 0:
        return {"state": "blocked", "apply_count": 0, "dry_run": dry_run}
    apply_result = command_evidence(list(commands["apply"]))
    readback = command_evidence(list(commands["readback"]))
    reconcile = command_evidence(list(commands["reconcile"]))
    complete = all(
        item["return_code"] == 0 for item in (apply_result, readback, reconcile)
    )
    return {
        "state": "done" if complete else "blocked",
        "apply_count": 1,
        "dry_run": dry_run,
        "apply": apply_result,
        "readback": readback,
        "reconcile": reconcile,
    }


def _run_legacy_mode(
    *,
    args: argparse.Namespace,
    client: GitHubClient,
    pr: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> int:
    merge_sha = exact_sha(args.merge_sha, "merge")
    deployed_sha = exact_sha(args.deployed_sha, "deployed")
    if deployed_sha != merge_sha:
        raise ApplyError("deployed SHA must equal exact merge SHA")
    if exact_sha(pr.get("merge_commit_sha"), "pr-merge") != merge_sha:
        raise ApplyError("PR merge binding mismatch")
    if re.fullmatch(r"[0-9a-f]{64}", str(args.manifest_sha256 or "")) is None:
        raise ApplyError("manifest SHA-256 is invalid")
    operation = str(args.operation_id or "")
    prior = [
        item
        for item in comments
        if marker(operation) in str(item.get("body") or "")
        and is_actions_bot_comment(item)
    ]
    if prior:
        if len(prior) != 1:
            raise ApplyError("duplicate or ambiguous durable apply receipt")
        receipt = {
            "schema": APPLY_RECEIPT_SCHEMA,
            "state": "already_terminal",
            "operation_id": operation,
            "pull_request": args.pr,
        }
        _write_receipt(args.output, receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    release_receipt = parse_legacy_release_receipt(
        comments,
        merge_sha=merge_sha,
        manifest_sha=str(args.manifest_sha256),
        operation=operation,
    )
    authorization = client.get(f"/issues/comments/{args.authorization_comment_id}")
    validate_legacy_authorization(
        authorization,
        pr=args.pr,
        merge_sha=merge_sha,
        deployed_sha=deployed_sha,
        manifest_sha=str(args.manifest_sha256),
        operation=operation,
    )
    subprocess.run(["git", "fetch", "--no-tags", "origin", merge_sha], cwd=ROOT, check=True)
    subprocess.run(["git", "checkout", "--detach", merge_sha], cwd=ROOT, check=True)
    binding = release_receipt["manifest"]
    manifest_path = (ROOT / str(binding["path"])).resolve()
    if ROOT not in manifest_path.parents:
        raise ApplyError("manifest path escapes repository")
    manifest = _load_legacy_manifest(manifest_path, str(args.manifest_sha256))
    if manifest.get("operation_id") != operation:
        raise ApplyError("manifest operation id mismatch")
    result = _run_legacy_commands(manifest)
    approval_body = str(authorization.get("body") or "").strip()
    receipt = {
        "schema": APPLY_RECEIPT_SCHEMA,
        "state": result["state"],
        "operation_id": operation,
        "repository": args.repository,
        "pull_request": args.pr,
        "merge_sha": merge_sha,
        "deployed_sha": deployed_sha,
        "manifest_sha256": str(args.manifest_sha256),
        "authorization_comment_id": args.authorization_comment_id,
        "authorization_body_sha256": digest(approval_body.encode("utf-8")),
        "apply_count": result["apply_count"],
        "evidence": result,
    }
    _write_receipt(args.output, receipt)
    body = (
        marker(operation)
        + "\nProtocol-v2 one-shot production apply receipt:\n```json\n"
        + json.dumps(receipt, sort_keys=True, indent=2)
        + "\n```"
    )
    client.post(f"/issues/{args.pr}/comments", {"body": body})
    print(json.dumps(receipt, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--authorization-mode",
        choices=("scope-goal", "exact-manifest", "receipt-recovery"),
        default="scope-goal",
    )
    parser.add_argument("--repository", default=CANONICAL_REPOSITORY)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--release-operation-id")
    parser.add_argument("--authorization-comment-id", required=True, type=int)
    parser.add_argument("--merge-sha")
    parser.add_argument("--deployed-sha")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--operation-id")
    parser.add_argument("--source-run-id", type=int)
    parser.add_argument("--source-artifact-name")
    parser.add_argument("--source-receipt-sha256")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    client = GitHubClient(args.repository, token)
    pr = client.get(f"/pulls/{args.pr}")
    if pr.get("merged") is not True:
        raise ApplyError("pull request is not merged")
    comments = list_comments(client, args.pr)
    if args.authorization_mode == "receipt-recovery":
        return _run_receipt_recovery(
            args=args,
            client=client,
            pr=pr,
            comments=comments,
        )
    if args.authorization_mode == "exact-manifest":
        required = {
            "merge_sha": args.merge_sha,
            "deployed_sha": args.deployed_sha,
            "manifest_sha256": args.manifest_sha256,
            "operation_id": args.operation_id,
        }
        missing = sorted(field for field, value in required.items() if not value)
        if missing:
            raise ApplyError(
                "exact-manifest mode inputs are missing: " + ", ".join(missing)
            )
        return _run_legacy_mode(
            args=args,
            client=client,
            pr=pr,
            comments=comments,
        )
    if not args.release_operation_id:
        raise ApplyError("scope-goal mode requires --release-operation-id")
    merge_sha = exact_sha(pr.get("merge_commit_sha"), "pr-merge")
    release_receipt = parse_release_receipt(
        comments,
        pr=args.pr,
        release_operation=args.release_operation_id,
        merge_sha=merge_sha,
    )
    authorization = client.get(f"/issues/comments/{args.authorization_comment_id}")
    goal = validate_authorization(
        authorization,
        repository=args.repository,
        pr=args.pr,
    )
    operation = operation_id(
        args.repository,
        args.pr,
        args.authorization_comment_id,
        goal,
    )
    prior = [
        item
        for item in comments
        if marker(operation) in str(item.get("body") or "")
        and is_actions_bot_comment(item)
    ]
    if prior:
        if len(prior) != 1:
            raise ApplyError("duplicate or ambiguous durable apply receipt")
        receipt = {
            "schema": APPLY_RECEIPT_SCHEMA,
            "state": "already_terminal",
            "operation_id": operation,
            "pull_request": args.pr,
        }
        _write_receipt(args.output, receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 0

    subprocess.run(["git", "fetch", "--no-tags", "origin", merge_sha], cwd=ROOT, check=True)
    subprocess.run(["git", "checkout", "--detach", merge_sha], cwd=ROOT, check=True)
    target = _canonical_target()
    approval_body = str(authorization.get("body") or "").strip()
    approval_reference = (
        f"github:{args.repository}:pr:{args.pr}:comment:{args.authorization_comment_id}:"
        f"sha256:{digest(approval_body.encode('utf-8'))}"
    )
    with tempfile.TemporaryDirectory(prefix="wb-core-production-goal-") as directory:
        configure_deploy_environment(Path(directory))
        result = run_dynamic_goal(
            target=target,
            merge_sha=merge_sha,
            goal=goal,
            operation=operation,
            approval_reference=approval_reference,
        )
    receipt = {
        "schema": APPLY_RECEIPT_SCHEMA,
        "state": result["state"],
        "operation_id": operation,
        "repository": args.repository,
        "pull_request": args.pr,
        "release_operation_id": release_receipt["operation_id"],
        "merge_sha": merge_sha,
        "deployed_sha": merge_sha,
        "authorization_comment_id": args.authorization_comment_id,
        "authorization_body_sha256": digest(approval_body.encode("utf-8")),
        "goal": goal,
        "apply_count": result["apply_count"],
        "evidence": result,
    }
    _write_receipt(args.output, receipt)
    body = (
        marker(operation)
        + "\nTask-scoped one-submit production apply receipt:\n```json\n"
        + json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n```"
    )
    client.post(f"/issues/{args.pr}/comments", {"body": body})
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
