"""Bounded history parent; scheduled activation requires an accepted ready store."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from functools import partial

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_finished_snapshot_build import (
    admission, bounded_worker, deadline_seconds, SAFE_FAILURE_COUNTERS,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.storage_registry import StoreRegistry
from packages.application.web_vitrina_history_live_adapter import (
    LiveNativeAdapter, LiveSourceUnavailable, update_live_history,
)
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable
from packages.application.web_vitrina_history_compiler import dates_between
from packages.business_time import current_business_date_iso
from packages.application.business_data_procedure_admission import admitted_write, MaintenanceAdmissionBlocked
from packages.application.business_data_write_barrier import barrier_status

SAFE_SOURCE_FAILURE_REASONS = frozenset({
    "live_source_resource_limit", "live_source_read_incomplete", "live_bootstrap_deadline",
    "live_ready_bootstrap_pending", "live_dated_bootstrap_pending",
    "live_components_bootstrap_pending", "live_temporal_bootstrap_pending",
    "source_cache_resource_limit", "book_source_resource_limit", "book_blob_content_unknown",
    "bound_book_blob_missing", "bound_book_version_missing", "current_bundle_missing",
    "accepted_ready_source_disappeared", "live_inventory_capture_manifest_invalid",
    "live_source_replaced_while_pinning", "native_revision_trigger_unknown",
    "native_source_revision_missing", "ready_revision_missing", "temporal_revision_schema_missing",
    "live_sqlite_family_unavailable", "live_sqlite_header_unknown", "live_sqlite_journal_unknown",
    "live_sqlite_versions_unknown", "live_sqlite_wal_family_unknown", "lifecycle_quality_pin_closed",
    "live_source_unavailable",
    "history_future_date_unsupported", "history_backfill_outside_source_range",
    "history_catalog_columns_incompatible", "history_row_identity_incompatible",
    "history_catalog_limit", "history_catalog_corrupt", "history_storage_limit",
    "history_group_candidate_source_changed", "history_group_candidate_superseded",
    "history_group_candidate_pending", "history_group_candidate_catalog",
    "history_group_candidate_dates_missing", "history_group_candidate_mixed_catalog",
    "history_group_candidate_proof_incomplete", "history_group_candidate_deadline",
    "history_group_candidate_object_corrupt", "history_group_candidate_preview_mismatch",
})
# Only this caller opts into the worker's validated, payload-free diagnostics.
bounded_worker = partial(bounded_worker, allowed_failure_reasons=SAFE_SOURCE_FAILURE_REASONS)


def source_failure_result(error, stats):
    reason = str(error).partition(":")[0]
    return {"status": "build_failed", "last_good_retained": True,
            "reason_code": reason if reason in SAFE_SOURCE_FAILURE_REASONS else "live_source_unavailable",
            "source_reads": {key: value for key, value in stats.items()
                if key in SAFE_FAILURE_COUNTERS and type(value) is int and 0 <= value < 2**63}}


def runtime_storage_admission(root, contract_path, formula_epoch, *, group_migration=False):
    """Private trial's exact mount/reserve/formula guards, before any writes."""
    contract = json.loads(contract_path.read_text())
    mount = Path('/mnt/wb-core-extra100')
    expected_root = contract['group_migration_root'] if group_migration else contract['candidate_root']
    if str(root) != expected_root or root != root.resolve() or not os.path.ismount(mount):
        raise ValueError('history_storage_path_or_mount')
    actual = subprocess.check_output(['findmnt', '-n', '-T', str(mount), '-o',
                                     'TARGET,SOURCE,FSTYPE,OPTIONS'], text=True, timeout=2)
    if actual.split() != contract['mount'].split():
        raise ValueError('history_mount_identity_drift')
    disk = os.statvfs(mount)
    if disk.f_bavail * disk.f_frsize < contract['reserve_bytes']:
        raise ValueError('history_storage_reserve')
    hashes = contract['formula_code_hashes']
    epoch = 'wbc0069k16-reviewed-native-v1:' + hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if formula_epoch != epoch or contract['formula_epoch'] != epoch:
        raise ValueError('history_formula_epoch_drift')
    repo = Path(__file__).resolve().parents[1]
    for relative, expected in hashes.items():
        if hashlib.sha256((repo / relative).read_bytes()).hexdigest() != expected:
            raise ValueError('history_formula_code_drift')


@contextmanager
def candidate_singleflight(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "candidate.lock").open("a+b") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


@contextmanager
def finished_builder_slot(source):
    try:
        file = (source / ".web-vitrina-finished-builder.lock").open("rb")
    except OSError:
        yield "unknown"
        return
    with file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield "busy"
            return
        try:
            yield "idle"
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


