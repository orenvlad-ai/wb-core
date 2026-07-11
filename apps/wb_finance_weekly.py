#!/usr/bin/env python3
"""Repo-owned CLI for WB Finance weekly backfill, sync, recalculation and due tick."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_finance_weekly import WbFinanceApiClient, block_from_env  # noqa: E402


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in os.environ:
            continue
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            parsed = []
        os.environ[key.strip()] = parsed[0] if parsed else value.strip().strip("\"'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "ensure-schema",
            "backfill",
            "sync-week",
            "recalculate",
            "recalculate-all",
            "repair-derived-orphans",
            "tick",
            "status",
        ),
    )
    parser.add_argument(
        "--runtime-dir",
        default=os.environ.get(
            "REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload"
        ),
    )
    parser.add_argument("--env-file", default="/opt/wb-ai/.env")
    parser.add_argument("--date-from", default="")
    parser.add_argument("--date-to", default="")
    parser.add_argument("--today", default="")
    parser.add_argument("--min-interval-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    _load_env(Path(args.env_file))
    block = block_from_env(Path(args.runtime_dir))
    if args.command == "ensure-schema":
        block.ensure_schema()
        result = {"status": "ok", "schema": "wb_finance_weekly_v1"}
    elif args.command == "status":
        result = block.build_payload()
    elif args.command == "recalculate":
        result = block.recalculate_week(
            date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
        )
    elif args.command == "recalculate-all":
        result = block.recalculate_all_weeks()
    elif args.command == "repair-derived-orphans":
        result = block.repair_orphan_derived_rows()
    else:
        client = WbFinanceApiClient(
            os.environ.get("WB_API_TOKEN", ""),
            min_interval_seconds=args.min_interval_seconds,
        )
        if args.command == "backfill":
            result = block.run_backfill(
                client, today=date.fromisoformat(args.today) if args.today else None
            )
        elif args.command == "sync-week":
            result = block.sync_week(
                date.fromisoformat(args.date_from),
                date.fromisoformat(args.date_to),
                client,
            )
        else:
            due = block.due_tick_week()
            result = (
                {"status": "no_due_week"}
                if due is None
                else block.sync_week(due[0], due[1], client)
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return (
        0 if str(result.get("status")) not in {"error", "completed_with_errors"} else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
