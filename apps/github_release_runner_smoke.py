#!/usr/bin/env python3
"""Offline checks for the compact Release Runner."""

import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

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


def main() -> None:
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
