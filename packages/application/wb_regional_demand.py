"""Regional demand estimation for WB supply allocation.

This module keeps the WB regional supply methodology separate from the
operator-facing calculation block. It uses stock depletion only to estimate
district shares; total SKU demand remains based on orderCount.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Any, Mapping

from packages.application.demand_estimation import DEMAND_VALID_DAY_BASELINE_RATIO
from packages.application.factory_order_sales_history import SALES_HISTORY_SOURCE_KEY
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.wb_regional_supply import DISTRICT_KEYS


STOCKS_SOURCE_KEY = "stocks"
REGIONAL_DEMAND_METHOD_STOCK_DEPLETION = "stock_depletion_valid_days"
REGIONAL_DEMAND_METHOD_STOCK_SHARE_FALLBACK = "current_stock_share_fallback"
TOTAL_DAILY_DEMAND_SOURCE_ORDER_COUNT = "orderCount"

DEFAULT_MIN_LOOKUP_DAYS = 120
DEFAULT_MAX_LOOKUP_DAYS = 365


@dataclass(frozen=True)
class WbRegionalDemandEstimate:
    nm_id: int
    daily_demand_total: float
    district_daily_demand_by_key: dict[str, float]
    average_depletion_share_by_district: dict[str, float]
    diagnostics: dict[str, Any]
    warning: str


def estimate_wb_regional_demand(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    report_date: date,
    nm_ids: list[int],
    requested_valid_day_count: int,
    district_field_by_key: Mapping[str, str],
    current_stock_by_nm: Mapping[int, Mapping[str, float]],
) -> dict[int, WbRegionalDemandEstimate]:
    """Estimate district demand for each SKU from clean historical stock depletion days."""

    requested_count = max(int(requested_valid_day_count), 1)
    max_lookup_days = _stock_depletion_lookup_days(requested_count)
    candidate_dates = [
        report_date - timedelta(days=offset)
        for offset in range(1, max_lookup_days + 1)
    ]
    stock_payloads = _load_temporal_payloads(
        runtime=runtime,
        source_key=STOCKS_SOURCE_KEY,
        date_from=report_date - timedelta(days=max_lookup_days + 1),
        date_to=report_date - timedelta(days=1),
    )
    sales_payloads = _load_temporal_payloads(
        runtime=runtime,
        source_key=SALES_HISTORY_SOURCE_KEY,
        date_from=report_date - timedelta(days=max_lookup_days),
        date_to=report_date - timedelta(days=1),
    )
    stock_by_date = {
        snapshot_date: _stocks_by_nm_id(payload, district_field_by_key=district_field_by_key)
        for snapshot_date, payload in stock_payloads.items()
    }
    order_counts_by_date = {
        snapshot_date: _order_count_by_nm_id(payload)
        for snapshot_date, payload in sales_payloads.items()
    }

    out: dict[int, WbRegionalDemandEstimate] = {}
    for nm_id in nm_ids:
        out[int(nm_id)] = _estimate_one_sku(
            nm_id=int(nm_id),
            report_date=report_date,
            requested_valid_day_count=requested_count,
            max_lookup_days=max_lookup_days,
            candidate_dates=candidate_dates,
            stock_by_date=stock_by_date,
            order_counts_by_date=order_counts_by_date,
            current_stock_by_district=current_stock_by_nm.get(int(nm_id), {}),
        )
    return out


def build_result_diagnostics(estimates: Mapping[int, WbRegionalDemandEstimate]) -> dict[str, Any]:
    items = list(estimates.values())
    method_counts: dict[str, int] = {}
    fallback_sku_ids: list[int] = []
    warning_count = 0
    selected_counts: list[int] = []
    inspected_counts: list[int] = []
    excluded_reason_counts: dict[str, int] = {}
    for estimate in items:
        diagnostics = estimate.diagnostics
        method = str(diagnostics.get("regional_demand_method") or "")
        method_counts[method] = method_counts.get(method, 0) + 1
        if bool(diagnostics.get("fallback_used")):
            fallback_sku_ids.append(int(estimate.nm_id))
        if (
            estimate.warning
            and int(diagnostics.get("initial_window_valid_day_count") or 0)
            < int(diagnostics.get("requested_valid_day_count") or 0)
        ):
            warning_count += 1
        selected_counts.append(int(diagnostics.get("selected_valid_day_count") or 0))
        inspected_counts.append(int(diagnostics.get("inspected_day_count") or 0))
        for reason, count in dict(diagnostics.get("excluded_day_reason_counts") or {}).items():
            excluded_reason_counts[str(reason)] = excluded_reason_counts.get(str(reason), 0) + int(count)

    if not items:
        return {
            "regional_demand_method": REGIONAL_DEMAND_METHOD_STOCK_DEPLETION,
            "sku_count": 0,
            "fallback_sku_count": 0,
            "method_counts": {},
            "warnings": [],
        }

    primary_count = method_counts.get(REGIONAL_DEMAND_METHOD_STOCK_DEPLETION, 0)
    fallback_count = method_counts.get(REGIONAL_DEMAND_METHOD_STOCK_SHARE_FALLBACK, 0)
    if fallback_count and primary_count:
        result_method = "mixed_stock_depletion_with_current_stock_share_fallback"
    elif fallback_count:
        result_method = REGIONAL_DEMAND_METHOD_STOCK_SHARE_FALLBACK
    else:
        result_method = REGIONAL_DEMAND_METHOD_STOCK_DEPLETION

    warnings: list[str] = []
    if fallback_sku_ids:
        warnings.append(
            "Fallback current-stock-share used for SKU count="
            f"{len(fallback_sku_ids)}; nmIds={','.join(str(item) for item in fallback_sku_ids[:20])}"
        )
    if warning_count and not fallback_sku_ids:
        warnings.append(f"Rows with stock-depletion lookup warnings: {warning_count}")
    return {
        "regional_demand_method": result_method,
        "sku_count": len(items),
        "fallback_sku_count": len(fallback_sku_ids),
        "fallback_nm_ids": fallback_sku_ids,
        "method_counts": method_counts,
        "requested_valid_day_count": int(
            items[0].diagnostics.get("requested_valid_day_count") or 0
        ),
        "min_selected_valid_day_count": min(selected_counts) if selected_counts else 0,
        "max_selected_valid_day_count": max(selected_counts) if selected_counts else 0,
        "max_inspected_day_count": max(inspected_counts) if inspected_counts else 0,
        "excluded_day_reason_counts": excluded_reason_counts,
        "warnings": warnings,
    }


def _estimate_one_sku(
    *,
    nm_id: int,
    report_date: date,
    requested_valid_day_count: int,
    max_lookup_days: int,
    candidate_dates: list[date],
    stock_by_date: Mapping[str, Mapping[int, Mapping[str, float]]],
    order_counts_by_date: Mapping[str, Mapping[int, float]],
    current_stock_by_district: Mapping[str, float],
) -> WbRegionalDemandEstimate:
    order_values = [
        float(order_counts_by_date.get(candidate.isoformat(), {}).get(nm_id, 0.0))
        for candidate in candidate_dates
        if nm_id in order_counts_by_date.get(candidate.isoformat(), {})
    ]
    positive_order_values = [value for value in order_values if value > 0]
    baseline_daily_sales = float(median(positive_order_values)) if positive_order_values else 0.0
    valid_day_threshold = (
        max(1.0, baseline_daily_sales * DEMAND_VALID_DAY_BASELINE_RATIO)
        if baseline_daily_sales > 0
        else 0.0
    )
    selected: list[dict[str, Any]] = []
    excluded_reason_counts: dict[str, int] = {}
    inspected_day_count = 0
    initial_window_valid_day_count = 0
    for candidate in candidate_dates:
        if len(selected) >= requested_valid_day_count:
            break
        inspected_day_count += 1
        validation = _validate_stock_depletion_day(
            nm_id=nm_id,
            depletion_date=candidate,
            stock_by_date=stock_by_date,
            order_counts_by_date=order_counts_by_date,
            valid_day_threshold=valid_day_threshold,
        )
        if validation["valid"]:
            if inspected_day_count <= requested_valid_day_count:
                initial_window_valid_day_count += 1
            selected.append(validation)
        else:
            reason = str(validation["reason"])
            excluded_reason_counts[reason] = excluded_reason_counts.get(reason, 0) + 1

    if len(selected) >= requested_valid_day_count:
        return _primary_estimate(
            nm_id=nm_id,
            report_date=report_date,
            requested_valid_day_count=requested_valid_day_count,
            max_lookup_days=max_lookup_days,
            selected=selected,
            inspected_day_count=inspected_day_count,
            initial_window_valid_day_count=initial_window_valid_day_count,
            excluded_reason_counts=excluded_reason_counts,
            baseline_daily_sales=baseline_daily_sales,
            valid_day_threshold=valid_day_threshold,
        )

    return _fallback_estimate(
        nm_id=nm_id,
        report_date=report_date,
        requested_valid_day_count=requested_valid_day_count,
        max_lookup_days=max_lookup_days,
        selected=selected,
        inspected_day_count=inspected_day_count,
        initial_window_valid_day_count=initial_window_valid_day_count,
        excluded_reason_counts=excluded_reason_counts,
        baseline_daily_sales=baseline_daily_sales,
        valid_day_threshold=valid_day_threshold,
        candidate_dates=candidate_dates,
        order_counts_by_date=order_counts_by_date,
        current_stock_by_district=current_stock_by_district,
    )


def _primary_estimate(
    *,
    nm_id: int,
    report_date: date,
    requested_valid_day_count: int,
    max_lookup_days: int,
    selected: list[dict[str, Any]],
    inspected_day_count: int,
    initial_window_valid_day_count: int,
    excluded_reason_counts: Mapping[str, int],
    baseline_daily_sales: float,
    valid_day_threshold: float,
) -> WbRegionalDemandEstimate:
    shares = {key: 0.0 for key in DISTRICT_KEYS}
    for item in selected:
        total_depletion = float(item["total_depletion"])
        for key in DISTRICT_KEYS:
            shares[key] += float(item["depletions"].get(key, 0.0)) / total_depletion
    shares = _normalize_shares({key: value / len(selected) for key, value in shares.items()})
    daily_demand_total = sum(float(item["order_count"]) for item in selected) / len(selected)
    district_daily_demand = {
        key: float(daily_demand_total) * float(shares.get(key, 0.0))
        for key in DISTRICT_KEYS
    }
    used_dates = sorted(str(item["date"]) for item in selected)
    lookup_depth_days = (report_date - date.fromisoformat(used_dates[0])).days if used_dates else 0
    excluded_from_initial = max(requested_valid_day_count - initial_window_valid_day_count, 0)
    if excluded_from_initial:
        warning = (
            f"Пропорции округов рассчитаны по {len(selected)} валидным дням выбывания остатков; "
            f"исходное окно {requested_valid_day_count} дней содержало {excluded_from_initial} исключённых дней, "
            f"lookup ушёл на {lookup_depth_days} календарных дней назад."
        )
    else:
        warning = (
            f"Пропорции округов рассчитаны полностью внутри исходного окна "
            f"{requested_valid_day_count} дней."
        )
    diagnostics = _base_diagnostics(
        method=REGIONAL_DEMAND_METHOD_STOCK_DEPLETION,
        requested_valid_day_count=requested_valid_day_count,
        max_lookup_days=max_lookup_days,
        selected_valid_day_count=len(selected),
        inspected_day_count=inspected_day_count,
        initial_window_valid_day_count=initial_window_valid_day_count,
        excluded_reason_counts=excluded_reason_counts,
        baseline_daily_sales=baseline_daily_sales,
        valid_day_threshold=valid_day_threshold,
        used_dates=used_dates,
        lookup_depth_days=lookup_depth_days,
        average_shares=shares,
        warning=warning,
        fallback_used=False,
        fallback_reason="",
    )
    return WbRegionalDemandEstimate(
        nm_id=nm_id,
        daily_demand_total=float(daily_demand_total),
        district_daily_demand_by_key=district_daily_demand,
        average_depletion_share_by_district=shares,
        diagnostics=diagnostics,
        warning=warning,
    )


def _fallback_estimate(
    *,
    nm_id: int,
    report_date: date,
    requested_valid_day_count: int,
    max_lookup_days: int,
    selected: list[dict[str, Any]],
    inspected_day_count: int,
    initial_window_valid_day_count: int,
    excluded_reason_counts: Mapping[str, int],
    baseline_daily_sales: float,
    valid_day_threshold: float,
    candidate_dates: list[date],
    order_counts_by_date: Mapping[str, Mapping[int, float]],
    current_stock_by_district: Mapping[str, float],
) -> WbRegionalDemandEstimate:
    order_samples: list[tuple[str, float]] = []
    for candidate in candidate_dates:
        value = order_counts_by_date.get(candidate.isoformat(), {}).get(nm_id)
        if value is None:
            continue
        if valid_day_threshold > 0 and float(value) < valid_day_threshold:
            continue
        order_samples.append((candidate.isoformat(), float(value)))
        if len(order_samples) >= requested_valid_day_count:
            break
    if not order_samples:
        positive_fallback = [
            (
                candidate.isoformat(),
                float(order_counts_by_date.get(candidate.isoformat(), {}).get(nm_id, 0.0)),
            )
            for candidate in candidate_dates
            if float(order_counts_by_date.get(candidate.isoformat(), {}).get(nm_id, 0.0)) > 0
        ]
        order_samples = positive_fallback[:requested_valid_day_count]
    daily_demand_total = (
        sum(value for _, value in order_samples) / len(order_samples)
        if order_samples
        else 0.0
    )
    shares = _current_stock_shares(current_stock_by_district)
    district_daily_demand = {
        key: float(daily_demand_total) * float(shares.get(key, 0.0))
        for key in DISTRICT_KEYS
    }
    used_dates = sorted(snapshot_date for snapshot_date, _ in order_samples)
    fallback_reason = (
        f"collected {len(selected)} valid stock-depletion days from requested "
        f"{requested_valid_day_count} within bounded lookup {max_lookup_days}"
    )
    warning = (
        "Не удалось собрать достаточное число валидных дней выбывания остатков; "
        "применён явный fallback на текущую структуру остатков. "
        f"Причина: {fallback_reason}."
    )
    diagnostics = _base_diagnostics(
        method=REGIONAL_DEMAND_METHOD_STOCK_SHARE_FALLBACK,
        requested_valid_day_count=requested_valid_day_count,
        max_lookup_days=max_lookup_days,
        selected_valid_day_count=len(selected),
        inspected_day_count=inspected_day_count,
        initial_window_valid_day_count=initial_window_valid_day_count,
        excluded_reason_counts=excluded_reason_counts,
        baseline_daily_sales=baseline_daily_sales,
        valid_day_threshold=valid_day_threshold,
        used_dates=used_dates,
        lookup_depth_days=(report_date - date.fromisoformat(used_dates[0])).days if used_dates else inspected_day_count,
        average_shares=shares,
        warning=warning,
        fallback_used=True,
        fallback_reason=fallback_reason,
    )
    return WbRegionalDemandEstimate(
        nm_id=nm_id,
        daily_demand_total=float(daily_demand_total),
        district_daily_demand_by_key=district_daily_demand,
        average_depletion_share_by_district=shares,
        diagnostics=diagnostics,
        warning=warning,
    )


def _validate_stock_depletion_day(
    *,
    nm_id: int,
    depletion_date: date,
    stock_by_date: Mapping[str, Mapping[int, Mapping[str, float]]],
    order_counts_by_date: Mapping[str, Mapping[int, float]],
    valid_day_threshold: float,
) -> dict[str, Any]:
    current_date = depletion_date.isoformat()
    previous_date = (depletion_date - timedelta(days=1)).isoformat()
    previous_snapshot = stock_by_date.get(previous_date)
    current_snapshot = stock_by_date.get(current_date)
    if previous_snapshot is None:
        return _invalid_day(current_date, "missing_previous_stock_snapshot")
    if current_snapshot is None:
        return _invalid_day(current_date, "missing_current_stock_snapshot")
    previous_row = previous_snapshot.get(nm_id)
    current_row = current_snapshot.get(nm_id)
    if previous_row is None or current_row is None:
        return _invalid_day(current_date, "missing_sku_stock_coverage")
    order_count = order_counts_by_date.get(current_date, {}).get(nm_id)
    if order_count is None:
        return _invalid_day(current_date, "missing_order_count")
    if valid_day_threshold > 0 and float(order_count) < valid_day_threshold:
        return _invalid_day(current_date, "low_order_count_signal")

    depletions: dict[str, float] = {}
    for key in DISTRICT_KEYS:
        if key not in previous_row or key not in current_row:
            return _invalid_day(current_date, "missing_district_stock")
        previous_value = previous_row.get(key)
        current_value = current_row.get(key)
        if not _is_number(previous_value) or not _is_number(current_value):
            return _invalid_day(current_date, "non_numeric_district_stock")
        previous_float = float(previous_value)
        current_float = float(current_value)
        if previous_float < 0 or current_float < 0:
            return _invalid_day(current_date, "negative_district_stock")
        if current_float > previous_float:
            return _invalid_day(current_date, "district_restock_or_upward_correction")
        depletion = previous_float - current_float
        if (previous_float <= 0 or current_float <= 0) and (float(order_count) > 0 or depletion > 0):
            return _invalid_day(current_date, "district_out_of_stock_risk")
        depletions[key] = depletion

    total_depletion = sum(depletions.values())
    if total_depletion <= 0:
        if float(order_count) > 0:
            return _invalid_day(current_date, "zero_total_depletion_with_positive_order_count")
        return _invalid_day(current_date, "zero_total_depletion")
    return {
        "valid": True,
        "date": current_date,
        "order_count": float(order_count),
        "depletions": depletions,
        "total_depletion": float(total_depletion),
    }


def _base_diagnostics(
    *,
    method: str,
    requested_valid_day_count: int,
    max_lookup_days: int,
    selected_valid_day_count: int,
    inspected_day_count: int,
    initial_window_valid_day_count: int,
    excluded_reason_counts: Mapping[str, int],
    baseline_daily_sales: float,
    valid_day_threshold: float,
    used_dates: list[str],
    lookup_depth_days: int,
    average_shares: Mapping[str, float],
    warning: str,
    fallback_used: bool,
    fallback_reason: str,
) -> dict[str, Any]:
    return {
        "regional_demand_method": method,
        "requested_valid_day_count": int(requested_valid_day_count),
        "selected_valid_day_count": int(selected_valid_day_count),
        "inspected_day_count": int(inspected_day_count),
        "initial_window_valid_day_count": int(initial_window_valid_day_count),
        "excluded_day_count": int(inspected_day_count) - int(selected_valid_day_count),
        "excluded_day_reason_counts": {
            str(key): int(value)
            for key, value in sorted(dict(excluded_reason_counts).items())
        },
        "earliest_used_stock_depletion_date": used_dates[0] if used_dates and not fallback_used else "",
        "latest_used_stock_depletion_date": used_dates[-1] if used_dates and not fallback_used else "",
        "lookup_depth_days": int(lookup_depth_days),
        "max_lookup_days": int(max_lookup_days),
        "used_stock_depletion_dates": list(used_dates) if not fallback_used else [],
        "average_depletion_share_by_district": {
            key: float(average_shares.get(key, 0.0))
            for key in DISTRICT_KEYS
        },
        "total_daily_demand_source": TOTAL_DAILY_DEMAND_SOURCE_ORDER_COUNT,
        "fallback_used": bool(fallback_used),
        "fallback_reason": str(fallback_reason),
        "warning": str(warning),
        "baseline_daily_sales": float(baseline_daily_sales),
        "valid_day_threshold": float(valid_day_threshold),
    }


def _load_temporal_payloads(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    source_key: str,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    payloads: dict[str, Any] = {}
    current = date_from
    while current <= date_to:
        payload, _ = runtime.load_temporal_source_snapshot(
            source_key=source_key,
            snapshot_date=current.isoformat(),
        )
        if payload is not None:
            payloads[current.isoformat()] = payload
        current += timedelta(days=1)
    return payloads


def _stocks_by_nm_id(
    payload: Any,
    *,
    district_field_by_key: Mapping[str, str],
) -> dict[int, dict[str, float]]:
    result = getattr(payload, "result", payload)
    if str(getattr(result, "kind", "")) != "success":
        return {}
    out: dict[int, dict[str, float]] = {}
    for item in list(getattr(result, "items", []) or []):
        nm_id = getattr(item, "nm_id", None)
        if not isinstance(nm_id, int):
            continue
        row: dict[str, float] = {}
        for key in DISTRICT_KEYS:
            field_name = str(district_field_by_key[key])
            value = getattr(item, field_name, None)
            if _is_number(value):
                row[key] = float(value)
        out[int(nm_id)] = row
    return out


def _order_count_by_nm_id(payload: Any) -> dict[int, float]:
    result = getattr(payload, "result", payload)
    if str(getattr(result, "kind", "")) != "success":
        return {}
    out: dict[int, float] = {}
    for item in list(getattr(result, "items", []) or []):
        if str(getattr(item, "metric", "") or "") != "orderCount":
            continue
        nm_id = getattr(item, "nm_id", None)
        value = getattr(item, "value", None)
        if isinstance(nm_id, int) and _is_number(value):
            out[int(nm_id)] = float(value)
    return out


def _current_stock_shares(current_stock_by_district: Mapping[str, float]) -> dict[str, float]:
    positive_total = sum(max(float(current_stock_by_district.get(key, 0.0)), 0.0) for key in DISTRICT_KEYS)
    if positive_total <= 0:
        equal_share = 1.0 / len(DISTRICT_KEYS)
        return {key: equal_share for key in DISTRICT_KEYS}
    return {
        key: max(float(current_stock_by_district.get(key, 0.0)), 0.0) / positive_total
        for key in DISTRICT_KEYS
    }


def _normalize_shares(shares: Mapping[str, float]) -> dict[str, float]:
    total = sum(max(float(shares.get(key, 0.0)), 0.0) for key in DISTRICT_KEYS)
    if total <= 0:
        return {key: 0.0 for key in DISTRICT_KEYS}
    return {
        key: max(float(shares.get(key, 0.0)), 0.0) / total
        for key in DISTRICT_KEYS
    }


def _invalid_day(snapshot_date: str, reason: str) -> dict[str, Any]:
    return {
        "valid": False,
        "date": snapshot_date,
        "reason": reason,
    }


def _stock_depletion_lookup_days(requested_valid_day_count: int) -> int:
    return min(
        DEFAULT_MAX_LOOKUP_DAYS,
        max(DEFAULT_MIN_LOOKUP_DAYS, requested_valid_day_count * 8),
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
