#!/usr/bin/env python3
"""Actual HTTP admission/journal races, with only external domain services stubbed."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci.fixture_process import checkpoint, fixture_process
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint, SheetVitrinaV1OperatorJobStore
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.warehouse_update_journal import WarehouseUpdateJournal
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock, WarehouseFunctionalBusyError, require_warehouse_job_owner
from packages.application.warehouse_functional import WarehouseFunctionalBlock, WarehouseFunctionalError, _fingerprint

NOW = "2026-08-11T08:00:00Z"


def entry_fixture(root):
    entry = RegistryUploadHttpEntrypoint.__new__(RegistryUploadHttpEntrypoint)
    entry.runtime = RegistryUploadDbBackedRuntime(runtime_dir=root)
    real_block = WarehouseFunctionalBlock(runtime=entry.runtime, timestamp_factory=lambda: NOW)
    from packages.application.fulfillment_services import _ensure_schema
    with sqlite3.connect(entry.runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        conn.commit()
    entry.warehouse_update_journal = WarehouseUpdateJournal(db_path=entry.runtime.db_path, runtime_dir=root)
    entry.operator_jobs = SheetVitrinaV1OperatorJobStore(lambda: datetime.now(timezone.utc).isoformat())
    entry.activated_at_factory = lambda: NOW
    effects = []
    def effect(name, value):
        require_warehouse_job_owner(root)
        effects.append(name)
        return value
    entry.calculation_parameters_block = SimpleNamespace(
        prepare_functional_economics_backup=lambda: effect("backup", {}),
        process_pending_targeted_recalculations=lambda **kw: effect("proxy", {"request_count": 0}),
        publish_current_functional_economics=lambda **kw: effect("economics", {}),
    )
    entry.wb_supplies_block = SimpleNamespace(
        sync_functional_sources=lambda **kw: effect("network", {}),
        collect_all_due_transit_costs=lambda: effect("transit", {}),
        reconcile_functional_ff_state=lambda: effect("ff", {}),
    )
    entry.our_wb_cost_block = SimpleNamespace(materialize_wb_supply_cost_layers=lambda **kw: effect("cost", 0))
    def apply(plan, **kw):
        with warehouse_functional_write_lock(root):
            return effect("apply", {"active_version": {"version_id": "fixture", "business_date": NOW[:10]}})
    entry.warehouse_functional_block = SimpleNamespace(
        build_sync_plan=lambda: effect("candidate", {"plan_fingerprint": "fixture", "diff": {"lines": []}}),
        apply_plan=apply,
        record_failed_sync=lambda exc: effect("failed", None),
    )
    entry.inventory_planning = SimpleNamespace(current=lambda: {})
    entry.wb_finance_weekly_block = SimpleNamespace(recalculate_stale_cost_weeks=lambda: effect("finance", {}))
    entry.runtime.finalize_completed_wb_transit_cost_recalculations = lambda **kw: {}
    return entry, effects, real_block


def wait_terminal(entry, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = entry.handle_warehouse_manual_sync_status_request(run_id)
        if value["status"] not in {"running", "busy", "accepted", "queued"}:
            return value
        time.sleep(0.01)
    raise AssertionError("HTTP worker failed to terminate")


def hold_cli(channel, root, db):
    journal = WarehouseUpdateJournal(db_path=db, runtime_dir=root)
    with warehouse_functional_job_lock(root):
        run = journal.start(trigger_source="hourly")
        checkpoint(channel, "held")
        journal.finish(run, status="success")


def rows(db):
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        return (conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_update_runs ORDER BY run_id").fetchall(),
                conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_update_phases ORDER BY run_id,phase_key").fetchall())


def http_races(root):
    entry, effects, _ = entry_fixture(root)
    # Both never-run and historical manual-success polling must stay truthful.
    assert entry.handle_warehouse_manual_sync_status_request()["status"] == "never"
    assert not (root / ".warehouse-functional-job.lock").exists(), "GET created a lock file"
    with warehouse_functional_job_lock(root):
        historical = entry.warehouse_update_journal.start(trigger_source="manual")
        entry.warehouse_update_journal.finish(historical, status="success")
    with fixture_process(hold_cli, root, entry.runtime.db_path) as child:
        child.wait("held")
        before = rows(entry.runtime.db_path)
        assert entry.handle_warehouse_manual_sync_start_request()["status"] == "busy"
        assert entry.handle_warehouse_manual_sync_start_request()["run_id"] == ""
        lock_path = root / ".warehouse-functional-job.lock"
        lock_before = (lock_path.stat().st_mtime_ns, lock_path.read_bytes())
        poll = entry.handle_warehouse_manual_sync_status_request("")
        assert (lock_path.stat().st_mtime_ns, lock_path.read_bytes()) == lock_before, "GET wrote the lock file"
        assert poll["status"] == "busy" and poll["run_id"] == ""
        assert poll["user_status"] == "Уже выполняется другой пересчёт"
        try:
            entry.handle_warehouse_manual_sync_request()
            raise AssertionError("HTTP entered an admitted CLI scope")
        except WarehouseFunctionalBusyError:
            pass
        assert not effects and not entry.operator_jobs._jobs
        assert rows(entry.runtime.db_path) == before, "loser wrote journal/error state"
        child.release("held"); child.finish()
    released = entry.handle_warehouse_manual_sync_status_request("")
    assert released["status"] == "success" and released["run_id"] == historical
    release, reached = threading.Event(), threading.Event()
    def network(**kw):
        reached.set()
        assert release.wait(5)
        effects.append("network")
        return {}
    entry.wb_supplies_block.sync_functional_sources = network
    gate = threading.Barrier(3)
    def post():
        gate.wait(5)
        return entry.handle_warehouse_manual_sync_start_request()
    with patch("packages.application.fbs_accounting_runtime.refresh", return_value={}):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(post) for _ in range(2)]
            gate.wait(5)
            results = [f.result(5) for f in futures]
        assert reached.wait(5)
        assert sorted(r["status"] for r in results) == ["accepted", "busy"]
        assert results[0]["run_id"] == results[1]["run_id"]
        assert len(entry.operator_jobs._threads) == 1
        assert effects.count("backup") == 1
        release.set()
        final = wait_terminal(entry, results[0]["run_id"])
    assert final["status"] == "success", final
    assert final["user_status"] == "Без изменений: данные уже актуальны"
    assert entry.handle_warehouse_manual_sync_status_request()["run_id"] == final["run_id"], "page reload lost last job"
    metrics = final["technical_details"]["lock_metrics"]
    assert metrics["hold_ms"] > 0 and metrics["writer_count"] == 1
    assert metrics["writer_hold_ms"] < metrics["hold_ms"]
    assert entry.warehouse_update_journal.public_status()["manual_updates"]["status"] == "success"
    entry.warehouse_functional_block.build_sync_plan = lambda: {
        "plan_fingerprint": "changed", "diff": {"changed_line_count": 2,
            "lines": [{"warehouse_key": "ff", "nm_id": 1}, {"warehouse_key": "wb", "nm_id": 1}]}}
    with patch("packages.application.fbs_accounting_runtime.refresh", return_value={}):
        changed = entry.handle_warehouse_manual_sync_start_request()
        changed_result = wait_terminal(entry, changed["run_id"])
    assert changed_result["user_status"] == "Готово: все 6 складов и себестоимости обновлены"
    assert changed_result["changed_warehouses"] == 2 and changed_result["changed_skus"] == 1
    assert changed_result["functional_version_id"] == "fixture"
    assert entry.handle_warehouse_manual_sync_status_request()["run_id"] == changed["run_id"]
    with fixture_process(hold_cli, root, entry.runtime.db_path) as child:
        child.wait("held")
        before = rows(entry.runtime.db_path)
        assert entry.handle_warehouse_manual_sync_start_request()["status"] == "busy"
        assert entry.handle_warehouse_manual_sync_status_request("")["status"] == "busy", "old operator success hid CLI owner"
        assert rows(entry.runtime.db_path) == before
        child.release("held"); child.finish()
    assert entry.handle_warehouse_manual_sync_status_request()["run_id"] == changed["run_id"]
    # A domain failure is terminal under admission and is not admission-busy.
    def failure(**kw):
        raise ValueError("fixture upstream failure")
    entry.wb_supplies_block.sync_functional_sources = failure
    failed = entry.handle_warehouse_manual_sync_start_request()
    result = wait_terminal(entry, failed["run_id"])
    assert result["status"] == "failed" and effects.count("failed") == 1
    assert result["technical_details"]["lock_metrics"]["outcome"] == "error"
    assert entry.warehouse_update_journal.public_status()["manual_updates"]["status"] == "failed"
    with warehouse_functional_job_lock(root):
        pass
    # An unavailable terminal journal cannot be reported as a successful job.
    entry.wb_supplies_block.sync_functional_sources = lambda **kw: {}
    original_finish = entry.warehouse_update_journal.finish
    entry.warehouse_update_journal.finish = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("terminal unavailable"))
    with patch("packages.application.fbs_accounting_runtime.refresh", return_value={}):
        failed_terminal = entry.handle_warehouse_manual_sync_start_request()
        terminal_result = wait_terminal(entry, failed_terminal["run_id"])
    assert terminal_result["status"] == "interrupted" and not terminal_result["can_start_new"]
    assert entry.warehouse_update_journal.public_status()["active_run"]["status"] == "running"
    entry.warehouse_update_journal.finish = original_finish
    with warehouse_functional_job_lock(root):
        pass
    print("HTTP races: CLI x HTTP no writes; two POSTs one worker; success/error terminal and released metrics", metrics)


def cancelled_handshake(root):
    entry, effects, _ = entry_fixture(root)
    blocked, release, finished = threading.Event(), threading.Event(), threading.Event()
    @contextmanager
    def delayed_admission(runtime_dir):
        # Inject a bounded filesystem/admission delay, then take the actual lock.
        blocked.set()
        assert release.wait(8)
        try:
            with warehouse_functional_job_lock(runtime_dir) as metrics:
                yield metrics
        finally:
            finished.set()
    with patch("packages.application.registry_upload_http_entrypoint.warehouse_functional_job_lock", delayed_admission):
        started = time.monotonic()
        result = entry.handle_warehouse_manual_sync_start_request()
        elapsed = time.monotonic() - started
        assert blocked.is_set() and 4.9 <= elapsed < 6.5
        assert result["status"] == "consumer_pending" and result["run_id"] == "" and result["request_accepted"] is None
        release.set()
        assert finished.wait(3)
    assert not effects and not entry.operator_jobs._jobs, "cancelled late worker performed an effect"
    assert not rows(entry.runtime.db_path)[0], "cancelled worker created a durable run"
    print("bounded handshake: delayed real acquisition cancelled before domain effects", round(elapsed * 1000, 3))


def paused_candidate(channel, root):
    entry, effects, real = entry_fixture(root)
    def capture():
        candidate = {"kind": "hourly_wb_sync", "captured_at": NOW,
                     "effective_date": NOW[:10], "wb_snapshot": {"snapshot_date": NOW[:10]},
                     "base_active_version_id": "", "diff": {},
                     "local_source_digest": real._local_source_digest(recovery_end_date=NOW[:10])}
        candidate["plan_fingerprint"] = _fingerprint(candidate)
        checkpoint(channel, "candidate")
        return candidate
    entry.warehouse_functional_block.build_sync_plan = capture
    entry.warehouse_functional_block.apply_plan = real.apply_plan
    try:
        entry.handle_warehouse_manual_sync_request()
        raise AssertionError("stale document source was published")
    except WarehouseFunctionalError as exc:
        assert "local sources drifted" in str(exc), str(exc)
    assert effects.count("failed") == 1
    checkpoint(channel, "cas_rejected")


def post_document(channel, root, db, request_id):
    from packages.application.ff_pool_documents import FfPoolDocumentService
    service = FfPoolDocumentService(runtime_dir=root, db_path=db, timestamp_factory=lambda: NOW, resume=False)
    result = service.post(request_id, defer_replay=True)
    assert result["state"] in {"posted", "complete", "replay"}, result
    checkpoint(channel, "document_committed")


def document_barrier(root):
    from apps.ff_pool_documents_smoke import _seed, Clock, identity
    from packages.application.ff_pool_documents import FfPoolDocumentService, DOCUMENTS_TABLE
    entry, _, _ = entry_fixture(root)
    service = FfPoolDocumentService(runtime_dir=root, db_path=entry.runtime.db_path, timestamp_factory=lambda: NOW, resume=False)
    _seed(service, Clock())
    with sqlite3.connect(entry.runtime.db_path) as conn:
        conn.execute("INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions VALUES('fixture',1,'2026-08-11','fixture','2026-08-11','0',0,'0',0,0,'fixture','{}',1,?,NULL)", (NOW,))
        conn.commit()
    preview = service.accept_preview(identity=identity("b202-opening"), document_kind="facility_pool_opening",
        manifest={"aggregate_rows": [{"nm_id": 101, "quantity": 1, "capital_rub": "100.00"}],
                  "allocations": [{"facility_id": "fac_msk", "pool": "FBS", "nm_id": 101, "quantity": 1, "capital_rub": "100.00"}]})
    assert preview["state"] == "ready", preview
    with fixture_process(paused_candidate, root) as sync:
        sync.wait("candidate")
        started = time.monotonic()
        with fixture_process(post_document, root, entry.runtime.db_path, preview["request_id"]) as document:
            document.wait("document_committed")
            duration = round((time.monotonic() - started) * 1000, 3)
            with sqlite3.connect(entry.runtime.db_path) as conn:
                assert conn.execute(f"SELECT COUNT(*) FROM {DOCUMENTS_TABLE}").fetchone()[0] == 1
            document.release("document_committed"); document.finish()
        # The candidate barrier remained held until AFTER actual document commit.
        sync.release("candidate")
        sync.wait("cas_rejected"); sync.release("cas_rejected"); sync.finish()
    print("C02: real pool document committed before candidate barrier release; actual apply CAS rejected stale source; process-inclusive document ms", duration)


def main():
    with tempfile.TemporaryDirectory(prefix="warehouse-http-admission-") as raw:
        http_races(Path(raw) / "race")
        cancelled_handshake(Path(raw) / "cancel")
        document_barrier(Path(raw) / "document")
    print("warehouse_current_sync_job_smoke: OK")

if __name__ == "__main__":
    main()
