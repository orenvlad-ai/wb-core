#!/usr/bin/env python3
"""Actual persistent-writer handoff/status boundaries, private fixtures only."""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.business_data_maintenance import legacy_hold_preflight, ALL_BUSINESS_TIMER_UNITS, INDEPENDENT_WRITER_TIMER_UNITS
from packages.application.business_data_procedure_admission import (
    admission_idle, admitted_write, initialize_admission, guarded_cli,
)
from packages.application.business_data_write_barrier import (
    acquire_barrier, barrier_status, confirm_barrier_hold, release_barrier,
)
from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint, SheetVitrinaV1OperatorJobStore,
)
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.contracts.search_cluster_cleaner import Principal
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.warehouse_update_journal import WarehouseUpdateJournal, validate_warehouse_request


def hold(runtime):
    acquire_barrier(runtime, window_id="boundary-test-001", window_kind="maintenance_pause",
                    plan_fingerprint="sha256:" + "c" * 64,
                    approval_reference="boundary-approval", actor="test", reason="boundary test")


def check_async(runtime):
    initialize_admission(runtime)
    store = SheetVitrinaV1OperatorJobStore(lambda: "2026-10-04T12:00:00Z", runtime_dir=runtime)
    entered, finish = threading.Event(), threading.Event()

    def runner(log):
        entered.set()
        assert finish.wait(5)
        raise ValueError("owned runner failure")

    with admitted_write(runtime):
        hold(runtime)  # Barrier raced after HTTP admission, before async handoff.
        job = store.start(operation="test", runner=runner)
    assert entered.wait(5)
    assert store.maintenance_live_jobs() and not admission_idle(runtime)["idle"]
    finish.set()
    store._threads[job["job_id"]].join(5)
    assert store.get(job["job_id"])["status"] == "error"
    assert not store.maintenance_live_jobs() and admission_idle(runtime)["idle"]
    assert barrier_status(runtime)["active"]


def check_start_failure(runtime):
    initialize_admission(runtime)
    store = SheetVitrinaV1OperatorJobStore(lambda: "now", runtime_dir=runtime)
    with patch("threading.Thread.start", side_effect=RuntimeError("thread refused")):
        try:
            store.start(operation="test", runner=lambda log: {})
            raise AssertionError("start failure ignored")
        except RuntimeError:
            pass
    assert not store.maintenance_live_jobs() and admission_idle(runtime)["idle"]
    assert list(store._jobs.values())[0].status == "error"


def check_specialized_warehouse(runtime):
    initialize_admission(runtime)
    store = SheetVitrinaV1OperatorJobStore(lambda: "now", runtime_dir=runtime)
    checked, continue_worker = threading.Event(), threading.Event()
    mutations = []

    def before_recovery(path):
        checked.set()
        assert continue_worker.wait(5)
        return False  # The old raw-check result raced with barrier acquisition.

    journal = SimpleNamespace(recover_and_pick=lambda: mutations.append("recovery") or None)
    with patch("packages.application.registry_upload_http_entrypoint.warehouse_functional_job_lock", return_value=nullcontext({})), \
         patch("packages.application.registry_upload_http_entrypoint.warehouse_start_is_held", side_effect=before_recovery):
        caller = threading.Thread(target=lambda: store.start_warehouse_if_idle(
            runtime_dir=runtime, journal=journal, runner=lambda *args: {}))
        caller.start()
        assert checked.wait(5)
        hold(runtime)
        assert not admission_idle(runtime)["idle"], "unregistered warehouse recovery must be drain-visible"
        continue_worker.set()
        caller.join(5)
        assert not caller.is_alive()
    assert mutations == ["recovery"] and admission_idle(runtime)["idle"]


