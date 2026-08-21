#!/usr/bin/env python3
"""Dry-run/apply/readback for the exact five posted FBS overhead documents."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_overhead_backfill import (  # noqa: E402
    FfPoolOverheadBackfill,
    FfPoolOverheadBackfillError,
)


def _read_plan(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.plan_file) == bool(args.reviewed_plan_stdin):
        raise FfPoolOverheadBackfillError(
            "apply requires exactly one of --plan-file or --reviewed-plan-stdin"
        )
    raw = (
        Path(args.plan_file).read_text(encoding="utf-8")
        if args.plan_file
        else sys.stdin.read()
    )
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise FfPoolOverheadBackfillError(
            "reviewed overhead backfill plan must be a JSON object"
        )
    return value


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    output = path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--compact", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("dry-run")
    apply = commands.add_parser("apply")
    apply.add_argument("--plan-file", default="")
    apply.add_argument("--reviewed-plan-stdin", action="store_true")
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--backup-dir", required=True)
    apply.add_argument("--evidence-dir", required=True)
    readback = commands.add_parser("readback")
    readback.add_argument("--plan-file", default="")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    mutation = FfPoolOverheadBackfill(
        runtime_dir=Path(args.runtime_dir).resolve(),
        deployed_sha=str(args.deployed_sha),
    )
    if args.command == "dry-run":
        return mutation.build_plan()
    if args.command == "apply":
        return mutation.apply(
            _read_plan(args),
            fingerprint=str(args.fingerprint),
            approval_reference=str(args.approval_reference),
            actor=str(args.actor),
            backup_dir=Path(args.backup_dir).resolve(),
            evidence_dir=Path(args.evidence_dir).resolve(),
        )
    reviewed = None
    if str(args.plan_file or "").strip():
        value = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise FfPoolOverheadBackfillError(
                "readback plan must be a JSON object"
            )
        reviewed = value
    return mutation.readback(reviewed_plan=reviewed)


def main() -> int:
    try:
        args = build_parser().parse_args()
        payload = run(args)
        if args.output:
            _write_private(Path(args.output), payload)
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=None if args.compact else 2,
            )
        )
        return 0 if payload.get("status") not in {"blocked", "error"} else 2
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
