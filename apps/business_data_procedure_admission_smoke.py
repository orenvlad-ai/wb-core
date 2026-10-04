#!/usr/bin/env python3
"""Private tempfile proof of writer lease ownership and drain races."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.business_data_procedure_admission import (
    AdmissionLease, MaintenanceAdmissionBlocked, admitted_write,
    admission_idle, already_admitted, initialize_admission,
)
from packages.application.business_data_write_barrier import acquire_barrier


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        runtime = Path(directory)
        assert admission_idle(runtime)["ready"] is False
        assert list(runtime.iterdir()) == []
        initialize_admission(runtime)
        assert admission_idle(runtime) == {"ready": True, "idle": True, "reason": ""}
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]

        def writer(index: int) -> None:
            with admitted_write(runtime):
                assert already_admitted(runtime)
                with admitted_write(runtime):
                    assert not admission_idle(runtime)["idle"]
                entered[index].set()
                assert release[index].wait(5)
                assert already_admitted(runtime)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(writer, i) for i in range(2)]
            assert all(event.wait(5) for event in entered)
            acquire_barrier(runtime, window_id="race-test-001", window_kind="maintenance_pause",
                            plan_fingerprint="sha256:" + "a" * 64,
                            approval_reference="test-approval-001", actor="test", reason="admission race")
            release[0].set()
            futures[0].result(5)
            assert not admission_idle(runtime)["idle"], "first finish must not release second lease"
            try:
                with admitted_write(runtime):
                    raise AssertionError("new writer admitted after barrier")
            except MaintenanceAdmissionBlocked:
                pass
            release[1].set()
            futures[1].result(5)
        assert admission_idle(runtime)["idle"]
        assert not already_admitted(runtime)

    with tempfile.TemporaryDirectory() as directory:
        runtime = Path(directory)
        initialize_admission(runtime)
        with admitted_write(runtime):
            acquire_barrier(runtime, window_id="transfer-test-001", window_kind="maintenance_pause",
                            plan_fingerprint="sha256:" + "b" * 64,
                            approval_reference="test-approval-002", actor="test", reason="accepted transfer")
            lease = AdmissionLease(runtime, independent=True)
        assert not admission_idle(runtime)["idle"], "async transfer must own independent SH"
        def async_finish() -> None:
            try:
                with lease.entered():
                    assert already_admitted(runtime)
                    raise ValueError("runner failure")
            except ValueError:
                pass
            finally:
                lease.close()
        thread = threading.Thread(target=async_finish)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and admission_idle(runtime)["idle"]
        # A separate isolated runtime keeps the fork ownership case independent.
        fork_runtime = runtime / "fork"
        fork_runtime.mkdir()
        initialize_admission(fork_runtime)
        lease = AdmissionLease(fork_runtime)
        if hasattr(os, "fork"):
            child = os.fork()
            if child == 0:
                try:
                    try:
                        with lease.entered():
                            os._exit(2)
                    except MaintenanceAdmissionBlocked:
                        lease.close()
                        os._exit(0)
                except BaseException:
                    os._exit(3)
            _, status = os.waitpid(child, 0)
            assert os.waitstatus_to_exitcode(status) == 0
            assert not admission_idle(fork_runtime)["idle"], "child close must not unlock parent"
        lease.close()
        assert admission_idle(runtime)["idle"]
    print("business_data_procedure_admission_smoke: OK")


if __name__ == "__main__":
    main()
