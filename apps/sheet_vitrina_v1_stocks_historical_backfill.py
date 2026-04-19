"""One-off historical stocks backfill into runtime temporal snapshots."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.stocks_block import HistoricalCsvBackedStocksSource
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.stocks_block import transform_legacy_payload


DEFAULT_RUNTIME_DIR = Path(
    os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", "/opt/wb-core-runtime/state")
).expanduser()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill historical stocks snapshots into temporal_source_snapshots[source_key=stocks]."
    )
    parser.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR))
    parser.add_argument("--date-from", default="2026-03-01")
    parser.add_argument("--date-to", default="2026-04-18")
    args = parser.parse_args()

    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    current_state = runtime.load_current_state()
    enabled_nm_ids = sorted(int(item.nm_id) for item in current_state.config_v2 if item.enabled)
    if not enabled_nm_ids:
        raise SystemExit("current registry config_v2 does not contain enabled nmIds")

    source = HistoricalCsvBackedStocksSource()
    result = source.fetch_window(
        date_from=args.date_from,
        date_to=args.date_to,
        nm_ids=enabled_nm_ids,
    )

    deleted = runtime.delete_temporal_source_snapshots(
        source_key="stocks",
        date_from=args.date_from,
        date_to=args.date_to,
    )
    captured_at = _utc_timestamp()
    totals_by_date: dict[str, float] = {}
    for snapshot_date, payload in sorted(result.payloads.items()):
        envelope = transform_legacy_payload(payload)
        if envelope.result.kind != "success":
            raise SystemExit(
                f"stocks backfill saved incomplete payload for {snapshot_date}: {asdict(envelope.result)}"
            )
        runtime.save_temporal_source_snapshot(
            source_key="stocks",
            snapshot_date=snapshot_date,
            captured_at=captured_at,
            payload=envelope.result,
        )
        totals_by_date[snapshot_date] = sum(item.stock_total for item in envelope.result.items)

    print(
        json.dumps(
            {
                "status": "success",
                "runtime_dir": str(runtime_dir),
                "bundle_version": current_state.bundle_version,
                "date_from": args.date_from,
                "date_to": args.date_to,
                "enabled_nm_id_count": len(enabled_nm_ids),
                "deleted_existing_snapshots": deleted,
                "saved_dates": len(result.payloads),
                "download_ids": result.download_ids,
                "report_names": result.report_names,
                "row_count": result.row_count,
                "unique_nm_id_count": result.unique_nm_id_count,
                "totals_by_date": totals_by_date,
                "notes_by_date": result.notes_by_date,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
