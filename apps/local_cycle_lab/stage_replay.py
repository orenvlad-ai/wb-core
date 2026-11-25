"""Capture full-cycle inputs or run one native warehouse/History stage on a private copy."""
import argparse
import hashlib
from datetime import datetime, timezone
import json
import os
import time
import subprocess
from unittest.mock import patch
from uuid import uuid4

from .full_cycle import execute, expect, publication_cells, stored_ready_cells, verify_cells, verify_surface
from .full_history import isolation_evidence, prepare_history
from .stage_checkpoint import CASE, CONTROL, REFERENCE, SOURCE_ID, SUPPORTED, file_hash, history_root, recording, restore
from .state import atomic_json, read_json


def posted_ledger(runtime):
    from packages.application.ready_publication import readonly
    tables = ("ff_pool_documents", "ff_pool_movement_lines", "ff_pool_overhead_confirmations", "ff_pool_balances")
    result = {}
    with readonly(runtime.db_path) as conn:
        for name in tables:
            # Include every SKU, document type and movement, in stable row order.
            rows = sorted(json.dumps(list(row), ensure_ascii=False) for row in
                          conn.execute("SELECT * FROM sheet_vitrina_v1_" + name))
            result[name] = {"rows": len(rows), "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest()}
    return result


class AttemptStore:
    def __init__(self, output):
        self.output = output

    def write(self, receipt):
        atomic_json(self.output / "attempt.json", receipt)


def invoke(entry, config, source, stage, checkpoint_hash, *, output=CONTROL, collection_hash=None):
    from packages.application.fbs_accounting_degraded import degraded_accounting_scope
    from packages.application.business_data_procedure_admission import admitted_write
    from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog
    from packages.application.sheet_vitrina_v1_cycle import (
        STAGES, finance_raw_proof, require_cycle_business_date, run_closed_ready_stage,
        run_final_ready_stage, run_rolling_history_stage,
    )
    from packages.application.web_vitrina_snapshot_admission import process_identity

    stages = {row["stage"]: row for row in source["stages"]}
    versions = dict(source["final_versions"])
    fbs = {**stages["fbs_generation"]["versions"]}
    # The warehouse may have refreshed the catalog once after source collection.
    fbs.update({key: versions[key] for key in fbs})
    finance = stages["finance_sources"]["versions"]
    if stage not in (*SUPPORTED, "daily_projection"):
        raise ValueError("Unsupported recovery stage")
    resume_warehouse = stage == "warehouse" and collection_hash is not None
    suffix = stage == "daily_projection" or resume_warehouse
    selected = ("daily_projection", "closed_ready", "final_ready", "rolling14") if suffix else (stage,)
    scope = "publication_suffix" if suffix else "one_stage"
    if resume_warehouse:
        selected = ("warehouse", *selected)
        scope = "warehouse_and_publication"
    if suffix:
        expect(selected == STAGES[STAGES.index(stage):], "Native publication stage order changed")
    fresh = dict(schema="local_selected_stage_v1", cycle_id=uuid4().hex, source_cycle=source["cycle_id"],
                 source_job=source["job_id"], checkpoint_sha256=checkpoint_hash, replay_code=read_json(SOURCE_ID),
                 scope=scope, current_stage=stage, business_date=source["business_date"],
                 slot_utc=entry.now_factory().isoformat(), owner_pid=os.getpid(),
                 process_identity=process_identity(os.getpid()), status="accepted",
                 history_config_fingerprint=config.fingerprint(), final_versions=versions,
                 stages=[dict(stage=name, status="pending") for name in selected])
    expect(fresh["history_config_fingerprint"] == source["history_config_fingerprint"], "History authority differs")
    store = AttemptStore(output)

    def accepted(job_id):
        fresh["job_id"] = job_id
        store.write(fresh)

    def actions():
        if stage == "warehouse" and not suffix:
            raw = finance_raw_proof(entry)
            expect(all(finance[key] == value for key, value in raw.items()), "Finance source changed")
            from packages.application.official_fbs_stock_read import read_complete_official_fbs_stock
            from packages.application.ready_publication import readonly
            with readonly(entry.runtime.db_path) as conn:
                observed = read_complete_official_fbs_stock(conn, universe=None, day=fresh["business_date"], now=entry.now_factory())
            expect(observed["generation_id"] == fbs["fbs_generation"] and observed["generation_digest"] == fbs["fbs_digest"],
                   "FBS generation changed")
            yield entry._cycle_warehouse(store, fresh, fbs)
            return
        if suffix:
            from .publication_checkpoint import restore as restore_collection
            adapter, handle, closed = restore_collection(entry, source, output / "collection.json", collection_hash, finance, fbs)
            if resume_warehouse:
                yield entry._cycle_warehouse(store, fresh, fbs)
            yield entry._cycle_daily_projection()
            dates, proof = run_closed_ready_stage(entry, fresh, adapter, handle, finance, fbs, closed)
            yield proof
            ready, proof = run_final_ready_stage(entry, fresh, adapter, handle, finance, fbs)
            del handle
            yield proof
        else:
            entry._cycle_validate_predecessors(finance, fbs, versions)
            dates = tuple(json.loads(versions["closed_backfill_dates"]))
            closed = ClosedBacklog(entry._cycle_sources()) if dates else None
            ready = stages["final_ready"]["versions"]
        yield run_rolling_history_stage(entry, store, fresh, config, ready=ready,
            finance=finance, fbs=fbs, backfill_dates=dates, closed=closed)

    def worker(log):
        with entry._sheet_cycle_lock:
            row = None
            try:
                entry._cycle_owned_worker_identity(fresh)
                steps = actions()
                for row in fresh["stages"]:
                    require_cycle_business_date(fresh["business_date"], entry.now_factory())
                    row.update(status="running", started_at=entry.activated_at_factory())
                    fresh.update(status="running", current_stage=row["stage"])
                    store.write(fresh)
                    with degraded_accounting_scope(entry.runtime, versions):
                        proof = next(steps)
                    require_cycle_business_date(fresh["business_date"], entry.now_factory())
                    expect(proof.versions and all(isinstance(v, str) and v for v in proof.versions.values()),
                           "Native stage proof missing")
                    row.update(status="degraded" if proof.warnings else "complete", versions=proof.versions,
                               warnings=list(proof.warnings), finished_at=entry.activated_at_factory())
                    versions.update(proof.versions)
                    store.write(fresh)
                steps.close()
                fresh.update(status="degraded" if any(s["warnings"] for s in fresh["stages"]) else "complete",
                             current_stage="" if suffix else stage)
            except BaseException as exc:
                fresh.update(status="failed", error_code=type(exc).__name__)
                if row is not None and row["status"] == "running":
                    row.update(status="failed", finished_at=entry.activated_at_factory())
                raise
            finally:
                store.write(fresh)
            return fresh

    with admitted_write(entry.runtime.runtime_dir):
        job = entry.operator_jobs.start(operation="cycle", runner=worker, on_accept=accepted)
    expect(job["job_id"] == fresh["job_id"], "Unexpected single flight")
    entry.operator_jobs._threads[job["job_id"]].join()
    log, _ = entry.operator_jobs.get_text(job["job_id"])
    (output / "stage.log").write_text(log)
    terminal = entry.operator_jobs.get(job["job_id"])
    expect(terminal["status"] == "success", f"Stage job failed: {terminal.get('error') or terminal.get('message') or terminal}")
    return read_json(output / "attempt.json")


def replay(stage):
    from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
    from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint, SheetVitrinaV1OperatorJobStore
    from packages.application.operator_ff_overhead import read_acceptance, journal
    from .full_sources import configure_sources
    from .full_warehouse import verify
    from .sources import Replay

    started = time.monotonic()
    point, manifest = restore(stage)
    source = read_json(point / "source-receipt.json")
    expect(source["cycle_id"] == manifest["source_cycle"] and source["current_stage"] == stage,
           "Checkpoint lineage differs")
    scenario = read_json(point / "scenario.json")
    now = lambda: datetime.now(timezone.utc)
    stamp = lambda: now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    operation = read_json(point / "before.json")["operation_id"]
    os.environ.update(REGISTRY_UPLOAD_RUNTIME_DIR=str(CONTROL / "runtime"), WB_API_TOKEN="lab-only",
                      ONEC_STOCKS_ACCOUNT_ID="000000001")
    runtime = RegistryUploadDbBackedRuntime(CONTROL / "runtime")
    config = prepare_history(runtime.runtime_dir)
    expect(config.candidate_root == history_root(), "History bind path changed")
    with patch.object(SheetVitrinaV1OperatorJobStore, "resume_warehouse_pending", return_value=None):
        entry = RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir, runtime=runtime,
            now_factory=now, activated_at_factory=stamp, refreshed_at_factory=stamp)
    entry.operator_jobs.enable_snapshot_admission(runtime.runtime_dir)
    before = read_acceptance(runtime.db_path, operation)
    expect(before["state"] == ("accepted" if stage == "warehouse" else "completed"), "Unexpected starting expense state")
    original = verify(entry, operation) if stage == "rolling14" else None
    ledger_before = posted_ledger(runtime)
    tape = Replay(scenario, "normal")
    rejected_http = []
    def reject(*args, **kwargs):
        rejected_http.append("urlopen")
        raise RuntimeError("History stage attempted source HTTP")
    suppliers = configure_sources(entry, tape, now()) if stage == "warehouse" else patch("urllib.request.urlopen", reject)
    if stage == "rolling14":
        expect(not (config.candidate_root / "history/CURRENT.json").exists(), "History already published on this control")
    with suppliers:
        receipt = invoke(entry, config, source, stage, file_hash(point / "manifest.json"))
    expect(not rejected_http, "History attempted source HTTP in the parent process")
    expect(not tape.unexpected, f"Unexpected requests: {tape.unexpected}")
    business = verify(entry, operation)
    expect(journal(runtime.db_path)["total"] == 1, "Extra expense")
    ledger_after = posted_ledger(runtime)
    if stage == "warehouse":
        for name in ("ff_pool_documents", "ff_pool_movement_lines"):
            expect(ledger_after[name]["rows"] == ledger_before[name]["rows"] + 1, "Unexpected total " + name)
        expect(ledger_after["ff_pool_overhead_confirmations"]["rows"] == ledger_before["ff_pool_overhead_confirmations"]["rows"] == 1,
               "Expense confirmation count changed")
    else:
        expect(ledger_before == ledger_after, "History changed the posted FF ledger")
    wanted = read_json(point / "opening.json")["expected_cells"]
    if stage == "warehouse":
        snapshot, raw = stored_ready_cells(entry, wanted, scenario["today"])
        verify_surface("ready_cells", raw, wanted)
        publication = {"ready_snapshot": snapshot, "ready_cells": raw}
    else:
        expect(original == business, "History changed the expense or costs")
        publication = publication_cells(entry, config, receipt, wanted, scenario["today"])
        verify_cells(publication, wanted)
    golden_path = REFERENCE / "control/after-1.json"
    if golden_path.is_file():
        golden = read_json(golden_path)
        if stage == "rolling14":
            expect(business == golden["business"], "History accounting differs from full control")
        else:
            # Newly built valuation evidence can carry a different source fingerprint.
            # Compare every accounting operand and the identity and value of each allocation.
            def costs(result):
                row = result["accounting_row"]
                return {**result, "accounting_row": {**row, "documents": [
                    {k: doc[k] for k in ("document_id", "kind", "facility_id", "nm_id", "quantity", "capital_rub")}
                    for doc in row["documents"]]}}
            expect(costs(business) == costs(golden["business"]), "Replayed warehouse differs from full control")
        expect(publication["ready_cells"] == golden["publication"]["ready_cells"], "Published Ready differs from full control")
        if stage == "rolling14":
            expect(publication["history_cells"] == golden["publication"]["history_cells"],
                   "Full control's published cells differ")
    result = dict(status="passed", stage=stage, stages_executed=[r["stage"] for r in receipt["stages"]],
                  source_cycle=source["cycle_id"], source_job=source["job_id"], job_id=receipt["job_id"],
                  checkpoint_sha256=receipt["checkpoint_sha256"], replay_code=receipt["replay_code"], starting_state=before["state"],
                  seconds=round(time.monotonic() - started, 3), business=business, publication=publication,
                  ledger_before=ledger_before, ledger_after=ledger_after,
                  source_calls=tape.totals if stage == "warehouse" else None,
                  attempted_parent_source_http=rejected_http if stage == "rolling14" else None,
                  compared_to_full=golden_path.is_file())
    atomic_json(CONTROL / "result.json", result)
    print(f"PASS {stage}: {result['seconds']} s", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    full = actions.add_parser("capture")
    full.add_argument("--interrupt-after", choices=SUPPORTED)
    single = actions.add_parser("replay")
    single.add_argument("--stage", choices=SUPPORTED, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    expect(not isolation_evidence()["host_mounts"] and subprocess.run(["findmnt", "-n", "-M", str(CASE)], stdout=subprocess.DEVNULL).returncode == 0,
           "Use the private stage VM launcher")
    if args.action == "capture":
        with recording(CONTROL, interrupt_after=args.interrupt_after):
            execute(CONTROL, repeat=False)
    else:
        replay(args.stage)


if __name__ == "__main__":
    main()
