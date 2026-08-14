#!/usr/bin/env python3
"""Dry-run-default CLI for exact Stage 7C recovery supersession."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_cutover_recovery_supersession import (  # noqa: E402
    FfPoolCutoverRecoverySupersession,
    FfPoolCutoverRecoverySupersessionError,
)


def _read_object(path: str, *, stdin: bool) -> dict[str, Any]:
    if bool(path) == bool(stdin):
        raise FfPoolCutoverRecoverySupersessionError(
            "reviewed_plan_required",
            "Choose exactly one reviewed plan file or stdin",
        )
    raw = sys.stdin.read() if stdin else Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise FfPoolCutoverRecoverySupersessionError(
            "reviewed_plan_invalid", "Reviewed plan must be a JSON object"
        )
    return payload


def run(args: argparse.Namespace) -> int:
    runner = FfPoolCutoverRecoverySupersession(
        runtime_dir=Path(args.runtime_dir).resolve(),
        deployed_sha=str(args.deployed_sha),
    )
    if args.command == "dry-run":
        payload = runner.build_plan(str(args.operation_id))
    elif args.command == "apply":
        payload = runner.apply(
            _read_object(
                str(args.plan_file), stdin=bool(args.reviewed_plan_stdin)
            ),
            fingerprint=str(args.fingerprint),
            actor=str(args.actor),
            approval_reference=str(args.approval_reference),
        )
    else:
        payload = runner.readback(str(args.operation_id))
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if args.compact else 2,
        )
    )
    return 0 if str(payload.get("status") or "") not in {"blocked", "error"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prove and append one exact terminal supersession relation for a "
            "stale failed Stage 7C recovery."
        )
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--compact", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    dry_run = commands.add_parser("dry-run")
    dry_run.add_argument("--operation-id", required=True)

    apply = commands.add_parser("apply")
    apply.add_argument("--plan-file", default="")
    apply.add_argument("--reviewed-plan-stdin", action="store_true")
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--actor", required=True)

    readback = commands.add_parser("readback")
    readback.add_argument("--operation-id", required=True)
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": str(getattr(exc, "code", "error")),
                    "error": str(exc),
                    "details": getattr(exc, "details", None),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
