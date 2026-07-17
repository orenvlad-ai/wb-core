#!/usr/bin/env python3
"""Guarded runner for the one-time six-warehouse opening snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_stocks import WarehouseStocksBlock  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry_run = subparsers.add_parser("dry-run", help="read sources and build an exact plan")
    dry_run.add_argument("--output", default="", help="optional local JSON plan path")

    apply = subparsers.add_parser("apply", help="apply one previously reviewed exact plan")
    apply.add_argument("--plan-file", required=True)
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--backup-dir", required=True)

    subparsers.add_parser("readback", help="read stored cutover documents and reconciliation")

    rollback = subparsers.add_parser("rollback", help="remove only this opening cutover")
    rollback.add_argument("--fingerprint", required=True)
    rollback.add_argument("--backup-dir", required=True)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    runtime_dir = Path(str(args.runtime_dir)).resolve()
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    block = WarehouseStocksBlock(runtime=runtime)
    if args.command == "dry-run":
        result = block.build_opening_plan()
        output = str(args.output or "").strip()
        if output:
            target = Path(output).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_json(result) + "\n", encoding="utf-8")
            result = {**result, "plan_file": str(target)}
        return result
    if args.command == "apply":
        plan_path = Path(str(args.plan_file)).resolve()
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise ValueError("plan-file must contain a JSON object")
        return block.apply_opening_plan(
            plan,
            confirm_fingerprint=str(args.fingerprint),
            backup_dir=Path(str(args.backup_dir)).resolve(),
        )
    if args.command == "readback":
        return block.readback()
    if args.command == "rollback":
        return block.rollback_opening_cutover(
            confirm_fingerprint=str(args.fingerprint),
            backup_dir=Path(str(args.backup_dir)).resolve(),
        )
    raise ValueError(f"unsupported command: {args.command}")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def main() -> int:
    try:
        result = run(build_parser().parse_args())
    except Exception as exc:
        print(_json({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
