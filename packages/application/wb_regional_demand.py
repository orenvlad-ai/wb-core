"""Regional demand estimation for WB supply allocation.

The module estimates district shares only. Total SKU demand remains based on
authoritative ``orderCount`` history.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, timedelta
import re
from statistics import median
from typing import Any, Mapping

from packages.application.demand_estimation import DEMAND_VALID_DAY_BASELINE_RATIO
from packages.application.factory_order_sales_history import SALES_HISTORY_SOURCE_KEY
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.wb_regional_supply import DISTRICT_KEYS as CANONICAL_DISTRICT_KEYS
from packages.contracts.wb_supply_planning_zones import (
    CENTRAL_PLANNING_ZONE_KEYS,
    PLANNING_ZONE_CENTRAL_EAST,
    PLANNING_ZONE_CENTRAL_NORTH,
    PLANNING_ZONE_CENTRAL_SOUTH,
)


STOCKS_SOURCE_KEY = "stocks"
_DISTRICT_KEYS_CONTEXT: ContextVar[tuple[str, ...]] = ContextVar(
    "wb_regional_demand_district_keys",
    default=tuple(CANONICAL_DISTRICT_KEYS),
)


def _active_district_keys() -> tuple[str, ...]:
    return _DISTRICT_KEYS_CONTEXT.get()

REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS = "full_clean_days"
REGIONAL_SHARE_SOURCE_PARTIAL_OBSERVATIONS = "partial_district_observations"
REGIONAL_SHARE_SOURCE_PARTIAL_BLENDED = "partial_observations_blended_with_prior"
REGIONAL_SHARE_SOURCE_GROUP_PRIOR = "sku_group_prior"
REGIONAL_SHARE_SOURCE_GLOBAL_PRIOR = "global_prior"
REGIONAL_SHARE_SOURCE_SEED_FLOOR = "seed_floor"
REGIONAL_SHARE_SOURCE_SEED_FLOOR_CANDIDATE = "seed_floor_candidate"
REGIONAL_SHARE_SOURCE_EXCLUDED = "excluded"
REGIONAL_DEMAND_METHOD_LADDER = "regional_share_ladder"
REGIONAL_DEMAND_METHOD_STOCK_DEPLETION = REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS
REGIONAL_DEMAND_METHOD_STOCK_SHARE_FALLBACK = "current_stock_share_fallback"
TOTAL_DAILY_DEMAND_SOURCE_ORDER_COUNT = "orderCount"
TOTAL_DAILY_DEMAND_SOURCE_ORDER_COUNT_VALID_DAYS = "order_count_valid_days"
PERSISTENT_ZERO_NEUTRALIZED_REASON = "district_zero_zero_no_signal"

DEFAULT_MIN_LOOKUP_DAYS = 120
DEFAULT_MAX_LOOKUP_DAYS = 365
OWN_HIGH_CONFIDENCE_THRESHOLD = 0.75
OWN_MEDIUM_CONFIDENCE_THRESHOLD = 0.35
GROUP_PRIOR_MIN_PEERS = 1
GLOBAL_PRIOR_MIN_PEERS = 1


@dataclass(frozen=True)
class WbRegionalDemandEstimate:
    nm_id: int
    daily_demand_total: float
    district_daily_demand_by_key: dict[str, float]
    average_depletion_share_by_district: dict[str, float]
    diagnostics: dict[str, Any]
    warning: str


@dataclass
class _DistrictObservationStats:
    ratios: list[float] = field(default_factory=list)
    positive_depletion_count: int = 0
    zero_depletion_count: int = 0
    zero_zero_no_signal_count: int = 0
    stockout_risk_count: int = 0
    restock_count: int = 0
    invalid_count: int = 0

    @property
    def observation_count(self) -> int:
        return len(self.ratios)

    @property
    def score(self) -> float:
        return _robust_average(self.ratios)


@dataclass
class _SkuSignal:
    nm_id: int
    requested_valid_day_count: int
    max_lookup_days: int
    included_district_keys: tuple[str, ...]
    baseline_daily_sales: float
    valid_day_threshold: float
    full_clean_selected: list[dict[str, Any]]
    full_clean_inspected_day_count: int
    initial_window_full_clean_day_count: int
    full_clean_excluded_reason_counts: dict[str, int]
    partial_global_day_reason_counts: dict[str, int]
    observation_stats_by_district: dict[str, _DistrictObservationStats]
    legacy_observation_stats_by_district: dict[str, _DistrictObservationStats]
    order_count_samples: list[tuple[str, float]]
    order_count_positive_fallback_used: bool
    current_stock_by_district: Mapping[str, float]
    metadata: Mapping[str, Any]


def estimate_wb_regional_demand(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    report_date: date,
    nm_ids: list[int],
    requested_valid_day_count: int,
    district_field_by_key: Mapping[str, str],
    current_stock_by_nm: Mapping[int, Mapping[str, float]],
    included_district_keys: list[str] | tuple[str, ...] | None = None,
    persistent_zero_current_stock_max_qty: float = 0.0,
    sku_metadata_by_nm: Mapping[int, Mapping[str, Any]] | None = None,
    district_keys: list[str] | tuple[str, ...] | None = None,
    legacy_district_field_by_key: Mapping[str, str] | None = None,
) -> dict[int, WbRegionalDemandEstimate]:
    """Estimate demand using an explicit, request-scoped region contract."""

    active_keys = tuple(
        dict.fromkeys(
            str(item or "").strip().lower()
            for item in (district_keys or tuple(district_field_by_key.keys()) or CANONICAL_DISTRICT_KEYS)
            if str(item or "").strip()
        )
    )
    if not active_keys:
        raise ValueError("district_keys must not be empty")
    missing_fields = [key for key in active_keys if key not in district_field_by_key]
    if missing_fields:
        raise ValueError("district_field_by_key is incomplete: " + ", ".join(missing_fields))
    token = _DISTRICT_KEYS_CONTEXT.set(active_keys)
    try:
        return _estimate_wb_regional_demand_inner(
            runtime=runtime,
            report_date=report_date,
            nm_ids=nm_ids,
            requested_valid_day_count=requested_valid_day_count,
            district_field_by_key=district_field_by_key,
            current_stock_by_nm=current_stock_by_nm,
            included_district_keys=included_district_keys,
            persistent_zero_current_stock_max_qty=persistent_zero_current_stock_max_qty,
            sku_metadata_by_nm=sku_metadata_by_nm,
            legacy_district_field_by_key=legacy_district_field_by_key,
        )
    finally:
        _DISTRICT_KEYS_CONTEXT.reset(token)


def _estimate_wb_regional_demand_inner(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    report_date: date,
    nm_ids: list[int],
    requested_valid_day_count: int,
    district_field_by_key: Mapping[str, str],
    current_stock_by_nm: Mapping[int, Mapping[str, float]],
    included_district_keys: list[str] | tuple[str, ...] | None = None,
    persistent_zero_current_stock_max_qty: float = 0.0,
    sku_metadata_by_nm: Mapping[int, Mapping[str, Any]] | None = None,
    legacy_district_field_by_key: Mapping[str, str] | None = None,
) -> dict[int, WbRegionalDemandEstimate]:
    """Estimate district demand for each SKU using the regional share ladder."""

    requested_count = max(int(requested_valid_day_count), 1)
    included_keys = _normalize_included_district_keys(included_district_keys)
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
    legacy_fields = dict(legacy_district_field_by_key or {})
    legacy_stock_by_date = (
        {
            snapshot_date: _stocks_by_nm_id(
                payload,
                district_field_by_key=legacy_fields,
                keys=tuple(legacy_fields),
            )
            for snapshot_date, payload in stock_payloads.items()
        }
        if legacy_fields
        else {}
    )
    order_counts_by_date = {
        snapshot_date: _order_count_by_nm_id(payload)
        for snapshot_date, payload in sales_payloads.items()
    }

    metadata_by_nm = {int(key): dict(value) for key, value in dict(sku_metadata_by_nm or {}).items()}
    signals: dict[int, _SkuSignal] = {}
    for nm_id in nm_ids:
        normalized_nm_id = int(nm_id)
        signals[normalized_nm_id] = _collect_sku_signal(
            nm_id=normalized_nm_id,
            requested_valid_day_count=requested_count,
            max_lookup_days=max_lookup_days,
            candidate_dates=candidate_dates,
            stock_by_date=stock_by_date,
            order_counts_by_date=order_counts_by_date,
            current_stock_by_district=current_stock_by_nm.get(normalized_nm_id, {}),
            included_district_keys=included_keys,
            metadata=metadata_by_nm.get(normalized_nm_id, {}),
            legacy_stock_by_date=legacy_stock_by_date,
            legacy_district_keys=tuple(legacy_fields),
        )

    prior_candidates = _build_prior_candidates(signals)
    out: dict[int, WbRegionalDemandEstimate] = {}
    for nm_id in nm_ids:
        normalized_nm_id = int(nm_id)
        signal = signals[normalized_nm_id]
        if len(signal.full_clean_selected) >= requested_count:
            out[normalized_nm_id] = _full_clean_estimate(
                signal=signal,
                report_date=report_date,
                persistent_zero_current_stock_max_qty=persistent_zero_current_stock_max_qty,
            )
            continue
        group_prior = _select_group_prior(
            nm_id=normalized_nm_id,
            signals=signals,
            prior_candidates=prior_candidates,
            included_district_keys=included_keys,
        )
        global_prior = _build_prior_distribution(
            [
                item
                for peer_nm_id, item in prior_candidates.items()
                if int(peer_nm_id) != int(normalized_nm_id)
            ],
            included_district_keys=included_keys,
            min_peer_count=GLOBAL_PRIOR_MIN_PEERS,
        )
        estimate = _ladder_estimate(
            signal=signal,
            report_date=report_date,
            group_prior=group_prior,
            global_prior=global_prior,
            persistent_zero_current_stock_max_qty=persistent_zero_current_stock_max_qty,
        )
        out[normalized_nm_id] = _apply_central_transition(estimate, signal=signal)
    return out


def build_result_diagnostics(estimates: Mapping[int, WbRegionalDemandEstimate]) -> dict[str, Any]:
    first_estimate = next(iter(estimates.values()), None)
    diagnostics = first_estimate.diagnostics if first_estimate is not None else {}
    keys = tuple(
        str(item)
        for item in diagnostics.get("all_district_keys", CANONICAL_DISTRICT_KEYS)
        if str(item)
    ) or tuple(CANONICAL_DISTRICT_KEYS)
    token = _DISTRICT_KEYS_CONTEXT.set(keys)
    try:
        return _build_result_diagnostics_inner(estimates)
    finally:
        _DISTRICT_KEYS_CONTEXT.reset(token)


def _build_result_diagnostics_inner(estimates: Mapping[int, WbRegionalDemandEstimate]) -> dict[str, Any]:
    items = list(estimates.values())
    method_counts: dict[str, int] = {}
    share_source_counts: dict[str, int] = {}
    fallback_sku_ids: list[int] = []
    seed_floor_nm_ids: set[int] = set()
    low_confidence_sku_district_count = 0
    partial_observation_sku_district_count = 0
    group_prior_sku_district_count = 0
    global_prior_sku_district_count = 0
    seed_floor_sku_district_count = 0
    primary_sku_ids: set[int] = set()
    selected_full_clean_counts: list[int] = []
    order_valid_counts: list[int] = []
    inspected_counts: list[int] = []
    excluded_reason_counts: dict[str, int] = {}
    partial_global_reason_counts: dict[str, int] = {}
    zero_zero_no_signal_by_district: dict[str, int] = {}
    stockout_risk_by_district: dict[str, int] = {}
    restock_by_district: dict[str, int] = {}

    for estimate in items:
        diagnostics = estimate.diagnostics
        method = str(diagnostics.get("share_estimation_method") or diagnostics.get("regional_demand_method") or "")
        method_counts[method] = method_counts.get(method, 0) + 1
        if bool(diagnostics.get("fallback_used")):
            fallback_sku_ids.append(int(estimate.nm_id))
        if sum(float(estimate.average_depletion_share_by_district.get(key, 0.0)) for key in _active_district_keys()) > 0:
            primary_sku_ids.add(int(estimate.nm_id))
        selected_full_clean_counts.append(int(diagnostics.get("selected_full_clean_day_count") or 0))
        order_valid_counts.append(int(diagnostics.get("order_count_valid_day_count") or 0))
        inspected_counts.append(int(diagnostics.get("inspected_day_count") or 0))
        for reason, count in dict(diagnostics.get("excluded_day_reason_counts") or {}).items():
            excluded_reason_counts[str(reason)] = excluded_reason_counts.get(str(reason), 0) + int(count)
        for reason, count in dict(diagnostics.get("partial_global_day_reason_counts") or {}).items():
            partial_global_reason_counts[str(reason)] = partial_global_reason_counts.get(str(reason), 0) + int(count)
        for key, count in dict(diagnostics.get("district_zero_zero_no_signal_counts") or {}).items():
            if str(key) in _active_district_keys():
                zero_zero_no_signal_by_district[str(key)] = zero_zero_no_signal_by_district.get(str(key), 0) + int(count)
        for key, count in dict(diagnostics.get("district_stockout_risk_counts") or {}).items():
            if str(key) in _active_district_keys():
                stockout_risk_by_district[str(key)] = stockout_risk_by_district.get(str(key), 0) + int(count)
        for key, count in dict(diagnostics.get("district_restock_counts") or {}).items():
            if str(key) in _active_district_keys():
                restock_by_district[str(key)] = restock_by_district.get(str(key), 0) + int(count)

        sources = dict(diagnostics.get("district_share_sources") or {})
        confidences = dict(diagnostics.get("confidence_by_district") or {})
        included = set(diagnostics.get("included_district_keys") or _active_district_keys())
        seed_reasons = dict(diagnostics.get("seed_reason_by_district") or {})
        for district_key in _active_district_keys():
            source = str(sources.get(district_key) or "")
            if district_key not in included:
                continue
            if source:
                share_source_counts[source] = share_source_counts.get(source, 0) + 1
            if source in {REGIONAL_SHARE_SOURCE_PARTIAL_OBSERVATIONS, REGIONAL_SHARE_SOURCE_PARTIAL_BLENDED}:
                partial_observation_sku_district_count += 1
            elif source == REGIONAL_SHARE_SOURCE_GROUP_PRIOR:
                group_prior_sku_district_count += 1
            elif source == REGIONAL_SHARE_SOURCE_GLOBAL_PRIOR:
                global_prior_sku_district_count += 1
            elif source in {REGIONAL_SHARE_SOURCE_SEED_FLOOR, REGIONAL_SHARE_SOURCE_SEED_FLOOR_CANDIDATE}:
                seed_floor_sku_district_count += 1
                seed_floor_nm_ids.add(int(estimate.nm_id))
            if district_key in seed_reasons:
                seed_floor_nm_ids.add(int(estimate.nm_id))
            if source not in {
                REGIONAL_SHARE_SOURCE_SEED_FLOOR,
                REGIONAL_SHARE_SOURCE_SEED_FLOOR_CANDIDATE,
                REGIONAL_SHARE_SOURCE_EXCLUDED,
            }:
                try:
                    confidence = float(confidences.get(district_key, 0.0) or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                if confidence < OWN_HIGH_CONFIDENCE_THRESHOLD:
                    low_confidence_sku_district_count += 1
    if not items:
        return {
            "regional_demand_method": REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS,
            "sku_count": 0,
            "fallback_sku_count": 0,
            "fallback_nm_ids": [],
            "primary_sku_count": 0,
            "regional_share_method_counts": {},
            "method_counts": {},
            "share_source_counts": {},
            "included_district_keys": list(_active_district_keys()),
            "excluded_district_keys": [],
            "district_selection_mode": "all_districts",
            "seed_sku_count": 0,
            "seed_sku_district_count": 0,
            "seed_allocated_qty_total": 0,
            "low_confidence_sku_district_count": 0,
            "partial_observation_sku_district_count": 0,
            "group_prior_sku_district_count": 0,
            "global_prior_sku_district_count": 0,
            "seed_floor_sku_district_count": 0,
            "calculation_mode": "Стартовое распределение",
            "central_transition": {},
            "warnings": [],
        }

    if len(method_counts) == 1:
        result_method = next(iter(method_counts))
    elif method_counts.get(REGIONAL_DEMAND_METHOD_STOCK_SHARE_FALLBACK):
        result_method = "mixed_regional_share_ladder_with_current_stock_share_fallback"
    else:
        result_method = REGIONAL_DEMAND_METHOD_LADDER

    compact_warnings: list[str] = []
    if fallback_sku_ids:
        compact_warnings.append(
            "Fallback current-stock-share used for SKU count="
            f"{len(fallback_sku_ids)}"
        )

    first = items[0].diagnostics
    return {
        "regional_demand_method": result_method,
        "sku_count": len(items),
        "fallback_sku_count": len(fallback_sku_ids),
        "fallback_nm_ids": fallback_sku_ids,
        "primary_sku_count": len(primary_sku_ids),
        "regional_share_method_counts": method_counts,
        "method_counts": method_counts,
        "share_source_counts": share_source_counts,
        "seed_sku_count": 0,
        "seed_sku_district_count": 0,
        "seed_allocated_qty_total": 0,
        "seed_floor_sku_count": len(seed_floor_nm_ids),
        "seed_floor_sku_district_count": seed_floor_sku_district_count,
        "low_confidence_sku_district_count": low_confidence_sku_district_count,
        "partial_observation_sku_district_count": partial_observation_sku_district_count,
        "group_prior_sku_district_count": group_prior_sku_district_count,
        "global_prior_sku_district_count": global_prior_sku_district_count,
        "requested_valid_day_count": int(first.get("requested_valid_day_count") or 0),
        "included_district_keys": list(first.get("included_district_keys") or _active_district_keys()),
        "excluded_district_keys": list(first.get("excluded_district_keys") or []),
        "district_selection_mode": str(first.get("district_selection_mode") or "all_districts"),
        "min_selected_valid_day_count": min(selected_full_clean_counts) if selected_full_clean_counts else 0,
        "max_selected_valid_day_count": max(selected_full_clean_counts) if selected_full_clean_counts else 0,
        "min_order_count_valid_day_count": min(order_valid_counts) if order_valid_counts else 0,
        "max_order_count_valid_day_count": max(order_valid_counts) if order_valid_counts else 0,
        "max_inspected_day_count": max(inspected_counts) if inspected_counts else 0,
        "excluded_day_reason_counts": excluded_reason_counts,
        "partial_global_day_reason_counts": partial_global_reason_counts,
        "zero_zero_no_signal_day_count_by_district": {
            key: int(value) for key, value in sorted(zero_zero_no_signal_by_district.items())
        },
        "stockout_risk_count_by_district": {
            key: int(value) for key, value in sorted(stockout_risk_by_district.items())
        },
        "restock_count_by_district": {
            key: int(value) for key, value in sorted(restock_by_district.items())
        },
        "persistent_zero_neutralization_enabled": False,
        "persistent_zero_sku_count": len(seed_floor_nm_ids),
        "persistent_zero_sku_district_count": seed_floor_sku_district_count,
        "persistent_zero_day_count": sum(zero_zero_no_signal_by_district.values()),
        "persistent_zero_nm_ids": sorted(seed_floor_nm_ids),
        "persistent_zero_day_count_by_district": {
            key: int(value) for key, value in sorted(zero_zero_no_signal_by_district.items())
        },
        "persistent_zero_district_counts": {},
        "calculation_mode": str(first.get("calculation_mode") or ""),
        "central_transition": dict(first.get("central_transition") or {}),
        "warnings": compact_warnings,
    }


def _collect_sku_signal(
    *,
    nm_id: int,
    requested_valid_day_count: int,
    max_lookup_days: int,
    candidate_dates: list[date],
    stock_by_date: Mapping[str, Mapping[int, Mapping[str, float]]],
    order_counts_by_date: Mapping[str, Mapping[int, float]],
    current_stock_by_district: Mapping[str, float],
    included_district_keys: tuple[str, ...],
    metadata: Mapping[str, Any],
    legacy_stock_by_date: Mapping[str, Mapping[int, Mapping[str, float]]],
    legacy_district_keys: tuple[str, ...],
) -> _SkuSignal:
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
    order_samples, positive_fallback_used = _collect_order_count_samples(
        nm_id=nm_id,
        candidate_dates=candidate_dates,
        order_counts_by_date=order_counts_by_date,
        valid_day_threshold=valid_day_threshold,
        requested_valid_day_count=requested_valid_day_count,
    )

    full_clean_selected: list[dict[str, Any]] = []
    full_clean_excluded_reason_counts: dict[str, int] = {}
    partial_global_day_reason_counts: dict[str, int] = {}
    observation_stats_by_district = {
        key: _DistrictObservationStats()
        for key in _active_district_keys()
    }
    legacy_observation_stats_by_district = {
        key: _DistrictObservationStats()
        for key in legacy_district_keys
    }
    full_clean_inspected_day_count = 0
    initial_window_full_clean_day_count = 0

    for index, candidate in enumerate(candidate_dates, start=1):
        if len(full_clean_selected) < requested_valid_day_count:
            full_clean_inspected_day_count += 1
            validation = _validate_full_clean_day(
                nm_id=nm_id,
                depletion_date=candidate,
                stock_by_date=stock_by_date,
                order_counts_by_date=order_counts_by_date,
                valid_day_threshold=valid_day_threshold,
                included_district_keys=included_district_keys,
            )
            if validation["valid"]:
                if index <= requested_valid_day_count:
                    initial_window_full_clean_day_count += 1
                full_clean_selected.append(validation)
            else:
                reason = str(validation["reason"])
                full_clean_excluded_reason_counts[reason] = full_clean_excluded_reason_counts.get(reason, 0) + 1

        global_validation = _validate_partial_global_day(
            nm_id=nm_id,
            depletion_date=candidate,
            stock_by_date=stock_by_date,
            order_counts_by_date=order_counts_by_date,
            valid_day_threshold=valid_day_threshold,
        )
        if not global_validation["valid"]:
            reason = str(global_validation["reason"])
            partial_global_day_reason_counts[reason] = partial_global_day_reason_counts.get(reason, 0) + 1
            continue
        previous_row = global_validation["previous_row"]
        current_row = global_validation["current_row"]
        order_count = float(global_validation["order_count"])
        for key in included_district_keys:
            _collect_district_observation(
                key=key,
                previous_row=previous_row,
                current_row=current_row,
                order_count=order_count,
                stats=observation_stats_by_district[key],
            )
        legacy_previous_row = legacy_stock_by_date.get(previous_date := (candidate - timedelta(days=1)).isoformat(), {}).get(nm_id)
        legacy_current_row = legacy_stock_by_date.get(candidate.isoformat(), {}).get(nm_id)
        if legacy_previous_row is not None and legacy_current_row is not None:
            for key in legacy_district_keys:
                _collect_district_observation(
                    key=key,
                    previous_row=legacy_previous_row,
                    current_row=legacy_current_row,
                    order_count=order_count,
                    stats=legacy_observation_stats_by_district[key],
                )

    return _SkuSignal(
        nm_id=nm_id,
        requested_valid_day_count=requested_valid_day_count,
        max_lookup_days=max_lookup_days,
        included_district_keys=included_district_keys,
        baseline_daily_sales=baseline_daily_sales,
        valid_day_threshold=valid_day_threshold,
        full_clean_selected=full_clean_selected,
        full_clean_inspected_day_count=full_clean_inspected_day_count,
        initial_window_full_clean_day_count=initial_window_full_clean_day_count,
        full_clean_excluded_reason_counts=full_clean_excluded_reason_counts,
        partial_global_day_reason_counts=partial_global_day_reason_counts,
        observation_stats_by_district=observation_stats_by_district,
        legacy_observation_stats_by_district=legacy_observation_stats_by_district,
        order_count_samples=order_samples,
        order_count_positive_fallback_used=positive_fallback_used,
        current_stock_by_district=current_stock_by_district,
        metadata=metadata,
    )


def _full_clean_estimate(
    *,
    signal: _SkuSignal,
    report_date: date,
    persistent_zero_current_stock_max_qty: float,
) -> WbRegionalDemandEstimate:
    shares = {key: 0.0 for key in _active_district_keys()}
    for item in signal.full_clean_selected:
        total_depletion = float(item["total_depletion"])
        for key in signal.included_district_keys:
            shares[key] += float(item["depletions"].get(key, 0.0)) / total_depletion
    shares = _normalize_shares(
        {key: value / len(signal.full_clean_selected) for key, value in shares.items()},
        included_district_keys=signal.included_district_keys,
    )
    daily_demand_total = (
        sum(float(item["order_count"]) for item in signal.full_clean_selected) / len(signal.full_clean_selected)
    )
    district_daily_demand = {
        key: float(daily_demand_total) * float(shares.get(key, 0.0))
        for key in _active_district_keys()
    }
    used_dates = sorted(str(item["date"]) for item in signal.full_clean_selected)
    lookup_depth_days = (report_date - date.fromisoformat(used_dates[0])).days if used_dates else 0
    excluded_from_initial = max(signal.requested_valid_day_count - signal.initial_window_full_clean_day_count, 0)
    if excluded_from_initial:
        warning = (
            f"Пропорции округов рассчитаны по {len(signal.full_clean_selected)} full clean days; "
            f"исходное окно {signal.requested_valid_day_count} дней содержало {excluded_from_initial} исключённых дней, "
            f"lookup ушёл на {lookup_depth_days} календарных дней назад."
        )
    else:
        warning = (
            f"Пропорции округов рассчитаны по full clean days внутри исходного окна "
            f"{signal.requested_valid_day_count} дней."
        )
    diagnostics = _base_diagnostics(
        signal=signal,
        report_date=report_date,
        method=REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS,
        shares=shares,
        confidence_by_district={
            key: (1.0 if key in signal.included_district_keys else 0.0)
            for key in _active_district_keys()
        },
        district_share_sources={
            key: (
                REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS
                if key in signal.included_district_keys
                else REGIONAL_SHARE_SOURCE_EXCLUDED
            )
            for key in _active_district_keys()
        },
        used_dates=used_dates,
        lookup_depth_days=lookup_depth_days,
        daily_demand_total=daily_demand_total,
        total_demand_source=REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS,
        warning=warning,
        fallback_used=False,
        fallback_reason="",
        group_prior={},
        global_prior={},
        seed_reason_by_district={},
        persistent_zero_current_stock_max_qty=persistent_zero_current_stock_max_qty,
    )
    return WbRegionalDemandEstimate(
        nm_id=signal.nm_id,
        daily_demand_total=float(daily_demand_total),
        district_daily_demand_by_key=district_daily_demand,
        average_depletion_share_by_district=shares,
        diagnostics=diagnostics,
        warning=warning,
    )


def _ladder_estimate(
    *,
    signal: _SkuSignal,
    report_date: date,
    group_prior: Mapping[str, Any],
    global_prior: Mapping[str, Any],
    persistent_zero_current_stock_max_qty: float,
) -> WbRegionalDemandEstimate:
    own_scores = {
        key: signal.observation_stats_by_district[key].score
        for key in _active_district_keys()
    }
    own_confidence = {
        key: min(
            1.0,
            float(signal.observation_stats_by_district[key].observation_count)
            / float(max(signal.requested_valid_day_count, 1)),
        )
        for key in _active_district_keys()
    }
    group_scores = dict(group_prior.get("shares") or {})
    global_scores = dict(global_prior.get("shares") or {})
    group_available = bool(group_scores)
    global_available = bool(global_scores)
    final_scores = {key: 0.0 for key in _active_district_keys()}
    district_sources: dict[str, str] = {}
    confidence_by_district: dict[str, float] = {}
    seed_reason_by_district: dict[str, str] = {}

    for key in _active_district_keys():
        if key not in signal.included_district_keys:
            district_sources[key] = REGIONAL_SHARE_SOURCE_EXCLUDED
            confidence_by_district[key] = 0.0
            continue
        stats = signal.observation_stats_by_district[key]
        own_has_observation = stats.observation_count > 0
        own_score = float(own_scores.get(key, 0.0))
        own_conf = float(own_confidence.get(key, 0.0))
        if group_available:
            prior_score = float(group_scores.get(key, 0.0))
            prior_confidence = float(group_prior.get("confidence") or 0.0)
            prior_source = REGIONAL_SHARE_SOURCE_GROUP_PRIOR
        elif global_available:
            prior_score = float(global_scores.get(key, 0.0))
            prior_confidence = float(global_prior.get("confidence") or 0.0)
            prior_source = REGIONAL_SHARE_SOURCE_GLOBAL_PRIOR
        else:
            prior_score = 0.0
            prior_confidence = 0.0
            prior_source = ""

        if own_conf >= OWN_HIGH_CONFIDENCE_THRESHOLD:
            final_scores[key] = own_score
            district_sources[key] = REGIONAL_SHARE_SOURCE_PARTIAL_OBSERVATIONS
            confidence_by_district[key] = own_conf
        elif own_has_observation and prior_source:
            final_scores[key] = (own_conf * own_score) + ((1.0 - own_conf) * prior_score)
            district_sources[key] = REGIONAL_SHARE_SOURCE_PARTIAL_BLENDED
            confidence_by_district[key] = max(own_conf, min(prior_confidence, OWN_HIGH_CONFIDENCE_THRESHOLD))
        elif own_has_observation:
            final_scores[key] = own_score
            district_sources[key] = REGIONAL_SHARE_SOURCE_PARTIAL_OBSERVATIONS
            confidence_by_district[key] = own_conf
        elif group_available:
            final_scores[key] = float(group_scores.get(key, 0.0))
            district_sources[key] = REGIONAL_SHARE_SOURCE_GROUP_PRIOR
            confidence_by_district[key] = float(group_prior.get("confidence") or 0.0)
        elif global_available:
            final_scores[key] = float(global_scores.get(key, 0.0))
            district_sources[key] = REGIONAL_SHARE_SOURCE_GLOBAL_PRIOR
            confidence_by_district[key] = float(global_prior.get("confidence") or 0.0)
        else:
            final_scores[key] = 0.0
            district_sources[key] = REGIONAL_SHARE_SOURCE_SEED_FLOOR_CANDIDATE
            confidence_by_district[key] = 0.0

    shares = _normalize_shares(final_scores, included_district_keys=signal.included_district_keys)
    daily_demand_total = _average_order_count_samples(signal.order_count_samples)
    for key in signal.included_district_keys:
        if (
            district_sources.get(key) == REGIONAL_SHARE_SOURCE_SEED_FLOOR_CANDIDATE
            and float(daily_demand_total) > 0
            and _is_missing_or_below_stock_floor(
                signal.current_stock_by_district.get(key),
                floor_qty=persistent_zero_current_stock_max_qty + 1.0,
            )
        ):
            district_sources[key] = REGIONAL_SHARE_SOURCE_SEED_FLOOR
            seed_reason_by_district[key] = "no_recoverable_own_group_or_global_share_with_zero_or_no_usable_stock"

    district_daily_demand = {
        key: float(daily_demand_total) * float(shares.get(key, 0.0))
        for key in _active_district_keys()
    }
    used_dates = sorted(snapshot_date for snapshot_date, _ in signal.order_count_samples)
    lookup_depth_days = (report_date - date.fromisoformat(used_dates[0])).days if used_dates else signal.full_clean_inspected_day_count
    method = _sku_method_from_sources(district_sources, included_district_keys=signal.included_district_keys)
    warning = ""
    if method != REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS:
        warning = (
            "Full clean days недостаточно; региональные доли восстановлены по ladder "
            f"({method}) без fallback на текущую структуру остатков."
        )
    diagnostics = _base_diagnostics(
        signal=signal,
        report_date=report_date,
        method=method,
        shares=shares,
        confidence_by_district=confidence_by_district,
        district_share_sources=district_sources,
        used_dates=used_dates,
        lookup_depth_days=lookup_depth_days,
        daily_demand_total=daily_demand_total,
        total_demand_source=TOTAL_DAILY_DEMAND_SOURCE_ORDER_COUNT_VALID_DAYS,
        warning=warning,
        fallback_used=False,
        fallback_reason="",
        group_prior=group_prior,
        global_prior=global_prior,
        seed_reason_by_district=seed_reason_by_district,
        persistent_zero_current_stock_max_qty=persistent_zero_current_stock_max_qty,
    )
    return WbRegionalDemandEstimate(
        nm_id=signal.nm_id,
        daily_demand_total=float(daily_demand_total),
        district_daily_demand_by_key=district_daily_demand,
        average_depletion_share_by_district=shares,
        diagnostics=diagnostics,
        warning=warning,
    )


def _apply_central_transition(
    estimate: WbRegionalDemandEstimate,
    *,
    signal: _SkuSignal,
) -> WbRegionalDemandEstimate:
    """Keep legacy Central demand inside Central while directional history warms up."""

    central_keys = tuple(key for key in CENTRAL_PLANNING_ZONE_KEYS if key in _active_district_keys())
    selected_central = tuple(key for key in central_keys if key in signal.included_district_keys)
    legacy_stats = signal.legacy_observation_stats_by_district
    legacy_scores = {
        key: max(float(legacy_stats.get(key, _DistrictObservationStats()).score), 0.0)
        for key in legacy_stats
    }
    legacy_total = sum(legacy_scores.values())
    if not selected_central or legacy_total <= 0:
        return estimate

    legacy_shares = {
        key: value / legacy_total
        for key, value in legacy_scores.items()
    }
    directed_counts = {
        key: int(signal.observation_stats_by_district[key].observation_count)
        for key in central_keys
    }
    directed_scores = {
        key: max(float(signal.observation_stats_by_district[key].score), 0.0)
        for key in central_keys
    }
    transition_confidence = min(
        1.0,
        max(directed_counts.values(), default=0) / float(max(signal.requested_valid_day_count, 1)),
    )
    raw: dict[str, float] = {}
    for key in signal.included_district_keys:
        if key in selected_central:
            startup_share = float(legacy_shares.get("central", 0.0)) / float(len(selected_central))
            raw[key] = (
                (1.0 - transition_confidence) * startup_share
                + transition_confidence * directed_scores.get(key, 0.0)
            )
        elif key in legacy_shares:
            raw[key] = float(legacy_shares[key])
        else:
            raw[key] = 0.0
    total = sum(max(value, 0.0) for value in raw.values())
    if total <= 0:
        return estimate
    shares = {key: 0.0 for key in _active_district_keys()}
    for key in signal.included_district_keys:
        shares[key] = max(raw.get(key, 0.0), 0.0) / total
    source = (
        "central_start_distribution"
        if transition_confidence <= 0
        else "central_mixed_distribution"
        if transition_confidence < 1
        else "central_direction_history"
    )
    diagnostics = dict(estimate.diagnostics)
    share_sources = dict(diagnostics.get("district_share_sources") or {})
    confidences = dict(diagnostics.get("confidence_by_district") or {})
    for key in selected_central:
        share_sources[key] = source
        confidences[key] = transition_confidence
    diagnostics.update(
        {
            "calculation_mode": (
                "Стартовое распределение"
                if transition_confidence <= 0
                else "Смешанное распределение"
                if transition_confidence < 1
                else "Распределение по истории"
            ),
            "central_transition": {
                "legacy_central_share": round(float(legacy_shares.get("central", 0.0)), 6),
                "direction_observation_counts": directed_counts,
                "required_observation_count": int(signal.requested_valid_day_count),
                "confidence": round(transition_confidence, 6),
                "startup_model_weight": round(1.0 - transition_confidence, 6),
                "directed_history_weight": round(transition_confidence, 6),
                "source": source,
            },
            "central_legacy_share_by_district": {
                key: round(float(value), 6) for key, value in legacy_shares.items()
            },
        }
    )
    diagnostics["district_share_sources"] = share_sources
    diagnostics["source_used"] = dict(share_sources)
    diagnostics["confidence_by_district"] = confidences
    diagnostics["average_depletion_share_by_district"] = dict(shares)
    diagnostics["final_share_by_district"] = dict(shares)
    return WbRegionalDemandEstimate(
        nm_id=estimate.nm_id,
        daily_demand_total=estimate.daily_demand_total,
        district_daily_demand_by_key={
            key: float(estimate.daily_demand_total) * float(shares.get(key, 0.0))
            for key in _active_district_keys()
        },
        average_depletion_share_by_district=shares,
        diagnostics=diagnostics,
        warning=estimate.warning,
    )


def _base_diagnostics(
    *,
    signal: _SkuSignal,
    report_date: date,
    method: str,
    shares: Mapping[str, float],
    confidence_by_district: Mapping[str, float],
    district_share_sources: Mapping[str, str],
    used_dates: list[str],
    lookup_depth_days: int,
    daily_demand_total: float,
    total_demand_source: str,
    warning: str,
    fallback_used: bool,
    fallback_reason: str,
    group_prior: Mapping[str, Any],
    global_prior: Mapping[str, Any],
    seed_reason_by_district: Mapping[str, str],
    persistent_zero_current_stock_max_qty: float,
) -> dict[str, Any]:
    excluded_district_keys = [key for key in _active_district_keys() if key not in set(signal.included_district_keys)]
    observation_counts = {
        key: int(signal.observation_stats_by_district[key].observation_count)
        for key in _active_district_keys()
    }
    positive_depletion_counts = {
        key: int(signal.observation_stats_by_district[key].positive_depletion_count)
        for key in _active_district_keys()
    }
    zero_depletion_counts = {
        key: int(signal.observation_stats_by_district[key].zero_depletion_count)
        for key in _active_district_keys()
    }
    zero_zero_counts = {
        key: int(signal.observation_stats_by_district[key].zero_zero_no_signal_count)
        for key in _active_district_keys()
    }
    stockout_counts = {
        key: int(signal.observation_stats_by_district[key].stockout_risk_count)
        for key in _active_district_keys()
    }
    restock_counts = {
        key: int(signal.observation_stats_by_district[key].restock_count)
        for key in _active_district_keys()
    }
    invalid_counts = {
        key: int(signal.observation_stats_by_district[key].invalid_count)
        for key in _active_district_keys()
    }
    legacy_observation_counts = {
        key: int(value.observation_count)
        for key, value in signal.legacy_observation_stats_by_district.items()
    }
    legacy_positive_counts = {
        key: int(value.positive_depletion_count)
        for key, value in signal.legacy_observation_stats_by_district.items()
    }
    group_keys = _sku_group_keys(signal.metadata)
    return {
        "regional_demand_method": method,
        "all_district_keys": list(_active_district_keys()),
        "share_estimation_method": method,
        "requested_valid_day_count": int(signal.requested_valid_day_count),
        "selected_valid_day_count": int(signal.full_clean_selected.__len__()),
        "selected_full_clean_day_count": int(signal.full_clean_selected.__len__()),
        "inspected_day_count": int(signal.full_clean_inspected_day_count),
        "initial_window_valid_day_count": int(signal.initial_window_full_clean_day_count),
        "initial_window_full_clean_day_count": int(signal.initial_window_full_clean_day_count),
        "excluded_day_count": int(signal.full_clean_inspected_day_count) - int(signal.full_clean_selected.__len__()),
        "excluded_day_reason_counts": {
            str(key): int(value)
            for key, value in sorted(dict(signal.full_clean_excluded_reason_counts).items())
        },
        "partial_global_day_reason_counts": {
            str(key): int(value)
            for key, value in sorted(dict(signal.partial_global_day_reason_counts).items())
        },
        "earliest_used_stock_depletion_date": used_dates[0] if used_dates and method == REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS else "",
        "latest_used_stock_depletion_date": used_dates[-1] if used_dates and method == REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS else "",
        "lookup_depth_days": int(lookup_depth_days),
        "max_lookup_days": int(signal.max_lookup_days),
        "used_stock_depletion_dates": list(used_dates) if method == REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS else [],
        "used_order_count_dates": list(used_dates),
        "order_count_valid_day_count": int(len(signal.order_count_samples)),
        "order_count_positive_fallback_used": bool(signal.order_count_positive_fallback_used),
        "average_depletion_share_by_district": {
            key: float(shares.get(key, 0.0))
            for key in _active_district_keys()
        },
        "final_share_by_district": {
            key: float(shares.get(key, 0.0))
            for key in _active_district_keys()
        },
        "district_share_sources": {
            key: str(district_share_sources.get(key) or "")
            for key in _active_district_keys()
        },
        "source_used": {
            key: str(district_share_sources.get(key) or "")
            for key in _active_district_keys()
        },
        "confidence_by_district": {
            key: float(confidence_by_district.get(key, 0.0))
            for key in _active_district_keys()
        },
        "district_observation_counts": observation_counts,
        "district_positive_depletion_counts": positive_depletion_counts,
        "district_zero_depletion_observation_counts": zero_depletion_counts,
        "district_zero_zero_no_signal_counts": zero_zero_counts,
        "district_stockout_risk_counts": stockout_counts,
        "district_restock_counts": restock_counts,
        "district_invalid_observation_counts": invalid_counts,
        "legacy_district_observation_counts": legacy_observation_counts,
        "legacy_district_positive_depletion_counts": legacy_positive_counts,
        "calculation_mode": "Распределение по истории" if method == REGIONAL_SHARE_SOURCE_FULL_CLEAN_DAYS else "Смешанное распределение",
        "positive_stock_observation_count": int(sum(observation_counts.values())),
        "positive_depletion_observation_count": int(sum(positive_depletion_counts.values())),
        "zero_zero_no_signal_day_count": int(sum(zero_zero_counts.values())),
        "stockout_risk_count": int(sum(stockout_counts.values())),
        "restock_count": int(sum(restock_counts.values())),
        "included_district_keys": list(signal.included_district_keys),
        "excluded_district_keys": excluded_district_keys,
        "district_selection_mode": "all_districts" if len(signal.included_district_keys) == len(_active_district_keys()) else "selected_districts",
        "total_daily_demand_source": TOTAL_DAILY_DEMAND_SOURCE_ORDER_COUNT,
        "total_demand_source": total_demand_source,
        "daily_demand_total": float(daily_demand_total),
        "group_prior_key": str(group_prior.get("group_prior_key") or ""),
        "group_prior_level": str(group_prior.get("group_prior_level") or ""),
        "group_prior_peer_count": int(group_prior.get("peer_count") or 0),
        "group_prior_peer_nm_ids_sample": list(group_prior.get("peer_nm_ids_sample") or []),
        "group_prior_confidence": float(group_prior.get("confidence") or 0.0),
        "sku_group_key_exact": group_keys.get("exact_key", ""),
        "sku_group_key_product_type": group_keys.get("product_type", ""),
        "global_prior_peer_count": int(global_prior.get("peer_count") or 0),
        "global_prior_confidence": float(global_prior.get("confidence") or 0.0),
        "seed_reason_by_district": {
            key: str(value)
            for key, value in sorted(dict(seed_reason_by_district).items())
            if key in _active_district_keys()
        },
        "seed_floor_note": (
            "Это тестовая поставка для сбора будущего сигнала, а не расчётная доля спроса."
            if seed_reason_by_district
            else ""
        ),
        "persistent_zero_neutralization_enabled": False,
        "persistent_zero_current_stock_max_qty": float(persistent_zero_current_stock_max_qty),
        "persistent_zero_district_keys": [
            key for key, count in zero_zero_counts.items() if int(count) > 0
        ],
        "persistent_zero_sku_district_count": len([key for key, count in zero_zero_counts.items() if int(count) > 0]),
        "persistent_zero_day_count": int(sum(zero_zero_counts.values())),
        "neutralized_day_count_by_district": {
            key: int(value)
            for key, value in sorted(zero_zero_counts.items())
            if int(value) > 0
        },
        "persistent_zero_neutralized_reason": PERSISTENT_ZERO_NEUTRALIZED_REASON,
        "fallback_used": bool(fallback_used),
        "fallback_reason": str(fallback_reason),
        "warning": str(warning),
        "baseline_daily_sales": float(signal.baseline_daily_sales),
        "valid_day_threshold": float(signal.valid_day_threshold),
        "ladder_thresholds": {
            "own_high_confidence": OWN_HIGH_CONFIDENCE_THRESHOLD,
            "own_medium_confidence": OWN_MEDIUM_CONFIDENCE_THRESHOLD,
            "group_prior_min_peers": GROUP_PRIOR_MIN_PEERS,
            "global_prior_min_peers": GLOBAL_PRIOR_MIN_PEERS,
        },
    }


def _validate_full_clean_day(
    *,
    nm_id: int,
    depletion_date: date,
    stock_by_date: Mapping[str, Mapping[int, Mapping[str, float]]],
    order_counts_by_date: Mapping[str, Mapping[int, float]],
    valid_day_threshold: float,
    included_district_keys: tuple[str, ...],
) -> dict[str, Any]:
    global_validation = _validate_partial_global_day(
        nm_id=nm_id,
        depletion_date=depletion_date,
        stock_by_date=stock_by_date,
        order_counts_by_date=order_counts_by_date,
        valid_day_threshold=valid_day_threshold,
    )
    current_date = depletion_date.isoformat()
    if not global_validation["valid"]:
        return _invalid_day(current_date, str(global_validation["reason"]))
    previous_row = global_validation["previous_row"]
    current_row = global_validation["current_row"]
    depletions: dict[str, float] = {}
    for key in included_district_keys:
        observation = _classify_district_observation(
            key=key,
            previous_row=previous_row,
            current_row=current_row,
            order_count=float(global_validation["order_count"]),
        )
        if not observation["valid"]:
            return _invalid_day(current_date, str(observation["reason"]))
        depletions[key] = float(observation["depletion"])
    total_depletion = sum(depletions.values())
    if total_depletion <= 0:
        return _invalid_day(current_date, "zero_total_depletion_with_positive_order_count")
    return {
        "valid": True,
        "date": current_date,
        "order_count": float(global_validation["order_count"]),
        "depletions": depletions,
        "total_depletion": float(total_depletion),
    }


def _validate_partial_global_day(
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
    if not _is_number(order_count):
        return _invalid_day(current_date, "non_numeric_order_count")
    if float(order_count) <= 0:
        return _invalid_day(current_date, "zero_order_count_signal")
    if valid_day_threshold > 0 and float(order_count) < valid_day_threshold:
        return _invalid_day(current_date, "low_order_count_signal")
    return {
        "valid": True,
        "date": current_date,
        "previous_row": previous_row,
        "current_row": current_row,
        "order_count": float(order_count),
    }


def _collect_district_observation(
    *,
    key: str,
    previous_row: Mapping[str, Any],
    current_row: Mapping[str, Any],
    order_count: float,
    stats: _DistrictObservationStats,
) -> None:
    observation = _classify_district_observation(
        key=key,
        previous_row=previous_row,
        current_row=current_row,
        order_count=order_count,
    )
    if not observation["valid"]:
        reason = str(observation["reason"])
        if reason == "district_zero_zero_no_signal":
            stats.zero_zero_no_signal_count += 1
        elif reason == "district_out_of_stock_risk":
            stats.stockout_risk_count += 1
        elif reason == "district_restock_or_upward_correction":
            stats.restock_count += 1
        else:
            stats.invalid_count += 1
        return
    depletion = float(observation["depletion"])
    ratio = max(min(float(observation["ratio"]), 1.0), 0.0)
    stats.ratios.append(ratio)
    if depletion > 0:
        stats.positive_depletion_count += 1
    else:
        stats.zero_depletion_count += 1


def _classify_district_observation(
    *,
    key: str,
    previous_row: Mapping[str, Any],
    current_row: Mapping[str, Any],
    order_count: float,
) -> dict[str, Any]:
    if key not in previous_row or key not in current_row:
        return {"valid": False, "reason": "missing_district_stock"}
    previous_value = previous_row.get(key)
    current_value = current_row.get(key)
    if not _is_number(previous_value) or not _is_number(current_value):
        return {"valid": False, "reason": "non_numeric_district_stock"}
    previous_float = float(previous_value)
    current_float = float(current_value)
    if previous_float < 0 or current_float < 0:
        return {"valid": False, "reason": "negative_district_stock"}
    if current_float > previous_float:
        return {"valid": False, "reason": "district_restock_or_upward_correction"}
    if previous_float > 0 and current_float == 0:
        return {"valid": False, "reason": "district_out_of_stock_risk"}
    if previous_float == 0 and current_float == 0:
        return {"valid": False, "reason": "district_zero_zero_no_signal"}
    if previous_float <= 0:
        return {"valid": False, "reason": "district_restock_or_upward_correction"}
    depletion = max(previous_float - current_float, 0.0)
    return {
        "valid": True,
        "depletion": depletion,
        "ratio": max(min(depletion / float(order_count), 1.0), 0.0),
    }


def _collect_order_count_samples(
    *,
    nm_id: int,
    candidate_dates: list[date],
    order_counts_by_date: Mapping[str, Mapping[int, float]],
    valid_day_threshold: float,
    requested_valid_day_count: int,
) -> tuple[list[tuple[str, float]], bool]:
    order_samples: list[tuple[str, float]] = []
    for candidate in candidate_dates:
        value = order_counts_by_date.get(candidate.isoformat(), {}).get(nm_id)
        if value is None or not _is_number(value):
            continue
        if float(value) <= 0:
            continue
        if valid_day_threshold > 0 and float(value) < valid_day_threshold:
            continue
        order_samples.append((candidate.isoformat(), float(value)))
        if len(order_samples) >= requested_valid_day_count:
            return order_samples, False
    if order_samples:
        return order_samples, False
    positive_fallback: list[tuple[str, float]] = []
    for candidate in candidate_dates:
        value = order_counts_by_date.get(candidate.isoformat(), {}).get(nm_id)
        if value is None or not _is_number(value) or float(value) <= 0:
            continue
        positive_fallback.append((candidate.isoformat(), float(value)))
        if len(positive_fallback) >= requested_valid_day_count:
            break
    return positive_fallback, bool(positive_fallback)


def _build_prior_candidates(signals: Mapping[int, _SkuSignal]) -> dict[int, dict[str, Any]]:
    candidates: dict[int, dict[str, Any]] = {}
    for nm_id, signal in signals.items():
        if len(signal.full_clean_selected) >= signal.requested_valid_day_count:
            share = _full_clean_share(signal)
            confidence = 1.0
        else:
            own_scores = {
                key: signal.observation_stats_by_district[key].score
                for key in _active_district_keys()
            }
            share = _normalize_shares(own_scores, included_district_keys=signal.included_district_keys)
            confidence = _own_distribution_confidence(signal)
        if sum(float(share.get(key, 0.0)) for key in signal.included_district_keys) <= 0:
            continue
        if confidence < OWN_MEDIUM_CONFIDENCE_THRESHOLD:
            continue
        group_keys = _sku_group_keys(signal.metadata)
        candidates[int(nm_id)] = {
            "nm_id": int(nm_id),
            "shares": share,
            "confidence": float(confidence),
            "exact_key": group_keys.get("exact_key", ""),
            "product_type": group_keys.get("product_type", ""),
        }
    return candidates


def _full_clean_share(signal: _SkuSignal) -> dict[str, float]:
    shares = {key: 0.0 for key in _active_district_keys()}
    for item in signal.full_clean_selected:
        total_depletion = float(item["total_depletion"])
        if total_depletion <= 0:
            continue
        for key in signal.included_district_keys:
            shares[key] += float(item["depletions"].get(key, 0.0)) / total_depletion
    if not signal.full_clean_selected:
        return shares
    return _normalize_shares(
        {key: value / len(signal.full_clean_selected) for key, value in shares.items()},
        included_district_keys=signal.included_district_keys,
    )


def _own_distribution_confidence(signal: _SkuSignal) -> float:
    counts = [
        signal.observation_stats_by_district[key].observation_count
        for key in signal.included_district_keys
    ]
    if not counts:
        return 0.0
    positive_signal = sum(
        signal.observation_stats_by_district[key].positive_depletion_count
        for key in signal.included_district_keys
    )
    if positive_signal <= 0:
        return 0.0
    return min(1.0, max(counts) / float(max(signal.requested_valid_day_count, 1)))


def _select_group_prior(
    *,
    nm_id: int,
    signals: Mapping[int, _SkuSignal],
    prior_candidates: Mapping[int, Mapping[str, Any]],
    included_district_keys: tuple[str, ...],
) -> dict[str, Any]:
    signal = signals[int(nm_id)]
    group_keys = _sku_group_keys(signal.metadata)
    exact_key = group_keys.get("exact_key", "")
    product_type = group_keys.get("product_type", "")
    if exact_key:
        exact_peers = [
            item
            for peer_nm_id, item in prior_candidates.items()
            if int(peer_nm_id) != int(nm_id) and str(item.get("exact_key") or "") == exact_key
        ]
        prior = _build_prior_distribution(
            exact_peers,
            included_district_keys=included_district_keys,
            min_peer_count=GROUP_PRIOR_MIN_PEERS,
        )
        if prior:
            prior["group_prior_key"] = exact_key
            prior["group_prior_level"] = "product_type_model"
            return prior
    if product_type:
        product_peers = [
            item
            for peer_nm_id, item in prior_candidates.items()
            if int(peer_nm_id) != int(nm_id) and str(item.get("product_type") or "") == product_type
        ]
        prior = _build_prior_distribution(
            product_peers,
            included_district_keys=included_district_keys,
            min_peer_count=GROUP_PRIOR_MIN_PEERS,
        )
        if prior:
            prior["group_prior_key"] = product_type
            prior["group_prior_level"] = "product_type"
            return prior
    return {}


def _build_prior_distribution(
    candidates: Any,
    *,
    included_district_keys: tuple[str, ...],
    min_peer_count: int,
) -> dict[str, Any]:
    peer_items = list(candidates)
    if len(peer_items) < int(min_peer_count):
        return {}
    shares = {
        key: _robust_average([
            float(dict(item.get("shares") or {}).get(key, 0.0))
            for item in peer_items
        ])
        for key in _active_district_keys()
    }
    normalized = _normalize_shares(shares, included_district_keys=included_district_keys)
    if sum(float(normalized.get(key, 0.0)) for key in included_district_keys) <= 0:
        return {}
    peer_count = len(peer_items)
    return {
        "shares": normalized,
        "peer_count": peer_count,
        "peer_nm_ids_sample": [int(item.get("nm_id")) for item in peer_items[:10]],
        "confidence": min(0.85, max(0.25, peer_count / 6.0)),
    }


def _sku_group_keys(metadata: Mapping[str, Any]) -> dict[str, str]:
    group = _normalize_group_text(metadata.get("group"))
    display_name = _normalize_group_text(metadata.get("display_name") or metadata.get("sku_comment") or metadata.get("name"))
    product_type = group
    display_tokens = display_name.split()
    group_tokens = group.split()
    model_tokens = display_tokens
    if group_tokens and display_tokens[: len(group_tokens)] == group_tokens:
        model_tokens = display_tokens[len(group_tokens):]
    elif display_tokens and not product_type:
        product_type = display_tokens[0]
        model_tokens = display_tokens[1:]
    model = " ".join(model_tokens).strip()
    exact_key = f"{product_type}|{model}" if product_type and model else ""
    return {
        "product_type": product_type,
        "model": model,
        "exact_key": exact_key,
    }


def _sku_method_from_sources(
    sources: Mapping[str, str],
    *,
    included_district_keys: tuple[str, ...],
) -> str:
    included_sources = {
        str(sources.get(key) or "")
        for key in included_district_keys
    }
    if not included_sources:
        return REGIONAL_DEMAND_METHOD_LADDER
    if included_sources == {REGIONAL_SHARE_SOURCE_GROUP_PRIOR}:
        return REGIONAL_SHARE_SOURCE_GROUP_PRIOR
    if included_sources == {REGIONAL_SHARE_SOURCE_GLOBAL_PRIOR}:
        return REGIONAL_SHARE_SOURCE_GLOBAL_PRIOR
    if included_sources <= {REGIONAL_SHARE_SOURCE_SEED_FLOOR, REGIONAL_SHARE_SOURCE_SEED_FLOOR_CANDIDATE}:
        return REGIONAL_SHARE_SOURCE_SEED_FLOOR
    if any(source in included_sources for source in {REGIONAL_SHARE_SOURCE_PARTIAL_OBSERVATIONS, REGIONAL_SHARE_SOURCE_PARTIAL_BLENDED}):
        return REGIONAL_SHARE_SOURCE_PARTIAL_OBSERVATIONS
    return REGIONAL_DEMAND_METHOD_LADDER


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
    keys: tuple[str, ...] | None = None,
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
        for key in (keys or _active_district_keys()):
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


def _normalize_shares(
    shares: Mapping[str, float],
    *,
    included_district_keys: tuple[str, ...],
) -> dict[str, float]:
    total = sum(max(float(shares.get(key, 0.0)), 0.0) for key in included_district_keys)
    normalized = {key: 0.0 for key in _active_district_keys()}
    if total <= 0:
        return normalized
    for key in included_district_keys:
        normalized[key] = max(float(shares.get(key, 0.0)), 0.0) / total
    return normalized


def _normalize_included_district_keys(value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if value is None:
        return tuple(_active_district_keys())
    raw_values = list(value)
    if not raw_values:
        raise ValueError("Выберите хотя бы один округ для расчёта пропорций")
    requested = {str(item or "").strip().lower() for item in raw_values}
    unknown = sorted(item for item in requested if item not in _active_district_keys())
    if unknown:
        raise ValueError("Неизвестный федеральный округ: " + ", ".join(unknown))
    included = tuple(key for key in _active_district_keys() if key in requested)
    if not included:
        raise ValueError("Выберите хотя бы один округ для расчёта пропорций")
    return included


def _average_order_count_samples(order_samples: list[tuple[str, float]]) -> float:
    if not order_samples:
        return 0.0
    return sum(float(value) for _, value in order_samples) / len(order_samples)


def _robust_average(values: list[float]) -> float:
    clean_values = [float(value) for value in values if _is_number(value)]
    if not clean_values:
        return 0.0
    return float(median(clean_values))


def _normalize_group_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9а-яё]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def _is_missing_or_below_stock_floor(value: Any, *, floor_qty: float) -> bool:
    if value in ("", None):
        return True
    try:
        return float(value) < max(float(floor_qty), 1.0)
    except (TypeError, ValueError):
        return True


def _invalid_day(snapshot_date: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "valid": False,
        "date": snapshot_date,
        "reason": reason,
        **extra,
    }


def _stock_depletion_lookup_days(requested_valid_day_count: int) -> int:
    return min(
        DEFAULT_MAX_LOOKUP_DAYS,
        max(DEFAULT_MIN_LOOKUP_DAYS, requested_valid_day_count * 8),
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
