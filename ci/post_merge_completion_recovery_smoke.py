#!/usr/bin/env python3
"""Offline proof for incident-bound metadata completion and one-shot readback."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci import post_merge_completion_recovery as completion
from ci import post_merge_release_recovery as recovery


def zipped(receipt: dict) -> bytes:
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w") as archive:
        archive.writestr("recovery-receipt.json", json.dumps(receipt))
    return memory.getvalue()


def expect(reason: str, call) -> None:
    try:
        call()
    except recovery.RecoveryError as exc:
        assert exc.reason == reason, (exc.reason, reason)
    else:
        raise AssertionError(f"expected {reason}")


class Client:
    repository = recovery.REPOSITORY

    def __init__(self) -> None:
        self.comments: list[str] = []
        self.receipts: dict[int, dict] = {}
        self.log = (f'{{"operation_id": "{completion.ORIGINAL_OPERATION}", '
                    f'"reason": "{completion.FAILED_REASON}", "state": "ambiguous"}}').encode()

    def get(self, path: str):
        for run_id, conclusion in ((completion.REVIEWED_PREVIEW_RUN_ID, "success"),
                                   (completion.FAILED_APPLY_RUN_ID, "failure")):
            if path == f"/actions/runs/{run_id}":
                return {"name": "Post-merge Release Recovery", "path": ".github/workflows/release-recovery.yml",
                        "event": "workflow_dispatch", "run_attempt": 1, "status": "completed",
                        "conclusion": conclusion, "head_sha": completion.MERGE_SHA}
            if path == f"/actions/runs/{run_id}/artifacts?per_page=100":
                return {"artifacts": [{"id": run_id, "name": f"release-recovery-{completion.RELEASE_RUN_ID}-{run_id}", "expired": False}]}
        if path == f"/actions/runs/{completion.FAILED_APPLY_RUN_ID}/jobs?filter=latest&per_page=100":
            return {"jobs": [{"id": completion.FAILED_APPLY_JOB_ID,
                              "name": "Proven post-merge deploy-tail recovery", "status": "completed", "conclusion": "failure",
                              "steps": [{"name": "Validate evidence and preview or continue only the proven tail", "conclusion": "failure"}]}]}
        if path.startswith("/issues/1321/comments?"):
            return [{"body": value} for value in self.comments]
        raise AssertionError(path)

    def request(self, method: str, path: str, *, raw: bool):
        assert method == "GET" and raw
        if path == f"/actions/jobs/{completion.FAILED_APPLY_JOB_ID}/logs":
            return self.log
        for run_id, receipt in self.receipts.items():
            if path == f"/actions/artifacts/{run_id}/zip":
                return zipped(receipt)
        raise AssertionError(path)

    def post(self, path: str, body: dict):
        assert path == "/issues/1321/comments"
        self.comments.append(body["body"])


def original_claim(client: Client) -> None:
    recovery._publish_once(client, 1321, recovery._claim_marker(completion.ORIGINAL_OPERATION), {
        "state": "claimed", "operation_id": completion.ORIGINAL_OPERATION,
        "source": completion.EXPECTED_SOURCE, "preview_fingerprint": completion.ORIGINAL_FINGERPRINT,
        "target": {"target_id": "eu", "ssh_destination": "host", "target_dir": "/srv/wbc/repo"},
    })


def fixture() -> Client:
    client = Client()
    client.receipts[completion.REVIEWED_PREVIEW_RUN_ID] = {
        "schema": recovery.RECOVERY_SCHEMA, "mode": "preview", "state": "reviewable",
        "release_run_id": completion.RELEASE_RUN_ID, "operation_id": completion.ORIGINAL_OPERATION,
        "preview_fingerprint": completion.ORIGINAL_FINGERPRINT,
        "source": completion.EXPECTED_SOURCE, "recovery_case": "storage-tail",
        "target": {"target_id": "eu", "ssh_destination": "host", "target_dir": "/srv/wbc/repo"},
        "prestate": {"metadata_sha256": "a" * 64, "main_pid": 101108,
                     "metadata": {"deployment_complete": False, "commit": completion.MERGE_SHA}},
    }
    client.receipts[completion.FAILED_APPLY_RUN_ID] = {
        "schema": recovery.RECOVERY_SCHEMA, "state": "ambiguous",
        "operation_id": completion.ORIGINAL_OPERATION, "preview_fingerprint": completion.ORIGINAL_FINGERPRINT,
        "source": completion.EXPECTED_SOURCE, "reason": completion.FAILED_REASON,
        "completed_stages": [{"stage": stage, "stdout_sha256": "b" * 64} for stage in completion.EXPECTED_STAGES],
    }
    original_claim(client)
    return client


def test_source_proof() -> None:
    client = fixture()
    old_collect = recovery.collect_evidence
    old_operation = recovery.recovery_operation_id
    old_fingerprint = recovery.preview_fingerprint
    recovery.collect_evidence = lambda *_: {"original_receipt": {"merge_sha": completion.MERGE_SHA,
        "pull_request": 1321}, "original_receipt_sha256": completion.EXPECTED_SOURCE["original_receipt_sha256"]}
    recovery.recovery_operation_id = lambda *_: completion.ORIGINAL_OPERATION
    recovery.preview_fingerprint = lambda *_: completion.ORIGINAL_FINGERPRINT
    try:
        proof = completion._failure_proof(client)
        assert proof["failed_apply"]["run_id"] == completion.FAILED_APPLY_RUN_ID
        assert proof["original_main_pid"] == 101108
        client.receipts[completion.FAILED_APPLY_RUN_ID]["completed_stages"].append({"stage": "completion", "stdout_sha256": "c" * 64})
        expect("completion-failed-receipt-invalid", lambda: completion._failure_proof(client))
        client.receipts[completion.FAILED_APPLY_RUN_ID]["completed_stages"].pop()
        client.receipts[completion.FAILED_APPLY_RUN_ID]["reason"] = "remote-readback-failed-255"
        expect("completion-failed-receipt-invalid", lambda: completion._failure_proof(client))
        client.receipts[completion.FAILED_APPLY_RUN_ID]["reason"] = completion.FAILED_REASON
        client.comments.clear()
        expect("completion-original-claim-invalid", lambda: completion._failure_proof(client))
    finally:
        recovery.collect_evidence = old_collect
        recovery.recovery_operation_id = old_operation
        recovery.preview_fingerprint = old_fingerprint


def test_exact_artifact_and_health_retry() -> None:
    client = fixture()
    receipt, identity = completion._run_artifact(client, completion.FAILED_APPLY_RUN_ID, conclusion="failure")
    assert identity["receipt_sha256"] == recovery.digest(recovery.canonical_bytes(receipt))
    client.receipts[completion.FAILED_APPLY_RUN_ID] = {"state": "complete"}
    attempts = 0
    old = recovery.collect_prestate
    def health(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise recovery.RecoveryError(completion.FAILED_REASON)
        return {"metadata": {"deployment_complete": False}}
    recovery.collect_prestate = health
    try:
        assert completion._prestate(object(), complete=False, sleep=lambda _s: None)
        assert attempts == 3
    finally:
        recovery.collect_prestate = old


def test_preview_exact_original_pid_and_metadata() -> None:
    client = fixture()
    target = SimpleNamespace(target_id="eu", ssh_destination="host", target_dir="/srv/wbc/repo")
    proof = {"original_target": {"target_id": "eu", "ssh_destination": "host", "target_dir": "/srv/wbc/repo"},
             "original_metadata_sha256": "a" * 64, "original_main_pid": 101108}
    state = {"runtime_sha": completion.MERGE_SHA, "metadata_sha256": "a" * 64,
             "main_pid": 101108, "metadata": {"deployment_complete": False}}
    old_proof, old_prestate, old_runner = completion._failure_proof, completion._prestate, recovery.prove_repo_only_descendant
    completion._failure_proof = lambda _client: proof
    completion._prestate = lambda _target, *, complete: state
    recovery.prove_repo_only_descendant = lambda *_: {"trusted_main_sha": completion.MERGE_SHA}
    try:
        assert completion.build_preview(client, target)["state"] == "reviewable"
        state["main_pid"] += 1
        expect("completion-target-binding-invalid", lambda: completion.build_preview(client, target))
        state["main_pid"] -= 1
        state["metadata_sha256"] = "b" * 64
        expect("completion-target-binding-invalid", lambda: completion.build_preview(client, target))
    finally:
        completion._failure_proof, completion._prestate = old_proof, old_prestate
        recovery.prove_repo_only_descendant = old_runner


def test_one_claim_one_cas_and_readback_only() -> None:
    client = fixture()
    target = SimpleNamespace()
    target.target_id = "eu"
    target.ssh_destination = "host"
    target.target_dir = "/srv/wbc/repo"
    state = {"complete": False, "cas_calls": 0}
    before = {"runtime_sha": completion.MERGE_SHA, "metadata": {"deployment_complete": False, "commit": completion.MERGE_SHA},
              "metadata_sha256": "a" * 64, "main_pid": 101108}
    after_metadata = {**before["metadata"], "deployment_complete": True}
    after_hash = recovery.digest((json.dumps(after_metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode())
    after = {**before, "metadata": after_metadata, "metadata_sha256": after_hash}
    proof = {"failed_apply": {"receipt_sha256": "d" * 64},
             "original_metadata_sha256": "a" * 64,
             "original_main_pid": 101108,
             "original_metadata": {"deployment_complete": False, "commit": completion.MERGE_SHA}}
    preview = {"preview_fingerprint": "e" * 64, "prestate": before, "failure_proof": proof,
               "runner": {"trusted_main_sha": completion.MERGE_SHA}}
    old_proof, old_prestate = completion._failure_proof, completion._prestate
    old_runner, old_commands = recovery.prove_repo_only_descendant, recovery.build_stage_commands
    old_succeed, old_stage = recovery._must_succeed, recovery._run_stage
    completion._failure_proof = lambda _client: proof
    completion._prestate = lambda _target, *, complete: after if state["complete"] else (before if not complete else (_ for _ in ()).throw(recovery.RecoveryError("target-marker-not-complete")))
    recovery.prove_repo_only_descendant = lambda *_: preview["runner"]
    recovery.build_stage_commands = lambda *_args, **_kwargs: {"root_storage_readback": [], "status": [], "auth": [], "completion": ["cas"], "completion_input": "script"}
    recovery._must_succeed = lambda *_args: None
    def cas(*_args, **_kwargs):
        state["cas_calls"] += 1
        # Simulate metadata CAS committed but SSH response lost.
        state["complete"] = True
        return subprocess.CompletedProcess(["cas"], 255, "", "transport lost")
    recovery._run_stage = cas
    try:
        result = completion.apply(client, preview, "e" * 64, target)
        assert result["state"] == "complete" and state["cas_calls"] == 1
        again = completion.existing_claim_readback(client, "e" * 64, target)
        assert again["state"] == "complete" and state["cas_calls"] == 1
        receipt_index = next(i for i, body in enumerate(client.comments) if completion._marker("receipt") in body)
        good_receipt = client.comments[receipt_index]
        client.comments[receipt_index] = good_receipt.replace(
            f'"operation_id": "{completion.OPERATION}"', '"operation_id": "forged"')
        expect("completion-receipt-invalid", lambda: completion.existing_claim_readback(client, "e" * 64, target))
        client.comments[receipt_index] = good_receipt
        saved = completion._failure_proof
        completion._failure_proof = lambda _client: {**proof, "original_main_pid": 999999}
        expect("completion-claim-provenance-invalid", lambda: completion.existing_claim_readback(client, "e" * 64, target))
        completion._failure_proof = saved
        state["complete"] = False
        ambiguous = completion.existing_claim_readback(client, "e" * 64, target)
        assert ambiguous["state"] == "ambiguous" and state["cas_calls"] == 1

        # The CAS response can be lost before the target has committed.  Even
        # after a later healthy readback, the existing claim forbids another CAS.
        client2 = fixture()
        state["complete"] = False
        state["cas_calls"] = 0
        def lost(*_args, **_kwargs):
            state["cas_calls"] += 1
            return subprocess.CompletedProcess(["cas"], 255, "", "transport lost")
        recovery._run_stage = lost
        first = completion.apply(client2, preview, "e" * 64, target)
        assert first["state"] == "ambiguous" and state["cas_calls"] == 1
        assert completion.existing_claim_readback(client2, "e" * 64, target)["state"] == "ambiguous"
        expect("completion-already-claimed", lambda: completion.apply(client2, preview, "e" * 64, target))
        state["complete"] = True
        assert completion.existing_claim_readback(client2, "e" * 64, target)["state"] == "complete"
        assert state["cas_calls"] == 1
    finally:
        completion._failure_proof, completion._prestate = old_proof, old_prestate
        recovery.prove_repo_only_descendant, recovery.build_stage_commands = old_runner, old_commands
        recovery._must_succeed, recovery._run_stage = old_succeed, old_stage


if __name__ == "__main__":
    test_source_proof()
    test_exact_artifact_and_health_retry()
    test_preview_exact_original_pid_and_metadata()
    test_one_claim_one_cas_and_readback_only()
    print("post-merge completion recovery smoke: PASS")
