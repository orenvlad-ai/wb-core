#!/usr/bin/env python3
"""Guarded runner for the one-time six-warehouse opening snapshot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
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
    parser.add_argument("--env-file", default="", help="optional dotenv file loaded without shell evaluation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry_run = subparsers.add_parser("dry-run", help="read sources and build an exact plan")
    dry_run.add_argument("--output", default="", help="optional local JSON plan path")

    apply = subparsers.add_parser("apply", help="apply one previously reviewed exact plan")
    apply.add_argument("--plan-file", required=True)
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--backup-dir", required=True)

    subparsers.add_parser("readback", help="read stored cutover documents and reconciliation")

    diagnostic = subparsers.add_parser(
        "diagnose-discrepancy",
        help="read bounded WB acceptance-discrepancy evidence without mutation",
    )
    diagnostic.add_argument("--nm-id", action="append", type=int, required=True)

    rollback = subparsers.add_parser("rollback", help="remove only this opening cutover")
    rollback.add_argument("--fingerprint", required=True)
    rollback.add_argument("--backup-dir", required=True)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    env_file = str(args.env_file or "").strip()
    if env_file:
        _load_env_file(Path(env_file))
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
        plan_file = str(args.plan_file or "").strip()
        if plan_file in {"-", "/dev/stdin"}:
            plan_text = sys.stdin.read()
        else:
            plan_text = Path(plan_file).resolve().read_text(encoding="utf-8")
        plan = json.loads(plan_text)
        if not isinstance(plan, dict):
            raise ValueError("plan-file must contain a JSON object")
        return block.apply_opening_plan(
            plan,
            confirm_fingerprint=str(args.fingerprint),
            backup_dir=Path(str(args.backup_dir)).resolve(),
        )
    if args.command == "readback":
        return block.readback()
    if args.command == "diagnose-discrepancy":
        return block.diagnose_wb_acceptance_discrepancy(nm_ids=args.nm_id)
    if args.command == "rollback":
        return block.rollback_opening_cutover(
            confirm_fingerprint=str(args.fingerprint),
            backup_dir=Path(str(args.backup_dir)).resolve(),
        )
    raise ValueError(f"unsupported command: {args.command}")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _load_env_file(path: Path) -> None:
    """Load simple dotenv assignments as data, never as executable shell."""

    if not path.is_file():
        raise ValueError(f"environment file does not exist: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key.isidentifier():
            continue
        lexer = shlex.shlex(raw_value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            value = " ".join(lexer)
        except ValueError as exc:
            raise ValueError(f"invalid value for environment key {key}") from exc
        os.environ[key] = value


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
