#!/usr/bin/env python3
"""Offline regression checks for the bounded post-merge recovery lane."""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import tempfile
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


def registry_precheck_log(
    gate_run_id: int = 35840710978, *, exit_status: int = 1, exact_stage: bool = True
) -> bytes:
    registry_line = (
        "registry_state_before = unit_state(registry_service)\n"
        "RuntimeError: systemd quiesce service state is invalid: wb-core-registry-http.service\n"
        if exact_stage
        else "RuntimeError: systemd quiesce service state is invalid: wb-core-autoanswers-worker.service\n"
    )
    return f'''python3 apps/github_release_runner.py run --workflow-run-id "{gate_run_id}" --output receipt.json
deploy_current_checkout
wb_autoanswers_activation.py prepare-deploy
{registry_line}subprocess.CalledProcessError: canonical wb_autoanswers_activation.py prepare-deploy
returned non-zero exit status {exit_status}.
'''.encode()


def worker_health_precheck_log(
    gate_run_id: int = 36274008618, *, exit_status: int = 1, exact_stage: bool = True
) -> bytes:
    worker_line = (
        "service_states_before = [unit_state(unit) for unit in services]\n"
        "RuntimeError: systemd quiesce unit is unhealthy: wb-core-autoanswers-worker.service\n"
        if exact_stage else
        "RuntimeError: systemd quiesce unit is unhealthy: wb-core-autoanswers-worker.service\n"
    )
    return f'''python3 apps/github_release_runner.py run --workflow-run-id "{gate_run_id}" --output receipt.json
deploy_current_checkout
wb_autoanswers_activation.py prepare-deploy
{worker_line}subprocess.CalledProcessError: Command ['ssh', 'wb_autoanswers_activation.py prepare-deploy'] returned non-zero exit status {exit_status}.
'''.encode()


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



def test_empty_intervening_commit_is_rejected() -> None:
    original_git = recovery._git
    original_plan = recovery.build_plan_from_paths

    class DescendantClient:
        repository = recovery.REPOSITORY

        def get(self, path: str):
            assert path == "/git/ref/heads/main"
            return {"object": {"sha": C1}}

    def fake_git(args: list[str], *, check: bool = True):
        if args == ["rev-parse", "HEAD"]:
            return _completed(args, stdout=C1 + "\n")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return _completed(args)
        if args[:3] == ["rev-list", "--reverse", "--ancestry-path"]:
            return _completed(args, stdout=C1 + "\n")
        if args[:3] == ["show", "-s", "--format=%P"]:
            return _completed(args, stdout=M + "\n")
        if args[:2] == ["diff", "--name-only"]:
            return _completed(args, stdout="")
        raise AssertionError(args)

    recovery._git = fake_git
    recovery.build_plan_from_paths = lambda **_kwargs: {"release_kind": "repo_only", "plan_sha256": "e" * 64}
    try:
        expect_reason(
            "intervening-commit-not-repo-only",
            lambda: recovery.prove_repo_only_descendant(
                DescendantClient(), {"merge_sha": M, "pull_request": 7}
            ),
        )
    finally:
        recovery._git = original_git
        recovery.build_plan_from_paths = original_plan


def test_exact_target_requires_no_intervening_diff() -> None:
    original_git = recovery._git
    original_plan = recovery.build_plan_from_paths

    class ExactClient:
        repository = recovery.REPOSITORY

        def get(self, path: str):
            assert path == "/git/ref/heads/main"
            return {"object": {"sha": M}}

    def fake_git(args: list[str], *, check: bool = True):
        if args == ["rev-parse", "HEAD"]:
            return _completed(args, stdout=M + "\n")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return _completed(args)
        raise AssertionError(args)

    recovery._git = fake_git
    recovery.build_plan_from_paths = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("exact target must not manufacture an intervening plan")
    )
    try:
        proof = recovery.prove_repo_only_descendant(
            ExactClient(), {"merge_sha": M, "pull_request": 7}
        )
    finally:
        recovery._git = original_git
        recovery.build_plan_from_paths = original_plan
    assert proof == {
        "trusted_main_sha": M,
        "target_merge_sha": M,
        "intervening_proof": "exact-target-no-intervening-commit",
        "intervening_paths": [],
        "intervening_plan_sha256": None,
        "intervening_release_kind": "exact_target",
        "intervening_commits": [],
    }

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
            "finance_pilot": {
                "flags": {},
                "write_mode": "write_enabled",
                "readonly_guard": None,
                "unit_active": True,
                "environment_bound": True,
                "store_present": True,
                "routes_bound": True,
                "loopback_listener": True,
                "legacy_unisolated_absent": True,
            },
        },
        "stages": [
            "root-storage-status-artifact-readback",
            "managed-service-status",
            "auth-preflight",
            "change-registry-activation-exact-target",
            "deployment-metadata-cas-complete",
            "final-runtime-services-health-finance-pilot-readback",
        ],
        "forbidden_stages": ["merge", "rsync", "dependencies", "systemd-install", "restart", "nginx"],
        "finance_acceptance": "write_enabled",
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
        "cleaner_precomplete_probe": ["cleaner-probe"],
        "completion": ["completion"],
        "completion_input": "cas",
    }


