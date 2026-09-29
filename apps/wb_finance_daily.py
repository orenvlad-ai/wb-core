#!/usr/bin/env python3
"""Bounded official daily WB Finance acquisition and status."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
import fcntl
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_finance_weekly import _load_env  # noqa: E402
from packages.adapters.wb_finance_api import WbFinanceApiClient  # noqa: E402
from packages.application.wb_finance_daily import daily_block_from_env  # noqa: E402
from packages.application.wb_finance_weekly import block_from_env as weekly_block_from_env  # noqa: E402


@contextmanager
def _worker_lock(runtime_dir: Path):
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / ".wb-finance-daily-worker.lock"
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ensure-schema", "tick", "bootstrap", "sync-day", "status"))
    parser.add_argument("--runtime-dir", default=os.environ.get(
        "REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload"))
    parser.add_argument("--env-file", default="/opt/wb-ai/.env")
    parser.add_argument("--day", default="")
    parser.add_argument("--max-days", type=int, default=2)
    args = parser.parse_args(argv)
    _load_env(Path(args.env_file))
    block = daily_block_from_env(Path(args.runtime_dir))
    if args.command == "ensure-schema":
        block.ensure_schema()
        result = {"status": "ok", "schema": "wb_finance_daily_v1"}
    elif args.command == "status":
        result = block.build_daily_payload()
    else:
        with _worker_lock(Path(args.runtime_dir)) as acquired:
            if not acquired:
                result = {"status": "busy", "reason": "daily Finance worker already runs"}
            else:
                client = WbFinanceApiClient(
                    os.environ.get("WB_API_TOKEN", ""),
                    rate_gate_root=Path(args.runtime_dir),
                )
                if args.command == "sync-day":
                    result = block.sync_day(date.fromisoformat(args.day), client)
                else:
                    result = block.tick(client, max_days=(14 if args.command == "bootstrap" else args.max_days))
                    # The weekly timer is disabled. Keep the last ten weekly
                    # SPP disclosures current during the active daily tick,
                    # with a separate weekly block and no Finance API fetch.
                    result["weekly_spp_refresh"] = weekly_block_from_env(
                        Path(args.runtime_dir)
                    ).refresh_recent_spp()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") not in {"error_loading", "rate_limited"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
