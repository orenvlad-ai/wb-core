#!/usr/bin/env python3
"""Canonical exact-date Finance daily parity/recovery runner."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.finance_daily_historical_recovery import (  # noqa: E402
    apply_finance_daily_recovery,
    build_finance_daily_recovery_plan,
    readback_finance_daily_recovery,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


def _runtime_dir(value: str) -> Path:
    normalized = str(value or "").strip() or str(
        os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if not normalized:
        raise ValueError("runtime directory is required")
    return Path(normalized).resolve()


def _deployed_sha(value: str, sha_file: str) -> str:
    direct = str(value or "").strip()
    path = Path(str(sha_file or "")).resolve() if sha_file else None
    from_file = path.read_text(encoding="utf-8").strip() if path else ""
    if direct and from_file and direct != from_file:
        raise ValueError("deployed SHA input does not match the runtime SHA receipt")
    result = direct or from_file
    if len(result) != 40 or any(char not in "0123456789abcdef" for char in result):
        raise ValueError("exact deployed SHA is required")
    return result


def _write_private(path: Path, payload: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def _load_env(path: Path) -> None:
    if not path.is_file():
        raise ValueError("hosted environment file is missing")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--deployed-sha", default="")
    parser.add_argument("--deployed-sha-file", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("plan", "parity"):
        child = subparsers.add_parser(command)
        child.add_argument("--target-date", required=True)
        child.add_argument("--output", default="")
        child.add_argument("--stdout-plan", action="store_true")

    apply = subparsers.add_parser("apply")
    apply.add_argument("--target-date", required=True)
    apply.add_argument("--plan-file", default="")
    apply.add_argument("--reviewed-plan-stdin", action="store_true")
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--actor", required=True)

    readback = subparsers.add_parser("readback")
    readback.add_argument("--operation-id", required=True)

    args = parser.parse_args()
    if args.env_file:
        _load_env(Path(args.env_file).resolve())
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=_runtime_dir(args.runtime_dir))
    deployed_sha = _deployed_sha(args.deployed_sha, args.deployed_sha_file)

    if args.command in {"plan", "parity"}:
        plan, _, _ = build_finance_daily_recovery_plan(
            runtime,
            target_date=args.target_date,
            deployed_sha=deployed_sha,
            mode="recovery" if args.command == "plan" else "parity",
        )
        if not args.output and not args.stdout_plan:
            raise ValueError(f"{args.command} requires --output or --stdout-plan")
        if args.output:
            _write_private(Path(args.output).resolve(), plan)
        if args.stdout_plan:
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        else:
            print(
                json.dumps(
                    {
                        "status": "planned",
                        "target_date": plan["target_date"],
                        "fingerprint": plan["fingerprint"],
                        "source_digest": plan["source"]["source_digest"],
                        "pages": plan["source"]["pages"],
                        "coverage": plan["source"]["coverage"],
                        "target_cells": plan["expected_target_cells"],
                        "changed_cells": plan["changed_cells"],
                        "parity_status": plan["parity_status"],
                        "output": str(Path(args.output).resolve()),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0

    if args.command == "readback":
        result = readback_finance_daily_recovery(
            runtime,
            operation_id=args.operation_id,
        )
        if result.get("deployed_sha") != deployed_sha:
            raise ValueError("Finance recovery readback deployed SHA does not match runtime")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if bool(args.plan_file) == bool(args.reviewed_plan_stdin):
        raise ValueError("apply requires exactly one reviewed plan source")
    if args.reviewed_plan_stdin:
        reviewed = json.load(sys.stdin)
    else:
        plan_path = Path(args.plan_file).resolve()
        if not plan_path.is_file():
            raise ValueError("reviewed Finance daily plan does not exist")
        reviewed = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(reviewed, dict):
        raise ValueError("reviewed Finance daily plan must be an object")
    if reviewed.get("target_date") != args.target_date:
        raise ValueError("reviewed Finance daily plan date does not match apply scope")
    result = apply_finance_daily_recovery(
        runtime,
        reviewed_plan=reviewed,
        fingerprint=args.fingerprint,
        approval_reference=args.approval_reference,
        actor=args.actor,
        deployed_sha=deployed_sha,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
