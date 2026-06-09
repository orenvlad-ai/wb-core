"""Read-only diagnostics for the WB regional share ladder.

The helper does not run production calculate() and does not mutate last_result.
It replays demand estimation from persisted runtime snapshots.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.wb_regional_demand import build_result_diagnostics, estimate_wb_regional_demand
from packages.application.wb_regional_supply import _DISTRICT_FIELD_BY_KEY
from packages.contracts.wb_regional_supply import DISTRICT_KEYS


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect WB regional share ladder diagnostics from runtime snapshots. "
            "Read-only: does not call the production supply calculate route."
        )
    )
    parser.add_argument("--runtime-dir", required=True, help="Registry runtime directory.")
    parser.add_argument("--report-date", default="", help="YYYY-MM-DD; defaults to last saved WB regional result or today.")
    parser.add_argument("--requested-days", type=int, default=0, help="Requested quality days; defaults to last result settings or 14.")
    parser.add_argument("--include-districts", default="", help="Comma-separated district keys to include.")
    parser.add_argument("--exclude-districts", default="", help="Comma-separated district keys to exclude from methodology.")
    parser.add_argument("--max-per-sku", type=int, default=50, help="Max per-SKU rows to print.")
    args = parser.parse_args()

    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    last_result = runtime.load_wb_regional_supply_result_state() or {}
    settings = last_result.get("settings") if isinstance(last_result.get("settings"), Mapping) else {}
    report_date = date.fromisoformat(
        args.report_date
        or str(last_result.get("report_date") or "")
        or date.today().isoformat()
    )
    requested_days = int(args.requested_days or settings.get("sales_avg_period_days") or 14)
    order_batch_qty = int(settings.get("order_batch_qty") or 250)
    included_district_keys = _resolve_included_districts(
        include_arg=args.include_districts,
        exclude_arg=args.exclude_districts,
        settings=settings,
    )

    current_state = runtime.load_current_state()
    active_items = [item for item in current_state.config_v2 if bool(getattr(item, "enabled", False))]
    nm_ids = [int(item.nm_id) for item in active_items]
    sku_metadata_by_nm = {
        int(item.nm_id): {
            "display_name": str(getattr(item, "display_name", "") or ""),
            "group": str(getattr(item, "group", "") or ""),
        }
        for item in active_items
    }
    current_stock_by_nm = _current_stock_by_nm_from_last_result(last_result)
    current_stock_by_nm = {
        nm_id: current_stock_by_nm.get(nm_id, {key: 0.0 for key in DISTRICT_KEYS})
        for nm_id in nm_ids
    }

    estimates = estimate_wb_regional_demand(
        runtime=runtime,
        report_date=report_date,
        nm_ids=nm_ids,
        requested_valid_day_count=requested_days,
        district_field_by_key=_DISTRICT_FIELD_BY_KEY,
        current_stock_by_nm=current_stock_by_nm,
        included_district_keys=included_district_keys,
        persistent_zero_current_stock_max_qty=max(float(order_batch_qty - 1), 0.0),
        sku_metadata_by_nm=sku_metadata_by_nm,
    )
    diagnostics = build_result_diagnostics(estimates)
    seed_replacement_examples = _seed_replacement_examples(
        estimates,
        current_stock_by_nm=current_stock_by_nm,
        order_batch_qty=order_batch_qty,
    )
    payload = {
        "read_only": True,
        "production_calculate_mutated_last_result": False,
        "report_date": report_date.isoformat(),
        "active_sku_count": len(nm_ids),
        "requested_valid_day_count": requested_days,
        "order_batch_qty": order_batch_qty,
        "included_district_keys": list(included_district_keys),
        "excluded_district_keys": [key for key in DISTRICT_KEYS if key not in set(included_district_keys)],
        "fallback_sku_count": int(diagnostics.get("fallback_sku_count") or 0),
        "share_source_counts": dict(diagnostics.get("share_source_counts") or {}),
        "regional_share_method_counts": dict(diagnostics.get("regional_share_method_counts") or {}),
        "low_confidence_sku_district_count": int(diagnostics.get("low_confidence_sku_district_count") or 0),
        "seed_floor_sku_district_count": int(diagnostics.get("seed_floor_sku_district_count") or 0),
        "seed_floor_sku_count": int(diagnostics.get("seed_floor_sku_count") or 0),
        "zero_zero_no_signal_day_count_by_district": dict(diagnostics.get("zero_zero_no_signal_day_count_by_district") or {}),
        "top_invalid_reasons": sorted(
            dict(diagnostics.get("excluded_day_reason_counts") or {}).items(),
            key=lambda item: -int(item[1]),
        )[:10],
        "top_partial_global_invalid_reasons": sorted(
            dict(diagnostics.get("partial_global_day_reason_counts") or {}).items(),
            key=lambda item: -int(item[1]),
        )[:10],
        "seed_replaced_by_demand_based_examples": seed_replacement_examples[:20],
        "per_sku": _per_sku_rows(estimates, limit=max(int(args.max_per_sku), 0)),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _resolve_included_districts(
    *,
    include_arg: str,
    exclude_arg: str,
    settings: Mapping[str, Any],
) -> tuple[str, ...]:
    if include_arg.strip():
        requested = _parse_district_csv(include_arg)
        _validate_districts(requested)
        return tuple(key for key in DISTRICT_KEYS if key in set(requested))
    configured = settings.get("included_district_keys")
    if isinstance(configured, list) and configured:
        requested = [str(item) for item in configured]
    else:
        requested = list(DISTRICT_KEYS)
    excluded = set(_parse_district_csv(exclude_arg))
    _validate_districts(requested)
    _validate_districts(excluded)
    included = tuple(key for key in DISTRICT_KEYS if key in set(requested) and key not in excluded)
    if not included:
        raise SystemExit("included district selection is empty")
    return included


def _parse_district_csv(value: str) -> list[str]:
    if not value.strip():
        return []
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def _validate_districts(values: Any) -> None:
    unknown = sorted(str(item) for item in values if str(item) not in DISTRICT_KEYS)
    if unknown:
        raise SystemExit("unknown district keys: " + ", ".join(unknown))


def _per_sku_rows(estimates: Mapping[int, Any], *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for nm_id in sorted(estimates):
        if limit and len(rows) >= limit:
            break
        estimate = estimates[nm_id]
        diagnostics = getattr(estimate, "diagnostics", {}) or {}
        rows.append(
            {
                "nm_id": int(nm_id),
                "share_estimation_method": diagnostics.get("share_estimation_method"),
                "total_demand_source": diagnostics.get("total_demand_source"),
                "daily_demand_total": getattr(estimate, "daily_demand_total", 0.0),
                "district_share_sources": diagnostics.get("district_share_sources", {}),
                "confidence_by_district": diagnostics.get("confidence_by_district", {}),
                "final_share_by_district": diagnostics.get("final_share_by_district", {}),
                "district_observation_counts": diagnostics.get("district_observation_counts", {}),
                "district_positive_depletion_counts": diagnostics.get("district_positive_depletion_counts", {}),
                "district_zero_zero_no_signal_counts": diagnostics.get("district_zero_zero_no_signal_counts", {}),
                "district_stockout_risk_counts": diagnostics.get("district_stockout_risk_counts", {}),
                "district_restock_counts": diagnostics.get("district_restock_counts", {}),
                "group_prior_key": diagnostics.get("group_prior_key", ""),
                "group_prior_peer_count": diagnostics.get("group_prior_peer_count", 0),
                "seed_reason_by_district": diagnostics.get("seed_reason_by_district", {}),
            }
        )
    return rows


def _seed_replacement_examples(
    estimates: Mapping[int, Any],
    *,
    current_stock_by_nm: Mapping[int, Mapping[str, float]],
    order_batch_qty: int,
) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for nm_id, estimate in sorted(estimates.items()):
        diagnostics = getattr(estimate, "diagnostics", {}) or {}
        zero_zero_counts = dict(diagnostics.get("district_zero_zero_no_signal_counts") or {})
        sources = dict(diagnostics.get("district_share_sources") or {})
        shares = dict(diagnostics.get("final_share_by_district") or {})
        current_stock = current_stock_by_nm.get(int(nm_id), {})
        for key in DISTRICT_KEYS:
            if int(zero_zero_counts.get(key, 0) or 0) <= 0:
                continue
            if str(sources.get(key) or "") in {"seed_floor", "seed_floor_candidate", "excluded"}:
                continue
            if float(shares.get(key, 0.0) or 0.0) <= 0:
                continue
            try:
                stock_value = float(current_stock.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                stock_value = 0.0
            if stock_value >= max(float(order_batch_qty), 1.0):
                continue
            examples.append(
                {
                    "nm_id": int(nm_id),
                    "district_key": key,
                    "new_share_source": str(sources.get(key)),
                    "final_share": float(shares.get(key, 0.0) or 0.0),
                    "zero_zero_no_signal_days": int(zero_zero_counts.get(key, 0) or 0),
                }
            )
    return examples


def _current_stock_by_nm_from_last_result(payload: Mapping[str, Any]) -> dict[int, dict[str, float]]:
    out: dict[int, dict[str, float]] = {}
    districts = payload.get("districts") if isinstance(payload.get("districts"), list) else []
    for district in districts:
        if not isinstance(district, Mapping):
            continue
        district_key = str(district.get("district_key") or "")
        if district_key not in DISTRICT_KEYS:
            continue
        for row in district.get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            try:
                nm_id = int(row.get("nm_id"))
                current_stock = float(row.get("current_stock") or 0.0)
            except (TypeError, ValueError):
                continue
            out.setdefault(nm_id, {key: 0.0 for key in DISTRICT_KEYS})[district_key] = current_stock
    return out


if __name__ == "__main__":
    main()
