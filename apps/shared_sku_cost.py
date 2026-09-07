#!/usr/bin/env python3
"""Prepare isolated shared SKU costs; no active reporting or accounting writes."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.fbs_snapshot_cost import CandidateStore, candidate_period_view
from packages.application.shared_sku_cost import SharedSkuCostStore, build_shared_cost_day
from packages.application.shared_sku_cost_sources import capture_wb_component


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    subs = result.add_subparsers(dest="command", required=True)
    capture = subs.add_parser("capture-wb", help="query-only exact-date WB component")
    capture.add_argument("--source-db", type=Path, required=True)
    capture.add_argument("--fbs-candidate-db", type=Path, required=True)
    capture.add_argument("--day", required=True)
    capture.add_argument("--version-id")
    build = subs.add_parser("build", help="save to a separate shared-cost candidate file")
    build.add_argument("--fbs-candidate-db", type=Path, required=True)
    build.add_argument("--wb-capture-file", type=Path, required=True)
    build.add_argument("--candidate-db", type=Path, required=True)
    build.add_argument("--day", required=True)
    read = subs.add_parser("read", help="read one exact date; no fallback")
    read.add_argument("--candidate-db", type=Path, required=True)
    read.add_argument("--day", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in {"capture-wb", "build"}:
            state, _ = CandidateStore(args.fbs_candidate_db).load()
            if state is None:
                raise ValueError("fbs_candidate_not_initialized")
        if args.command == "capture-wb":
            view = candidate_period_view(state, args.day)
            if not view["available"]:
                raise ValueError("exact_fbs_period_unavailable")
            nm_ids = sorted({r["nm_id"] for r in view["rows"].values()})
            result = capture_wb_component(args.source_db, day=args.day, nm_ids=nm_ids, version_id=args.version_id)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["complete"] else 2
        store = SharedSkuCostStore(args.candidate_db)
        if args.command == "build":
            if args.candidate_db.resolve() in {args.fbs_candidate_db.resolve(), args.wb_capture_file.resolve()}:
                raise ValueError("candidate_database_is_input")
            before = store.read(args.day)
            result = build_shared_cost_day(state, json.loads(args.wb_capture_file.read_text()), args.day)
            store.save(result, expected_version=before["version_id"] if before else None)
        else:
            result = store.read(args.day)
            if result is None:
                raise ValueError("shared_cost_exact_date_missing")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["quality"] == "complete" else 2
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"candidate_only": True, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
