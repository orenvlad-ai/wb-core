"""Run resumable live WB supplies full backfill against the runtime SQLite DB."""

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
    DB_FILENAME,
    RegistryUploadDbBackedRuntime,
)
from packages.application.wb_supplies import WbSuppliesBlock  # noqa: E402


def main() -> int:
    args = _parse_args()
    if not os.environ.get("WB_API_TOKEN"):
        print(json.dumps({"status": "blocked", "error": "WB_API_TOKEN is required"}, ensure_ascii=False))
        return 2
    runtime_dir = Path(args.runtime_dir or os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ROOT / ".runtime" / "registry_upload")
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    block = WbSuppliesBlock(runtime=runtime)
    run = block.run_full_backfill(
        {
            "limit": args.limit,
            "start_offset": args.start_offset,
            "resume": not args.no_resume,
            "enrich": not args.no_enrich,
            "max_pages": args.max_pages,
        }
    )
    state = runtime.load_wb_supplies_sync_state()
    rows = runtime.list_wb_supplies()
    report = {
        "status": run.get("status"),
        "runtime_db": str(runtime_dir / DB_FILENAME),
        "run_id": run.get("run_id"),
        "pages_fetched": run.get("pages_fetched"),
        "raw_fetched": run.get("raw_fetched"),
        "upserted": run.get("upserted"),
        "new_rows": run.get("new_rows"),
        "changed_rows": run.get("changed_rows"),
        "unchanged_rows": run.get("unchanged_rows"),
        "enriched": run.get("enriched"),
        "failed_enrich": run.get("failed_enrich"),
        "total_cached_rows": len(rows),
        "highest_synced_offset": state.get("highest_synced_offset"),
        "backfill_complete": bool(state.get("backfill_complete")),
        "may_have_more": bool(state.get("may_have_more")),
        "last_error": state.get("last_error") or run.get("last_error") or "",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if run.get("status") != "success" or not state.get("backfill_complete"):
        return 1
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default="", help="Runtime dir containing registry_upload_runtime.sqlite3.")
    parser.add_argument("--limit", type=int, default=1000, help="WB list page size, max 1000.")
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--no-enrich", action="store_true")
    args = parser.parse_args()
    args.limit = min(max(int(args.limit or 1000), 1), 1000)
    args.start_offset = max(int(args.start_offset or 0), 0)
    return args


if __name__ == "__main__":
    raise SystemExit(main())