def check_warehouse_picker_resume(runtime):
    for mode in ("startup-held", "existing-picker", "admission-race"):
        startup_held = mode == "startup-held"
        source = runtime / mode
        source.mkdir()
        initialize_admission(source)
        journal = WarehouseUpdateJournal(db_path=source / "journal.sqlite3", runtime_dir=source)
        key, fingerprint, body = validate_warehouse_request({"request_key": "fixture-pending-001"}, "fixture")
        accepted, created = journal.accept(request_key=key, request_scope="fixture",
                                          payload_fingerprint=fingerprint, request_payload_json=body)
        assert created and accepted["status"] == "accepted"
        store = SheetVitrinaV1OperatorJobStore(lambda: "2026-10-04T12:00:00Z", runtime_dir=source)
        before_hold_sleep, held_sleep = threading.Event(), threading.Event()
        continue_pick, resumed, completed = threading.Event(), threading.Event(), threading.Event()
        raced_admission = threading.Event()
        reads, effects = [], []

        def needs_pickup():
            assert not barrier_status(source)["active"], "journal must not be polled during hold"
            reads.append("needs_pickup")
            pending = journal.needs_pickup()
            if (mode == "admission-race"
                    and threading.current_thread().name == "warehouse-pending-picker"
                    and not raced_admission.is_set()):
                # Exact race: source eligibility returned true, but SH handoff
                # has not happened. The next admitted_thread must refuse.
                hold(source)
                raced_admission.set()
            return pending

        def sleep_picker(seconds):
            assert seconds == 5.0
            if barrier_status(source)["active"]:
                held_sleep.set()
                assert resumed.wait(5)
            else:
                before_hold_sleep.set()
                assert continue_pick.wait(5)

        def runner(log, run_id):
            assert not barrier_status(source)["active"]
            assert not admission_idle(source)["idle"], "pickup effects require shared admission"
            effects.append(run_id)
            journal.finish(run_id, status="success", result={"fixture": True})
            completed.set()
            return {"fixture": True}

        tracked = SimpleNamespace(needs_pickup=needs_pickup, recover_and_pick=journal.recover_and_pick,
                                  claim=journal.claim, lookup=journal.lookup)
        if startup_held:
            hold(source)
        if mode != "existing-picker":
            continue_pick.set()
        # The live-picker case first encounters the normal busy domain owner.
        domain_owner = warehouse_functional_job_lock(source) if mode == "existing-picker" else nullcontext()
        with patch("packages.application.registry_upload_http_entrypoint.time",
                   SimpleNamespace(monotonic=time.monotonic, sleep=sleep_picker)):
            with domain_owner:
                picker = store.resume_warehouse_pending(runtime_dir=source, journal=tracked, runner=runner)
                assert picker is not None
                if mode == "existing-picker":
                    assert before_hold_sleep.wait(5)
                    hold(source)
                    continue_pick.set()
            assert held_sleep.wait(5)
            assert not effects and admission_idle(source)["idle"]
            if startup_held:
                assert not reads
            if mode == "admission-race":
                assert raced_admission.is_set() and len(reads) == 2
            assert store.resume_warehouse_pending(runtime_dir=source, journal=tracked, runner=runner) is picker
            confirm_barrier_hold(source, window_id="boundary-test-001", plan_fingerprint="sha256:" + "c" * 64,
                                 maintenance_state={"schema_version": "business_data_maintenance_pause_v1",
                                                    "phase": "held", "hold_readback": {"quiet": True}})
            release_barrier(source, window_id="boundary-test-001", plan_fingerprint="sha256:" + "c" * 64,
                            actor="test", reason="fixture exact restore",
                            restore_readback={"status": "restored", "exact_prior_state_restored": True})
            resumed.set()
            assert completed.wait(5)
            picker.join(5)
            assert not picker.is_alive()
            for thread in list(store._threads.values()):
                thread.join(5)
            assert effects == [accepted["durable_run_id"]]
            assert journal.lookup(public_id=accepted["job_id"], request_scope="fixture")["status"] == "success"
            assert not journal.needs_pickup() and admission_idle(source)["idle"]
            assert store.resume_warehouse_pending(runtime_dir=source, journal=tracked, runner=runner) is None


def check_status_and_cached_catalog(runtime):
    initialize_admission(runtime)
    app = RegistryUploadHttpEntrypoint.__new__(RegistryUploadHttpEntrypoint)
    app.runtime = SimpleNamespace(runtime_dir=runtime)
    calls = []

    def status(params, *, reconcile=True):
        if reconcile:
            assert not admission_idle(runtime)["idle"]
            hold(runtime)
        calls.append(reconcile)
        return {"reconcile": reconcile}

    app.spp_tester_block = SimpleNamespace(status=status)
    assert app.handle_sheet_prices_spp_test_status_request()["reconcile"]
    assert not app.handle_sheet_prices_spp_test_status_request()["reconcile"]
    assert calls == [True, False]
    for name in ("buyer_session_recovery", "seller_portal_recovery"):
        def read_status(**kwargs):
            assert kwargs["with_probe"] is False
            return {"cached": True}
        setattr(app, name, SimpleNamespace(read_status=read_status))
    assert app.handle_wb_buyer_session_recovery_status_request(launcher_download_path="/cached")["cached"]
    assert app.handle_seller_portal_recovery_status_request(launcher_download_path="/cached")["cached"]
    cleaner = SimpleNamespace(store=SimpleNamespace(registry=SimpleNamespace(runtime_dir=runtime)))
    web = CleanerWeb(cleaner)
    web._registry_ready = lambda: True
    with patch("packages.application.search_cluster_cleaner_web.admitted_thread", side_effect=AssertionError("read spawned source work")):
        assert web._campaign_catalog([1], refresh=True) == ({}, None, False)
        assert web.batch_eligibility(Principal("test", True, True, True, True), refresh=True)["loading"] is False
    constructors = []
    assert guarded_cli(lambda: constructors.append("writer"), argv=["--runtime-dir", str(runtime)]) == 0
    assert not constructors


def main():
    for check in (check_async, check_start_failure, check_specialized_warehouse,
                  check_warehouse_picker_resume, check_status_and_cached_catalog):
        with tempfile.TemporaryDirectory() as directory:
            check(Path(directory))
    baseline = {unit: {"is_enabled": "enabled", "is_active": "active"} for unit in ALL_BUSINESS_TIMER_UNITS}
    core_timer = next(unit for unit in ALL_BUSINESS_TIMER_UNITS if unit not in INDEPENDENT_WRITER_TIMER_UNITS)
    for enabled, active in (("enabled", "inactive"), ("disabled", "active")):
        try:
            baseline[core_timer] = {"is_enabled": enabled, "is_active": active}
            legacy_hold_preflight({"timers": baseline})
            raise AssertionError("legacy mixed state accepted")
        except RuntimeError as exc:
            assert "exact legacy timer baseline" in str(exc), str(exc)
    print("business_data_maintenance_boundary_smoke: OK")


if __name__ == "__main__":
    main()
