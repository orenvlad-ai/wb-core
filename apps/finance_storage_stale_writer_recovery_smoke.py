#!/usr/bin/env python3
"""Deterministic safety checks for exact stale-writer recovery."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application import finance_storage_stale_writer_recovery as recovery  # noqa: E402
from packages.application.finance_storage_recovery_contract import (  # noqa: E402
    _reviewed_plan_fingerprint,
)


SHA = "a" * 40
MAIN_PID = 101
CHILD_PID = 102
STARTED_AT = "Mon 2026-07-27 05:35:06 UTC"
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


class FakeSystemctl:
    def __init__(
        self,
        *,
        installed_fragment: Path,
        proc_root: Path,
        fail_stop: bool = False,
    ) -> None:
        self.installed_fragment = installed_fragment
        self.proc_root = proc_root
        self.fail_stop = fail_stop
        self.stopped = False
        self.stop_calls = 0

    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["systemctl", "stop"]:
            self.stop_calls += 1
            if self.fail_stop:
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr="injected stop failure",
                )
            self.stopped = True
            for pid in (MAIN_PID, CHILD_PID):
                shutil.rmtree(self.proc_root / str(pid), ignore_errors=True)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        unit = command[2]
        if unit == recovery.SERVICE_UNIT:
            properties = {
                "LoadState": "loaded",
                "UnitFileState": "static",
                "ActiveState": "inactive" if self.stopped else "activating",
                "SubState": "dead" if self.stopped else "start",
                "Result": "success",
                "ExecMainStatus": "0",
                "MainPID": "0" if self.stopped else str(MAIN_PID),
                "ExecMainStartTimestamp": STARTED_AT,
                "TimeoutStartUSec": "30min",
                "ControlGroup": "/system.slice/test.service",
                "FragmentPath": str(self.installed_fragment),
                "NeedDaemonReload": "no",
            }
        elif unit == recovery.TIMER_UNIT:
            properties = {
                "LoadState": "loaded",
                "UnitFileState": "enabled",
                "ActiveState": "active",
                "SubState": "waiting" if self.stopped else "running",
                "Result": "success",
                "ExecMainStatus": "0",
                "MainPID": "0",
                "ExecMainStartTimestamp": "",
                "TimeoutStartUSec": "infinity",
                "ControlGroup": "",
                "FragmentPath": "",
                "NeedDaemonReload": "no",
            }
        else:
            raise AssertionError(f"unexpected unit: {unit}")
        stdout = "".join(
            f"{key}={value}\n" for key, value in properties.items()
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _fixture(root: Path, *, fail_stop: bool = False) -> tuple[
    recovery.FinanceStorageStaleWriterRecovery,
    FakeSystemctl,
    Path,
]:
    runtime = root / "runtime"
    repo = root / "repo"
    proc = root / "proc"
    cgroup = root / "cgroup"
    runtime.mkdir()
    source_fragment = repo / recovery.SERVICE_ARTIFACT
    source_fragment.parent.mkdir(parents=True)
    source_fragment.write_text(
        "[Service]\nTimeoutStartSec=1800\nExecStart=/usr/bin/python3 runner.py\n",
        encoding="utf-8",
    )
    installed_fragment = root / "installed.service"
    shutil.copyfile(source_fragment, installed_fragment)
    (repo / ".wb-core-runtime-sha").write_text(SHA + "\n", encoding="utf-8")
    _write_json(
        runtime / recovery.MAINTENANCE_FILENAME,
        {
            "phase": "restored",
            "exact_prior_state_restored": True,
            "restore_control_signature": {
                "fingerprint": "sha256:" + "b" * 64
            },
        },
    )
    _write_json(
        runtime / recovery.POLICY_FILENAME,
        {
            "schema_version": "auto_updates_owner_policy_v2",
            "revision": 20,
            "policy_fingerprint": "sha256:" + "c" * 64,
            "master_desired": True,
            "processes": {
                "vitrina_closure_retry": {"desired": True}
            },
        },
    )
    for pid, cmdline in (
        (
            MAIN_PID,
            "/usr/bin/python3 /opt/wb-core-runtime/app/"
            + recovery.PROCESS_MARKER,
        ),
        (
            CHILD_PID,
            "/usr/local/lib/python3.12/dist-packages/playwright/driver/node",
        ),
    ):
        process = proc / str(pid)
        (process / "fd").mkdir(parents=True)
        (process / "cmdline").write_bytes(
            cmdline.replace(" ", "\0").encode() + b"\0"
        )
        (process / "wchan").write_text("ep_poll\n", encoding="utf-8")
    cgroup_dir = cgroup / "system.slice" / "test.service"
    cgroup_dir.mkdir(parents=True)
    (cgroup_dir / "cgroup.procs").write_text(
        f"{MAIN_PID}\n{CHILD_PID}\n",
        encoding="utf-8",
    )
    (proc / "net").mkdir()
    for name in ("tcp", "tcp6", "udp", "udp6"):
        (proc / "net" / name).write_text(
            "sl local_address rem_address st tx_queue rx_queue tr tm->when "
            "retrnsmt uid timeout inode\n",
            encoding="utf-8",
        )
    systemctl = FakeSystemctl(
        installed_fragment=installed_fragment,
        proc_root=proc,
        fail_stop=fail_stop,
    )
    runner = recovery.FinanceStorageStaleWriterRecovery(
        runtime,
        deployed_sha=SHA,
        repo_root=repo,
        proc_root=proc,
        cgroup_root=cgroup,
        systemctl=systemctl,
        now_factory=lambda: NOW,
    )
    return runner, systemctl, runtime


def main() -> int:
    unit_text = (ROOT / recovery.SERVICE_ARTIFACT).read_text(
        encoding="utf-8"
    )
    assert unit_text.count("TimeoutStartSec=1800") == 1
    original_barrier_status = recovery.barrier_status
    recovery.barrier_status = lambda _runtime: {
        "active": False,
        "phase": "released",
        "window_id": "snapshot-prior",
    }
    try:
        with TemporaryDirectory(prefix="finance-stale-writer-") as tmp:
            runner, systemctl, runtime = _fixture(Path(tmp))
            plan = runner.build_plan()
            assert plan["stop_allowed_by_machine_preflight"] is True
            assert plan["blockers"] == []
            assert plan["action"]["business_data_mutation_count"] == 0
            assert systemctl.stop_calls == 0

            plan["deploy_lease"] = {
                "lease": {
                    "deployed_sha": SHA,
                    "revision": 1,
                }
            }
            tampered_plan = json.loads(json.dumps(plan))
            tampered_plan["service"]["main_pid"] = MAIN_PID + 1
            assert (
                _reviewed_plan_fingerprint("stale-writer-stop", plan)
                == plan["fingerprint"]
            )
            assert (
                _reviewed_plan_fingerprint(
                    "stale-writer-stop",
                    tampered_plan,
                )
                != plan["fingerprint"]
            )
            try:
                runner.apply(
                    reviewed_plan=tampered_plan,
                    expected_fingerprint=plan["fingerprint"],
                    approval_reference="tampered stale generation",
                )
            except recovery.FinanceStorageStaleWriterRecoveryError as exc:
                assert "not approved" in str(exc)
            else:
                raise AssertionError(
                    "tampered stale-writer plan retained its old fingerprint"
                )
            assert systemctl.stop_calls == 0

            opened = runtime / "registry_upload_runtime.sqlite3"
            opened.write_bytes(b"fixture")
            fd = runner.proc_root / str(MAIN_PID) / "fd" / "9"
            fd.symlink_to(opened)
            blocked = runner.build_plan()
            assert blocked["stop_allowed_by_machine_preflight"] is False
            assert {
                item["code"] for item in blocked["blockers"]
            } == {"runtime_file_descriptor_open"}
            fd.unlink()

            result = runner.apply(
                reviewed_plan=plan,
                expected_fingerprint=plan["fingerprint"],
                approval_reference="task:019fa739 exact stale generation",
            )
            assert result["status"] == "stopped"
            assert result["stop_count"] == 1
            assert systemctl.stop_calls == 1
            assert result["terminal_audit"]["timer_readback"][
                "unit_file_state"
            ] == "enabled"
            assert result["terminal_audit"][
                "owner_policy_unchanged"
            ] is True
            repeated = runner.apply(
                reviewed_plan=plan,
                expected_fingerprint=plan["fingerprint"],
                approval_reference="task:019fa739 exact stale generation",
            )
            assert repeated["status"] == "already_completed"
            assert repeated["stop_count"] == 0
            assert systemctl.stop_calls == 1

        with TemporaryDirectory(prefix="finance-stale-writer-fail-") as tmp:
            runner, systemctl, runtime = _fixture(
                Path(tmp),
                fail_stop=True,
            )
            plan = runner.build_plan()
            try:
                runner.apply(
                    reviewed_plan=plan,
                    expected_fingerprint=plan["fingerprint"],
                    approval_reference="failure injection",
                )
            except recovery.FinanceStorageStaleWriterRecoveryError as exc:
                assert "no retry" in str(exc)
            else:
                raise AssertionError("injected stop failure was accepted")
            assert systemctl.stop_calls == 1
            try:
                runner.apply(
                    reviewed_plan=plan,
                    expected_fingerprint=plan["fingerprint"],
                    approval_reference="forbidden repeated failure",
                )
            except recovery.FinanceStorageStaleWriterRecoveryError as exc:
                assert "already attempted" in str(exc)
            else:
                raise AssertionError(
                    "failed exact stop was automatically repeated"
                )
            assert systemctl.stop_calls == 1
            audit = (
                runtime / recovery.AUDIT_FILENAME
            ).read_text(encoding="utf-8")
            assert '"event":"stale_writer_stop_failed"' in audit

        print("finance_storage_stale_writer_recovery_smoke: 19/19 ok")
        return 0
    finally:
        recovery.barrier_status = original_barrier_status


if __name__ == "__main__":
    raise SystemExit(main())
