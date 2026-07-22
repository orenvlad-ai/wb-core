#!/usr/bin/env python3
"""CLI for bounded read-only Partner/Finance production reconciliation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.partner_finance_production_diagnostic import (  # noqa: E402
    DiagnosticScope,
    PartnerFinanceDiagnosticError,
    run_partner_finance_diagnostic,
)


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in os.environ:
            continue
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            parsed = []
        os.environ[key] = parsed[0] if parsed else value.strip().strip("\"'")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only reconciliation of ads_compact, Finance marketing and "
            "Partner current other_withholdings."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database", type=Path, help="Runtime SQLite database path")
    source.add_argument(
        "--runtime-dir",
        type=Path,
        help="Runtime directory containing registry_upload_runtime.sqlite3",
    )
    parser.add_argument("--env-file", default="/opt/wb-ai/.env")
    parser.add_argument("--seller-id", default="")
    parser.add_argument("--nm-id", default="")
    parser.add_argument(
        "--week",
        action="append",
        default=[],
        help="Selected week start YYYY-MM-DD; repeat for multiple weeks",
    )
    parser.add_argument(
        "--weeks",
        default="",
        help="Comma-separated selected week starts",
    )
    parser.add_argument(
        "--server-settings",
        action="store_true",
        help=(
            "Resolve the most recent complete current Partner setting. If no "
            "weeks are supplied, select all Finance weeks like production UI Flow."
        ),
    )
    parser.add_argument("--max-weeks", type=int, default=64)
    parser.add_argument("--max-groups", type=int, default=200)
    parser.add_argument("--max-examples", type=int, default=3)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    _load_env(Path(args.env_file))

    database = (
        args.database
        if args.database is not None
        else args.runtime_dir / "registry_upload_runtime.sqlite3"
    )
    weeks = [str(item).strip() for item in args.week if str(item).strip()]
    weeks.extend(
        item.strip() for item in str(args.weeks or "").split(",") if item.strip()
    )
    scope = DiagnosticScope(
        database=database,
        seller_id=str(
            args.seller_id
            or os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID")
            or "canonical"
        ),
        nm_id=str(args.nm_id or "").strip(),
        weeks=tuple(weeks),
        server_settings=bool(args.server_settings),
        max_weeks=args.max_weeks,
        max_groups=args.max_groups,
        max_examples=args.max_examples,
    )
    try:
        result = run_partner_finance_diagnostic(scope)
    except (PartnerFinanceDiagnosticError, sqlite3.Error) as exc:
        payload = {
            "status": "error",
            "code": "partner_finance_diagnostic_failed",
            "error": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
        )
    )
    return 0 if result.get("status") == "ready" else 3
if __name__ == "__main__":
    raise SystemExit(main())
