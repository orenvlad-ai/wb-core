#!/usr/bin/env python3
"""Regression checks for the bounded warehouse maintenance hold."""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.warehouse_functional_maintenance import (
    WAREHOUSE_FUNCTIONAL_MAINTENANCE_AUDIT_FILENAME,
    WAREHOUSE_FUNCTIONAL_MAINTENANCE_STATE_FILENAME,
    WAREHOUSE_FUNCTIONAL_SERVICE_UNIT,
    WAREHOUSE_FUNCTIONAL_TIMER_UNIT,
    maintenance_hold,
    maintenance_restore,
    maintenance_status,
)


class FakeSystemd:
    def __init__(
        self,
        *,
        service_active_reads: int = 0,
        service_terminal_state: str = "inactive",
    ) -> None:
        self.enabled = "enabled"
        self.timer_active = "active"
        self.service_active_reads = service_active_reads
        self.service_terminal_state = service_terminal_state
        self.mutations: list[tuple[str, str]] = []
        self.timer_digest = "sha256:timer"
        self.service_digest = "sha256:service"

    def scalar(self, action: str, unit: str) -> str:
        if action == "is-enabled" and unit == WAREHOUSE_FUNCTIONAL_TIMER_UNIT:
            return self.enabled
        if action == "is-active" and unit == WAREHOUSE_FUNCTIONAL_TIMER_UNIT:
            return self.timer_active
        if action == "is-active" and unit == WAREHOUSE_FUNCTIONAL_SERVICE_UNIT:
            if self.service_active_reads > 0:
                self.service_active_reads -= 1
                return "active"
            return self.service_terminal_state
        raise AssertionError((action, unit))

    def properties(self, unit: str, names: Sequence[str]) -> dict[str, str]:
        if unit == WAREHOUSE_FUNCTIONAL_TIMER_UNIT:
            return {
                "LoadState": "loaded",
                "UnitFileState": self.enabled,
                "ActiveState": self.timer_active,
                "LastTriggerUSec": "Mon 2026-07-21 19:17:00 UTC",
                "NextElapseUSecRealtime": "Mon 2026-07-21 20:17:00 UTC",
            }
        return {
            "LoadState": "loaded",
            "ActiveState": self.service_terminal_state if self.service_active_reads == 0 else "active",
            "Result": "exit-code" if self.service_terminal_state == "failed" else "success",
            "ExecMainStatus": "1" if self.service_terminal_state == "failed" else "0",
        }

    def cat_digest(self, unit: str) -> str:
        return self.timer_digest if unit == WAREHOUSE_FUNCTIONAL_TIMER_UNIT else self.service_digest

    def mutate(self, action: str, unit: str) -> None:
        self.mutations.append((action, unit))
        if unit != WAREHOUSE_FUNCTIONAL_TIMER_UNIT:
            raise AssertionError(unit)
        if action == "stop":
            self.timer_active = "inactive"
        elif action == "start":
            self.timer_active = "active"
        elif action == "enable":
            self.enabled = "enabled"
        elif action == "disable":
            self.enabled = "disabled"
        else:
            raise AssertionError(action)


def _write_finance_process(proc_root: Path, pid: int = 4321) -> None:
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    (process / "cmdline").write_bytes(
        b"python3\0apps/wb_finance_weekly.py\0canonical-cost-backfill\0--apply\0"
    )


def _assert_maintenance_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw) / "state"
        runtime_dir.mkdir()
        (runtime_dir / ".warehouse-functional-sync.lock").touch()
        proc_root = Path(raw) / "proc"
        proc_root.mkdir()
        systemd = FakeSystemd(service_active_reads=2)

        before = maintenance_status(runtime_dir, client=systemd, proc_root=proc_root)
        assert before["units"]["timer"]["is_enabled"] == "enabled"
        assert before["units"]["timer"]["is_active"] == "active"
        assert before["units"]["service"]["is_active"] == "active"
        assert before["warehouse_lock"]["exists"] is True
        assert before["warehouse_lock"]["held"] is False
        assert before["finance_apply_processes"] == []

        held = maintenance_hold(
            runtime_dir,
            client=systemd,
            proc_root=proc_root,
            wait_timeout_seconds=1,
            poll_interval_seconds=0.01,
        )
        assert held["status"] == "held"
        assert held["units"]["timer"]["is_enabled"] == "enabled"
        assert held["units"]["timer"]["is_active"] == "inactive"
        assert held["units"]["service"]["is_active"] == "inactive"
        assert systemd.mutations == [("stop", WAREHOUSE_FUNCTIONAL_TIMER_UNIT)]
        state_path = runtime_dir / WAREHOUSE_FUNCTIONAL_MAINTENANCE_STATE_FILENAME
        audit_path = runtime_dir / WAREHOUSE_FUNCTIONAL_MAINTENANCE_AUDIT_FILENAME
        assert state_path.stat().st_mode & 0o777 == 0o600
        assert audit_path.stat().st_mode & 0o777 == 0o600
        state = json.loads(state_path.read_text())
        assert state["phase"] == "held"
        assert state["baseline"]["units"]["timer"]["is_active"] == "active"

        held_again = maintenance_hold(
            runtime_dir,
            client=systemd,
            proc_root=proc_root,
        )
        assert held_again["idempotent"] is True
        assert systemd.mutations == [("stop", WAREHOUSE_FUNCTIONAL_TIMER_UNIT)]

        restored = maintenance_restore(runtime_dir, client=systemd, proc_root=proc_root)
        assert restored["status"] == "restored"
        assert restored["units"]["timer"]["is_enabled"] == "enabled"
        assert restored["units"]["timer"]["is_active"] == "active"
        assert systemd.mutations[-2:] == [
            ("enable", WAREHOUSE_FUNCTIONAL_TIMER_UNIT),
            ("start", WAREHOUSE_FUNCTIONAL_TIMER_UNIT),
        ]
        restored_again = maintenance_restore(
            runtime_dir, client=systemd, proc_root=proc_root
        )
        assert restored_again["idempotent"] is True


