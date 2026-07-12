#!/usr/bin/env python3
"""Read-only audit for the retired parallel own-product-capital backfill.

Production apply moved to ``apps/canonical_cost_engine_backfill.py``.  This
command remains only so historical automation gets an explicit, safe answer.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--backup-dir", default="")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if bool(getattr(args, "apply", False)):
        raise ValueError(
            "legacy own-product-capital runner is audit/dry-run only; "
            "use apps/canonical_cost_engine_backfill.py for the single guarded apply path"
        )
    start = date.fromisoformat(str(args.date_from)).isoformat()
    end = date.fromisoformat(str(args.date_to)).isoformat()
    if end < start:
        raise ValueError("date_to must be on or after date_from")
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    if not runtime.db_path.exists():
        raise ValueError("runtime SQLite database does not exist")
    with sqlite3.connect(f"file:{runtime.db_path.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        rows = []
        table = "sheet_vitrina_v1_own_capital_daily_state"
        if table in tables:
            rows = [
                list(row) for row in conn.execute(
                    f"SELECT * FROM {table} WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date,nm_id,stage",
                    (start, end),
                ).fetchall()
            ]
        blockers = 0
        blocker_table = "sheet_vitrina_v1_own_capital_blockers"
        if blocker_table in tables:
            blockers = int(conn.execute(
                f"SELECT COUNT(*) FROM {blocker_table} WHERE resolved_at IS NULL"
            ).fetchone()[0])
    fingerprint = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
    return {
        "contract_name": "own_product_capital_legacy_audit_v1",
        "mode": "dry-run",
        "scope": {"date_from": start, "date_to": end},
        "legacy_row_count": len(rows),
        "legacy_digest": fingerprint,
        "unresolved_blocker_count": blockers,
        "applied": False,
        "would_change": False,
        "successor": "apps/canonical_cost_engine_backfill.py",
    }


def main() -> int:
    print(json.dumps(run(build_parser().parse_args()), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
