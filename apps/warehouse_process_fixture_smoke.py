"""Existing file-lock exclusion and crash release; not future B2 durability."""

from pathlib import Path
import sys
import argparse
import sqlite3
import threading
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.fixture_process import checkpoint, fixture_process
from packages.application.warehouse_functional_lock import (
    WarehouseFunctionalBusyError,
    WarehouseJobOwnershipError,
    require_warehouse_job_owner,
    warehouse_functional_job_lock,
    warehouse_functional_write_lock,
)

LOCKS = {"job": warehouse_functional_job_lock, "write": warehouse_functional_write_lock}


def hold_lock(channel, runtime_dir, kind):
    with LOCKS[kind](runtime_dir, blocking=False):
        if kind == "write":
            with warehouse_functional_write_lock(runtime_dir, blocking=False) as evidence:
                assert evidence["reentrant"] == 1.0
        checkpoint(channel, "held")


def hold_journal(channel, root, db):
    from packages.application.warehouse_update_journal import WarehouseUpdateJournal, PHASES
    with warehouse_functional_job_lock(root):
        journal = WarehouseUpdateJournal(db_path=db, runtime_dir=root)
        run = journal.start(trigger_source="hourly")
        journal.phase_started(run, PHASES[0])
        journal.phase_finished(run, PHASES[0], details={"receipt": "preserve-after-crash"})
        journal.phase_started(run, PHASES[1])
        checkpoint(channel, "journal_running")


def ownership_fixture(root):
    from packages.application.warehouse_update_journal import WarehouseUpdateJournal, PHASES
    from apps.warehouse_functional_runner import _run
    root.mkdir()
    db = root / "journal.sqlite3"
    journal = WarehouseUpdateJournal(db_path=db, runtime_dir=root)
    with fixture_process(hold_journal, root, db) as child:
        child.wait("journal_running")
        with sqlite3.connect(db) as conn:
            run, prior_token = conn.execute("SELECT run_id,owner_token FROM sheet_vitrina_v1_warehouse_update_runs").fetchone()
        # A copied token and process metadata never confer ownership.
        for token in (prior_token, "forged-token"):
            try:
                journal.finish(run, status="failed", owner_token=token)
                raise AssertionError("unowned token changed a live run")
            except WarehouseJobOwnershipError:
                pass
        alien = root / "other-scope"
        with warehouse_functional_job_lock(alien):
            alien_journal = WarehouseUpdateJournal(db_path=db, runtime_dir=alien)
            try:
                alien_journal.start(trigger_source="manual")
                raise AssertionError("another scope interrupted a live owner")
            except WarehouseJobOwnershipError:
                pass
        before = db.read_bytes()
        for command in ("hourly-sync", "manual-sync", "sync-apply"):
            try:
                _run(argparse.Namespace(runtime_dir=str(root), command=command), sqlite_busy_timeout_ms=1)
                raise AssertionError("second actual CLI entry entered admitted scope")
            except WarehouseFunctionalBusyError:
                pass
        assert db.read_bytes() == before and not (root / "registry.sqlite3").exists(), "busy CLI initialized a database"
        child.crash()
    with warehouse_functional_job_lock(root) as owner:
        assert owner["owner_token"] != prior_token
        for token in (prior_token, "forged-token"):
            try:
                journal.finish(run, status="failed", owner_token=token)
                raise AssertionError("reacquire revived old token")
            except WarehouseJobOwnershipError:
                pass
        replacement = journal.start(trigger_source="manual")
        try:
            journal.phase_started(run, PHASES[1])
            raise AssertionError("new owner changed old run")
        except WarehouseJobOwnershipError:
            pass
        try:
            journal.start(trigger_source="manual")
            raise AssertionError("same owner interrupted itself")
        except WarehouseJobOwnershipError:
            pass
        errors = []
        def cross_thread():
            try:
                journal.finish(replacement, status="failed", owner_token=owner["owner_token"])
            except WarehouseJobOwnershipError:
                errors.append("rejected")
        thread = threading.Thread(target=cross_thread); thread.start(); thread.join(2)
        assert errors == ["rejected"] and not thread.is_alive()
        journal.finish(replacement, status="success")
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT status FROM sheet_vitrina_v1_warehouse_update_runs WHERE run_id=?", (run,)).fetchone()[0] == "interrupted"
            receipt = conn.execute("SELECT details_json FROM sheet_vitrina_v1_warehouse_update_phases WHERE run_id=? AND phase_key=?", (run, PHASES[0])).fetchone()[0]
            assert "preserve-after-crash" in receipt
        completed_token = owner["owner_token"]
    try:
        require_warehouse_job_owner(root, completed_token)
        raise AssertionError("released owner remains valid")
    except WarehouseJobOwnershipError:
        pass
    with warehouse_functional_job_lock(root):
        try:
            journal.finish(replacement, status="failed", owner_token=completed_token)
            raise AssertionError("terminal token revived after same-scope reacquire")
        except WarehouseJobOwnershipError:
            pass
    print("ownership: actual CLI busy before constructors; crash takeover, stale/forged/cross-thread token rejected, receipts retained")


def writer_metrics_fixture(root):
    from packages.application.warehouse_sync_lock import warehouse_sync_lock
    with fixture_process(hold_lock, root, "write") as child:
        child.wait("held")
        with warehouse_functional_job_lock(root) as metrics:
            try:
                with warehouse_functional_write_lock(root, timeout_seconds=0.05):
                    raise AssertionError("timeout admitted a conflicting writer")
            except WarehouseFunctionalBusyError:
                pass
            assert metrics["writer_busy_count"] == 1
            assert metrics["writer_wait_ms"] >= 50
        child.release("held"); child.finish()
    with warehouse_functional_job_lock(root) as metrics:
        try:
            with warehouse_functional_write_lock(root):
                with warehouse_sync_lock(root, blocking=False):
                    require_warehouse_job_owner(root)
                raise ValueError("writer fixture exception")
        except ValueError:
            pass
        assert metrics["writer_count"] == 1, "nested compatibility writer was double-counted"
        assert metrics["writer_error_count"] == 1 and metrics["writer_hold_ms"] > 0
        with warehouse_functional_write_lock(root, blocking=False):
            pass
    assert metrics["hold_ms"] is not None
    print("writer metrics: real timeout, exception release and nested wrapper compatibility", metrics)


def main():
    with TemporaryDirectory(prefix="warehouse-process-check-") as temporary:
        root = Path(temporary)
        ownership_fixture(root / "ownership")
        writer_metrics_fixture(root / "writer-metrics")
        for kind, lock in LOCKS.items():
            for crash in (False, True):
                with fixture_process(hold_lock, root, kind) as child:
                    child.wait("held")
                    try:
                        with lock(root, blocking=False):
                            raise AssertionError(f"second process acquired {kind} lock")
                    except WarehouseFunctionalBusyError:
                        pass
                    # Separate admission and writer locks already exist. This
                    # does not prove that HTTP uses the correct admission yet.
                    other = LOCKS["write" if kind == "job" else "job"]
                    with other(root, blocking=False):
                        pass
                    if crash:
                        child.crash()
                    else:
                        child.release("held")
                        child.finish()
                with lock(root, blocking=False):
                    pass
    print("warehouse_process_fixture_smoke: OK (two processes, release and actual termination)")


if __name__ == "__main__":
    main()
