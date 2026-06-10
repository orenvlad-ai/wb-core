"""Diagnostics for cached/lazy WB supplies goods composition."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_supplies import HttpBackedWbSuppliesSource  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.wb_supplies import WbSuppliesBlock, _supply_detail_payload  # noqa: E402


DEFAULT_TARGET_IDS = ["39265540", "39265492", "39265519", "39605280"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default=os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR") or str(ROOT / ".runtime" / "registry_upload"))
    parser.add_argument("--target-id", action="append", default=[])
    parser.add_argument("--live-fetch", action="store_true", help="Allow detail route lazy fetch through WB_API_TOKEN for missing goods.")
    parser.add_argument("--output-json", action="store_true")
    args = parser.parse_args()

    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    target_ids = args.target_id or DEFAULT_TARGET_IDS
    block = WbSuppliesBlock(runtime=runtime, source=HttpBackedWbSuppliesSource()) if args.live_fetch else None
    rows = []
    for supply_id in target_ids:
        try:
            if block is not None:
                detail = block.get_supply(supply_id)
            else:
                record = runtime.load_wb_supply_record(supply_id)
                if record is None:
                    raise RuntimeError("not found in cache")
                detail = {"contract_name": "sheet_vitrina_v1_wb_supplies", **_supply_detail_payload(record)}
            goods = detail.get("goods") or []
            summary = detail.get("goods_summary") or {}
            rows.append(
                {
                    "supply_id": supply_id,
                    "status": "ok",
                    "composition_status": detail.get("composition_status"),
                    "composition_error": detail.get("composition_error") or "",
                    "composition_last_enriched_at": detail.get("composition_last_enriched_at") or "",
                    "goods_summary": summary,
                    "top_goods": goods[:5],
                }
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must report exact row failure.
            rows.append({"supply_id": supply_id, "status": "error", "error": str(exc)})
    summary = {
        "target_count": len(rows),
        "available": sum(1 for row in rows if row.get("composition_status") == "available"),
        "missing": sum(1 for row in rows if row.get("composition_status") == "missing"),
        "error": sum(1 for row in rows if row.get("status") == "error" or row.get("composition_status") == "error"),
    }
    payload = {"summary": summary, "rows": rows}
    if args.output_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        print("summary:", json.dumps(summary, ensure_ascii=False))
    return 0 if summary["error"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
