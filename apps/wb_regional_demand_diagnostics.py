"""Read-only diagnostics for WB regional demand fallback causes."""

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
from packages.contracts.wb_regional_supply import DISTRICT_FAR_SIBERIA, DISTRICT_KEYS


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare WB regional stock-depletion fallback diagnostics for all districts "
            "and with far_siberia excluded. This is read-only and does not run calculate()."
        )
    )
    parser.add_argument("--runtime-dir", required=True, help="Registry runtime directory.")
    parser.add_argument("--report-date", default="", help="YYYY-MM-DD; defaults to last saved WB regional result.")
    parser.add_argument("--requested-days", type=int, default=0, help="Requested valid depletion days; defaults to last result settings.")
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
    nm_ids = [
        int(item.nm_id)
        for item in runtime.load_current_state().config_v2
        if bool(getattr(item, "enabled", False))
    ]
    current_stock_by_nm = _current_stock_by_nm_from_last_result(last_result)
    current_stock_by_nm = {
        nm_id: current_stock_by_nm.get(nm_id, {key: 0.0 for key in DISTRICT_KEYS})
        for nm_id in nm_ids
    }
    order_batch_qty = int(settings.get("order_batch_qty") or 250)

    all_estimates = estimate_wb_regional_demand(
        runtime=runtime,
        report_date=report_date,
        nm_ids=nm_ids,
        requested_valid_day_count=requested_days,
        district_field_by_key=_DISTRICT_FIELD_BY_KEY,
        current_stock_by_nm=current_stock_by_nm,
        included_district_keys=tuple(DISTRICT_KEYS),
        persistent_zero_current_stock_max_qty=max(float(order_batch_qty - 1), 0.0),
    )
    without_far_keys = tuple(key for key in DISTRICT_KEYS if key != DISTRICT_FAR_SIBERIA)
    without_far_estimates = estimate_wb_regional_demand(
        runtime=runtime,
        report_date=report_date,
        nm_ids=nm_ids,
        requested_valid_day_count=requested_days,
        district_field_by_key=_DISTRICT_FIELD_BY_KEY,
        current_stock_by_nm=current_stock_by_nm,
        included_district_keys=without_far_keys,
        persistent_zero_current_stock_max_qty=max(float(order_batch_qty - 1), 0.0),
    )
    all_summary = _summary(
        all_estimates,
        current_stock_by_nm=current_stock_by_nm,
        order_batch_qty=order_batch_qty,
    )
    without_far_summary = _summary(
        without_far_estimates,
        current_stock_by_nm=current_stock_by_nm,
        order_batch_qty=order_batch_qty,
    )
    changed_to_valid = sorted(
        set(all_summary["fallback_nm_ids"]) - set(without_far_summary["fallback_nm_ids"])
    )
    payload = {
        "report_date": report_date.isoformat(),
        "active_sku_count": len(nm_ids),
        "requested_valid_day_count": requested_days,
        "order_batch_qty": order_batch_qty,
        "with_far_siberia": all_summary,
        "without_far_siberia": without_far_summary,
        "changed_to_valid_count": len(changed_to_valid),
        "changed_to_valid_sample": changed_to_valid[:20],
        "conclusion": _conclusion(all_summary, without_far_summary),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _summary(
    estimates: Mapping[int, Any],
    *,
    current_stock_by_nm: Mapping[int, Mapping[str, float]],
    order_batch_qty: int,
) -> dict[str, Any]:
    diagnostics = build_result_diagnostics(estimates)
    fallback_nm_ids = list(diagnostics.get("fallback_nm_ids") or [])
    seed_candidate_summary = _seed_candidate_summary(
        estimates,
        current_stock_by_nm=current_stock_by_nm,
        order_batch_qty=order_batch_qty,
    )
    return {
        "included_district_keys": list(diagnostics.get("included_district_keys") or DISTRICT_KEYS),
        "excluded_district_keys": list(diagnostics.get("excluded_district_keys") or []),
        "valid_sku_count": int(diagnostics.get("primary_sku_count") or 0),
        "fallback_sku_count": int(diagnostics.get("fallback_sku_count") or 0),
        "fallback_nm_ids": fallback_nm_ids,
        "persistent_zero_sku_count": int(diagnostics.get("persistent_zero_sku_count") or 0),
        "persistent_zero_sku_district_count": int(diagnostics.get("persistent_zero_sku_district_count") or 0),
        "persistent_zero_day_count": int(diagnostics.get("persistent_zero_day_count") or 0),
        "persistent_zero_day_count_by_district": dict(diagnostics.get("persistent_zero_day_count_by_district") or {}),
        "seed_candidate_sku_count": seed_candidate_summary["seed_candidate_sku_count"],
        "seed_candidate_sku_district_count": seed_candidate_summary["seed_candidate_sku_district_count"],
        "seed_requested_qty_total_before_ff_limit": seed_candidate_summary["seed_requested_qty_total_before_ff_limit"],
        "seed_candidate_nm_ids": seed_candidate_summary["seed_candidate_nm_ids"],
        "min_selected_valid_day_count": int(diagnostics.get("min_selected_valid_day_count") or 0),
        "max_selected_valid_day_count": int(diagnostics.get("max_selected_valid_day_count") or 0),
        "max_inspected_day_count": int(diagnostics.get("max_inspected_day_count") or 0),
        "top_invalid_reasons": sorted(
            dict(diagnostics.get("excluded_day_reason_counts") or {}).items(),
            key=lambda item: -int(item[1]),
        )[:10],
    }


def _seed_candidate_summary(
    estimates: Mapping[int, Any],
    *,
    current_stock_by_nm: Mapping[int, Mapping[str, float]],
    order_batch_qty: int,
) -> dict[str, Any]:
    candidate_nm_ids: list[int] = []
    candidate_district_count = 0
    for nm_id, estimate in estimates.items():
        diagnostics = getattr(estimate, "diagnostics", {}) or {}
        included = set(diagnostics.get("included_district_keys") or DISTRICT_KEYS)
        persistent_zero_keys = [
            str(item)
            for item in list(diagnostics.get("persistent_zero_district_keys") or [])
            if str(item) in included
        ]
        sku_candidate_count = 0
        current_stock = current_stock_by_nm.get(int(nm_id), {})
        district_daily_demand = getattr(estimate, "district_daily_demand_by_key", {}) or {}
        if float(getattr(estimate, "daily_demand_total", 0.0) or 0.0) > 0:
            for key in persistent_zero_keys:
                try:
                    stock_value = float(current_stock.get(key, 0.0) or 0.0)
                except (TypeError, ValueError):
                    stock_value = 0.0
                if stock_value >= max(float(order_batch_qty), 1.0):
                    continue
                if float(district_daily_demand.get(key, 0.0) or 0.0) != 0.0:
                    continue
                sku_candidate_count += 1
        if sku_candidate_count:
            candidate_nm_ids.append(int(nm_id))
            candidate_district_count += sku_candidate_count
    return {
        "seed_candidate_sku_count": len(candidate_nm_ids),
        "seed_candidate_sku_district_count": candidate_district_count,
        "seed_requested_qty_total_before_ff_limit": int(candidate_district_count * max(int(order_batch_qty), 0)),
        "seed_candidate_nm_ids": sorted(candidate_nm_ids),
    }


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


def _conclusion(all_summary: Mapping[str, Any], without_far_summary: Mapping[str, Any]) -> str:
    before = int(all_summary.get("fallback_sku_count") or 0)
    after = int(without_far_summary.get("fallback_sku_count") or 0)
    if after < before:
        return f"excluding far_siberia reduced fallback SKU count from {before} to {after}"
    if after == before:
        return f"excluding far_siberia did not change fallback SKU count ({before})"
    return f"excluding far_siberia increased fallback SKU count from {before} to {after}"


if __name__ == "__main__":
    main()
