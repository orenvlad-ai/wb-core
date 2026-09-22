#!/usr/bin/env python3
"""Offline regression checks for the bounded post-merge recovery lane."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci import post_merge_release_recovery as recovery


M = "a" * 40
C1 = "b" * 40
C2 = "c" * 40
FINGERPRINT = "d" * 64


class CommentsClient:
    repository = recovery.REPOSITORY

    def __init__(self, values: list[str] | None = None, *, ambiguous_post: bool = False) -> None:
        self.values = list(values or [])
        self.ambiguous_post = ambiguous_post

    def get(self, path: str):
        if path.startswith("/issues/"):
            return [{"body": body} for body in self.values]
        if path == "/git/ref/heads/main":
            return {"object": {"sha": C2}}
        raise AssertionError(path)

    def post(self, path: str, body: dict):
        assert path.startswith("/issues/")
        self.values.append(body["body"])
        if self.ambiguous_post:
            raise recovery.release.RunnerError("github-transport:lost")
        return {"id": len(self.values)}


def expect_reason(reason: str, call) -> None:
    try:
        call()
    except recovery.RecoveryError as exc:
        assert exc.reason == reason, (exc.reason, reason)
    else:
        raise AssertionError(f"expected {reason}")


def failure_log(exit_status: int = 3, *, exact_stage: bool = True) -> bytes:
    stage = (
        'run_stage("readback", root_storage_commands["status_artifact_readback"])'
        if exact_stage
        else 'run_stage("restart", restart_command)'
    )
    return f"""
