#!/usr/bin/env python3
"""Build and inspect isolated FBS cost candidates; never activate accounting."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.application.fbs_snapshot_cost import (
    CandidateStore, FbsSnapshotCostError, candidate_period_view,
    close_candidate_period, evaluate_candidate, initialize_candidate,
)
from packages.application.fbs_snapshot_cost_sources import capture_current
from packages.business_time import current_business_date_iso


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", help="read existing official snapshots and documents only")
    capture.add_argument("--source-db", type=Path, required=True)
    capture.add_argument("--without-baseline", action="store_true", help="subsequent observations do not read old costs")
    for name in ("initialize", "refresh"):
        command = commands.add_parser(name)
        command.add_argument("--capture-file", type=Path, required=True)
        command.add_argument("--candidate-db", type=Path, required=True)
    close = commands.add_parser("close", help="close a complete past candidate day explicitly")
    close.add_argument("--candidate-db", type=Path, required=True)
    close.add_argument("--day", required=True)
    read = commands.add_parser("read")
    read.add_argument("--candidate-db", type=Path, required=True)
    read.add_argument("--day", required=True, help="exact business date; never falls back to another date")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "capture":
            result = capture_current(args.source_db, now=datetime.now(timezone.utc), include_baseline=not args.without_baseline)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["quantity_snapshot"]["complete"] and result["documents_complete"] else 2
        store = CandidateStore(args.candidate_db)
        state, expected = store.load()
        if args.command == "initialize":
            capture = json.loads(args.capture_file.read_text())
            if args.candidate_db.resolve() == args.capture_file.resolve():
                raise FbsSnapshotCostError("candidate_database_is_source_file")
            initial = initialize_candidate(capture)
            # Reusing an existing candidate cannot replace its baseline or its
            # subsequent periods, even when the latest published prices change.
            if state is not None and state != initial:
                raise FbsSnapshotCostError("candidate_already_initialized")
            state = initial
            day = state["baseline"]["business_date"]
        else:
            if state is None:
                raise FbsSnapshotCostError("candidate_not_initialized")
            if args.command == "refresh":
                capture = json.loads(args.capture_file.read_text())
                state = evaluate_candidate(state, capture)
                day = capture["business_date"]
            elif args.command == "close":
                day = args.day
                state = close_candidate_period(state, day, today=current_business_date_iso(datetime.now(timezone.utc)))
            else:
                view = candidate_period_view(state, args.day)
                print(json.dumps(view, ensure_ascii=False, sort_keys=True))
                return 0 if view["available"] else 2
        digest = store.save(state, expected_fingerprint=expected)
        view = candidate_period_view(state, day)
        print(json.dumps({"candidate_only": True, "candidate_db": str(store.path),
                          "state_fingerprint": digest, "last_attempt": state["last_attempt"],
                          "period": {key: value for key, value in view.items() if key != "rows"}},
                         ensure_ascii=False, sort_keys=True))
        return 0 if view["available"] and view["quality"] != "incomplete" else 2
    except (FbsSnapshotCostError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"candidate_only": True, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
