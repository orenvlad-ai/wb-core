#!/usr/bin/env python3
"""Validate an isolated Finance copy; exit 0 means a report, not readiness or posting."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.finance_cost_readonly_validation import validate_finance_cost_weeks_readonly


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-runtime-copy", type=Path, required=True)
    parser.add_argument("--expected-book-version", required=True)
    parser.add_argument("--expected-storage-manifest-sha256", required=True)
    parser.add_argument("--expected-week", action="append", required=True,
                        help="Exact week_start:week_end; repeat for every forward candidate week")
    parser.add_argument("--seller-id", default="canonical")
    args = parser.parse_args()
    try:
        result = validate_finance_cost_weeks_readonly(args.offline_runtime_copy,
            expected_book_version=args.expected_book_version,
            expected_storage_manifest_sha256=args.expected_storage_manifest_sha256,
            expected_weeks=[tuple(w.split(":")) for w in args.expected_week],
            seller_id=args.seller_id)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "validation_failed", "runtime_mutation": False,
                          "apply_allowed": False, "posting_proof": False, "reason": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
