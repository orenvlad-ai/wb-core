"""CLI for bounded web-vitrina ready facts -> accepted temporal source reconciliation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.web_vitrina_ready_fact_reconcile import (
    DEFAULT_METRICS,
    DEFAULT_RECONCILE_DATE_FROM,
    DEFAULT_RECONCILE_DATE_TO,
    apply_ready_fact_reconcile,
    dry_run_ready_fact_reconcile,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded reconcile of web-vitrina ready fact rows into accepted temporal source slots."
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--date-from", default=DEFAULT_RECONCILE_DATE_FROM)
    parser.add_argument("--date-to", default=DEFAULT_RECONCILE_DATE_TO)
    parser.add_argument("--metrics", default=",".join(DEFAULT_METRICS))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("dry-run")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--captured-at")
    args = parser.parse_args()

    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir).expanduser().resolve())
    metric_keys = tuple(item.strip() for item in str(args.metrics).split(",") if item.strip())
    if args.command == "dry-run":
        payload = dry_run_ready_fact_reconcile(
            runtime=runtime,
            date_from=args.date_from,
            date_to=args.date_to,
            metric_keys=metric_keys,
        )
    elif args.command == "apply":
        payload = apply_ready_fact_reconcile(
            runtime=runtime,
            date_from=args.date_from,
            date_to=args.date_to,
            metric_keys=metric_keys,
            captured_at=args.captured_at,
        )
    else:  # pragma: no cover
        raise SystemExit(f"unsupported command: {args.command}")

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
