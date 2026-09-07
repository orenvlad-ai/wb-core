#!/usr/bin/env python3
"""Prepare an opening or run the existing hourly accounting publication step."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.fbs_accounting_runtime import load, prepare, refresh
from packages.application.fbs_snapshot_cost import fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "refresh", "status"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        if not args.output:
            parser.error("prepare requires --output")
        book, _ = prepare(args.runtime_dir, opening=True)
        args.output.write_text(json.dumps(book, ensure_ascii=False, sort_keys=True))
        result = {"candidate_sha256": fingerprint(book), "effective_date": book["effective_date"]}
    elif args.command == "refresh":
        result = refresh(args.runtime_dir)
    else:
        book, version = load(args.runtime_dir)
        result = {"active": bool(book and book["active"]), "version": version,
                  "effective_date": book["effective_date"] if book else None,
                  "prepared_at": book["prepared_at"] if book else None}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