@contextmanager
def history_procedure_admission(args):
    if args.maintenance_window_id:
        status = barrier_status(args.runtime_dir)
        if not (args.manual and status.get("active") is True
                and status.get("phase") == "held" and status.get("hold_confirmed") is True
                and status.get("window_kind") == "maintenance_pause"
                and status.get("window_id") == args.maintenance_window_id):
            raise MaintenanceAdmissionBlocked("history_maintenance_window_mismatch")
        # Derived-only exception: the outer root harness still owns exact
        # baseline/quiet/source/code receipts; this flag grants no business write.
        yield
    else:
        with admitted_write(args.runtime_dir):
            yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", default="business-today")
    parser.add_argument("--formula-epoch", required=True)
    parser.add_argument("--budget-seconds", type=float, default=180)
    parser.add_argument("--max-recomputes", type=int, default=31)
    parser.add_argument("--backfill-from", help="explicit archive rebuild start; requires --backfill-to")
    parser.add_argument("--backfill-to", help="explicit archive rebuild end; requires --backfill-from")
    parser.add_argument("--group-candidate-preview", action="store_true", help="read-only bounded preview of a completed group candidate")
    parser.add_argument("--group-candidate-publish-token", default="", help="one-submit token returned by exact candidate preview")
    parser.add_argument("--expected-current", default="", help="CAS identity of the currently served edition")
    parser.add_argument("--expected-candidate", default="", help="exact completed candidate edition")
    parser.add_argument("--full-history-group-migration", action="store_true",
                        help="explicit one-off isolated group catalog rebuild; ordinary builds remain rolling14")
    parser.add_argument("--manual", action="store_true",
                        help="explicit bounded manual trial; bypass calendar window only")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-contract", type=Path)
    parser.add_argument("--captured-now", default="", help=argparse.SUPPRESS)
    parser.add_argument("--maintenance-window-id", default="",
                        help="exact confirmed held window for controlled manual derived build")
    args = parser.parse_args()
    try:
        with history_procedure_admission(args):
            return run_admitted(args)
    except MaintenanceAdmissionBlocked as exc:
        print(json.dumps({"status": "skipped_maintenance", "reason": str(exc),
                          "last_good_retained": True}))
        return 0


def run_admitted(args):
    source = args.runtime_dir.resolve()
    root = args.candidate_root.resolve()
    now = datetime.fromisoformat(args.captured_now) if args.captured_now else datetime.now(timezone.utc)
    date_to = current_business_date_iso(now) if args.date_to == "business-today" else args.date_to
    if bool(args.backfill_from) != bool(args.backfill_to):
        raise ValueError("both explicit backfill boundaries required")
    backfill_dates = dates_between(args.backfill_from, args.backfill_to) if args.backfill_from else []
    if getattr(args, "full_history_group_migration", False) and not (args.manual and args.maintenance_window_id and args.runtime_contract):
        raise ValueError("group migration requires explicit held manual candidate contract")
    if root.is_relative_to(source) or not 0 < args.budget_seconds <= 240:
        raise ValueError("separate candidate root and bounded budget required")
    seconds = (min(args.budget_seconds, 180) if args.manual else
               min(args.budget_seconds, deadline_seconds(datetime.now(timezone.utc))))
    if seconds <= 0:
        print(json.dumps({"status": "skipped_window", "last_good_retained": True}))
        return
    if args.worker:
        if args.runtime_contract:
            runtime_storage_admission(root, args.runtime_contract, args.formula_epoch, group_migration=getattr(args, "full_history_group_migration", False))
        registry = StoreRegistry(source)
        runtime = RegistryUploadDbBackedRuntime(source, store_registry=registry)
        adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=source,
            cache_dir=root / "proofs", now=now,
            date_from=args.date_from, date_to=date_to, formula_epoch=args.formula_epoch)
        try:
            # Future metric start dates are fixed entries in the reviewed runtime
            # contract, never a moving default inferred at reader request time.
            starts = json.loads(args.runtime_contract.read_bytes()).get("metric_start_dates", {}) if args.runtime_contract else {}
            if getattr(args, "group_candidate_preview", False) or getattr(args, "group_candidate_publish_token", ""):
                if not (getattr(args, "full_history_group_migration", False) and getattr(args, "expected_current", "") and getattr(args, "expected_candidate", "")):
                    raise ValueError("explicit group candidate identities required")
                contract = json.loads(args.runtime_contract.read_bytes())
                target = HistoryStore(Path(contract['candidate_root']) / 'history')
                candidate = HistoryStore(root / 'history')
                deadline = time.monotonic() + seconds
                vector = adapter.capture()
                fence = adapter.fence
                if candidate.edition(getattr(args, "expected_candidate", ""))['consumed'] != vector:
                    raise HistoryUnavailable('history_group_candidate_source_changed')
                def revalidate():
                    fresh = adapter.capture()
                    return fresh if adapter.fence == fence else {**fresh, 'publication_fence': 'changed'}
                if getattr(args, "group_candidate_preview", False):
                    result = target.group_candidate_preview(candidate, expected_current=getattr(args, "expected_current", ""),
                        expected_candidate=getattr(args, "expected_candidate", ""), deadline_monotonic=deadline)
                else:
                    result = target.publish_group_candidate(candidate, expected_current=getattr(args, "expected_current", ""),
                        expected_candidate=getattr(args, "expected_candidate", ""), preview_token=getattr(args, "group_candidate_publish_token", ""),
                        revalidate=revalidate, deadline_monotonic=deadline)
            else:
                result = update_live_history(adapter=adapter, runtime=runtime,
                store=HistoryStore(root / "history"), max_recomputes=args.max_recomputes,
                deadline_monotonic=time.monotonic() + seconds, rolling14=not getattr(args, "full_history_group_migration", False),
                backfill_dates=backfill_dates, metric_start_dates=starts, group_blocks=True)
        except HistoryUnavailable as exc:
            result = source_failure_result(exc, adapter.stats)
    else:
        state = admission(source)  # Existing probes only; no business locks held.
        if state != "idle":
            result = {"status": "skipped_" + state, "last_good_retained": True}
        else:
            with finished_builder_slot(source) as slot:
                if slot != "idle":
                    result = {"status": "skipped_" + slot, "last_good_retained": True}
                else:
                    if args.runtime_contract:
                        try:
                            runtime_storage_admission(root, args.runtime_contract, args.formula_epoch, group_migration=getattr(args, "full_history_group_migration", False))
                        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                            print(json.dumps({'status': 'skipped_storage', 'reason': type(exc).__name__,
                                              'last_good_retained': True}))
                            return
                    with candidate_singleflight(root) as acquired:
                        seconds = (min(args.budget_seconds, 180) if args.manual else
                                   min(args.budget_seconds, deadline_seconds(datetime.now(timezone.utc))))
                        result = bounded_worker([sys.executable, str(Path(__file__).resolve()),
                            *sys.argv[1:], "--date-to", date_to, "--captured-now", now.isoformat(),
                            "--worker"], seconds) if acquired and seconds > 0 else {
                                "status": "skipped_busy", "last_good_retained": True}
    print(json.dumps(result))
    return 1 if result["status"] == "build_failed" else 0


