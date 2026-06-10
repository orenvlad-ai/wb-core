"""Diagnostics for first 20 accepted WB supplies parity against cache/API evidence."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_supplies import HttpBackedWbSuppliesSource, WbSuppliesHttpStatusError, WbSuppliesTransportError  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402


TARGET_IDS = [
    "39961480",
    "39914199",
    "39750013",
    "39605280",
    "39572333",
    "39558014",
    "39543474",
    "39423793",
    "39389370",
    "39389369",
    "39375226",
    "39361305",
    "39361304",
    "39332993",
    "39265540",
    "39265519",
    "39265492",
    "39265590",
    "39265571",
    "39238882",
]

EXPECTED = {
    "39961480": {"warehouse": "Екатеринбург - Перспективная 14", "quantity": 2, "cost": 0},
    "39914199": {"warehouse": "Екатеринбург - Перспективная 14", "quantity": 1, "cost": 0},
    "39750013": {"warehouse": "Электросталь", "quantity": 1, "cost": 0},
    "39605280": {"warehouse": "Краснодар (Тихорецкая)", "quantity": 1, "cost": 0},
    "39361304": {"warehouse": "Электросталь", "quantity": 3, "cost": 0},
    "39238882": {"warehouse": "Электросталь", "quantity": 3, "cost": 0},
    "39265519": {"warehouse": "Краснодар (Тихорецкая) → Обухово", "quantity": 4750, "accepted": 4728, "transit": True},
    "39265492": {"warehouse": "Склад Шушары → Обухово", "quantity": 7500, "accepted": 7483, "transit": True},
    "39265590": {"warehouse": "Екатеринбург - Перспективная 14 → Чехов 2, Новоселки вл 11 стр 7", "quantity": 3000, "accepted": 2996, "transit": True},
    "39265571": {"warehouse": "Новосемейкино → Чехов 1, Новоселки вл 11 стр 2", "quantity": 5750, "accepted": 5735, "transit": True},
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default=os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR") or str(ROOT / ".runtime" / "registry_upload"))
    parser.add_argument("--target-id", action="append", default=[])
    parser.add_argument("--live", action="store_true", help="Also fetch official detail/goods evidence for target IDs.")
    parser.add_argument("--delay", type=float, default=2.1, help="Delay between live upstream calls to avoid WB global limiter.")
    parser.add_argument("--output-json", action="store_true")
    args = parser.parse_args()

    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    records = runtime.list_wb_supplies_cache_records()
    by_id = _index_records(records)
    source = HttpBackedWbSuppliesSource() if args.live else None
    target_ids = args.target_id or TARGET_IDS
    rows = []
    for supply_id in target_ids:
        record = by_id.get(supply_id)
        normalized = dict((record or {}).get("normalized") or {})
        live_detail: dict[str, Any] | None = None
        live_goods: list[Mapping[str, Any]] | None = None
        live_error = ""
        if source is not None:
            detail_result = _safe_call(lambda: source.fetch_supply_details(supply_id, is_preorder_id=False))
            if detail_result["ok"]:
                live_detail = detail_result["value"]
            else:
                live_error = detail_result["error"]
            time.sleep(max(args.delay, 0))
            goods_result = _safe_call(lambda: source.fetch_supply_goods(supply_id, limit=1000, offset=0, is_preorder_id=False))
            if goods_result["ok"]:
                live_goods = goods_result["value"]
            else:
                live_error = "; ".join(item for item in (live_error, goods_result["error"]) if item)
            time.sleep(max(args.delay, 0))
        expected = EXPECTED.get(supply_id, {})
        rows.append(
            {
                "supply_id": supply_id,
                "expected": expected,
                "current": {
                    "warehouse": normalized.get("warehouse_display"),
                    "type": normalized.get("type_label"),
                    "quantity": normalized.get("quantity_added"),
                    "packed": normalized.get("packed_quantity"),
                    "accepted": normalized.get("accepted_quantity"),
                    "coefficient": normalized.get("acceptance_coefficient"),
                    "cost": normalized.get("cost_total"),
                    "cost_evidence": normalized.get("cost_evidence"),
                },
                "cache_evidence": {
                    "has_list": bool(record and record.get("raw_list")),
                    "has_detail": bool(record and record.get("raw_detail")),
                    "has_goods": bool(record and record.get("raw_goods")),
                    "has_package": bool(record and record.get("raw_package")),
                    "raw_hashes": {
                        "list": (record or {}).get("raw_list_hash") or "",
                        "detail": (record or {}).get("raw_detail_hash") or "",
                        "goods": (record or {}).get("raw_goods_hash") or "",
                        "package": (record or {}).get("raw_package_hash") or "",
                    },
                },
                "official_api": {
                    "detail": _pick_detail(live_detail),
                    "goods_summary": _goods_summary(live_goods),
                    "error": live_error,
                },
                "flags": _flags(expected, normalized),
            }
        )
    summary = {
        "target_count": len(rows),
        "missing_warehouse": sum(1 for row in rows if row["flags"]["missing_warehouse"]),
        "missing_quantity": sum(1 for row in rows if row["flags"]["missing_quantity"]),
        "technical_type": sum(1 for row in rows if row["flags"]["technical_type"]),
        "missing_cost": sum(1 for row in rows if row["flags"]["missing_cost"]),
        "missing_goods": sum(1 for row in rows if not row["cache_evidence"]["has_goods"]),
    }
    payload = {"summary": summary, "rows": rows}
    if args.output_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        print("summary:", json.dumps(summary, ensure_ascii=False))
    return 0


def _index_records(records: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for record in records:
        for key in (record.get("supply_id"), record.get("wb_supply_id"), record.get("cache_key")):
            if key:
                result[str(key).replace("supply:", "")] = record
    return result


def _safe_call(fn):
    try:
        return {"ok": True, "value": fn()}
    except (WbSuppliesHttpStatusError, WbSuppliesTransportError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}


def _pick_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    if not detail:
        return {}
    keys = (
        "warehouseID",
        "warehouseName",
        "actualWarehouseID",
        "actualWarehouseName",
        "transitWarehouseID",
        "transitWarehouseName",
        "boxTypeID",
        "virtualTypeID",
        "quantity",
        "acceptedQuantity",
        "acceptanceCost",
        "paidAcceptanceCoefficient",
        "storageCoef",
        "deliveryCoef",
    )
    return {key: detail.get(key) for key in keys if key in detail}


def _goods_summary(goods: list[Mapping[str, Any]] | None) -> dict[str, Any]:
    if goods is None:
        return {"count": None}
    return {
        "count": len(goods),
        "quantity": sum(float(item.get("quantity") or 0) for item in goods if isinstance(item, Mapping)),
        "acceptedQuantity": sum(float(item.get("acceptedQuantity") or 0) for item in goods if isinstance(item, Mapping)),
    }


def _flags(expected: Mapping[str, Any], normalized: Mapping[str, Any]) -> dict[str, bool]:
    warehouse = str(normalized.get("warehouse_display") or "").strip()
    quantity = normalized.get("quantity_added")
    cost = normalized.get("cost_total")
    is_transit = bool(expected.get("transit"))
    return {
        "missing_warehouse": bool(expected.get("warehouse")) and warehouse != expected.get("warehouse"),
        "missing_quantity": expected.get("quantity") is not None and quantity != expected.get("quantity"),
        "technical_type": "Тип " in str(normalized.get("type_label") or ""),
        "missing_cost": not is_transit and expected.get("cost") is not None and cost != expected.get("cost"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
