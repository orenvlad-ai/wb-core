#!/usr/bin/env python3
"""Dry-run/apply/readback CLI for the Orenburg FBS mapping extension."""

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

from packages.application.ff_fbs_mapping_extension_production import (  # noqa: E402
    FfFbsMappingExtensionProductionError,
    FfFbsMappingExtensionProductionMutation,
)


def _load_env(path: Path) -> None:
    if not path.is_file():
        raise FfFbsMappingExtensionProductionError(
            f"environment file does not exist: {path}"
        )
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


def _read_plan(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.plan_file) == bool(args.reviewed_plan_stdin):
        raise FfFbsMappingExtensionProductionError(
            "apply requires exactly one of --plan-file or --reviewed-plan-stdin"
        )
    raw = (
        Path(args.plan_file).read_text(encoding="utf-8")
        if args.plan_file
        else sys.stdin.read()
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FfFbsMappingExtensionProductionError(
            "reviewed mapping-extension plan is invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise FfFbsMappingExtensionProductionError(
            "reviewed mapping-extension plan must be a JSON object"
        )
    return payload


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
    env_file = Path(args.env_file).resolve()
    _load_env(env_file)
    mutation = FfFbsMappingExtensionProductionMutation(
        runtime_dir=Path(args.runtime_dir).resolve(),
        deployed_sha=str(args.deployed_sha),
    )
    if args.command == "dry-run":
        payload = mutation.build_plan()
    elif args.command == "apply":
        payload = mutation.apply(
            _read_plan(args),
            fingerprint=str(args.fingerprint),
            approval_reference=str(args.approval_reference),
            actor=str(args.actor),
            evidence_dir=Path(args.evidence_dir).resolve(),
        )
    else:
        payload = mutation.readback()
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
        description="Owner-gated exact Orenburg FBS mapping/backlog production runner."
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--env-file", required=True)
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
    apply.add_argument("--evidence-dir", required=True)
    commands.add_parser("readback")
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
