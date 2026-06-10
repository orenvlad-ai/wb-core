"""Rebuild WB supplies normalized cache rows from stored raw evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
from packages.application.wb_supplies import (  # noqa: E402
    _normalize_supply_row,
    _resolve_upstream_lookup_id,
    _row_needs_enrichment,
    _stable_cache_key,
    _stable_payload_hash,
    _warehouse_map,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default=os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR") or str(ROOT / ".runtime" / "registry_upload"))
    parser.add_argument("--target-id", action="append", default=[])
    parser.add_argument("--enrich-missing-critical", action="store_true")
    parser.add_argument("--enrich-missing-goods", action="store_true")
    parser.add_argument("--delay", type=float, default=2.1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    records = runtime.list_wb_supplies_cache_records()
    target_ids = {str(item).strip() for item in args.target_id if str(item).strip()}
    selected = [record for record in records if not target_ids or _record_matches(record, target_ids)]
    warehouse_rows = runtime.list_wb_supplies_warehouses()
    warehouse_by_id = _warehouse_map(warehouse_rows)
    source = HttpBackedWbSuppliesSource() if (args.enrich_missing_critical or args.enrich_missing_goods) else None
    synced_at = _now_utc()
    rows_to_save: list[dict[str, Any]] = []
    stats = {
        "selected": len(selected),
        "renormalized": 0,
        "enriched_detail": 0,
        "enriched_goods": 0,
        "enriched_package": 0,
        "failed_enrich": 0,
        "warnings": [],
    }
    for record in selected:
        raw_list = record.get("raw_list") if isinstance(record.get("raw_list"), Mapping) else None
        if raw_list is None:
            stats["warnings"].append(f"{record.get('supply_id')}: missing raw_list; skipped")
            continue
        raw_detail = record.get("raw_detail") if isinstance(record.get("raw_detail"), Mapping) else None
        raw_goods = record.get("raw_goods") if isinstance(record.get("raw_goods"), list) else None
        raw_package = record.get("raw_package") if isinstance(record.get("raw_package"), list) else None
        row_warnings: list[str] = []
        needs = _row_needs_enrichment(record.get("normalized") or {})
        should_enrich = source is not None and (bool(target_ids) or needs)
        lookup_id, is_preorder_id = _resolve_upstream_lookup_id(raw_detail or raw_list)
        if should_enrich and lookup_id:
            if raw_detail is None or args.enrich_missing_critical:
                detail = _safe_call(lambda: source.fetch_supply_details(lookup_id, is_preorder_id=is_preorder_id))
                if detail["ok"]:
                    raw_detail = detail["value"]
                    stats["enriched_detail"] += 1
                else:
                    row_warnings.append(f"details fetch failed for {lookup_id}: {detail['error']}")
                time.sleep(max(args.delay, 0))
            if raw_goods is None or args.enrich_missing_goods:
                goods = _safe_call(lambda: source.fetch_supply_goods(lookup_id, limit=1000, offset=0, is_preorder_id=is_preorder_id))
                if goods["ok"]:
                    raw_goods = goods["value"]
                    stats["enriched_goods"] += 1
                else:
                    row_warnings.append(f"goods fetch failed for {lookup_id}: {goods['error']}")
                time.sleep(max(args.delay, 0))
            if raw_package is None and not is_preorder_id:
                package = _safe_call(lambda: source.fetch_supply_package(lookup_id))
                if package["ok"]:
                    raw_package = package["value"]
                    stats["enriched_package"] += 1
                else:
                    row_warnings.append(f"package fetch failed for {lookup_id}: {package['error']}")
                time.sleep(max(args.delay, 0))
        if row_warnings:
            stats["failed_enrich"] += 1
            stats["warnings"].extend(row_warnings)
        normalized = _normalize_supply_row(
            raw_list=raw_list,
            raw_detail=raw_detail,
            raw_goods=raw_goods,
            raw_package=raw_package,
            warehouse_by_id=warehouse_by_id,
            synced_at=synced_at,
            warnings=row_warnings,
        )
        normalized["cache_key"] = str(record.get("cache_key") or _stable_cache_key(raw_list) or "").strip()
        normalized["raw_list_hash"] = _stable_payload_hash(raw_list)
        normalized["raw_detail_hash"] = _stable_payload_hash(raw_detail) if raw_detail is not None else str(record.get("raw_detail_hash") or "")
        normalized["raw_goods_hash"] = _stable_payload_hash(raw_goods) if raw_goods is not None else str(record.get("raw_goods_hash") or "")
        normalized["raw_package_hash"] = _stable_payload_hash(raw_package) if raw_package is not None else str(record.get("raw_package_hash") or "")
        normalized["last_list_synced_at"] = str((record.get("normalized") or {}).get("last_list_synced_at") or "")
        normalized["last_enriched_at"] = synced_at if should_enrich and not row_warnings else str(record.get("last_enriched_at") or "")
        normalized["enrichment_status"] = "failed" if row_warnings and should_enrich else "ok" if should_enrich else str(record.get("enrichment_status") or "not_requested")
        normalized["enrichment_error"] = "; ".join(row_warnings) if row_warnings else ""
        rows_to_save.append(normalized)
        stats["renormalized"] += 1
    if rows_to_save and not args.dry_run:
        runtime.save_wb_supply_rows(rows=rows_to_save, warehouses=[], synced_at=synced_at)
    print(json.dumps({"status": "ok", "dry_run": bool(args.dry_run), **stats}, ensure_ascii=False, indent=2))
    return 0 if not stats["failed_enrich"] else 2


def _record_matches(record: Mapping[str, Any], target_ids: set[str]) -> bool:
    values = {
        str(record.get("supply_id") or ""),
        str(record.get("wb_supply_id") or ""),
        str(record.get("cache_key") or ""),
        str(record.get("preorder_id") or ""),
    }
    values |= {item.replace("supply:", "") for item in values}
    return bool(values & target_ids)


def _safe_call(fn):
    try:
        return {"ok": True, "value": fn()}
    except (WbSuppliesHttpStatusError, WbSuppliesTransportError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