def build_owned_cycle_history(*, runtime, config, cycle_owner, now):
    """Reviewed rolling14 seam for the exact live cycle owner, in its SH lease.

    The caller proves live thread/job identity. All other actor, filesystem,
    formula, finished-builder and candidate guards remain enforced.
    """
    from apps.web_vitrina_finished_snapshot_build import systemd_admission, lock_admission
    from packages.application.web_vitrina_snapshot_admission import api_jobs_admission
    from packages.application.warehouse_functional_lock import warehouse_functional_job_is_busy
    source = Path(runtime.runtime_dir).resolve()
    root = Path(config.candidate_root)
    if root != root.resolve() or root.is_relative_to(source) or not 0 < config.budget_seconds <= 240:
        raise ValueError('cycle_history_config_invalid')
    for state in (api_jobs_admission(source, cycle_owner=cycle_owner), systemd_admission(),
                  lock_admission(source / '.wb-finance-daily-worker.lock')):
        if state != 'idle':
            raise ValueError('cycle_history_admission_' + state)
    if warehouse_functional_job_is_busy(source):
        raise ValueError('cycle_history_warehouse_busy')
    runtime_storage_admission(root, config.runtime_contract, config.formula_epoch)
    with finished_builder_slot(source) as slot:
        if slot != 'idle':
            raise ValueError('cycle_history_builder_' + slot)
        with candidate_singleflight(root) as acquired:
            if not acquired:
                raise ValueError('cycle_history_candidate_busy')
            today = current_business_date_iso(now)
            first = (datetime.fromisoformat(today) - timedelta(days=13)).date().isoformat()
            adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=source,
                cache_dir=root / 'proofs', now=now, date_from=first, date_to=today,
                formula_epoch=config.formula_epoch)
            store = HistoryStore(root / 'history')
            starts = json.loads(config.runtime_contract.read_bytes()).get('metric_start_dates', {})
            result = update_live_history(adapter=adapter, runtime=runtime, store=store,
                max_recomputes=config.max_recomputes, deadline_monotonic=time.monotonic() + config.budget_seconds,
                rolling14=True, metric_start_dates=starts, group_blocks=True)
            if result.get('status') not in {'published', 'unchanged'} or not result.get('edition_id'):
                raise ValueError('cycle_history_incomplete')
            edition = store.edition(result['edition_id'])
            fence = adapter.fence
            vector = adapter.capture()
            status = store.rolling_status(vector, today)
            days = dates_between(first, today)
            proofs = store.day_proofs(edition)
            if (store._current()['current'] != result['edition_id'] or adapter.fence != fence
                    or any(proofs.get(day) != {'epoch': vector['epoch'], 'token': vector['dates'].get(day)} for day in days)
                    or status['dirty_dates'] or
                    status['window_from'] != first or status['business_date'] != today or
                    not set(days) <= set(edition['days'])):
                raise ValueError('cycle_history_final_vector_changed')
            return {'edition_id': result['edition_id'], 'vector_digest': hashlib.sha256(
                json.dumps(vector, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'window_from': first, 'window_to': today}


if __name__ == "__main__":
    raise SystemExit(main())
