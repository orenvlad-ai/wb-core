"""Bounded history parent; scheduled activation requires an accepted ready store."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_finished_snapshot_build import admission, bounded_worker, deadline_seconds
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.storage_registry import StoreRegistry
from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter, update_live_history
from packages.application.web_vitrina_history_store import HistoryStore
from packages.business_time import current_business_date_iso
from packages.application.business_data_procedure_admission import admitted_write, MaintenanceAdmissionBlocked
from packages.application.business_data_write_barrier import barrier_status


def runtime_storage_admission(root, contract_path, formula_epoch):
    """Private trial's exact mount/reserve/formula guards, before any writes."""
    contract = json.loads(contract_path.read_text())
    mount = Path('/mnt/wb-core-extra100')
    if str(root) != contract['candidate_root'] or root != root.resolve() or not os.path.ismount(mount):
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
    if root.is_relative_to(source) or not 0 < args.budget_seconds <= 240:
        raise ValueError("separate candidate root and bounded budget required")
    seconds = (min(args.budget_seconds, 180) if args.manual else
               min(args.budget_seconds, deadline_seconds(datetime.now(timezone.utc))))
    if seconds <= 0:
        print(json.dumps({"status": "skipped_window", "last_good_retained": True}))
        return
    if args.worker:
        if args.runtime_contract:
            runtime_storage_admission(root, args.runtime_contract, args.formula_epoch)
        registry = StoreRegistry(source)
        runtime = RegistryUploadDbBackedRuntime(source, store_registry=registry)
        adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=source,
            cache_dir=root / "proofs", now=now,
            date_from=args.date_from, date_to=date_to, formula_epoch=args.formula_epoch)
        result = update_live_history(adapter=adapter, runtime=runtime,
            store=HistoryStore(root / "history"), max_recomputes=args.max_recomputes,
            deadline_monotonic=time.monotonic() + seconds)
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
                            runtime_storage_admission(root, args.runtime_contract, args.formula_epoch)
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


if __name__ == "__main__":
    raise SystemExit(main())
