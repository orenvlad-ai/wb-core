#!/usr/bin/env python3
"""Dry-run/apply/rollback for bounded July 2026 warehouse recovery batches."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_early_wb_recovery import (  # noqa: E402
    apply_early_wb_recovery_plan,
    build_early_wb_recovery_plan,
    public_plan as public_early_plan,
    rollback_early_wb_recovery,
)
from packages.application.warehouse_historical_recovery import (  # noqa: E402
    apply_historical_recovery_plan,
    build_historical_recovery_plan,
    public_plan as public_historical_plan,
    rollback_historical_recovery,
)
from packages.application.warehouse_business_projection_recovery import (  # noqa: E402
    apply_business_projection_recovery_plan,
    build_business_projection_recovery_plan,
    public_plan as public_projection_plan,
    rollback_business_projection_recovery,
)
from packages.application.warehouse_transit_historical_recovery import (  # noqa: E402
    apply_transit_historical_recovery_plan,
    build_transit_historical_recovery_plan,
    public_plan as public_transit_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument(
        "--batch",
        choices=("a", "b", "transit", "projection"),
        required=True,
        help="A=19–29, B=01–18 partial WB, transit=query-only fact evidence.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--batch-a-fingerprint", default="")
    parser.add_argument("--backup-path", default="")
    parser.add_argument("--source-sha256", default="")
    parser.add_argument("--business-date", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and args.rollback:
        raise ValueError("--apply and --rollback are mutually exclusive")
    runtime = RegistryUploadDbBackedRuntime(
        runtime_dir=Path(args.runtime_dir).resolve()
    )
    if args.batch == "a":
        result = _run_batch_a(runtime, args)
    elif args.batch == "b":
        result = _run_batch_b(runtime, args)
    elif args.batch == "transit":
        result = _run_transit(runtime, args)
    else:
        result = _run_projection(runtime, args)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
        )
    )
    return 0


def _run_batch_a(
    runtime: RegistryUploadDbBackedRuntime,
    args: argparse.Namespace,
) -> dict[str, object]:
    if args.rollback:
        return rollback_historical_recovery(
            runtime,
            fingerprint=str(args.fingerprint),
            reason=str(args.reason or "operator requested bounded rollback"),
        )
    plan = build_historical_recovery_plan(runtime)
    if args.apply:
        return apply_historical_recovery_plan(
            runtime,
            plan,
            confirm_fingerprint=str(args.fingerprint),
            approval_reference=str(args.approval_reference),
        )
    return public_historical_plan(plan)


def _run_batch_b(
    runtime: RegistryUploadDbBackedRuntime,
    args: argparse.Namespace,
) -> dict[str, object]:
    if args.rollback:
        return rollback_early_wb_recovery(
            runtime,
            fingerprint=str(args.fingerprint),
            reason=str(args.reason or "operator requested bounded rollback"),
        )
    plan = build_early_wb_recovery_plan(runtime)
    if args.apply:
        return apply_early_wb_recovery_plan(
            runtime,
            plan,
            confirm_fingerprint=str(args.fingerprint),
            approval_reference=str(args.approval_reference),
            batch_a_fingerprint=str(args.batch_a_fingerprint),
        )
    return public_early_plan(plan)


def _run_transit(
    runtime: RegistryUploadDbBackedRuntime,
    args: argparse.Namespace,
) -> dict[str, object]:
    if args.rollback:
        raise ValueError("transit evidence submanifest performs no mutation")
    if not str(args.backup_path or "").strip():
        raise ValueError("transit submanifest requires --backup-path")
    plan = build_transit_historical_recovery_plan(
        runtime,
        backup_path=Path(args.backup_path),
    )
    if args.apply:
        return apply_transit_historical_recovery_plan(
            runtime,
            plan,
            confirm_fingerprint=str(args.fingerprint),
            approval_reference=str(args.approval_reference),
        )
    return public_transit_plan(plan)


def _run_projection(
    runtime: RegistryUploadDbBackedRuntime,
    args: argparse.Namespace,
) -> dict[str, object]:
    if args.rollback:
        return rollback_business_projection_recovery(
            runtime,
            fingerprint=str(args.fingerprint),
            reason=str(args.reason or "operator requested bounded rollback"),
        )
    if not str(args.source_sha256 or "") or not str(args.business_date or ""):
        raise ValueError(
            "projection recovery requires --source-sha256 and --business-date"
        )
    plan = build_business_projection_recovery_plan(
        runtime,
        source_sha256=str(args.source_sha256),
        business_date=str(args.business_date),
    )
    if args.apply:
        return apply_business_projection_recovery_plan(
            runtime,
            plan,
            confirm_fingerprint=str(args.fingerprint),
            approval_reference=str(args.approval_reference),
        )
    return public_projection_plan(plan)


if __name__ == "__main__":
    raise SystemExit(main())
