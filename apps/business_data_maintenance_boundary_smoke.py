#!/usr/bin/env python3
"""Actual persistent-writer handoff/status boundaries, private fixtures only."""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.business_data_maintenance import legacy_hold_preflight, ALL_BUSINESS_TIMER_UNITS, INDEPENDENT_WRITER_TIMER_UNITS
from packages.application.business_data_procedure_admission import (
    admission_idle, admitted_write, initialize_admission, guarded_cli,
)
from packages.application.business_data_write_barrier import acquire_barrier, barrier_status
from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint, SheetVitrinaV1OperatorJobStore,
)
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.contracts.search_cluster_cleaner import Principal


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
    # A persistent HTTP restart must not launch a warehouse picker during hold.
    journal = SimpleNamespace(needs_pickup=lambda: (_ for _ in ()).throw(AssertionError("startup journal probed")))
    assert store.resume_warehouse_pending(runtime_dir=runtime, journal=journal, runner=lambda *args: {}) is None
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
    for check in (check_async, check_start_failure, check_specialized_warehouse, check_status_and_cached_catalog):
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
