#!/usr/bin/env python3
"""Repo-owned maintenance hold for warehouse functional updates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.warehouse_functional_maintenance import (  # noqa: E402
    maintenance_hold,
    maintenance_restore,
    maintenance_status,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "hold", "restore"))
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--wait-timeout-seconds", type=float, default=1200.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument(
        "--disable-timer",
        action="store_true",
        help="Keep the audited hold restorable while making the timer disabled as well as inactive.",
    )
    args = parser.parse_args(argv)
    runtime_dir = Path(args.runtime_dir).resolve()
    if args.action == "status":
        result = maintenance_status(runtime_dir)
    elif args.action == "hold":
        result = maintenance_hold(
            runtime_dir,
            wait_timeout_seconds=args.wait_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            disable_timer=bool(args.disable_timer),
        )
    else:
        result = maintenance_restore(runtime_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
