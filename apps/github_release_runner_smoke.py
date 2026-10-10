#!/usr/bin/env python3
"""Offline checks for the compact Release Runner."""

import io
import json
import os
import sys
import subprocess
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import github_release_runner as runner
from ci.loopback_listener_proof import owned_loopback_listener, remote_source
from ci.select_checks import canonical_bytes


def plan() -> dict:
    value = {
        "schema": "wb-core.check-plan/v1",
        "pull_request": 7,
        "base_sha": "1" * 40,
        "head_sha": "2" * 40,
        "changed_paths": ["docs/example.md"],
        "groups": [],
        "commands": [],
        "pip": [],
        "release_kind": "repo_only",
        "check_map_sha256": "3" * 64,
    }
    value["plan_sha256"] = runner.sha256(canonical_bytes(value))
    return value


def documentation_reuse_checks() -> None:
    """Use real Git trees and the real selector; fake only the GitHub transport."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        def git(*args, **kwargs):
            return subprocess.run(["git", *args], cwd=root, check=True,
                                  capture_output=True, text=True, **kwargs).stdout.strip()

        def save(path, content):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        def commit():
            git("add", ".")
            git("-c", "commit.gpgsign=false", "commit", "-m", "offline fixture")
            return git("rev-parse", "HEAD")

        git("init", "-b", "main")
        git("config", "user.name", "Offline Fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("remote", "add", "origin", str(root))
        save("apps/example.py", "print('old')\n")
        save("docs/guide.md", "old guide\n")
        save("docs/shared.md", "base line\n")
        save("AGENTS.md", "old rules\n")
        (root / "ci").mkdir()
        for name in ("select_checks.py", "checks.json"):
            shutil.copyfile(runner.ROOT / "ci" / name, root / "ci" / name)
        tested_base = commit()
        git("checkout", "-b", "candidate")
        save("apps/example.py", "print('checked candidate')\n")
        save("docs/shared.md", "candidate line\n")
        head = commit()
        git("update-ref", "refs/pull/7/head", head)
        git("checkout", "main")
        save("docs/guide.md", "new ordinary guide\n")
        save("AGENTS.md", "new ordinary rules\n")
        base = commit()
        with patch.object(runner, "ROOT", root):
            original = runner.recompute_plan(7, tested_base, head, trusted_base=base)
            expected_tree = runner.documentation_merge_tree(tested_base, base, head)
            assert git("show", expected_tree + ":apps/example.py") == "print('checked candidate')"
            assert git("show", expected_tree + ":docs/guide.md") == "new ordinary guide"
            gate = dict(id=10, name=runner.WORKFLOW_NAME, path=runner.WORKFLOW_PATH,
                        event="pull_request", run_attempt=1, status="completed", conclusion="success",
                        head_sha=head, pull_requests=[{"number": 7}])

            class Client:
                repository = runner.REPOSITORY
                main_sha = base
                merge_calls = 0
                wrong_tree = False

                def get(self, path):
                    if path == "/pulls/7":
                        return dict(state="open", draft=False, mergeable=True,
                                    head={"sha": head, "repo": {"full_name": self.repository}},
                                    base={"sha": self.main_sha, "ref": "main",
                                          "repo": {"full_name": self.repository}})
                    if path == "/actions/runs/10/jobs?filter=latest&per_page=100":
                        return {"jobs": [dict(name=name, status="completed", conclusion="success")
                                         for name in ("Core", "Plan", "Checks", "pr-gate")]}
                    if path == "/git/ref/heads/main":
                        return {"object": {"sha": self.main_sha}}
                    if path == "/git/commits/" + self.main_sha:
                        return {"parents": [{"sha": base}], "tree": {
                            "sha": "f" * 40 if self.wrong_tree else expected_tree}}
                    raise AssertionError("Unexpected GitHub request: " + path)

                def put(self, path, body):
                    assert path == "/pulls/7/merge" and body == {"sha": head, "merge_method": "squash"}
                    self.merge_calls += 1
                    self.main_sha = git("commit-tree", expected_tree, "-p", base, input="offline squash\n")
                    return {"merged": True, "sha": self.main_sha}

            client = Client()
            with patch.object(runner, "collect_plan", return_value=(gate, original)):
                admitted, checked, pr, actual, candidate = runner.admit(client, 10)
                assert (pr, actual, candidate) == (7, base, head)
                assert checked == original and checked["base_sha"] == tested_base
                assert admitted["admitted_merge_tree"] == expected_tree
                with patch.object(runner, "prepare_deploy_ownership"), \
                        patch.object(runner, "checkout_merge"), \
                        patch.object(runner, "deploy_exact", side_effect=lambda p, h, m, **k: m) as deploy, \
                        patch.object(runner, "publish"):
                    result = runner.run(client, 10, root / "receipt.json")
                    assert result["state"] == "done", result
                    assert result["base_sha"] == base
                    assert result["tested_base_sha"] == tested_base
                    assert result["gate_plan_sha256"] == original["plan_sha256"]
                    assert result["deployed_sha"] == client.main_sha
                    assert client.merge_calls == 1 and deploy.call_count == 1

            # The existing stage-bounded recovery collector must accept the
            # new tested/actual tuple without weakening any failure-stage rule.
            from ci import post_merge_release_recovery as recovery
            blocked = runner.receipt(state="blocked", run_id=10, pr=7, base=base, head=head,
                plan=original, merge=client.main_sha, reason="CalledProcessError")
            log = b'''Run python3 apps/github_release_runner.py run --workflow-run-id "$GATE_RUN_ID" --output receipt.json
  GATE_RUN_ID: 10
deploy_current_checkout
run_stage("readback", root_storage_commands["status_artifact_readback"])
apps/root_storage_policy.py status-readback
subprocess.CalledProcessError: Command ['ssh'] returned non-zero exit status 3.
'''

            class RecoveryClient(Client):
                value = blocked
                event = "workflow_dispatch"
                branch = "main"
                tree_sha = expected_tree
                raw_log = log

                def get(self, path):
                    if path == "/actions/runs/123":
                        return dict(name=recovery.RELEASE_WORKFLOW, path=recovery.RELEASE_WORKFLOW_PATH,
                                    event=self.event, head_branch=self.branch, run_attempt=1,
                                    status="completed", conclusion="failure", head_sha=self.value["base_sha"])
                    if path == "/actions/runs/123/artifacts?per_page=100":
                        return {"artifacts": [dict(id=19, name="release-receipt-10", expired=False)]}
                    if path == "/actions/runs/123/jobs?filter=latest&per_page=100":
                        return {"jobs": [dict(id=17, name="One-shot deployed release", status="completed",
                            conclusion="failure", steps=[dict(conclusion="failure",
                                name="Admit once, merge expected head once, deploy exact merge once, emit one receipt")])]}
                    if path == "/pulls/7":
                        return dict(state="closed", merged=True, merge_commit_sha=self.value["merge_sha"],
                            head={"sha": head, "repo": {"full_name": self.repository}},
                            base={"ref": "main", "repo": {"full_name": self.repository}})
                    if path == "/git/commits/" + self.value["merge_sha"]:
                        return {"parents": [{"sha": self.value["base_sha"]}], "tree": {"sha": self.tree_sha}}
                    if path == "/issues/7/comments?per_page=100&page=1":
                        marker = f"<!-- {runner.RECEIPT_MARKER} operation={self.value['operation_id']} -->"
                        return [{"body": marker + "\n```json\n" + json.dumps(self.value) + "\n```"}]
                    return super().get(path)

                def request(self, method, path, *, raw=False):
                    assert method == "GET" and raw is True
                    if path == "/actions/jobs/17/logs":
                        return self.raw_log
                    assert path == "/actions/artifacts/19/zip"
                    stream = io.BytesIO()
                    with zipfile.ZipFile(stream, "w") as archive:
                        archive.writestr("release-receipt.json", json.dumps(self.value))
                    return stream.getvalue()

            with patch.object(recovery, "ROOT", root), \
                    patch.object(runner, "collect_plan", return_value=(gate, original)):
                evidence = recovery.collect_evidence(RecoveryClient(), 123)
                assert evidence["original_receipt"] == blocked
                assert evidence["gate_plan"]["base_sha"] == tested_base
                assert evidence["failure"]["job_log_sha256"] == recovery.digest(log)
                for formatted in (
                    b"\n".join(b"2026-10-10T10:00:00Z " + line for line in log.splitlines()),
                    log.replace(b"  GATE_RUN_ID: 10", b"\x1b[36;1m  GATE_RUN_ID: 10\x1b[0m"),
                ):
                    formatted_client = RecoveryClient()
                    formatted_client.raw_log = formatted
                    proof = recovery.collect_evidence(formatted_client, 123)
                    assert proof["failure"]["job_log_sha256"] == recovery.digest(formatted)
                for case, reason in (("hash", "original-gate-plan-hash-mismatch"),
                                     ("tested-base", "original-gate-binding-invalid"),
                                     ("tree", "original-merge-tree-mismatch"),
                                     ("branch", "release-provenance-invalid"),
                                     ("event", "release-provenance-invalid"),
                                     ("log-id", "gate-run-log-binding-invalid"),
                                     ("log-missing", "gate-run-log-binding-invalid"),
                                     ("log-duplicate", "gate-run-log-binding-invalid")):
                    recovery_client = RecoveryClient()
                    recovery_client.value = dict(blocked)
                    if case == "hash":
                        recovery_client.value["gate_plan_sha256"] = "f" * 64
                    elif case == "tested-base":
                        recovery_client.value["tested_base_sha"] = head
                    elif case == "tree":
                        recovery_client.tree_sha = "f" * 40
                    elif case == "branch":
                        recovery_client.branch = "untrusted-branch"
                    elif case == "event":
                        recovery_client.event = "push"
                    elif case == "log-id":
                        recovery_client.raw_log = log.replace(b"GATE_RUN_ID: 10", b"GATE_RUN_ID: 11")
                    elif case == "log-missing":
                        recovery_client.raw_log = log.replace(b"  GATE_RUN_ID: 10\n", b"")
                    elif case == "log-duplicate":
                        recovery_client.raw_log = log + b"  GATE_RUN_ID: 10\n"
                    try:
                        recovery.collect_evidence(recovery_client, 123)
                    except recovery.RecoveryError as exc:
                        assert exc.reason == reason, (case, exc.reason)
                    else:
                        raise AssertionError("Invalid recovery evidence admitted: " + case)
                legacy = RecoveryClient()
                legacy.value = runner.receipt(state="blocked", run_id=10, pr=7, base=tested_base,
                    head=head, plan=original, merge=head, reason="CalledProcessError")
                legacy.value.pop("tested_base_sha")
                legacy.value.pop("gate_plan_sha256")
                legacy.event = "workflow_run"
                legacy.raw_log = log.replace(b'"$GATE_RUN_ID"', b'"10"')
                assert recovery.collect_evidence(legacy, 123)["original_receipt"] == legacy.value

            with patch.object(runner, "collect_plan", return_value=(gate, original)):

                client = Client()
                client.wrong_tree = True
                with patch.object(runner, "prepare_deploy_ownership"), \
                        patch.object(runner, "checkout_merge") as checkout, \
                        patch.object(runner, "deploy_exact") as deploy, patch.object(runner, "publish"):
                    result = runner.run(client, 10, root / "blocked.json")
                    assert result["state"] == "blocked" and result["reason"] == "merge-tree-mismatch"
                    checkout.assert_not_called()
                    deploy.assert_not_called()

            # Main/head drift immediately before merge refuses the only PUT.
            for target in ("main", "head"):
                client = Client()
                original_get = client.get
                def changed_get(path):
                    value = original_get(path)
                    if target == "main" and path == "/git/ref/heads/main":
                        value["object"]["sha"] = "f" * 40
                    if target == "head" and path == "/pulls/7":
                        value["head"]["sha"] = "f" * 40
                    return value
                client.get = changed_get
                try:
                    runner.merge_exact(client, 7, base, head, expected_tree=expected_tree)
                except runner.RunnerError as exc:
                    assert exc.reason == "premerge-identity-drift"
                else:
                    raise AssertionError("Premerge " + target + " drift accepted")
                assert client.merge_calls == 0

            altered = dict(original, changed_paths=["docs/shared.md"])
            altered.pop("plan_sha256")
            altered["plan_sha256"] = runner.sha256(canonical_bytes(altered))
            with patch.object(runner, "collect_plan", return_value=(gate, altered)):
                try:
                    runner.admit(Client(), 10)
                except runner.RunnerError as exc:
                    assert exc.reason == "plan-recomputation-mismatch"
                else:
                    raise AssertionError("Unexpected candidate diff hidden by reused plan")
            with patch.object(runner, "collect_plan", return_value=(gate, original)), \
                    patch.object(runner, "recompute_plan", side_effect=[original, altered]):
                try:
                    runner.admit(Client(), 10)
                except runner.RunnerError as exc:
                    assert exc.reason == "current-plan-mismatch"
                else:
                    raise AssertionError("Changed current check plan accepted")

            cases = (
                ("apps/example.py", "unchecked runtime\n", "main-change-requires-new-gate"),
                ("ci/checks.json", "{}\n", "main-change-requires-new-gate"),
                (".github/workflows/example.yml", "on: push\n", "main-change-requires-new-gate"),
                ("requirements.txt", "new dependency\n", "main-change-requires-new-gate"),
                ("artifacts/runtime.md", "runtime artifact\n", "main-change-requires-new-gate"),
                ("docs/config.json", "{}\n", "main-change-requires-new-gate"),
                ("README.md", "not in narrow allowance\n", "main-change-requires-new-gate"),
                ("docs/shared.md", "conflicting main line\n", "documentation-merge-conflict"),
            )
            for path, content, reason in cases:
                git("reset", "--hard", base)
                save(path, content)
                changed = commit()
                try:
                    runner.documentation_merge_tree(tested_base, changed, head)
                except runner.RunnerError as exc:
                    assert exc.reason == reason, (path, exc.reason)
                else:
                    raise AssertionError("Unchecked main change accepted: " + path)
            git("reset", "--hard", base)
            (root / "docs/guide.md").chmod(0o755)
            executable = commit()
            git("reset", "--hard", base)
            (root / "docs/guide.md").unlink()
            (root / "docs/guide.md").symlink_to("../apps/example.py")
            symlink = commit()
            git("reset", "--hard", base)
            git("update-index", "--add", "--cacheinfo", "160000," + head + ",docs/submodule.md")
            git("-c", "commit.gpgsign=false", "commit", "-m", "offline submodule")
            submodule = git("rev-parse", "HEAD")
            for changed in (executable, symlink, submodule):
                try:
                    runner.documentation_merge_tree(tested_base, changed, head)
                except runner.RunnerError as exc:
                    assert exc.reason == "main-change-requires-new-gate"
                else:
                    raise AssertionError("Non-regular documentation entry accepted")
            try:
                runner.documentation_merge_tree(head, base, head)
            except runner.RunnerError as exc:
                assert exc.reason == "tested-base-not-ancestor"
            else:
                raise AssertionError("Non-ancestor tested base accepted")
    print("documentation reuse: real selector/tree proof and negative main/race/tree cases OK")


def main() -> None:
    documentation_reuse_checks()
    with tempfile.TemporaryDirectory() as proc_fixture:
        proc = Path(proc_fixture)
        (proc / "123" / "fd").mkdir(parents=True)
        (proc / "124" / "fd").mkdir(parents=True)
        (proc / "net").mkdir()
        (proc / "123" / "fd" / "3").symlink_to("socket:[9001]")
        (proc / "124" / "fd" / "3").symlink_to("socket:[9002]")
        (proc / "net" / "tcp").write_text(
            "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
            "0: 0100007F:223D 00000000:0000 0A 0:0 00:0 00000000 0 0 9001\n",
            encoding="ascii",
        )
        namespace: dict = {}
        exec(remote_source(), namespace)
        for check in (owned_loopback_listener, namespace["owned_loopback_listener"]):
            assert check(123, 8765, proc_root=proc)
            assert not check(124, 8765, proc_root=proc)
            assert not check(123, 8777, proc_root=proc)
        (proc / "net" / "tcp").write_text(
            "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
            "0: 0100007F:223D 00000000:0000 01 0:0 00:0 00000000 0 0 9001\n",
            encoding="ascii",
        )
        assert not owned_loopback_listener(123, 8765, proc_root=proc)
    value = plan()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("check-plan.json", json.dumps(value))
    assert runner._extract_plan(stream.getvalue()) == value
    operation = runner.operation_id(10, 7, "1" * 40, "2" * 40, value["plan_sha256"])
    assert operation.startswith("release-v3-")
    data = runner.receipt(
        state="done", run_id=10, pr=7, base="1" * 40, head="2" * 40, plan=value, merge="4" * 40
    )
    assert data["deployed_sha"] is None
    assert data["release_kind"] == "repo_only"
    try:
        runner.exact_sha("short", "test")
    except runner.RunnerError:
        pass
    else:
        raise AssertionError("short SHA accepted")

    names = (
        "WB_CORE_DEPLOY_SSH_KEY",
        "WB_CORE_DEPLOY_KNOWN_HOSTS",
        "WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE",
        "WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS",
    )
    previous = {name: os.environ.get(name) for name in names}
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nbody\n-----END OPENSSH PRIVATE KEY-----\n"
    known_hosts = "example.invalid ssh-ed25519 AAAA\n"
    try:
        os.environ["WB_CORE_DEPLOY_SSH_KEY"] = key
        os.environ["WB_CORE_DEPLOY_KNOWN_HOSTS"] = known_hosts
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.configure_ssh(root)
            assert (root / "key").read_text(encoding="utf-8") == key
            assert (root / "known-hosts").read_text(encoding="utf-8") == known_hosts
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    readback = runner.runtime_readback_payload(
        {
            "target_dir": "/srv/app",
            "service_name": "primary.service",
            "loopback_base_url": "http://127.0.0.1:8765",
            "login_health_loopback_base_url": "http://127.0.0.1:8777",
            "public_base_url": "https://example.invalid",
            "managed_systemd_units": [
                {"name": "primary.service", "enable": True},
                {"name": "worker.service", "enable": False},
                {"name": "schedule.timer", "enable": True},
            ],
        },
        "4" * 40,
    )
    assert readback["expected_commit"] == "4" * 40
    assert readback["services"] == ["primary.service"]
    assert readback["main_service"] == "primary.service"
    assert readback["main_loopback"] == "http://127.0.0.1:8765"
    assert readback["urls"] == [
        "http://127.0.0.1:8777/login",
        "https://example.invalid/login",
    ]
    print("github_release_runner_smoke: ok")


if __name__ == "__main__":
    main()
