#!/usr/bin/env python3
"""Query-only Stage 6 FF facility/pool cutover planner.

There is intentionally no apply subcommand.  A later production-mutation task
must own the exact trusted-main/human-gated transaction after an approved
manifest has been built under the warehouse-domain write boundary.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_cutover import (
    build_ff_pool_cutover_plan,
    ff_pool_cutover_preflight_snapshot,
    read_ff_pool_cutover_status,
)


def _connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def main() -> int:
    parser = argparse.ArgumentParser(description="Query-only FF facility/pool cutover planner")
    parser.add_argument("--db", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("status")
    dry_run = sub.add_parser("dry-run")
    dry_run.add_argument("--proposal", required=True, type=Path)
    dry_run.add_argument("--deployed-sha", required=True)
    dry_run.add_argument("--cutover-at", default="")
    args = parser.parse_args()
    with _connection(args.db) as conn:
        if args.command == "preflight":
            result = ff_pool_cutover_preflight_snapshot(conn)
        elif args.command == "status":
            result = read_ff_pool_cutover_status(conn)
        else:
            if args.proposal.stat().st_size > 8 * 1024 * 1024:
                raise SystemExit("proposal exceeds 8 MiB bound")
            proposal = json.loads(args.proposal.read_text(encoding="utf-8"))
            if not isinstance(proposal, dict):
                raise SystemExit("proposal must be a JSON object")
            result = build_ff_pool_cutover_plan(
                conn,
                proposal=proposal,
                deployed_sha=args.deployed_sha,
                cutover_at=args.cutover_at,
            )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result.get("status") in {"ready", "not_applied", "awaiting_boundary"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
