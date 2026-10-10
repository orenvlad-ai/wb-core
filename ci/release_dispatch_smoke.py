#!/usr/bin/env python3
"""Offline admission and workflow checks for releasing an unchanged Draft Gate."""

import json
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps import github_release_runner as runner
from apps.github_release_runner_smoke import plan


def admission_checks() -> None:
    """A previously passed Draft Gate may release only unchanged, ready code."""
    checked = plan()
    gate = dict(id=10, name=runner.WORKFLOW_NAME, path=runner.WORKFLOW_PATH,
                event="pull_request", run_attempt=1, status="completed", conclusion="success",
                head_sha=checked["head_sha"], pull_requests=[{"number": 7}])
    ready = dict(state="open", draft=False, mergeable=True,
                 head={"sha": checked["head_sha"], "repo": {"full_name": runner.REPOSITORY}},
                 base={"sha": checked["base_sha"], "ref": "main",
                       "repo": {"full_name": runner.REPOSITORY}})
    jobs = {"jobs": [dict(name=name, status="completed", conclusion="success")
                     for name in ("Core", "Plan", "Checks", "pr-gate")]}

    class Client:
        repository = runner.REPOSITORY

        def get(self, path):
            if path == "/pulls/7":
                return self.pr
            if path == "/actions/runs/10/jobs?filter=latest&per_page=100":
                return self.jobs
            raise AssertionError("Unexpected request: " + path)

    client = Client()
    client.pr, client.jobs = deepcopy(ready), deepcopy(jobs)
    with patch.object(runner, "collect_plan", return_value=(gate, checked)), \
            patch.object(runner, "trusted_main_sha", return_value=checked["base_sha"]), \
            patch.object(runner, "recompute_plan", return_value=checked):
        client.pr["draft"] = True
        try:
            runner.admit(client, 10)
        except runner.RunnerError as exc:
            assert "pr-not-ready" in exc.reason
        else:
            raise AssertionError("Draft PR admitted")
        client.pr["draft"] = False
        assert runner.admit(client, 10)[2:] == (7, checked["base_sha"], checked["head_sha"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs"
            with patch.object(runner, "GitHub", return_value=client), \
                    patch.dict(os.environ, {"GITHUB_TOKEN": "offline-token"}), \
                    patch.object(sys, "argv", ["runner", "route", "--workflow-run-id", "10",
                                              "--github-output", str(output)]):
                assert runner.main() == 0
            assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
                "release_kind": "repo_only", "deploy_required": "false"}

    # Exercise run() and the real deploy_exact() under a dispatch event. Only
    # GitHub writes, checkout and remote transports are replaced with fixtures.
    from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
    runtime_plan = deepcopy(checked)
    runtime_plan["release_kind"] = "live_runtime"
    runtime_plan.pop("plan_sha256")
    runtime_plan["plan_sha256"] = runner.sha256(runner.canonical_bytes(runtime_plan))
    merge_sha = "4" * 40
    operation = runner.operation_id(10, 7, checked["base_sha"], checked["head_sha"],
                                    runtime_plan["plan_sha256"])
    client.pr, client.jobs = deepcopy(ready), deepcopy(jobs)
    transports = []

    def fake_transport(command, **kwargs):
        transports.append(command)
        if command[1:] == ["apps/registry_upload_http_entrypoint_hosted_runtime.py", "deploy"]:
            env = kwargs["env"]
            assert env["GITHUB_EVENT_NAME"] == "workflow_dispatch"
            assert env["WB_CORE_RELEASE_OPERATION_ID"] == operation
            assert env["WB_CORE_RELEASE_PR"] == "7"
            assert env["WB_CORE_RELEASE_HEAD"] == checked["head_sha"]
            # The adapter receives explicit identity; it must never need the
            # legacy bootstrap's native workflow_run event JSON.
            with patch.dict(os.environ, env):
                assert hosted.resolve_deploy_operation(merge_sha) == operation
        return subprocess.CompletedProcess(command, 0)

    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch",
                                    "GITHUB_EVENT_PATH": str(Path(directory) / "missing-event.json")}), \
            patch.object(runner, "collect_plan", return_value=(gate, runtime_plan)), \
            patch.object(runner, "recompute_plan", return_value=runtime_plan), \
            patch.object(runner, "trusted_main_sha", side_effect=[checked["base_sha"], merge_sha]), \
            patch.object(runner, "merge_exact", return_value=merge_sha) as merge, \
            patch.object(runner, "checkout_merge") as checkout, \
            patch.object(runner, "prepare_deploy_ownership") as ownership, \
            patch.object(runner, "configure_ssh"), \
            patch.object(runner, "runtime_readback") as readback, \
            patch.object(runner.subprocess, "run", side_effect=fake_transport), \
            patch.object(runner, "bootstrap_deploy_operation", side_effect=AssertionError("legacy bootstrap used")), \
            patch.object(runner, "publish") as publish:
        output = Path(directory) / "receipt.json"
        result = runner.run(client, 10, output)
        assert result["state"] == "done", result
        assert result["merge_sha"] == result["deployed_sha"] == merge_sha
        assert result["operation_id"] == operation
        assert json.loads(output.read_text()) == result
        merge.assert_called_once_with(client, 7, checked["base_sha"], checked["head_sha"])
        checkout.assert_called_once_with(merge_sha)
        ownership.assert_called_once_with(operation)
        assert readback.call_count == 1 and readback.call_args.args[1] == merge_sha
        publish.assert_called_once_with(client, result)
        assert len(transports) == 2  # adapter deploy, then owner finish

    for case, reason in (("failed", "workflow-not-successful"),
                         ("head", "workflow-head-mismatch"),
                         ("base", "base-main-drift"),
                         ("plan", "plan-binding-invalid"),
                         ("jobs", "gate-jobs-invalid"),
                         ("closed", "pr-not-ready"),
                         ("recompute", "plan-recomputation-mismatch")):
        current_gate, current_plan = deepcopy(gate), deepcopy(checked)
        client.pr, client.jobs = deepcopy(ready), deepcopy(jobs)
        recomputed = deepcopy(checked)
        if case == "failed":
            current_gate["conclusion"] = "failure"
        elif case == "head":
            client.pr["head"]["sha"] = "5" * 40
        elif case == "base":
            client.pr["base"]["sha"] = "6" * 40
        elif case == "plan":
            current_plan["pull_request"] = 8
        elif case == "jobs":
            client.jobs["jobs"][0]["conclusion"] = "failure"
        elif case == "closed":
            client.pr["state"] = "closed"
        elif case == "recompute":
            recomputed["commands"] = [["python3", "other-check.py"]]
        with patch.object(runner, "collect_plan", return_value=(current_gate, current_plan)), \
                patch.object(runner, "trusted_main_sha", return_value=checked["base_sha"]), \
                patch.object(runner, "recompute_plan", return_value=recomputed), \
                patch.object(runner, "merge_exact") as merge, \
                patch.object(runner, "prepare_deploy_ownership") as ownership, \
                patch.object(runner, "deploy_exact") as deploy, \
                patch.object(runner, "publish") as publish:
            try:
                runner.run(client, 10, Path("unused-receipt.json"))
            except runner.RunnerError as exc:
                assert reason in exc.reason, (case, exc.reason)
            else:
                raise AssertionError(case + " evidence admitted")
            for action in (merge, ownership, deploy, publish):
                action.assert_not_called()


