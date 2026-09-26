#!/usr/bin/env python3
"""One incident-bound, completion-only continuation of an already claimed recovery.

The failed apply receipt and job log prove that the metadata CAS was not
entered.  A new GitHub claim is published before the sole CAS attempt.  Once
that claim exists, this runner can only read the target; it never resubmits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci import post_merge_release_recovery as recovery  # noqa: E402

RELEASE_RUN_ID = 36242191181
REVIEWED_PREVIEW_RUN_ID = 36253329214
FAILED_APPLY_RUN_ID = 36253397612
FAILED_APPLY_JOB_ID = 108435537463
MERGE_SHA = "e2f06741b6a062559fb8070d0427e8d44bf4867a"
ORIGINAL_OPERATION = "release-recovery-v1-01babfcf36bec51b8bf2ad17c45523b4"
ORIGINAL_FINGERPRINT = "e939cd024a5ca7202f869782b670dd66efba633dd766396e474c35d0fe223f9f"
FAILED_REASON = "remote-readback-failed-1-stage-health-timeout"
EXPECTED_STAGES = ["root-storage-readback", "status", "auth", "activation"]
EXPECTED_SOURCE = {
    "base_sha": "0d41649e25b7627286712f3f3e955434f975b6c9",
    "gate_plan_sha256": "b5a282d237da643cb98fe73a2eab3869445c890d8920950031793c8337a5e9cc",
    "gate_run_id": 36241261915,
    "head_sha": "76e3b57374614ad4a42b5262bba282eec926354a",
    "merge_sha": MERGE_SHA,
    "original_operation_id": "release-v3-43fc4a562a53fc2362cb562cfcfd8ba0",
    "original_receipt_sha256": "4122a03d23fbe8121eb639d4418be940121f8e6cc14e7400088b9f5d64c21071",
    "pull_request": 1321,
}
SCHEMA = "wb-core.post-merge-completion-recovery/v1"
OPERATION = "release-completion-v1-" + recovery.digest(
    recovery.canonical_bytes({"failed_apply_run_id": FAILED_APPLY_RUN_ID, "original_operation": ORIGINAL_OPERATION})
)[:32]


def _run_artifact(client: Any, run_id: int, *, conclusion: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run = client.get(f"/actions/runs/{run_id}")
    if (
        client.repository != recovery.REPOSITORY
        or run.get("name") != "Post-merge Release Recovery"
        or run.get("path") != ".github/workflows/release-recovery.yml"
        or run.get("event") != "workflow_dispatch"
        or run.get("run_attempt") != 1
        or run.get("status") != "completed"
        or run.get("conclusion") != conclusion
        or run.get("head_sha") != MERGE_SHA
    ):
        raise recovery.RecoveryError("completion-source-run-invalid")
    artifacts = client.get(f"/actions/runs/{run_id}/artifacts?per_page=100")
    candidates = [
        item for item in (artifacts.get("artifacts") or [])
        if item.get("name") == f"release-recovery-{RELEASE_RUN_ID}-{run_id}"
        and item.get("expired") is not True
    ]
    if len(candidates) != 1:
        raise recovery.RecoveryError("completion-source-artifact-invalid")
    raw = client.request("GET", f"/actions/artifacts/{int(candidates[0]['id'])}/zip", raw=True)
    files = recovery._zip_files(raw, "completion-source-artifact")
    if len(files) != 1:
        raise recovery.RecoveryError("completion-source-artifact-files-invalid")
    receipt = recovery._json_file(files, "recovery-receipt.json", "completion-source-receipt")
    return receipt, {"run_id": run_id, "artifact_id": int(candidates[0]["id"]),
                     "receipt_sha256": recovery.digest(recovery.canonical_bytes(receipt))}


def _failure_proof(client: Any) -> dict[str, Any]:
    evidence = recovery.collect_evidence(client, RELEASE_RUN_ID)
    original = evidence["original_receipt"]
    if (original.get("merge_sha") != MERGE_SHA or original.get("pull_request") != 1321
            or evidence.get("original_receipt_sha256") != EXPECTED_SOURCE["original_receipt_sha256"]):
        raise recovery.RecoveryError("completion-original-release-drift")
    if recovery.recovery_operation_id(RELEASE_RUN_ID, original) != ORIGINAL_OPERATION:
        raise recovery.RecoveryError("completion-original-operation-drift")
    reviewed, reviewed_artifact = _run_artifact(client, REVIEWED_PREVIEW_RUN_ID, conclusion="success")
    if (
        reviewed.get("schema") != recovery.RECOVERY_SCHEMA
        or reviewed.get("mode") != "preview"
        or reviewed.get("state") != "reviewable"
        or reviewed.get("release_run_id") != RELEASE_RUN_ID
        or reviewed.get("operation_id") != ORIGINAL_OPERATION
        or reviewed.get("preview_fingerprint") != ORIGINAL_FINGERPRINT
        or recovery.preview_fingerprint(reviewed) != ORIGINAL_FINGERPRINT
        or reviewed.get("source") != EXPECTED_SOURCE
        or reviewed.get("recovery_case") != recovery.RecoveryCase.STORAGE_TAIL.value
    ):
        raise recovery.RecoveryError("completion-reviewed-preview-invalid")
    failed, failed_artifact = _run_artifact(client, FAILED_APPLY_RUN_ID, conclusion="failure")
    if (
        failed.get("schema") != recovery.RECOVERY_SCHEMA
        or failed.get("state") != "ambiguous"
        or failed.get("operation_id") != ORIGINAL_OPERATION
        or failed.get("preview_fingerprint") != ORIGINAL_FINGERPRINT
        or failed.get("source") != EXPECTED_SOURCE
        or failed.get("reason") != FAILED_REASON
        or not isinstance(failed.get("completed_stages"), list)
        or any(not isinstance(item, Mapping) for item in failed.get("completed_stages", []))
        or [item["stage"] for item in failed.get("completed_stages", [])] != EXPECTED_STAGES
        or any(not re.fullmatch(r"[0-9a-f]{64}", str(item.get("stdout_sha256") or ""))
               for item in failed.get("completed_stages", []))
        or "final_readback" in failed
    ):
        raise recovery.RecoveryError("completion-failed-receipt-invalid")
    jobs = client.get(f"/actions/runs/{FAILED_APPLY_RUN_ID}/jobs?filter=latest&per_page=100")
    matching = [item for item in (jobs.get("jobs") or []) if item.get("id") == FAILED_APPLY_JOB_ID]
    if len(matching) != 1 or matching[0].get("name") != "Proven post-merge deploy-tail recovery" or matching[0].get("conclusion") != "failure" or matching[0].get("status") != "completed":
        raise recovery.RecoveryError("completion-failed-job-invalid")
    failed_steps = [step.get("name") for step in (matching[0].get("steps") or []) if step.get("conclusion") == "failure"]
    if failed_steps != ["Validate evidence and preview or continue only the proven tail"]:
        raise recovery.RecoveryError("completion-failed-step-invalid")
    log = client.request("GET", f"/actions/jobs/{FAILED_APPLY_JOB_ID}/logs", raw=True)
    log_text = log.decode("utf-8", errors="replace")
    if (f'"reason": "{FAILED_REASON}"' not in log_text
            or f'"operation_id": "{ORIGINAL_OPERATION}"' not in log_text
            or '"state": "ambiguous"' not in log_text):
        raise recovery.RecoveryError("completion-failed-log-invalid")
    claims = recovery._matching_comments(client, 1321, recovery._claim_marker(ORIGINAL_OPERATION))
    receipts = recovery._matching_comments(client, 1321, recovery._receipt_marker(ORIGINAL_OPERATION))
    if (len(claims) != 1 or receipts or claims[0].get("state") != "claimed"
            or claims[0].get("operation_id") != ORIGINAL_OPERATION
            or claims[0].get("source") != EXPECTED_SOURCE
            or claims[0].get("preview_fingerprint") != ORIGINAL_FINGERPRINT
            or claims[0].get("target") != reviewed.get("target")):
        raise recovery.RecoveryError("completion-original-claim-invalid")
    return {"source": EXPECTED_SOURCE, "reviewed_preview": reviewed_artifact,
            "failed_apply": failed_artifact, "failed_job_id": FAILED_APPLY_JOB_ID,
            "failed_job_log_sha256": recovery.digest(log),
            "original_preview_prestate_sha256": recovery.digest(recovery.canonical_bytes(reviewed["prestate"])),
            "original_metadata_sha256": reviewed["prestate"]["metadata_sha256"],
            "original_metadata": reviewed["prestate"]["metadata"],
            "original_main_pid": reviewed["prestate"]["main_pid"],
            "original_target": reviewed["target"]}


def _prestate(target: Any, *, complete: bool, sleep: Any = time.sleep) -> dict[str, Any]:
    for attempt in range(3):
        try:
            return recovery.collect_prestate(target, MERGE_SHA, require_incomplete=not complete)
        except recovery.RecoveryError as exc:
            if exc.reason != FAILED_REASON or attempt == 2:
                raise
            sleep(2)
    raise AssertionError("unreachable")


def _marker(kind: str) -> str:
    return f"<!-- wb-core-release-completion-{kind} operation={OPERATION} -->"


def _fingerprint(preview: Mapping[str, Any]) -> str:
    return recovery.digest(recovery.canonical_bytes({key: value for key, value in preview.items() if key != "preview_fingerprint"}))


def _exact_completion_readback(final: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
    before = claim.get("before_metadata")
    if not isinstance(before, Mapping) or before.get("deployment_complete") is not False:
        return False
    after = {**before, "deployment_complete": True}
    expected_raw = (json.dumps(after, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return (
        final.get("runtime_sha") == MERGE_SHA
        and final.get("main_pid") == claim.get("main_pid")
        and final.get("metadata") == after
        and final.get("metadata_sha256") == recovery.digest(expected_raw)
    )


def build_preview(client: Any, target: Any) -> dict[str, Any]:
    proof = _failure_proof(client)
    runner = recovery.prove_repo_only_descendant(client, EXPECTED_SOURCE)
    prestate = _prestate(target, complete=False)
    target_identity = {"target_id": target.target_id, "ssh_destination": target.ssh_destination,
                       "target_dir": target.target_dir}
    if (proof["original_target"] != target_identity or prestate.get("runtime_sha") != MERGE_SHA
            or prestate.get("metadata_sha256") != proof["original_metadata_sha256"]
            or prestate.get("main_pid") != proof["original_main_pid"]
            or int(prestate.get("main_pid") or 0) <= 0):
        raise recovery.RecoveryError("completion-target-binding-invalid")
    preview = {"schema": SCHEMA, "mode": "completion-preview", "state": "reviewable",
               "operation_id": OPERATION, "release_run_id": RELEASE_RUN_ID,
               "source": EXPECTED_SOURCE, "failure_proof": proof, "runner": runner,
               "target": target_identity, "prestate": prestate,
               "stages": ["deployment-metadata-cas-complete", "final-runtime-readback"],
               "forbidden_stages": ["merge", "rsync", "dependencies", "activation", "systemd", "restart", "nginx"]}
    preview["preview_fingerprint"] = _fingerprint(preview)
    return preview


def existing_claim_readback(client: Any, expected_fingerprint: str, target: Any) -> dict[str, Any] | None:
    claims = recovery._matching_comments(client, 1321, _marker("claim"))
    receipts = recovery._matching_comments(client, 1321, _marker("receipt"))
    if not claims:
        if receipts:
            raise recovery.RecoveryError("completion-receipt-without-claim")
        return None
    if (len(claims) != 1 or claims[0].get("state") != "claimed"
            or claims[0].get("preview_fingerprint") != expected_fingerprint
            or claims[0].get("operation_id") != OPERATION
            or claims[0].get("source") != EXPECTED_SOURCE
            or claims[0].get("failed_apply_run_id") != FAILED_APPLY_RUN_ID
            or not re.fullmatch(r"[0-9a-f]{64}", str(claims[0].get("metadata_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(claims[0].get("failed_artifact_sha256") or ""))
            or not isinstance(claims[0].get("main_pid"), int) or claims[0]["main_pid"] <= 0
            or not isinstance(claims[0].get("before_metadata"), Mapping)
            or claims[0]["before_metadata"].get("deployment_complete") is not False):
        raise recovery.RecoveryError("completion-claim-invalid")
    proof = _failure_proof(client)
    if (claims[0]["failed_artifact_sha256"] != proof["failed_apply"]["receipt_sha256"]
            or claims[0]["metadata_sha256"] != proof["original_metadata_sha256"]
            or claims[0]["main_pid"] != proof["original_main_pid"]
            or claims[0]["before_metadata"] != proof["original_metadata"]):
        raise recovery.RecoveryError("completion-claim-provenance-invalid")
    try:
        final = _prestate(target, complete=True)
    except recovery.RecoveryError:
        return {"schema": SCHEMA, "state": "ambiguous", "operation_id": OPERATION,
                "preview_fingerprint": expected_fingerprint, "reason": "existing-completion-claim-readback-only"}
    if not _exact_completion_readback(final, claims[0]):
        return {"schema": SCHEMA, "state": "ambiguous", "operation_id": OPERATION,
                "preview_fingerprint": expected_fingerprint, "reason": "completion-readback-drift"}
    if receipts:
        if (len(receipts) != 1 or receipts[0].get("schema") != SCHEMA
                or receipts[0].get("state") != "complete"
                or receipts[0].get("operation_id") != OPERATION
                or receipts[0].get("source") != EXPECTED_SOURCE
                or receipts[0].get("preview_fingerprint") != expected_fingerprint
                or not isinstance(receipts[0].get("final_readback"), Mapping)
                or not _exact_completion_readback(receipts[0]["final_readback"], claims[0])
                or receipts[0]["final_readback"].get("metadata_sha256") != final.get("metadata_sha256")):
            raise recovery.RecoveryError("completion-receipt-invalid")
        return {**receipts[0], "fresh_final_readback": final}
    completed = {"schema": SCHEMA, "state": "complete", "operation_id": OPERATION,
                 "source": EXPECTED_SOURCE, "preview_fingerprint": expected_fingerprint,
                 "final_readback": final, "recovered_from_existing_claim": True}
    recovery._publish_once(client, 1321, _marker("receipt"), completed)
    return completed


def apply(client: Any, preview: Mapping[str, Any], expected_fingerprint: str, target: Any) -> dict[str, Any]:
    if preview.get("preview_fingerprint") != expected_fingerprint:
        raise recovery.RecoveryError("completion-preview-fingerprint-mismatch")
    if recovery._matching_comments(client, 1321, _marker("claim")) or recovery._matching_comments(client, 1321, _marker("receipt")):
        raise recovery.RecoveryError("completion-already-claimed")
    commands = recovery.build_stage_commands(target, MERGE_SHA, preview["prestate"]["metadata_sha256"], int(preview["prestate"]["main_pid"]))
    # All three commands and both source checks are read-only before claiming.
    for name in ("root_storage_readback", "status", "auth"):
        recovery._must_succeed(name, commands[name])
    if recovery.canonical_bytes(_failure_proof(client)) != recovery.canonical_bytes(preview["failure_proof"]):
        raise recovery.RecoveryError("completion-source-drift-before-claim")
    if recovery.canonical_bytes(_prestate(target, complete=False)) != recovery.canonical_bytes(preview["prestate"]):
        raise recovery.RecoveryError("completion-target-drift-before-claim")
    claim = {"schema": SCHEMA, "state": "claimed", "operation_id": OPERATION,
             "source": EXPECTED_SOURCE, "preview_fingerprint": expected_fingerprint,
             "failed_apply_run_id": FAILED_APPLY_RUN_ID,
             "failed_artifact_sha256": preview["failure_proof"]["failed_apply"]["receipt_sha256"],
             "metadata_sha256": preview["prestate"]["metadata_sha256"],
             "main_pid": preview["prestate"]["main_pid"],
             "before_metadata": preview["prestate"]["metadata"]}
    recovery._publish_once(client, 1321, _marker("claim"), claim)
    try:
        if recovery.canonical_bytes(_prestate(target, complete=False)) != recovery.canonical_bytes(preview["prestate"]):
            raise recovery.RecoveryError("completion-target-drift-after-claim")
        if recovery.canonical_bytes(recovery.prove_repo_only_descendant(client, EXPECTED_SOURCE)) != recovery.canonical_bytes(preview["runner"]):
            raise recovery.RecoveryError("completion-runner-drift-after-claim")
        outcome = recovery._run_stage(commands["completion"], input_text=commands["completion_input"])
        # A failed/transport-ambiguous CAS is never submitted again.  Accept
        # only a fresh exact complete readback, whatever the SSH exit status.
        final = _prestate(target, complete=True)
        if not _exact_completion_readback(final, claim):
            raise recovery.RecoveryError("completion-readback-drift")
        if outcome.returncode != 0:
            mode = "cas-response-ambiguous-confirmed-by-readback"
        else:
            mode = "cas-response-complete"
    except Exception as exc:
        reason = exc.reason if isinstance(exc, recovery.RecoveryError) else type(exc).__name__
        return {"schema": SCHEMA, "state": "ambiguous", "operation_id": OPERATION,
                "source": EXPECTED_SOURCE, "preview_fingerprint": expected_fingerprint,
                "reason": reason, "completion_claimed": True}
    completed = {"schema": SCHEMA, "state": "complete", "operation_id": OPERATION,
                 "source": EXPECTED_SOURCE, "preview_fingerprint": expected_fingerprint,
                 "final_readback": final, "completion_mode": mode,
                 "recovered_from_existing_claim": False}
    recovery._publish_once(client, 1321, _marker("receipt"), completed)
    return completed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("completion-preview", "completion-apply"))
    parser.add_argument("--repository", default=recovery.REPOSITORY)
    parser.add_argument("--preview-fingerprint", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "completion-apply" and not re.fullmatch(r"[0-9a-f]{64}", args.preview_fingerprint):
        raise SystemExit("reviewed completion --preview-fingerprint required")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN required")
    client = recovery.release.GitHub(args.repository, token)
    from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
    target = hosted.load_hosted_runtime_target(recovery.TARGET_FILE)
    with tempfile.TemporaryDirectory(prefix="wb-core-completion-recovery-") as directory:
        recovery.release.configure_ssh(Path(directory))
        if args.command == "completion-preview":
            result = build_preview(client, target)
        else:
            result = existing_claim_readback(client, args.preview_fingerprint, target)
            if result is None:
                result = apply(client, build_preview(client, target), args.preview_fingerprint, target)
    recovery.write_output(args.output, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {"reviewable", "complete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
