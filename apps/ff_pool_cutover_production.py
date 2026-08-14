#!/usr/bin/env python3
"""Dry-run-default CLI for the owner-gated Stage 7C production runner."""

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

from packages.application.ff_pool_cutover_production import (  # noqa: E402
    FfPoolCutoverProductionError,
    FfPoolCutoverProductionMutation,
)


def _load_env(path: Path) -> None:
    if not path.is_file():
        raise FfPoolCutoverProductionError("env_missing", f"Environment file is missing: {path}")
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


def _read_object(path: str, *, stdin: bool = False) -> dict[str, Any]:
    if bool(path) == bool(stdin):
        raise FfPoolCutoverProductionError(
            "input_required", "Choose exactly one plan/evidence file or stdin"
        )
    text = sys.stdin.read() if stdin else Path(path).read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise FfPoolCutoverProductionError("invalid_json_object", "Input must be a JSON object")
    return value


def _write_private(path: Path, payload: MappingLike) -> None:
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


MappingLike = dict[str, Any]


def _runner(args: argparse.Namespace) -> FfPoolCutoverProductionMutation:
    env_file = Path(args.env_file).resolve()
    _load_env(env_file)
    return FfPoolCutoverProductionMutation(
        runtime_dir=Path(args.runtime_dir).resolve(),
        env_file=env_file,
        deployed_sha=str(args.deployed_sha),
    )


def run(args: argparse.Namespace) -> int:
    runner = _runner(args)
    if args.command == "dry-run":
        payload = runner.build_gate_plan(
            excluded_shipment_ids=tuple(args.excluded_shipment_id),
            opening_facility_id=str(args.opening_facility_id or ""),
            proposed_window_minutes=int(args.proposed_window_minutes),
        )
    elif args.command == "apply":
        if args.reviewed_envelope_stdin:
            if args.plan_file or args.reviewed_plan_stdin or args.external_barrier_file:
                raise FfPoolCutoverProductionError(
                    "ambiguous_apply_input",
                    "The reviewed envelope cannot be combined with plan/barrier inputs",
                )
            envelope = json.loads(sys.stdin.read())
            if not isinstance(envelope, dict):
                raise FfPoolCutoverProductionError(
                    "invalid_apply_envelope", "Reviewed apply envelope must be an object"
                )
            reviewed = envelope.get("reviewed_plan")
            barrier = envelope.get("external_barrier")
        else:
            reviewed = _read_object(args.plan_file, stdin=bool(args.reviewed_plan_stdin))
            if not args.external_barrier_file:
                raise FfPoolCutoverProductionError(
                    "barrier_input_required", "Apply requires external barrier evidence"
                )
            barrier = json.loads(Path(args.external_barrier_file).read_text(encoding="utf-8"))
        if not isinstance(reviewed, dict):
            raise FfPoolCutoverProductionError(
                "invalid_reviewed_plan", "Reviewed plan must be an object"
            )
        if not isinstance(barrier, dict):
            raise FfPoolCutoverProductionError(
                "invalid_barrier_evidence", "External barrier evidence must be an object"
            )
        payload = runner.apply(
            reviewed,
            fingerprint=str(args.fingerprint),
            approval_reference=str(args.approval_reference),
            actor=str(args.actor),
            backup_dir=Path(args.backup_dir).resolve(),
            external_barrier_evidence=barrier,
        )
    else:
        payload = runner.readback()
    if args.output:
        _write_private(Path(args.output), payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=None if args.compact else 2))
    return 0 if payload.get("status") not in {"blocked", "error"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Owner-gated exact FF facility/pool opening and FBS cutover runner."
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--compact", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    dry = commands.add_parser("dry-run")
    dry.add_argument("--excluded-shipment-id", action="append", required=True)
    dry.add_argument("--opening-facility-id", default="")
    dry.add_argument("--proposed-window-minutes", type=int, default=15)
    apply = commands.add_parser("apply")
    apply.add_argument("--plan-file", default="")
    apply.add_argument("--reviewed-plan-stdin", action="store_true")
    apply.add_argument("--reviewed-envelope-stdin", action="store_true")
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--backup-dir", required=True)
    apply.add_argument("--external-barrier-file", default="")
    commands.add_parser("readback")
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        code = getattr(exc, "code", "error")
        details = getattr(exc, "details", None)
        print(
            json.dumps(
                {"status": "error", "code": code, "error": str(exc), "details": details},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
