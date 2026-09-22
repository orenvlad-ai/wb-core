#!/usr/bin/env python3
"""Offline regression checks for the bounded post-merge recovery lane."""

from __future__ import annotations

import ast
import json
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

    def run(command: list[str], *, input_text: str | None = None, timeout: int = 90):
        name = command[0]
        calls.append(name)
        status = activation_status if name == "activation" else activation_readback_status if name == "activation-readback" else 0
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
        public_base_url="https://example.invalid",
    )
    script = recovery._prestate_script(target, M)
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
    pilot_active = "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=running\n"
    legacy_absent = "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
    exact_flags = {
        name: {"main_process": "1", "pilot_process": "1", "pilot_source": "1"}
        for name in recovery.FINANCE_FLAGS
    }
    validate(exact_flags, 0, pilot_active, 0, legacy_absent)
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
    assert recovery._selective_b9_diff_proof()["immutable_paths_changed"] == []
    source = Path(recovery.__file__).read_text(encoding="utf-8")
    route = source[source.index("def build_stage_commands"):source.index("def _run_stage")]
    for required in ("_build_autoanswers_prepare_deploy_command", "_build_managed_systemd_commands", "_build_nginx_public_routes_command", "normal_activation_tail"):
        assert required in route
    tail = source[source.index("if case is RecoveryCase.SELECTIVE_B9_ACTIVATION:", source.index("def apply_recovery")):source.index("activation = _run_stage", source.index("def apply_recovery"))]
    for phase in ("autoanswers-prepare-deploy", "systemd-install", "daemon-reload", "nginx", "registry-http-restart", "systemd-reconcile", "root-storage-readback"):
        assert phase in tail
    assert "normal-tail-phase-already-recorded" in tail


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
    value["recovery_case"] = recovery.RecoveryCase.SELECTIVE_B9_ACTIVATION.value
    value["selective_b9_diff"] = {"proof": "normal-test"}
    value["prestate"]["selective_live_contract"] = {"stable": True}
    commands_value = commands()
    commands_value["normal_activation_tail"] = {name: [name] for name in ("prepare", "install", "daemon_reload", "nginx", "restart", "reconcile", "barrier", "storage", "storage_readback", "status", "auth")}
    calls = []
    original = (recovery.build_stage_commands, recovery._run_stage, recovery.collect_prestate, recovery.prove_repo_only_descendant, recovery._selective_b9_diff_proof)
    incomplete = value["prestate"]
    restarted = {**incomplete, "main_pid": 43}
    complete = {**restarted, "metadata": {**restarted["metadata"], "deployment_complete": True}, "metadata_sha256": "7" * 64}
    states = [incomplete, restarted, restarted, complete]
    recovery.build_stage_commands = lambda *_args, **_kwargs: commands_value
    recovery._run_stage = lambda command, **_kwargs: (calls.append(command[0]) or _completed(command, stdout=command[0]))
    recovery.collect_prestate = lambda *_args, **_kwargs: states.pop(0)
    recovery.prove_repo_only_descendant = lambda *_args, **_kwargs: value["runner"]
    recovery._selective_b9_diff_proof = lambda: {"proof": "normal-test"}
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
        (recovery.build_stage_commands, recovery._run_stage, recovery.collect_prestate, recovery.prove_repo_only_descendant, recovery._selective_b9_diff_proof) = original

def main() -> None:
    test_failure_evidence()
    test_every_intervening_commit_is_repo_only()
    test_empty_intervening_commit_is_rejected()
    test_exact_target_requires_no_intervening_diff()
    test_exact_tail_and_completion()
    test_activation_failure_never_completes()
    test_target_drift_halts_before_mutation()
    test_existing_claim_is_readback_only()
    test_finance_pilot_prestate_contract()
    test_selective_b9_contract()
    test_normal_tail_apply_and_claim_replay()
    test_bounded_status_readback_retries_only_read()
    test_safe_remote_failure_diagnostics()
    print("post_merge_release_recovery_smoke: ok")


if __name__ == "__main__":
    main()