def patch_apply(
    *,
    activation_status: int = 0,
    activation_readback_status: int = 0,
    cleaner_status: int = 0,
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

    def run(command: list[str], *, input_text: str | None = None, timeout: int = 90):
        name = command[0]
        calls.append(name)
        status = activation_status if name == "activation" else activation_readback_status if name == "activation-readback" else cleaner_status if name == "cleaner-probe" else 0
        return subprocess.CompletedProcess(command, status, stdout=name, stderr="")

    def state(_target, _merge, *, require_incomplete: bool, **_kwargs):
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
    recovery.collect_evidence = lambda *_args: {
        "original_receipt": original_receipt,
        "recovery_case": recovery.RecoveryCase.STORAGE_TAIL.value,
    }
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


def test_registry_precheck_existing_claim_and_receipt_are_readback_only() -> None:
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
    def body(marker: str, payload: dict) -> str:
        return marker + "\n```json\n" + recovery.json.dumps(payload, sort_keys=True) + "\n```"
    evidence = {
        "original_receipt": original_receipt,
        "recovery_case": recovery.RecoveryCase.REGISTRY_PRECHECK_ACTIVATION.value,
    }
    original_collect = recovery.collect_evidence
    original_prove = recovery.prove_repo_only_descendant
    original_state = recovery.collect_prestate
    recovery.collect_evidence = lambda *_args: evidence
    recovery.prove_repo_only_descendant = lambda *_args: value["runner"]
    calls = []
    try:
        incomplete = CommentsClient([body(recovery._claim_marker(value["operation_id"]), claim)])
        recovery.collect_prestate = lambda *_args, **kwargs: (
            calls.append(kwargs) or (_ for _ in ()).throw(recovery.RecoveryError("target-marker-not-complete"))
        )
        blocked = recovery.existing_recovery_readback(incomplete, 9, FINGERPRINT, object())
        assert blocked["state"] == "blocked"
        assert blocked["reason"] == "normal-claim-incomplete-readback-only"
        assert calls == [{"require_incomplete": False, "case": recovery.RecoveryCase.REGISTRY_PRECHECK_ACTIVATION}]
        assert len(incomplete.values) == 1

        receipt = {
            "schema": recovery.RECOVERY_SCHEMA,
            "state": "complete",
            "operation_id": value["operation_id"],
            "source": value["source"],
            "preview_fingerprint": FINGERPRINT,
        }
        complete = CommentsClient([
            body(recovery._claim_marker(value["operation_id"]), claim),
            body(recovery._receipt_marker(value["operation_id"]), receipt),
        ])
        calls.clear()
        recovery.collect_prestate = lambda *_args, **kwargs: (
            calls.append(kwargs) or {"metadata": {"deployment_complete": True}}
        )
        result = recovery.existing_recovery_readback(complete, 9, FINGERPRINT, object())
        assert result["state"] == "complete"
        assert calls == [{"require_incomplete": False, "case": recovery.RecoveryCase.REGISTRY_PRECHECK_ACTIVATION}]
        assert len(complete.values) == 2
    finally:
        recovery.collect_evidence = original_collect
        recovery.prove_repo_only_descendant = original_prove
        recovery.collect_prestate = original_state


def test_finance_pilot_prestate_contract() -> None:
    target = SimpleNamespace(
        managed_systemd_units=(
            SimpleNamespace(name="main.service", enable=True),
            SimpleNamespace(name="wb-core-finance-liquidity-pilot.service", enable=True),
        ),
        target_dir="/runtime",
        service_name="main.service",
        environment_file="/env",
        loopback_base_url="http://127.0.0.1:1",
        login_health_loopback_base_url="http://127.0.0.1:2",
        public_base_url="https://example.invalid",
    )
    script = recovery._prestate_script(target, M)
    compile(script, "prestate-fixture", "exec")
    assert "http://127.0.0.1:2/login" in script
    assert "owned_loopback_listener(int(pid), main_port)" in script
    assert "'main_loopback': 'http://127.0.0.1:1'" in script
    for required in (
        "wb-core-finance-liquidity-pilot.service",
        "finance-liquidity-pilot.sqlite3",
        "finance-liquidity-pilot.env",
        "wb-core-finance-liquidity.service",
        "/v1/finance/",
        "FINANCE_LIQUIDITY_WRITE_ENABLED",
        "FINANCE_LIQUIDITY_ORIGIN",
        "finance-liquidity-pilot-access.json",
        "8767",
        "EnvironmentFile=",
        "metadata_sha256",
        "MainPID",
    ):
        assert required in script
    assert "finance_pilot" in script
    assert "pilot_source != e['pilot_env_values']" in script
    assert "pilot_proc_env = finance_process_env(pilot_pid)" in script
    assert "unit_environment_files(unit).count(e['pilot_env']) != 1" in script
    assert "require_finance_off" not in script
    ast.parse(script)
    namespace: dict = {}
    validator_source = recovery._finance_pilot_validator_source()
    exec(validator_source, namespace)
    validate = namespace["require_finance_pilot"]
    bind_process = namespace["require_readonly_process_binding"]
    expected_cmdline = [
        "/usr/bin/python3", "/runtime/apps/finance_liquidity_http.py", "--db",
        "/opt/wb-core-runtime/state/finance-liquidity-pilot/finance-liquidity-pilot.sqlite3",
        "--host", "127.0.0.1", "--port", "8767", "--runtime-dir", "/opt/wb-core-runtime/state",
    ]
    expected_env = {
        "FINANCE_LIQUIDITY_ORIGIN": "https://api.selleros.pro",
        "FINANCE_LIQUIDITY_ACCESS_CONFIG": "/runtime/artifacts/finance_liquidity_cash/pilot/finance-liquidity-pilot-access.json",
    }
    bind_process(expected_cmdline, expected_cmdline, "/runtime", "/runtime", expected_env, expected_env)
    for bad_cmdline, bad_cwd, bad_env in (
        (expected_cmdline[:3] + ["/tmp/other.sqlite3"] + expected_cmdline[4:], "/runtime", expected_env),
        (expected_cmdline[:-1] + ["/tmp/other-runtime"], "/runtime", expected_env),
        (expected_cmdline, "/tmp/other-app", expected_env),
        (expected_cmdline, "/runtime", {**expected_env, "FINANCE_LIQUIDITY_ACCESS_CONFIG": "/tmp/other-access.json"}),
        (expected_cmdline, "/runtime", {**expected_env, "FINANCE_LIQUIDITY_ORIGIN": "https://other.invalid"}),
    ):
        try:
            bind_process(bad_cmdline, expected_cmdline, bad_cwd, "/runtime", bad_env, expected_env)
        except SystemExit as exc:
            assert exc.code == 32
        else:
            raise AssertionError("Alternate TEST process binding accepted")
    pilot_active = "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=running\n"
    legacy_absent = "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
    exact_flags = {
        name: {"main_process": "1", "pilot_process": "1", "pilot_source": "1"}
        for name in recovery.FINANCE_FLAGS
    }
    assert validate(exact_flags, 0, pilot_active, 0, legacy_absent) == "write_enabled"
    readonly_flags = {name: dict(values) for name, values in exact_flags.items()}
    readonly_flags["FINANCE_LIQUIDITY_WRITE_ENABLED"]["pilot_process"] = "0"
    guard = {"verified": True, "env_sha256": recovery.FINANCE_READONLY_ENV_SHA256,
             "dropin_sha256": recovery.FINANCE_READONLY_DROPIN_SHA256,
             "schema_version": 3, "store_mode": "isolated_test"}
    assert validate(readonly_flags, 0, pilot_active, 0, legacy_absent, guard) == "readonly_intermediate"
    for bad_flags, bad_guard in (
        (readonly_flags, None),
        (readonly_flags, {**guard, "verified": False}),
        (exact_flags, guard),
        ({**readonly_flags, "FINANCE_LIQUIDITY_READ_ENABLED": {"main_process": "1", "pilot_process": "0", "pilot_source": "1"}}, guard),
        ({**readonly_flags, "FINANCE_LIQUIDITY_WRITE_ENABLED": {"main_process": "0", "pilot_process": "0", "pilot_source": "1"}}, guard),
    ):
        try:
            validate(bad_flags, 0, pilot_active, 0, legacy_absent, bad_guard)
        except SystemExit as exc:
            assert exc.code == 23
        else:
            raise AssertionError("Unproven Finance readonly state accepted")
    for bad_flags in (
        {name: {"main_process": "0", "pilot_process": "1", "pilot_source": "1"} for name in recovery.FINANCE_FLAGS},
        {name: {"main_process": "1", "pilot_process": "0", "pilot_source": "1"} for name in recovery.FINANCE_FLAGS},
        {name: {"main_process": "1", "pilot_process": "1", "pilot_source": None} for name in recovery.FINANCE_FLAGS},
    ):
        try:
            validate(bad_flags, 0, pilot_active, 0, legacy_absent)
        except SystemExit as exc:
            assert exc.code == 23
        else:
            raise AssertionError("Finance pilot flag mismatch accepted")
    for pilot_returncode, pilot_properties in (
        (1, pilot_active),
        (0, "LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\nSubState=dead\n"),
        (0, "LoadState=loaded\nUnitFileState=disabled\nActiveState=active\nSubState=running\n"),
    ):
        try:
            validate(exact_flags, pilot_returncode, pilot_properties, 0, legacy_absent)
        except SystemExit as exc:
            assert exc.code == 24
        else:
            raise AssertionError("Finance pilot unit mismatch accepted")
    try:
        validate(
            exact_flags,
            0,
            pilot_active,
            0,
            "LoadState=loaded\nActiveState=inactive\nSubState=dead\n",
        )
    except SystemExit as exc:
        assert exc.code == 30
    else:
        raise AssertionError("Legacy Finance unit accepted")
    assert validator_source.strip() in script
    for guard_contract in (
        recovery.FINANCE_READONLY_ENV, recovery.FINANCE_READONLY_DROPIN,
        recovery.FINANCE_READONLY_ENV_SHA256, recovery.FINANCE_READONLY_DROPIN_SHA256,
        "DropInPaths", "EnvironmentFiles", "sqlite3.connect", "mode=ro",
        "PRAGMA query_only=ON", "isolated_test", "readonly_intermediate",
    ):
        assert guard_contract in script
    finance_unit_line = next(
        line
        for line in script.splitlines()
        if "--property=LoadState" in line and "e['pilot_unit']" in line
    )
    assert "--property=LoadState" in finance_unit_line
    assert "--property=UnitFileState" in finance_unit_line
    assert "--property=ActiveState" in finance_unit_line
    assert "--property=SubState" in finance_unit_line
    assert "--value" not in finance_unit_line
    byte_literals = [
        node.value
        for node in ast.walk(ast.parse(script))
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes)
    ]
    assert b"\x00" in byte_literals
    sample_environ = b"FIRST=value\x00FINANCE_LIQUIDITY_ENABLED=1\x00LAST=value\x00"
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

