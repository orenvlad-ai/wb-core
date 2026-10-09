"""Private fixed FD worker, dormant. No user-facing command or admission bypass."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import os
import sys
import time

from packages.application.owned_history_worker_capability import (
    CONTRACT, HistoryDelegationError, child_bootstrap, file_digest, fingerprint,
    lock_proof, process_generation, read_json, send_message, history_child_failure,
)


def _bound(cap):
    from packages.application.owned_history_worker import source_binding
    from packages.application.business_data_heavy_admission import current_heavy_owner
    from packages.application.business_data_procedure_admission import already_admitted
    runtime = Path(cap["runtime_dir"])
    root = Path(cap["candidate_root"])
    if (runtime != runtime.resolve() or root != root.resolve() or root.is_relative_to(runtime)
            or cap["parent_pid"] != os.getppid()
            or process_generation(cap["parent_pid"]) != cap["parent_generation"]
            or source_binding(runtime) != cap["source"]
            or file_digest(cap["runtime_contract"]) != cap["runtime_contract_digest"]
            or current_heavy_owner(runtime) is not None or already_admitted(runtime)):
        raise HistoryDelegationError("history_worker_binding_changed")
    for item in cap["locks"]:
        proof = item["proof"]
        if proof != lock_proof(item["fd"], proof["path"], mode=proof["mode"], parent_pid=cap["parent_pid"]):
            raise HistoryDelegationError("history_worker_lock_changed")
    job = cap["cycle_owner"]
    if job is not None:
        if read_json(runtime / "finished-snapshot-api-jobs" / ("job-" + job["job_id"] + ".json"), limit=4096) != job:
            raise HistoryDelegationError("history_worker_job_changed")
    else:
        from packages.application.web_vitrina_snapshot_admission import api_jobs_admission
        if cap.get("owner_kind") != "history" or api_jobs_admission(runtime) != "idle":
            raise HistoryDelegationError("history_worker_standalone_admission_changed")


def execute(cap):
    # Bootstrap has authenticated inherited kernel capability before these
    # imports/constructors. No ContextVar lease or source producer is installed.
    from packages.business_time import current_business_date_iso
    from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
    from packages.application.storage_registry import StoreRegistry
    from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter, capture_initial_proofs, update_live_history
    from packages.application.web_vitrina_history_store import HistoryStore, _atomic
    from packages.application.web_vitrina_history_compiler import digest, dates_between
    from packages.application.owned_history_native_ack import verified_derived_state, source_stamp
    _bound(cap)
    contract = read_json(cap["runtime_contract"], limit=1024 * 1024)
    repo = Path(__file__).resolve().parents[1]
    for relative, expected in contract["formula_code_hashes"].items():
        path = repo / relative
        if path.resolve() != path or not path.is_relative_to(repo) or file_digest(path) != expected:
            raise HistoryDelegationError("history_worker_formula_code_changed")
    if (contract["candidate_root"] != cap["candidate_root"] or contract["formula_epoch"] != cap["formula_epoch"]
            or not 0 < cap["inner_seconds"] <= 240 or not 1 <= cap["max_recomputes"] <= 31):
        raise HistoryDelegationError("history_worker_contract_changed")
    now = datetime.fromisoformat(cap["now"])
    if now.tzinfo is None:
        raise HistoryDelegationError("history_worker_clock_invalid")
    today = current_business_date_iso(now)
    first = (datetime.fromisoformat(today) - timedelta(days=13)).date().isoformat()
    root, runtime_dir = Path(cap["candidate_root"]), Path(cap["runtime_dir"])
    runtime = RegistryUploadDbBackedRuntime(runtime_dir, store_registry=StoreRegistry(runtime_dir))
    anchor = cap["anchor"]
    source_dates = cap.get("source_dates") or dates_between(first, today)
    backfill_dates = cap.get("backfill_dates") or []
    if (not isinstance(source_dates, list) or source_dates != sorted(set(source_dates)) or not source_dates
            or len(source_dates) > 366 or any(datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day or day > today for day in source_dates)
            or not set(backfill_dates) <= set(source_dates)):
        raise HistoryDelegationError("history_worker_date_scope_invalid")
    scope_dates = sorted({day for day in source_dates if first <= day <= today} | set(backfill_dates))
    historical=None
    if cap.get('historical_receipt') is not None:
        from packages.application.fbs_accounting_historical_history import HistoricalReceipt
        binding=cap['historical_receipt']
        historical=HistoricalReceipt(runtime,binding['operation_id'],binding['attempt_id']).freeze_scope(now)
        if cap.get('owner_kind')!='cycle' or historical.binding()!=binding:
            raise HistoryDelegationError('historical_history_worker_binding_changed')
        historical.validate_sources_readonly(now=now)
        old_dates={day for day in binding['dates'] if day < (datetime.fromisoformat(today)-timedelta(days=1)).date().isoformat()}
        closed_dates=set((cap.get('closed_receipt') or {}).get('dates',[]))
        if backfill_dates!=sorted(old_dates|closed_dates) or len(closed_dates)>2:
            raise HistoryDelegationError('historical_history_worker_scope_changed')
    if cap.get("owner_kind", "cycle") == "cycle" and ((historical is None and len(backfill_dates) > 2)
            or source_dates != dates_between(min((first, *backfill_dates)), today)):
        raise HistoryDelegationError("history_worker_cycle_date_scope_invalid")

    class AnchoredAdapter(LiveNativeAdapter):
        def capture(self):
            _bound(cap)
            vector = super().capture()
            if anchor is not None and (vector != anchor["vector"] or self.fence != anchor["fence"]):
                raise HistoryDelegationError("history_worker_anchor_changed")
            return vector

    adapter = AnchoredAdapter(db_path=runtime.db_path, runtime_dir=runtime_dir,
        cache_dir=root / "proofs", now=now, date_from=source_dates[0], date_to=source_dates[-1], formula_epoch=cap["formula_epoch"])
    # Canonical contiguous source context. Only rolling_scope may be written;
    # intervening archive dates are read without enrollment or re-evaluation.
    store = HistoryStore(root / "history")
    deadline = time.monotonic() + cap["inner_seconds"]
    if cap["mode"] == "capture":
        if anchor is not None:
            raise HistoryDelegationError("history_capture_must_not_accept_anchor")
        vector, _ = capture_initial_proofs(adapter, deadline)
        pointer = store._current()
        old = store.edition() if pointer else None
        catalog = store.day_catalogs(old)[max(old["days"])] if old and old["days"] else None
        value = {"now": cap["now"], "window_from": first, "window_to": today,
                 "vector": vector, "fence": adapter.fence, "base": pointer["current"] if pointer else None,
                 "existing_catalog": catalog, "catalog_context": digest({**adapter.context, "group_blocks": True}),
                 "source": cap["source"], "runtime_contract_digest": cap["runtime_contract_digest"],
                 "formula_epoch": cap["formula_epoch"], "source_dates": source_dates,
                 "scope_dates": scope_dates, "backfill_dates": backfill_dates}
        _bound(cap)
        return {"status": "captured", "anchor": value}
    if (not isinstance(anchor, dict) or anchor["now"] != cap["now"]
            or anchor["window_from"] != first or anchor["window_to"] != today
            or anchor["source"] != cap["source"] or anchor["runtime_contract_digest"] != cap["runtime_contract_digest"]
            or anchor["formula_epoch"] != cap["formula_epoch"] or anchor["source_dates"] != source_dates
            or anchor["scope_dates"] != scope_dates or anchor["backfill_dates"] != backfill_dates):
        raise HistoryDelegationError("history_portion_anchor_binding_mismatch")

    if cap["mode"] == "verify":
        stamp = source_stamp(runtime_dir, runtime.db_path)
        adapter.capture()  # Fresh bounded fence; no compiler/publication retry.
        observed = verified_derived_state(store, anchor, cap.get("target"), deadline)
        if observed["terminal"] and store.start_dates_changed(store.edition(), contract.get("metric_start_dates", {})):
            observed["terminal"] = False
        if observed["terminal"] and cap.get("closed_receipt") is not None:
            from types import SimpleNamespace
            from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog
            closed = ClosedBacklog(SimpleNamespace(block=SimpleNamespace(runtime=runtime)))
            value = closed.status()
            binding = cap["closed_receipt"]
            from packages.application.owned_history_native_ack import closed_receipt_digest
            if closed_receipt_digest(value) != binding["digest"] or not set(binding["dates"]) <= set(backfill_dates):
                raise HistoryDelegationError("history_closed_receipt_changed")
            canonical_adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=runtime_dir,
                cache_dir=root / "proofs", now=now, date_from=source_dates[0], date_to=source_dates[-1],
                formula_epoch=cap["formula_epoch"])
            native = closed.validate_native_readonly(value=value, adapter=canonical_adapter, store=store,
                backfill_dates=tuple(binding["dates"]))
            if (native["vector"] != anchor["vector"] or native["fence"] != anchor["fence"]
                    or native["current"] != observed["current"]):
                raise HistoryDelegationError("history_closed_native_anchor_changed")
            observed["closed"] = {key: native[key] for key in ("receipt_digest", "native")}
        if observed['terminal'] and historical is not None:
            canonical_adapter=LiveNativeAdapter(db_path=runtime.db_path,runtime_dir=runtime_dir,cache_dir=root/'proofs',
                now=now,date_from=source_dates[0],date_to=source_dates[-1],formula_epoch=cap['formula_epoch'])
            native=historical.validate_native_readonly(adapter=canonical_adapter,store=store,now=now)
            if native['vector']!=anchor['vector'] or native['fence']!=anchor['fence'] or native['current']!=observed['current']:
                raise HistoryDelegationError('historical_history_native_anchor_changed')
            observed['historical']={key:native[key] for key in ('receipt_digest','native')}
        if adapter.capture() != anchor["vector"] or store._current() != observed["current"]:
            raise HistoryDelegationError("history_readback_changed_during_verify")
        _bound(cap)
        if source_stamp(runtime_dir, runtime.db_path) != stamp:
            raise HistoryDelegationError("history_source_changed_during_readback")
        return {"status": "verified", "invocation": cap["invocation"], "source_stamp": stamp, **observed}

    def check_base():
        pointer = store._current()
        if (pointer["current"] if pointer else None) != anchor["base"]:
            raise HistoryDelegationError("history_worker_base_changed")
        old = store.edition() if pointer else None
        catalog = store.day_catalogs(old)[max(old["days"])] if old and old["days"] else None
        if catalog != anchor["existing_catalog"]:
            raise HistoryDelegationError("history_worker_base_catalog_changed")

    class AnchoredStore(HistoryStore):
        def update(self, **args):
            check_base()
            catalog = args["catalog"]
            if catalog["context_epoch"] != anchor["catalog_context"] or args["vector"] != anchor["vector"]:
                raise HistoryDelegationError("history_worker_catalog_context_changed")
            old = self.edition() if anchor["base"] else None
            merged = self._merged_catalog(old["catalog"] if old else None, catalog, args.get("metric_start_dates"))
            proof = {"contract": CONTRACT, "invocation": cap["invocation"], "anchor_digest": fingerprint(anchor),
                     "base": anchor["base"], "fresh_catalog": digest(catalog), "merged_catalog": digest(merged),
                     "target": digest([anchor["vector"], digest(merged), scope_dates])}
            if cap.get("target") is not None and any(proof[key] != cap["target"][key]
                    for key in ("fresh_catalog", "merged_catalog", "target")):
                raise HistoryDelegationError("history_worker_target_changed")
            # Read durable pre-existing progress before any pending mutation.
            # Reused old checkpoints cannot masquerade as newly completed days.
            proof["baseline_completed"] = verified_derived_state(store, anchor, proof, deadline,
                first_baseline=cap.get("target") is None)["completed"]
            intent = root / "proofs" / ("owned-portion-" + cap["invocation"] + ".json")
            if intent.exists():
                raise HistoryDelegationError("history_worker_invocation_already_submitted")
            _atomic(intent, proof)  # Exact dated target before writer mutation.
            return super().update(**args)

    check_base()
    result = update_live_history(adapter=adapter, runtime=runtime, store=AnchoredStore(store.root),
        max_recomputes=cap["max_recomputes"], deadline_monotonic=deadline, rolling14=True,
        metric_start_dates=contract.get("metric_start_dates", {}), group_blocks=True, backfill_dates=backfill_dates)
    _bound(cap)
    if result.get("status") in {"published", "unchanged"}:
        edition = store.edition(result["edition_id"])
        proofs = store.day_proofs(edition)
        if (store._current()["current"] != result["edition_id"] or adapter.capture() != anchor["vector"]
                or any(proofs.get(day) != {"epoch": anchor["vector"]["epoch"], "token": token}
                       for day, token in anchor["vector"]["dates"].items() if day in scope_dates)
                or store.rolling_status(anchor["vector"], today, backfill_dates)["dirty_dates"]):
            raise HistoryDelegationError("history_worker_terminal_proof_changed")
    return {key: result[key] for key in ("status", "edition_id", "recomputes", "completed", "total") if key in result}


def main():
    channel = None
    try:
        # Only an inherited endpoint index is accepted; argv never authorizes.
        if len(sys.argv) != 2:
            return 2
        channel, cap = child_bootstrap(int(sys.argv[1]))
        try:
            result = execute(cap)
        except Exception as exc:
            from packages.application.web_vitrina_history_store import HistoryUnavailable
            result = history_child_failure(exc, mode=cap["mode"], source_error=isinstance(exc, HistoryUnavailable))
        send_message(channel, {"contract": CONTRACT, "invocation": cap["invocation"], "result": result})
        return 0
    except Exception:
        return 2
    finally:
        if channel is not None:
            channel.close()


if __name__ == "__main__":
    raise SystemExit(main())
