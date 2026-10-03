"""Explicit unwired candidate command. No timer/unit/deploy integration."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import argparse
import fcntl
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_finished_snapshot_build import admission, bounded_worker, deadline_seconds
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.storage_registry import StoreRegistry
from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter, update_live_history
from packages.application.web_vitrina_history_store import HistoryStore


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--formula-epoch", required=True)
    parser.add_argument("--budget-seconds", type=float, default=180)
    parser.add_argument("--max-recomputes", type=int, default=31)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    source = args.runtime_dir.resolve()
    root = args.candidate_root.resolve()
    if root.is_relative_to(source) or not 0 < args.budget_seconds <= 240:
        raise ValueError("separate candidate root and bounded budget required")
    seconds = min(args.budget_seconds, deadline_seconds(datetime.now(timezone.utc)))
    if seconds <= 0:
        print(json.dumps({"status": "skipped_window", "last_good_retained": True}))
        return
    if args.worker:
        registry = StoreRegistry(source)
        runtime = RegistryUploadDbBackedRuntime(source, store_registry=registry)
        adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=source,
            cache_dir=root / "proofs", now=datetime.now(timezone.utc),
            date_from=args.date_from, date_to=args.date_to, formula_epoch=args.formula_epoch)
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
                    with candidate_singleflight(root) as acquired:
                        seconds = min(args.budget_seconds, deadline_seconds(datetime.now(timezone.utc)))
                        result = bounded_worker([sys.executable, str(Path(__file__).resolve()),
                            *sys.argv[1:], "--worker"], seconds) if acquired and seconds > 0 else {
                                "status": "skipped_busy", "last_good_retained": True}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