def _assert_finance_process_blocks_hold() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw) / "state"
        runtime_dir.mkdir()
        proc_root = Path(raw) / "proc"
        proc_root.mkdir()
        _write_finance_process(proc_root)
        systemd = FakeSystemd()
        try:
            maintenance_hold(runtime_dir, client=systemd, proc_root=proc_root)
        except RuntimeError as exc:
            assert "already running" in str(exc)
        else:
            raise AssertionError("Finance apply must block maintenance hold")
        assert systemd.mutations == []


def _assert_failed_oneshot_is_quiescent_evidence() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw) / "state"
        runtime_dir.mkdir()
        proc_root = Path(raw) / "proc"
        proc_root.mkdir()
        systemd = FakeSystemd(service_terminal_state="failed")
        before = maintenance_status(runtime_dir, client=systemd, proc_root=proc_root)
        assert before["units"]["service"]["is_active"] == "failed"
        assert before["units"]["service"]["quiescent"] is True
        assert before["units"]["service"]["properties"]["Result"] == "exit-code"
        held = maintenance_hold(
            runtime_dir,
            client=systemd,
            proc_root=proc_root,
            wait_timeout_seconds=0,
        )
        assert held["status"] == "held"
        assert held["units"]["service"]["is_active"] == "failed"
        assert held["units"]["service"]["quiescent"] is True


def _assert_timed_out_hold_preserves_original_baseline() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime_dir = Path(raw) / "state"
        runtime_dir.mkdir()
        (runtime_dir / ".warehouse-functional-sync.lock").touch()
        proc_root = Path(raw) / "proc"
        proc_root.mkdir()
        systemd = FakeSystemd(service_active_reads=100)
        try:
            maintenance_hold(
                runtime_dir,
                client=systemd,
                proc_root=proc_root,
                wait_timeout_seconds=0,
                poll_interval_seconds=0.01,
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("active service must keep the bounded hold in holding")
        state_path = runtime_dir / WAREHOUSE_FUNCTIONAL_MAINTENANCE_STATE_FILENAME
        state = json.loads(state_path.read_text())
        assert state["phase"] == "holding"
        assert state["baseline"]["units"]["timer"]["is_active"] == "active"
        systemd.service_active_reads = 0
        resumed = maintenance_hold(
            runtime_dir,
            client=systemd,
            proc_root=proc_root,
            wait_timeout_seconds=1,
            poll_interval_seconds=0.01,
        )
        assert resumed["status"] == "held"
        state = json.loads(state_path.read_text())
        assert state["baseline"]["units"]["timer"]["is_active"] == "active"
        restored = maintenance_restore(runtime_dir, client=systemd, proc_root=proc_root)
        assert restored["units"]["timer"]["is_active"] == "active"


def _load_finance_cli() -> Any:
    path = Path(__file__).resolve().with_name("wb_finance_weekly.py")
    spec = importlib.util.spec_from_file_location("wb_finance_weekly_lock_subject", path)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load Finance CLI")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_finance_apply_holds_shared_lock() -> None:
    module = _load_finance_cli()
    events: list[str] = []
    locked = {"value": False}

    class FakeBlock:
        def __init__(self, db_path: Path) -> None:
            self.db_path = db_path

        def plan_canonical_finance_backfill(self, **_: Any) -> dict[str, Any]:
            assert locked["value"] is True
            events.append("plan")
            return {"fingerprint": "sha256:approved", "apply_allowed": True}

        def canonical_finance_fingerprint_applied(self, **_: Any) -> bool:
            assert locked["value"] is True
            events.append("applied-check")
            return False

        def apply_canonical_finance_backfill(self, **_: Any) -> dict[str, Any]:
            assert locked["value"] is True
            events.append("apply-readback")
            return {"status": "applied"}

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        db_path = root / "state.sqlite3"
        db_path.touch()
        block = FakeBlock(db_path)

        @contextmanager
        def fake_lock(_: Path):
            events.append("lock-enter")
            locked["value"] = True
            try:
                yield
            finally:
                locked["value"] = False
                events.append("lock-exit")

        def fake_backup(*_: Any, **__: Any) -> dict[str, Any]:
            assert locked["value"] is True
            events.append("backup")
            return {"integrity_check": "ok"}

        module.block_from_env = lambda _: block
        module.warehouse_functional_write_lock = fake_lock
        module._create_sqlite_backup = fake_backup
        code = module.main(
            [
                "canonical-cost-backfill",
                "--runtime-dir",
                str(root),
                "--env-file",
                str(root / "missing.env"),
                "--apply",
                "--confirm-fingerprint",
                "sha256:approved",
                "--backup-dir",
                str(root / "backup"),
                "--approval-reference",
                "smoke approval",
            ]
        )
        assert code == 0
        assert events == [
            "lock-enter",
            "plan",
            "applied-check",
            "backup",
            "apply-readback",
            "lock-exit",
        ]


def main() -> int:
    _assert_maintenance_lifecycle()
    _assert_finance_process_blocks_hold()
    _assert_failed_oneshot_is_quiescent_evidence()
    _assert_timed_out_hold_preserves_original_baseline()
    _assert_finance_apply_holds_shared_lock()
    print("warehouse functional maintenance smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