def test_safe_remote_failure_diagnostics() -> None:
    secret = "SECRET-token-path-command-output-stderr"

    def target(directory: str) -> SimpleNamespace:
        return SimpleNamespace(
            managed_systemd_units=(SimpleNamespace(name="main.service", enable=True),),
            target_dir=directory,
            service_name="main.service",
            environment_file="/env",
            loopback_base_url="http://127.0.0.1:1",
            public_base_url="https://example.invalid",
        )

    def execute(script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )

    missing = execute(recovery._prestate_script(target(f"/missing-{secret}"), M))
    assert missing.returncode == 1
    assert missing.stdout == ""
    assert secret not in missing.stdout + missing.stderr
    missing_diagnostic = json.loads(missing.stderr)
    assert missing_diagnostic == {
        "errno": 2,
        "exception_category": "file-not-found",
        "schema": recovery.REMOTE_DIAGNOSTIC_SCHEMA,
        "stage": "metadata",
    }
    assert recovery._remote_failure_reason(1, missing.stderr) == (
        "remote-readback-failed-1-stage-metadata-file-not-found-errno-2"
    )

    base = recovery._prestate_script(target("/unused"), M)
    injection_point = "    root = Path(e['target_dir'])"
    injected_exceptions = (
        (
            "subprocess",
            "subprocess_returncode",
            9,
            "    raise subprocess.CalledProcessError(9, ['cmd', %r], output=%r, stderr=%r)"
            % (secret, secret, secret),
        ),
        ("timeout", "errno", 110, "    raise TimeoutError(110, %r)" % secret),
        ("generic", None, None, "    raise RuntimeError(%r)" % secret),
    )
    for category, numeric_name, numeric_value, replacement in injected_exceptions:
        script = base.replace(injection_point, replacement, 1)
        assert script != base
        failed = execute(script)
        assert failed.returncode == 1
        assert failed.stdout == ""
        assert secret not in failed.stdout + failed.stderr
        diagnostic = json.loads(failed.stderr)
        assert diagnostic["stage"] == "metadata"
        assert diagnostic["exception_category"] == category
        if numeric_name is not None:
            assert diagnostic[numeric_name] == numeric_value
        assert secret not in recovery._remote_failure_reason(1, failed.stderr)

    with tempfile.TemporaryDirectory(prefix="recovery-diagnostic-") as directory:
        root = Path(directory)
        (root / ".wb-core-runtime-sha").write_text("wrong\n", encoding="utf-8")
        (root / ".wb-core-deploy.json").write_text(
            json.dumps({"commit": M, "deployment_complete": False}), encoding="utf-8"
        )
        guard = execute(recovery._prestate_script(target(directory), M))
    assert guard.returncode == 20
    assert json.loads(guard.stderr) == {
        "exception_category": "system-exit",
        "guard_status": 20,
        "schema": recovery.REMOTE_DIAGNOSTIC_SCHEMA,
        "stage": "metadata",
    }

    generic = "remote-readback-failed-1"
    malformed = (
        "",
        secret,
        "{bad-json",
        json.dumps(
            {
                "schema": recovery.REMOTE_DIAGNOSTIC_SCHEMA,
                "stage": [],
                "exception_category": "generic",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "schema": recovery.REMOTE_DIAGNOSTIC_SCHEMA,
                "stage": "metadata",
                "exception_category": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "schema": recovery.REMOTE_DIAGNOSTIC_SCHEMA,
                "stage": "unknown-stage",
                "exception_category": "generic",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "schema": recovery.REMOTE_DIAGNOSTIC_SCHEMA,
                "stage": "metadata",
                "exception_category": "SecretCustomError",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "schema": recovery.REMOTE_DIAGNOSTIC_SCHEMA,
                "stage": "metadata",
                "exception_category": "file-not-found",
                "errno": "2",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "schema": recovery.REMOTE_DIAGNOSTIC_SCHEMA,
                "stage": "metadata",
                "exception_category": "generic",
                "extra": secret,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        missing.stderr + "untrusted trailing output",
    )
    for stderr in malformed:
        reason = recovery._remote_failure_reason(1, stderr)
        assert reason == generic
        assert secret not in reason
    pilot_environment_failure = json.dumps(
        {
            "schema": recovery.REMOTE_DIAGNOSTIC_SCHEMA,
            "stage": "pilot-env",
            "exception_category": "system-exit",
            "guard_status": 29,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert recovery._remote_failure_reason(1, pilot_environment_failure) == (
        "remote-readback-failed-1-stage-pilot-env-system-exit-guard_status-29"
    )
    assert recovery._remote_failure_reason(255, "") == "remote-readback-failed-255"

    original_command = recovery._remote_python_command
    original_run = recovery.subprocess.run
    recovery._remote_python_command = lambda _target: ["remote"]
    try:
        recovery.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr=secret
        )
        expect_reason(generic, lambda: recovery._run_remote_json(None, "unused"))
        recovery.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout='{"ok":true}', stderr=secret
        )
        assert recovery._run_remote_json(None, "unused") == {"ok": True}
    finally:
        recovery.subprocess.run = original_run
        recovery._remote_python_command = original_command




def test_selective_b9_contract() -> None:
    assert recovery.recovery_case(recovery.EXPECTED_SELECTIVE_RUN_ID) is recovery.RecoveryCase.SELECTIVE_B9_ACTIVATION
    assert recovery.recovery_case(77) is recovery.RecoveryCase.STORAGE_TAIL
    log = b'''deploy_current_checkout
wb_autoanswers_activation.py prepare-deploy
systemd quiesce unit unhealthy wb-core-autoanswers-worker.service
--workflow-run-id "77"
subprocess.CalledProcessError: Command ["ssh"] returned non-zero exit status 1.
'''
    proof = recovery._prove_failed_stage(log, 77, "One-shot deployed release", case=recovery.RecoveryCase.SELECTIVE_B9_ACTIVATION)
    assert proof["stage"] == "autoanswers-prepare-deploy-drain"
    assert proof["unit"] == "wb-core-autoanswers-worker.service"
    original_git = recovery._git
    allowed_paths = [
        "apps/sheet_vitrina_v1_buyout_confirmation_recovery.py", "apps/sheet_vitrina_v1_buyout_confirmation_recovery_smoke.py",
        "apps/sheet_vitrina_v1_buyout_percent_smoke.py", "ci/checks.json", "ci/post_merge_release_recovery.py",
        "ci/post_merge_release_recovery_smoke.py", "docs/modules/08_MODULE__SALES_FUNNEL_HISTORY_BLOCK.md",
        "packages/application/calculation_parameters_v4.py", "packages/application/sheet_vitrina_v1_buyout_percent.py",
    ]
    def fixture_git(paths):
        def run(args, *, check=True):
            assert args == ["diff", "--name-only", f"{recovery.EXPECTED_SELECTIVE_PREVIOUS_DEPLOYED_SHA}..{recovery.EXPECTED_SELECTIVE_MERGE_SHA}"]
            return _completed(args, stdout="\n".join(paths) + "\n")
        return run
    try:
        recovery._git = fixture_git(allowed_paths)
        assert recovery._selective_b9_diff_proof()["immutable_paths_changed"] == []
        recovery._git = fixture_git([*allowed_paths, "apps/wb_autoanswers_worker.py"])
        expect_reason("selective-b9-diff-not-exact", recovery._selective_b9_diff_proof)
    finally:
        recovery._git = original_git
    source = Path(recovery.__file__).read_text(encoding="utf-8")
    route = source[source.index("def build_stage_commands"):source.index("def _run_stage")]
    for required in ("_build_autoanswers_prepare_deploy_command", "_build_managed_systemd_commands", "_build_nginx_public_routes_command", "normal_activation_tail"):
        assert required in route
    tail = source[source.index("if normal_activation_tail_case(case):", source.index("def apply_recovery")):source.index("activation = _run_stage", source.index("def apply_recovery"))]
    for phase in ("autoanswers-prepare-deploy", "systemd-install", "daemon-reload", "nginx", "registry-http-restart", "systemd-reconcile", "root-storage-readback"):
        assert phase in tail
    assert "normal-tail-phase-already-recorded" in tail


def test_registry_precheck_contract_is_log_bound() -> None:
    assert recovery.recovery_case(35841112474) is recovery.RecoveryCase.STORAGE_TAIL
    proof = recovery._prove_failed_stage(
        registry_precheck_log(),
        35840710978,
        "One-shot deployed release",
        case=recovery.RecoveryCase.REGISTRY_PRECHECK_ACTIVATION,
    )
    assert proof["stage"] == "autoanswers-prepare-deploy-registry-precheck"
    assert proof["unit"] == "wb-core-registry-http.service"
    for bad_log, reason in (
        (registry_precheck_log(exact_stage=False), "failed-stage-not-registry-precheck-exit1"),
        (registry_precheck_log(exit_status=2), "failed-stage-not-registry-precheck-exit1"),
        (registry_precheck_log(gate_run_id=1), "failed-stage-not-registry-precheck-exit1"),
        (registry_precheck_log() + b"subprocess.CalledProcessError: Command ['ssh'] returned non-zero exit status 255.", "failed-stage-not-definite-single-exit1"),
    ):
        expect_reason(
            reason,
            lambda bad_log=bad_log: recovery._prove_failed_stage(
                bad_log,
                35840710978,
                "One-shot deployed release",
                case=recovery.RecoveryCase.REGISTRY_PRECHECK_ACTIVATION,
            ),
        )
    receipt = {
        "schema": recovery.release.RECEIPT_SCHEMA,
        "state": "blocked",
        "reason": "CalledProcessError",
        "release_kind": "live_runtime",
        "deployed_sha": None,
    }
    expect_reason(
        "original-receipt-identity-invalid",
        lambda: recovery._validate_original_receipt(
            receipt, case=recovery.RecoveryCase.REGISTRY_PRECHECK_ACTIVATION
        ),
    )
    source = Path(recovery.__file__).read_text(encoding="utf-8")
    assert "EXPECTED_REGISTRY_PRECHECK_RUN_ID" not in source


def test_worker_health_precheck_is_log_bound_and_normal_tail() -> None:
    log = worker_health_precheck_log()
    assert recovery.recovery_case(36274218397) is recovery.RecoveryCase.STORAGE_TAIL
    case = recovery._activation_case_from_log(log, 36274008618)
    assert case is recovery.RecoveryCase.WORKER_HEALTH_PRECHECK_ACTIVATION
    assert recovery.normal_activation_tail_case(case)
    proof = recovery._prove_failed_stage(log, 36274008618, "One-shot deployed release", case=case)
    assert proof["stage"] == "autoanswers-prepare-deploy-worker-health-precheck"
    assert proof["unit"] == "wb-core-autoanswers-worker.service"
    assert proof["exit_status"] == 1 and proof["job_log_sha256"] == recovery.digest(log)
    for bad_log, reason in (
        (worker_health_precheck_log(exact_stage=False), "failed-stage-not-worker-health-precheck-exit1"),
        (worker_health_precheck_log(exit_status=2), "failed-stage-not-worker-health-precheck-exit1"),
        (worker_health_precheck_log(gate_run_id=1), "failed-stage-not-worker-health-precheck-exit1"),
        (log + b"subprocess.CalledProcessError: Command ['ssh'] returned non-zero exit status 255.", "failed-stage-not-definite-single-exit1"),
        (log + b"subprocess.CalledProcessError: Command ['ssh'] returned non-zero exit status 1.", "failed-stage-not-definite-single-exit1"),
    ):
        expect_reason(
            reason,
            lambda bad_log=bad_log: recovery._prove_failed_stage(
                bad_log, 36274008618, "One-shot deployed release",
                case=recovery.RecoveryCase.WORKER_HEALTH_PRECHECK_ACTIVATION,
            ),
        )
    expect_reason(
        "failed-stage-ambiguous-activation-precheck",
        lambda: recovery._activation_case_from_log(log + registry_precheck_log(36274008618), 36274008618),
    )
    expect_reason(
        "failed-stage-ambiguous-activation-precheck",
        lambda: recovery._activation_case_from_log(
            log + b'run_stage("readback", root_storage_commands["status_artifact_readback"])',
            36274008618,
        ),
    )
    assert recovery._activation_case_from_log(worker_health_precheck_log(gate_run_id=1), 36274008618) is recovery.RecoveryCase.STORAGE_TAIL
    source = Path(recovery.__file__).read_text(encoding="utf-8")
    assert "EXPECTED_WORKER_HEALTH_PRECHECK_RUN_ID" not in source


def test_bounded_status_readback_retries_only_read() -> None:
    original = recovery._run_stage
    calls, sleeps = [], []
    results = [subprocess.CompletedProcess(["status"], 1, stdout="", stderr=""), subprocess.CompletedProcess(["status"], 0, stdout="ready", stderr="")]
    recovery._run_stage = lambda command, **_kwargs: (calls.append(command[0]) or results.pop(0))
    try:
        result = recovery._bounded_status_readback(["status"], sleep=lambda seconds: sleeps.append(seconds))
        assert result["attempt"] == 2 and calls == ["status", "status"] and sleeps == [5.0]
    finally:
        recovery._run_stage = original
    calls, sleeps = [], []
    recovery._run_stage = lambda command, **_kwargs: (calls.append(command[0]) or subprocess.CompletedProcess(command, 255, stdout="", stderr=""))
    try:
        expect_reason("managed-service-status-transport-ambiguous", lambda: recovery._bounded_status_readback(["status"], sleep=lambda seconds: sleeps.append(seconds)))
        assert calls == ["status"] and sleeps == []
    finally:
        recovery._run_stage = original


def test_normal_tail_apply_and_claim_replay() -> None:
    value = preview()
    value["recovery_case"] = recovery.RecoveryCase.REGISTRY_PRECHECK_ACTIVATION.value
    commands_value = commands()
    commands_value["normal_activation_tail"] = {name: [name] for name in ("prepare", "install", "daemon_reload", "nginx", "restart", "reconcile", "barrier", "storage", "storage_readback", "status", "auth")}
    calls = []
    original = (recovery.build_stage_commands, recovery._run_stage, recovery.collect_prestate, recovery.prove_repo_only_descendant)
    incomplete = value["prestate"]
    restarted = {**incomplete, "main_pid": 43}
    complete = {**restarted, "metadata": {**restarted["metadata"], "deployment_complete": True}, "metadata_sha256": "7" * 64}
    states = [incomplete, restarted, restarted, complete]
    recovery.build_stage_commands = lambda *_args, **_kwargs: commands_value
    recovery._run_stage = lambda command, **_kwargs: (calls.append(command[0]) or _completed(command, stdout=command[0]))
    recovery.collect_prestate = lambda *_args, **_kwargs: states.pop(0)
    recovery.prove_repo_only_descendant = lambda *_args, **_kwargs: value["runner"]
    client = CommentsClient()
    try:
        result = recovery.apply_recovery(client, value, FINGERPRINT, object())
        assert result["state"] == "complete"
        assert calls == ["root-readback", "status", "auth", "auth", "storage", "barrier", "prepare", "install", "daemon_reload", "nginx", "restart", "reconcile", "storage", "storage_readback", "status", "auth", "activation", "completion"]
        before_replay = list(calls)
        expect_reason("recovery-identity-already-claimed", lambda: recovery.apply_recovery(client, value, FINGERPRINT, object()))
        assert calls == before_replay
    except recovery.RecoveryError as exc:
        raise AssertionError(exc.reason) from exc
    finally:
        (recovery.build_stage_commands, recovery._run_stage, recovery.collect_prestate, recovery.prove_repo_only_descendant) = original

def sqlite_activation_log(*, gate: int = recovery.EXPECTED_ACTIVATION_GATE_RUN_ID,
                          merge: str = recovery.EXPECTED_ACTIVATION_MERGE_SHA,
                          exit_status: int = 1, extra_failure: bool = False) -> bytes:
    unit = f"wb-core-change-registry-activation@{merge}.service"
    extra = "subprocess.CalledProcessError: Command ['ssh'] returned non-zero exit status 1.\n" if extra_failure else ""
    return f'''Run python3 apps/github_release_runner.py run --workflow-run-id "{gate}" --output receipt.json
File "apps/registry_upload_http_entrypoint_hosted_runtime.py", line 1251, in deploy_current_checkout
    run_stage(
File "apps/registry_upload_http_entrypoint_hosted_runtime.py", line 1178, in run_stage
    _run_command(command)
Job for {unit} failed because the control process exited with error code.
{extra}subprocess.CalledProcessError: Command ['ssh', 'wb-core-eu-root', 'set -eu; systemctl start {unit}; cd /opt/wb-core-runtime/app; python3 apps/change_registry_observer.py --runtime-dir /opt/wb-core-runtime/state --env-file /opt/wb-ai/.env activation-status --deployed-sha {merge}'] returned non-zero exit status {exit_status}.
'''.encode()


def test_sqlite_activation_exact_log_and_receipt() -> None:
    case = recovery.RecoveryCase.SQLITE_ACTIVATION
    for run_id, profile in recovery.SQLITE_ACTIVATION_PROFILES.items():
        gate, merge = profile['gate_run_id'], profile['merge_sha']
        assert recovery.recovery_case(run_id) is case
        proof = recovery._prove_failed_stage(sqlite_activation_log(gate=gate, merge=merge), gate,
                                             'One-shot deployed release', case=case, release_run_id=run_id)
        assert proof['stage'] == 'change-registry-activation' and proof['exit_status'] == 1
        for raw in (sqlite_activation_log(gate=gate, merge='a' * 40),
                    sqlite_activation_log(gate=1, merge=merge),
                    sqlite_activation_log(gate=gate, merge=merge, exit_status=255)):
            try:
                recovery._prove_failed_stage(raw, gate, 'One-shot deployed release',
                                             case=case, release_run_id=run_id)
            except recovery.RecoveryError:
                pass
            else:
                raise AssertionError('mismatched release log was admitted')
        expect_reason('failed-stage-not-definite-single-exit1', lambda: recovery._prove_failed_stage(
            sqlite_activation_log(gate=gate, merge=merge, extra_failure=True), gate,
            'One-shot deployed release', case=case, release_run_id=run_id))
        receipt = {'schema': recovery.release.RECEIPT_SCHEMA, 'state': 'blocked', 'reason': 'CalledProcessError',
                   'release_kind': 'live_runtime', 'deployed_sha': None,
                   'pull_request': profile['pull_request'], 'gate_run_id': gate,
                   'base_sha': profile.get('base_sha', '1' * 40),
                   'head_sha': profile.get('head_sha', '2' * 40), 'merge_sha': merge,
                   'operation_id': 'release-v3-exact'}
        assert recovery._validate_original_receipt(receipt, case=case, release_run_id=run_id)['merge_sha'] == merge
        for key in ('pull_request', 'gate_run_id', 'base_sha', 'head_sha', 'merge_sha'):
            if key in profile:
                wrong = 1 if isinstance(profile[key], int) else 'a' * 40
                expect_reason('original-receipt-not-exact-sqlite-activation', lambda key=key, wrong=wrong:
                    recovery._validate_original_receipt({**receipt, key: wrong}, case=case, release_run_id=run_id))
        other = next(key for key in recovery.SQLITE_ACTIVATION_PROFILES if key != run_id)
        expect_reason('original-receipt-not-exact-sqlite-activation', lambda:
            recovery._validate_original_receipt(receipt, case=case, release_run_id=other))
    assert recovery.recovery_case(37356285296) is recovery.RecoveryCase.STORAGE_TAIL
    expect_reason('sqlite-activation-release-not-supported', lambda: recovery._sqlite_activation_profile(37356285296))


def test_sqlite_activation_remote_rollback_proof(run_id: int) -> None:
    profile = recovery.SQLITE_ACTIVATION_PROFILES[run_id]
    unit = f"wb-core-change-registry-activation@{profile['merge_sha']}.service"
    job = f"crjob_activation_{profile['merge_sha']}"
    invocation = profile.get('invocation_id', 'a' * 32)
    stamp = 1790606411873478
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = root / 'operational.sqlite3'
        conn = sqlite3.connect(db)
        conn.executescript('CREATE TABLE change_registry_observer_jobs(job_id TEXT);'
                           'CREATE TABLE change_registry_observer_job_events(job_id TEXT);'
                           'CREATE TABLE change_registry_observer_leases(owner_job_id TEXT);')
        conn.commit()
        (root / 'storage_generation_manifest.json').write_text(json.dumps({'operational': {'relative_path': db.name}}))
        systemctl = root / 'systemctl.txt'
        systemctl.write_text(f'Result=exit-code\nExecMainCode=1\nExecMainStatus=1\nActiveState=failed\nSubState=failed\nInvocationID={invocation}\n')
        journal = root / 'journal.txt'
        bin_dir = root / 'bin'; bin_dir.mkdir()
        for name, fixture in (('systemctl', systemctl), ('journalctl', journal)):
            command = bin_dir / name
            command.write_text(f'#!/bin/sh\n/bin/cat {fixture}\n')
            command.chmod(0o755)
        expected = {'unit': unit, 'job_id': job, 'runtime_dir': str(root),
                    'invocation_id': profile.get('invocation_id'),
                    'started_at': '2026-09-28T14:37:33Z', 'completed_at': '2026-09-28T14:40:16Z'}
        script = recovery._sqlite_activation_failure_script(expected)
        env = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', '')}

        def record(message: dict, *, record_invocation: str = invocation) -> str:
            return json.dumps({'_SYSTEMD_UNIT': unit, '_SYSTEMD_INVOCATION_ID': record_invocation,
                               '_PID': '460573', '__REALTIME_TIMESTAMP': str(stamp),
                               'MESSAGE': json.dumps(message)}, sort_keys=True)

        exhausted = record({'event': 'sqlite_contention_exhausted', 'phase': 'commit', 'exhausted': True,
                            'owner': 'change_registry_observer.py'})
        failed = record({'status': 'failed', 'error_code': 'SQLiteContentionExhausted'})

        def run(records: list[str]) -> subprocess.CompletedProcess[str]:
            journal.write_text('\n'.join(records) + '\n')
            return subprocess.run([sys.executable, '-c', script], cwd=root, env=env, text=True, capture_output=True)

        success = run([exhausted, failed])
        assert success.returncode == 0, success.stderr
        assert json.loads(success.stdout)['job_rows'] == 0
        first_manifest = json.loads(success.stdout)['manifest_sha256']
        (root / 'storage_generation_manifest.json').write_text(json.dumps({'operational': {'relative_path': db.name}, 'revision': 2}))
        changed = run([exhausted, failed])
        assert changed.returncode == 0 and json.loads(changed.stdout)['manifest_sha256'] != first_manifest
        assert run([record({'event': 'sqlite_contention_exhausted', 'phase': 'commit', 'exhausted': True,
                            'owner': 'change_registry_observer.py'}, record_invocation='b' * 32), failed]).returncode == 41
        assert run([record({'event': 'sqlite_contention_exhausted', 'phase': 'read_statement', 'exhausted': True,
                            'owner': 'change_registry_observer.py'}), failed]).returncode == 44
        if profile.get('invocation_id'):
            systemctl.write_text(systemctl.read_text().replace(invocation, 'b' * 32))
            assert run([exhausted, failed]).returncode == 40
            systemctl.write_text(systemctl.read_text().replace('b' * 32, invocation))
        for table, column in (('change_registry_observer_jobs', 'job_id'),
                              ('change_registry_observer_job_events', 'job_id'),
                              ('change_registry_observer_leases', 'owner_job_id')):
            conn.execute(f'INSERT INTO {table} ({column}) VALUES (?)', (job,)); conn.commit()
            assert run([exhausted, failed]).returncode == 47
            conn.execute(f'DELETE FROM {table}'); conn.commit()
        conn.close()


def test_sqlite_activation_preview_requires_fresh_absence_proof() -> None:
    from apps.registry_upload_http_entrypoint_hosted_runtime import load_hosted_runtime_target

    target = load_hosted_runtime_target(recovery.TARGET_FILE)
    originals = (recovery.collect_evidence, recovery.prove_repo_only_descendant,
                 recovery.collect_prestate, recovery._run_remote_json)
    try:
        for run_id, profile in recovery.SQLITE_ACTIVATION_PROFILES.items():
            merge = profile['merge_sha']
            unit = f'wb-core-change-registry-activation@{merge}.service'
            original = {**preview()['source'], **{key: profile[key] for key in
                        ('pull_request', 'gate_run_id', 'base_sha', 'head_sha', 'merge_sha') if key in profile},
                        'operation_id': 'release-v3-exact'}
            failure = {'stage': 'change-registry-activation', 'unit': unit,
                       'job_started_at': '2026-10-05T18:30:00Z', 'job_completed_at': '2026-10-05T18:33:00Z'}
            evidence = {'original_receipt': original, 'recovery_case': recovery.RecoveryCase.SQLITE_ACTIVATION.value,
                        'failure': failure, 'original_receipt_sha256': 'a' * 64,
                        'gate_plan': {'plan_sha256': 'b' * 64}}
            recovery.collect_evidence = lambda *_args: evidence
            recovery.prove_repo_only_descendant = lambda *_args: preview()['runner']
            recovery.collect_prestate = lambda *_args, **_kwargs: preview()['prestate']
            proof = {'unit': unit, 'job_id': f'crjob_activation_{merge}',
                     'invocation_id': profile.get('invocation_id', 'a' * 32),
                     'job_rows': 0, 'event_rows': 0, 'owned_leases': 0,
                     'commit_failure_at_us': 1, 'manifest_sha256': 'a' * 64,
                     'operational_relative_path': 'operational.sqlite3',
                     'operational_device': 1, 'operational_inode': 2}
            reads = []
            def remote(_target, script):
                reads.append(script)
                assert unit in script and f'crjob_activation_{merge}' in script
                assert "?mode=ro" in script and 'PRAGMA query_only=ON' in script
                if profile.get('invocation_id'):
                    assert profile['invocation_id'] in script
                return proof
            recovery._run_remote_json = remote
            client = CommentsClient()
            result = recovery.build_preview(client, run_id, target)
            assert result['activation_failure_proof'] == proof and len(reads) == 1
            assert client.values == []  # Preview has not claimed or submitted anything.
            for key in ('job_rows', 'event_rows', 'owned_leases'):
                proof[key] = 1
                expect_reason('sqlite-activation-remote-proof-invalid', lambda:
                    recovery.build_preview(client, run_id, target))
                proof[key] = 0
                assert client.values == []
    finally:
        (recovery.collect_evidence, recovery.prove_repo_only_descendant,
         recovery.collect_prestate, recovery._run_remote_json) = originals


def test_sqlite_activation_probe_blocks_metadata_cas_and_replay() -> None:
    value = preview()
    value['recovery_case'] = recovery.RecoveryCase.SQLITE_ACTIVATION.value
    value['failure'] = {'stage': 'change-registry-activation', 'unit': 'exact-unit'}
    value['activation_failure_proof'] = {'job_rows': 0}
    client = CommentsClient()
    calls, originals = patch_apply(cleaner_status=1)
    original_proof = recovery.collect_sqlite_activation_failure
    recovery.collect_sqlite_activation_failure = lambda *_args: value['activation_failure_proof']
    try:
        result = recovery.apply_recovery(client, value, FINGERPRINT, object())
        expect_reason('recovery-identity-already-claimed', lambda: recovery.apply_recovery(client, value, FINGERPRINT, object()))
    finally:
        recovery.collect_sqlite_activation_failure = original_proof
        restore_apply(originals)
    assert result['state'] == 'ambiguous' and result['reason'] == 'cleaner-before-complete-probe-failed-1'
    assert calls == ['root-readback', 'status', 'auth', 'activation', 'cleaner-probe']
    assert 'completion' not in calls
    assert sum(recovery.CLAIM_MARKER in body for body in client.values) == 1
    assert not any(recovery.RECEIPT_MARKER in body for body in client.values)


def test_sqlite_activation_exact_tail_success_and_ssh_ambiguity(run_id: int) -> None:
    from apps.registry_upload_http_entrypoint_hosted_runtime import load_hosted_runtime_target

    target = load_hosted_runtime_target(recovery.TARGET_FILE)
    profile = recovery.SQLITE_ACTIVATION_PROFILES[run_id]
    merge = profile['merge_sha']
    built = recovery.build_stage_commands(target, merge, 'a' * 64, 123,
                                          case=recovery.RecoveryCase.SQLITE_ACTIVATION, release_run_id=run_id)
    assert '--expected-sha ' + merge in built['cleaner_precomplete_probe'][-1]
    assert 'systemctl start wb-core-change-registry-activation@' + merge in built['activation'][-1]
    expect_reason('sqlite-activation-target-contract-invalid', lambda: recovery.build_stage_commands(
        target, 'a' * 40, 'a' * 64, 123, case=recovery.RecoveryCase.SQLITE_ACTIVATION, release_run_id=run_id))
    assert '--phase before_complete' in built['cleaner_precomplete_probe'][-1]
    assert 'normal_activation_tail' not in built
    for activation_status, readback_status, expected_state, expected_calls in (
        (0, 0, 'complete', ['root-readback', 'status', 'auth', 'activation', 'cleaner-probe', 'completion']),
        (255, 1, 'ambiguous', ['root-readback', 'status', 'auth', 'activation', 'activation-readback']),
    ):
        value = preview()
        value['release_run_id'] = run_id
        value['recovery_case'] = recovery.RecoveryCase.SQLITE_ACTIVATION.value
        value['failure'] = {'stage': 'change-registry-activation', 'unit': 'exact-unit'}
        value['activation_failure_proof'] = {'job_rows': 0}
        client = CommentsClient()
        calls, originals = patch_apply(activation_status=activation_status,
                                       activation_readback_status=readback_status)
        original_proof = recovery.collect_sqlite_activation_failure
        proof_calls = []
        def selected_proof(_target, _failure, selected_run):
            proof_calls.append(selected_run)
            return value['activation_failure_proof']
        recovery.collect_sqlite_activation_failure = selected_proof
        try:
            result = recovery.apply_recovery(client, value, FINGERPRINT, object())
        finally:
            recovery.collect_sqlite_activation_failure = original_proof
            restore_apply(originals)
        assert proof_calls == [run_id]
        assert result['state'] == expected_state and calls == expected_calls, (result, calls)
        assert 'restart' not in calls and 'dependencies' not in calls
        assert sum(recovery.CLAIM_MARKER in body for body in client.values) == 1


def test_sqlite_activation_proof_drift_and_existing_claim_are_readback_only() -> None:
    value = preview()
    value['recovery_case'] = recovery.RecoveryCase.SQLITE_ACTIVATION.value
    value['failure'] = {'stage': 'change-registry-activation', 'unit': 'exact-unit'}
    value['activation_failure_proof'] = {'manifest_sha256': 'a' * 64, 'job_rows': 0}
    client = CommentsClient()
    calls, originals = patch_apply()
    original_proof = recovery.collect_sqlite_activation_failure
    recovery.collect_sqlite_activation_failure = lambda *_args: {'manifest_sha256': 'b' * 64, 'job_rows': 0}
    try:
        result = recovery.apply_recovery(client, value, FINGERPRINT, object())
    finally:
        recovery.collect_sqlite_activation_failure = original_proof
        restore_apply(originals)
    assert result['state'] == 'ambiguous' and result['reason'] == 'sqlite-activation-failure-proof-drift-after-claim'
    assert calls == ['root-readback', 'status', 'auth']

    original_receipt = {**value['source'], 'operation_id': value['source']['original_operation_id']}
    value['operation_id'] = recovery.recovery_operation_id(9, original_receipt)
    claim = {'schema': recovery.RECOVERY_SCHEMA, 'state': 'claimed', 'operation_id': value['operation_id'],
             'source': value['source'], 'preview_fingerprint': FINGERPRINT, 'target': value['target']}
    body = recovery._claim_marker(value['operation_id']) + '\n```json\n' + json.dumps(claim, sort_keys=True) + '\n```'
    client = CommentsClient([body])
    originals = recovery.collect_evidence, recovery.prove_repo_only_descendant, recovery.collect_prestate,
    recovery.collect_evidence = lambda *_args: {'original_receipt': original_receipt,
                                                  'recovery_case': recovery.RecoveryCase.SQLITE_ACTIVATION.value}
    recovery.prove_repo_only_descendant = lambda *_args: value['runner']
    recovery.collect_prestate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        recovery.RecoveryError('target-marker-not-complete'))
    try:
        readback = recovery.existing_recovery_readback(client, 9, FINGERPRINT, object())
    finally:
        recovery.collect_evidence, recovery.prove_repo_only_descendant, recovery.collect_prestate = originals
    assert readback['state'] == 'ambiguous' and len(client.values) == 1


def main() -> None:
    test_failure_evidence()
    test_every_intervening_commit_is_repo_only()
    test_empty_intervening_commit_is_rejected()
    test_exact_target_requires_no_intervening_diff()
    test_exact_tail_and_completion()
    test_activation_failure_never_completes()
    test_target_drift_halts_before_mutation()
    test_existing_claim_is_readback_only()
    test_registry_precheck_existing_claim_and_receipt_are_readback_only()
    test_finance_pilot_prestate_contract()
    test_selective_b9_contract()
    test_registry_precheck_contract_is_log_bound()
    test_worker_health_precheck_is_log_bound_and_normal_tail()
    test_normal_tail_apply_and_claim_replay()
    test_bounded_status_readback_retries_only_read()
    test_safe_remote_failure_diagnostics()
    test_sqlite_activation_exact_log_and_receipt()
    for run_id in recovery.SQLITE_ACTIVATION_PROFILES:
        test_sqlite_activation_remote_rollback_proof(run_id)
    test_sqlite_activation_preview_requires_fresh_absence_proof()
    test_sqlite_activation_probe_blocks_metadata_cas_and_replay()
    for run_id in recovery.SQLITE_ACTIVATION_PROFILES:
        test_sqlite_activation_exact_tail_success_and_ssh_ambiguity(run_id)
    test_sqlite_activation_proof_drift_and_existing_claim_are_readback_only()
    print("post_merge_release_recovery_smoke: ok")


if __name__ == "__main__":
    main()