python3 apps/github_release_runner.py run --workflow-run-id "77" --output receipt.json
deploy_current_checkout
{stage}
apps/root_storage_policy.py --policy-file /runtime/root_storage_policy_v1.json status-readback
subprocess.CalledProcessError: Command ['ssh'] returned non-zero exit status {exit_status}.
""".encode()


def test_failure_evidence() -> None:
    proof = recovery._prove_failed_stage(failure_log(), 77, "One-shot deployed release")
    assert proof["stage"] == "root-storage-status-artifact-readback"
    assert proof["exit_status"] == 3
    expect_reason(
        "failed-stage-not-root-storage-readback-exit3",
        lambda: recovery._prove_failed_stage(failure_log(255), 77, "One-shot deployed release"),
    )
    bad_receipt = {
        "schema": recovery.release.RECEIPT_SCHEMA,
        "state": "done",
        "reason": None,
        "release_kind": "live_runtime",
        "deployed_sha": None,
    }
    expect_reason(
        "original-receipt-not-eligible",
        lambda: recovery._validate_original_receipt(bad_receipt),
    )
    expect_reason(
        "failed-stage-not-root-storage-readback-exit3",
        lambda: recovery._prove_failed_stage(
            failure_log(exact_stage=False), 77, "One-shot deployed release"
        ),
    )


def _completed(args: list[str], *, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def test_every_intervening_commit_is_repo_only() -> None:
    original_git = recovery._git
    original_plan = recovery.build_plan_from_paths

    def fake_git(args: list[str], *, check: bool = True):
        if args == ["rev-parse", "HEAD"]:
            return _completed(args, stdout=C2 + "\n")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return _completed(args)
        if args[:3] == ["rev-list", "--reverse", "--ancestry-path"]:
            return _completed(args, stdout=C1 + "\n" + C2 + "\n")
        if args[:3] == ["show", "-s", "--format=%P"]:
            return _completed(args, stdout=(M if args[3] == C1 else C1) + "\n")
        if args[:2] == ["diff", "--name-only"] and "--" in args:
            return _completed(args, stdout="")
        if args[:2] == ["diff", "--name-only"]:
            span = args[2]
            if span == f"{M}..{C1}":
                return _completed(args, stdout="apps/runtime.py\n")
            if span == f"{C1}..{C2}":
                return _completed(args, stdout="apps/runtime.py\n")
            return _completed(args, stdout="ci/recovery.py\n")
        raise AssertionError(args)

    def fake_plan(**kwargs):
        paths = kwargs["paths"]
        return {
            "release_kind": "repo_only" if all(p.startswith("ci/") for p in paths) else "live_runtime",
            "plan_sha256": "e" * 64,
        }

    recovery._git = fake_git
    recovery.build_plan_from_paths = fake_plan
    try:
        expect_reason(
            "intervening-commit-not-repo-only",
            lambda: recovery.prove_repo_only_descendant(
                CommentsClient(), {"merge_sha": M, "pull_request": 7}
            ),
        )
    finally:
        recovery._git = original_git
        recovery.build_plan_from_paths = original_plan


def preview() -> dict:
    value = {
        "schema": recovery.RECOVERY_SCHEMA,
        "mode": "preview",
        "state": "reviewable",
        "release_run_id": 9,
        "operation_id": "release-recovery-v1-" + "1" * 32,
        "source": {
            "pull_request": 7,
            "gate_run_id": 8,
            "base_sha": "1" * 40,
            "head_sha": "2" * 40,
            "merge_sha": M,
            "original_operation_id": "release-v3-old",
            "original_receipt_sha256": "3" * 64,
            "gate_plan_sha256": "4" * 64,
        },
        "runner": {"trusted_main_sha": C2},
        "target": {"target_id": "target", "ssh_destination": "host", "target_dir": "/runtime"},
        "failure": {"stage": "root-storage-status-artifact-readback", "exit_status": 3},
        "prestate": {
            "runtime_sha": M,
            "metadata": {"commit": M, "deployment_complete": False, "deployed_at": "old"},
            "metadata_sha256": "5" * 64,
            "main_pid": 42,
            "services": ["main.service"],
            "health": {"https://example/login": 200},
            "finance": {
                "flags": {},
                "unit_absent": True,
                "store_absent": True,
                "routes_absent": True,
                "listener_absent": True,
            },
        },
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
    value["preview_fingerprint"] = FINGERPRINT
    return value


def commands() -> dict:
    return {
        "root_storage_readback": ["root-readback"],
        "status": ["status"],
        "auth": ["auth"],
        "activation": ["activation"],
        "activation_readback": ["activation-readback"],
        "completion": ["completion"],
        "completion_input": "cas",
    }


def patch_apply(
    *,
    activation_status: int = 0,
    activation_readback_status: int = 0,
    drift_after_claim: bool = False,
):
    originals = (
        recovery.build_stage_commands,
        recovery._run_stage,
        recovery.collect_prestate,
        recovery.prove_repo_only_descendant,
    )
    calls: list[str] = []
    incomplete = preview()["prestate"]
    complete = {
        **incomplete,
        "metadata": {**incomplete["metadata"], "deployment_complete": True},
        "metadata_sha256": "6" * 64,
    }
    first = {**incomplete, "main_pid": 43} if drift_after_claim else incomplete
    states = [first, incomplete, complete]

    def run(command: list[str], *, input_text: str | None = None):
        name = command[0]
        calls.append(name)
        status = activation_status if name == "activation" else activation_readback_status if name == "activation-readback" else 0
        return subprocess.CompletedProcess(command, status, stdout=name, stderr="")

    def state(_target, _merge, *, require_incomplete: bool):
        value = states.pop(0)
        assert value["metadata"]["deployment_complete"] is (not require_incomplete)
        return value

    recovery.build_stage_commands = lambda *_args, **_kwargs: commands()
    recovery._run_stage = run
    recovery.collect_prestate = state
    recovery.prove_repo_only_descendant = lambda *_args, **_kwargs: preview()["runner"]
    return calls, originals


def restore_apply(originals) -> None:
    (
        recovery.build_stage_commands,
        recovery._run_stage,
        recovery.collect_prestate,
        recovery.prove_repo_only_descendant,
    ) = originals


def test_exact_tail_and_completion() -> None:
    client = CommentsClient(ambiguous_post=True)
    calls, originals = patch_apply()
    try:
        result = recovery.apply_recovery(client, preview(), FINGERPRINT, object())
    finally:
        restore_apply(originals)
    assert result["state"] == "complete"
    assert calls == ["root-readback", "status", "auth", "activation", "completion"]
    assert not set(calls) & {"merge", "rsync", "dependencies", "restart", "nginx"}
    assert sum(recovery.CLAIM_MARKER in body for body in client.values) == 1
    assert sum(recovery.RECEIPT_MARKER in body for body in client.values) == 1


def test_activation_failure_never_completes() -> None:
    client = CommentsClient()
    calls, originals = patch_apply(activation_status=255, activation_readback_status=1)
    try:
        result = recovery.apply_recovery(client, preview(), FINGERPRINT, object())
    finally:
        restore_apply(originals)
    assert result["state"] == "ambiguous"
    assert calls == ["root-readback", "status", "auth", "activation", "activation-readback"]
    assert "completion" not in calls
    assert sum(recovery.CLAIM_MARKER in body for body in client.values) == 1
    assert not any(recovery.RECEIPT_MARKER in body for body in client.values)


def test_target_drift_halts_before_mutation() -> None:
    client = CommentsClient()
    calls, originals = patch_apply(drift_after_claim=True)
    try:
        result = recovery.apply_recovery(client, preview(), FINGERPRINT, object())
    finally:
        restore_apply(originals)
    assert result["state"] == "ambiguous"
    assert result["reason"] == "target-prestate-drift-after-claim"
    assert calls == ["root-readback", "status", "auth"]
    assert "activation" not in calls and "completion" not in calls


def test_existing_claim_is_readback_only() -> None:
    value = preview()
    original_receipt = {
        **value["source"],
        "operation_id": value["source"]["original_operation_id"],
    }
    value["operation_id"] = recovery.recovery_operation_id(9, original_receipt)
    claim = {
        "schema": recovery.RECOVERY_SCHEMA,
        "state": "claimed",
        "operation_id": value["operation_id"],
        "source": value["source"],
        "preview_fingerprint": FINGERPRINT,
        "target": value["target"],
    }
    marker = recovery._claim_marker(value["operation_id"])
    body = marker + "\n```json\n" + recovery.json.dumps(claim, sort_keys=True) + "\n```"
    client = CommentsClient([body])
    original_collect = recovery.collect_evidence
    original_prove = recovery.prove_repo_only_descendant
    original_state = recovery.collect_prestate
    recovery.collect_evidence = lambda *_args: {"original_receipt": original_receipt}
    recovery.prove_repo_only_descendant = lambda *_args: value["runner"]
    recovery.collect_prestate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        recovery.RecoveryError("target-marker-not-complete")
    )
    try:
        result = recovery.existing_recovery_readback(client, 9, FINGERPRINT, object())
    finally:
        recovery.collect_evidence = original_collect
        recovery.prove_repo_only_descendant = original_prove
        recovery.collect_prestate = original_state
    assert result["state"] == "ambiguous"
    assert len(client.values) == 1


def test_finance_off_prestate_contract() -> None:
    target = SimpleNamespace(
        managed_systemd_units=(SimpleNamespace(name="main.service", enable=True),),
        target_dir="/runtime",
        service_name="main.service",
        environment_file="/env",
        loopback_base_url="http://127.0.0.1:1",
        public_base_url="https://example.invalid",
    )
    script = recovery._prestate_script(target, M)
    for required in (
        "wb-core-finance-liquidity.service",
        "finance-liquidity.sqlite3",
        "/v1/finance/",
        "FINANCE_LIQUIDITY_WRITE_ENABLED",
        "8767",
        "metadata_sha256",
        "MainPID",
    ):
        assert required in script
    namespace: dict = {}
    validator_source = recovery._finance_off_validator_source()
    exec(validator_source, namespace)
    validate = namespace["require_finance_off"]
    exact_absent = "SubState=dead\nLoadState=not-found\nActiveState=inactive\n"
    for off in (None, "", "0", "false", "FALSE", "no", "off", "'off'", '"0"'):
        validate({"flag": {"process": off, "source": None}}, 0, exact_absent)
    for enabled_or_unknown in ("1", "true", "yes", "on", "enabled", "garbage"):
        try:
            validate(
                {"flag": {"process": enabled_or_unknown, "source": None}},
                0,
                exact_absent,
            )
        except SystemExit as exc:
            assert exc.code == 23
        else:
            raise AssertionError(f"Finance flag accepted: {enabled_or_unknown}")
    for returncode, properties in (
        (1, exact_absent),
        (0, "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"),
        (0, "LoadState=masked\nActiveState=inactive\nSubState=dead\n"),
        (0, "LoadState=not-found\nActiveState=failed\nSubState=dead\n"),
    ):
        try:
            validate({"flag": {"process": None, "source": None}}, returncode, properties)
        except SystemExit as exc:
            assert exc.code == 24
        else:
            raise AssertionError(f"Finance unit state accepted: {returncode} {properties!r}")
    assert validator_source.strip() in script
    finance_unit_line = next(
        line for line in script.splitlines() if "e['finance_unit']" in line
    )
    assert "--property=LoadState" in finance_unit_line
    assert "--property=ActiveState" in finance_unit_line
    assert "--property=SubState" in finance_unit_line
    assert "--value" not in finance_unit_line
    byte_literals = [
        node.value
        for node in ast.walk(ast.parse(script))
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes)
    ]
    assert b"\x00" in byte_literals
    sample_environ = (
        b"FIRST=value\x00FINANCE_LIQUIDITY_ENABLED=1\x00LAST=value\x00"
    )
    entries = sample_environ.split(next(value for value in byte_literals if value == b"\x00"))
    parsed = dict(entry.split(b"=", 1) for entry in entries if b"=" in entry)
    assert parsed[b"FINANCE_LIQUIDITY_ENABLED"] == b"1"
    original_remote = recovery._run_remote_json
    recovery._run_remote_json = lambda *_args: {
        "metadata": {"commit": M, "deployment_complete": True}
    }
    try:
        expect_reason(
            "target-marker-not-incomplete",
            lambda: recovery.collect_prestate(target, M, require_incomplete=True),
        )
    finally:
        recovery._run_remote_json = original_remote


def main() -> None:
    test_failure_evidence()
    test_every_intervening_commit_is_repo_only()
    test_exact_tail_and_completion()
    test_activation_failure_never_completes()
    test_target_drift_halts_before_mutation()
    test_existing_claim_is_readback_only()
    test_finance_off_prestate_contract()
    print("post_merge_release_recovery_smoke: ok")


if __name__ == "__main__":
    main()
