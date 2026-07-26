#!/usr/bin/env python3
"""Canonical bounded runner for derived Vitrina incident metrics."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.vitrina_incident_rematerialization import (  # noqa: E402
    apply_vitrina_incident_rematerialization,
    plan_vitrina_incident_rematerialization,
)


def _runtime_dir(value: str) -> Path:
    normalized = str(value or "").strip()
    if not normalized:
        normalized = str(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if not normalized:
        raise ValueError(
            "runtime directory is required through --runtime-dir or REGISTRY_UPLOAD_RUNTIME_DIR"
        )
    return Path(normalized).resolve()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--seller-id", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry_run = subparsers.add_parser("dry-run")
    dry_run.add_argument("--date-from", required=True)
    dry_run.add_argument("--date-to", required=True)
    dry_run.add_argument("--max-dates", type=int, default=14)
    dry_run.add_argument("--output", default="")
    dry_run.add_argument("--stdout-plan", action="store_true")

    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan-file", default="")
    apply.add_argument("--reviewed-plan-stdin", action="store_true")
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--actor", required=True)

    args = parser.parse_args()
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=_runtime_dir(args.runtime_dir))
    if args.command == "dry-run":
        plan, _ = plan_vitrina_incident_rematerialization(
            runtime,
            date_from=args.date_from,
            date_to=args.date_to,
            max_dates=args.max_dates,
            seller_id=str(args.seller_id or "").strip() or None,
        )
        output = Path(args.output).resolve() if args.output else None
        if output is None and not args.stdout_plan:
            raise ValueError("dry-run requires --output or --stdout-plan")
        if output is not None:
            _write_json(output, plan)
        if args.stdout_plan:
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
            return 0
        print(
            json.dumps(
                {
                    "status": "planned",
                    "fingerprint": plan["fingerprint"],
                    "snapshot_count": plan["snapshot_count"],
                    "changed_cells": plan["changed_cells"],
                    "output": str(output),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    if bool(args.plan_file) == bool(args.reviewed_plan_stdin):
        raise ValueError(
            "apply requires exactly one of --plan-file or --reviewed-plan-stdin"
        )
    if args.reviewed_plan_stdin:
        reviewed_plan = json.load(sys.stdin)
    else:
        plan_path = Path(args.plan_file).resolve()
        if not plan_path.is_file():
            raise ValueError("reviewed incident rematerialization plan does not exist")
        reviewed_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(reviewed_plan, dict):
        raise ValueError("reviewed incident rematerialization plan must be an object")
    result = apply_vitrina_incident_rematerialization(
        runtime,
        reviewed_plan=reviewed_plan,
        fingerprint=args.fingerprint,
        approval_reference=args.approval_reference,
        actor=args.actor,
        seller_id=str(args.seller_id or "").strip() or None,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
