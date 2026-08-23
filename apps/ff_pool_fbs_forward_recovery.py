#!/usr/bin/env python3
"""Dry-run-default CLI for exact FBS lifecycle forward/recovery cutover."""

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

from packages.application.ff_pool_fbs_forward_recovery import (  # noqa: E402
    FfPoolFbsForwardRecoveryError,
    FfPoolFbsForwardRecoveryMutation,
)


def _read_plan(path: str, *, stdin: bool) -> dict[str, Any]:
    if bool(path) == bool(stdin):
        raise FfPoolFbsForwardRecoveryError(
            "plan_input_required",
            "This command requires exactly one of --plan-file or --reviewed-plan-stdin",
        )
    raw = sys.stdin.read() if stdin else Path(path).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise FfPoolFbsForwardRecoveryError(
            "invalid_plan", "Reviewed recovery plan must be a JSON object"
        )
    return value


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    output = path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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


def run(args: argparse.Namespace) -> int:
    runner = FfPoolFbsForwardRecoveryMutation(
        runtime_dir=Path(args.runtime_dir).resolve(),
        deployed_sha=str(args.deployed_sha),
    )
    if args.command == "apply":
        payload = runner.apply(
            _read_plan(args.plan_file, stdin=bool(args.reviewed_plan_stdin)),
            fingerprint=str(args.fingerprint),
            approval_reference=str(args.approval_reference),
            actor=str(args.actor),
            evidence_dir=Path(args.evidence_dir).resolve(),
        )
    elif args.command == "readback":
        payload = runner.readback(fingerprint=str(args.fingerprint or ""))
    elif args.command == "verify-noop":
        payload = runner.verify_noop(
            _read_plan(args.plan_file, stdin=bool(args.reviewed_plan_stdin)),
            fingerprint=str(args.fingerprint),
        )
    else:
        payload = runner.build_plan()
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Exact FBS C/C+1 forward cutover and immutable <=C backlog recovery; "
            "defaults to query-only dry-run."
        )
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--compact", action="store_true")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("dry-run")
    apply = commands.add_parser("apply")
    apply.add_argument("--plan-file", default="")
    apply.add_argument("--reviewed-plan-stdin", action="store_true")
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--evidence-dir", required=True)
    readback = commands.add_parser("readback")
    readback.add_argument("--fingerprint", default="")
    verify_noop = commands.add_parser("verify-noop")
    verify_noop.add_argument("--plan-file", default="")
    verify_noop.add_argument("--reviewed-plan-stdin", action="store_true")
    verify_noop.add_argument("--fingerprint", required=True)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        if args.command is None:
            args.command = "dry-run"
        return run(args)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": getattr(exc, "code", "error"),
                    "error": str(exc),
                    "details": getattr(exc, "details", None),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