def workflow_checks() -> None:
    # Ruby is already required by PR Gate's workflow syntax check.
    paths = [".github/workflows/pr-gate.yml", ".github/workflows/release-runner.yml"]
    source = 'require "yaml"; require "json"; puts JSON.generate(ARGV.map { |p| YAML.load_file(p) })'
    gate, release = json.loads(subprocess.check_output(["ruby", "-e", source, *paths], cwd=ROOT, text=True))
    assert "ready_for_review" not in gate["true"]["pull_request"]["types"]
    assert release["true"]["workflow_run"]["types"] == ["completed"]
    assert release["true"]["workflow_dispatch"]["inputs"]["workflow_run_id"]["required"] is True
    condition = release["jobs"]["route"]["if"].replace("&&", " and ").replace("||", " or ")
    for event, ref, conclusion, origin, expected in (
        ("workflow_dispatch", "refs/heads/main", "", "", True),
        ("workflow_dispatch", "refs/heads/candidate", "", "", False),
        ("workflow_run", "refs/heads/main", "success", "pull_request", True),
        ("workflow_run", "refs/heads/main", "failure", "pull_request", False),
        ("workflow_run", "refs/heads/main", "success", "push", False),
        ("push", "refs/heads/main", "success", "pull_request", False),
    ):
        github = SimpleNamespace(event_name=event, ref=ref, event=SimpleNamespace(
            workflow_run=SimpleNamespace(conclusion=conclusion, event=origin)))
        assert eval(condition, {"__builtins__": {}}, {"github": github}) is expected

    route_steps = release["jobs"]["route"]["steps"]
    route = next(step for step in route_steps if step.get("id") == "route")
    assert route_steps[0]["with"]["ref"] == "main"
    assert "${{ inputs." not in route["run"]
    # Exercise the actual shell guard with an offline substitute for the router.
    shell = 'python3() { echo "router_called=true" >> "$GITHUB_OUTPUT"; };\n' + route["run"]
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "outputs"
        for run_id in ("10", "", "0", "-1", "+10", "01", " 10", "10\n", "１０",
                       "1; echo bad", "$(echo 10)"):
            output.write_text("")
            result = subprocess.run(["bash", "-c", shell], env={**os.environ,
                "GATE_RUN_ID": run_id, "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True)
            if run_id == "10":
                assert result.returncode == 0, result.stderr
                assert output.read_text() == "router_called=true\ngate_run_id=10\n"
            else:
                assert result.returncode != 0, repr(run_id)
                assert output.read_text() == "", repr(run_id)
        # No run ID may propagate after failed admission.
        rejected = 'python3() { return 1; };\n' + route["run"]
        result = subprocess.run(["bash", "-c", rejected], env={**os.environ,
            "GATE_RUN_ID": "10", "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True)
        assert result.returncode != 0 and output.read_text() == ""
    for name in ("repo_release", "deployed_release"):
        steps = release["jobs"][name]["steps"]
        step = next(s for s in steps if "github_release_runner.py run" in s.get("run", ""))
        assert step["env"]["GATE_RUN_ID"] == "${{ needs.route.outputs.gate_run_id }}"
        assert '--workflow-run-id "$GATE_RUN_ID"' in step["run"]
        assert "${{ inputs." not in step["run"]
        assert steps[0]["with"]["ref"] == "main"
    assert release["concurrency"] == {
        "group": "wb-core-release-${{ inputs.workflow_run_id || github.event.workflow_run.id }}",
        "cancel-in-progress": False}


if __name__ == "__main__":
    admission_checks()
    workflow_checks()
    print("release_dispatch_smoke: ok")
