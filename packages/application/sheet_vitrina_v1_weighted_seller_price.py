"""Explicit order-weighted TOTAL seller-price metric for Web Vitrina."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
from typing import Any, Iterable

from packages.contracts.registry_upload_bundle_v1 import MetricV2Item
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope


SELLER_PRICE_DISCOUNTED_METRIC_KEY = "price_seller_discounted"
LEGACY_AVG_SELLER_PRICE_DISCOUNTED_METRIC_KEY = "avg_price_seller_discounted"
WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY = "weighted_price_seller_discounted"
SELLER_PRICE_ORDER_WEIGHT_METRIC_KEY = "orderCount"
WEIGHTED_SELLER_PRICE_DISCOUNTED_LABEL_RU = "Цена продавца взвеш."
WEIGHTED_SELLER_PRICE_DISCOUNTED_AGGREGATION_RULE = (
    "SUM(price_seller_discounted * orderCount) / SUM(orderCount)"
)
ORDER_PRICE_EFFECTIVE_FROM = "2026-09-14"
ORDER_PRICE_AGGREGATION_RULE = "SUM(orderSum) / SUM(orderCount)"
WEIGHTED_PRICE_ROW_ID = "TOTAL|" + WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY
WEIGHTED_PRICE_CALC_REF = (
    f"dated:{ORDER_PRICE_EFFECTIVE_FROM}:orderSum/orderCount:"
    "legacy:aggregate:positive_weight_fail_closed:price_seller_discounted:orderCount"
)


def uses_order_price(column_date: str) -> bool:
    return bool(column_date) and column_date >= ORDER_PRICE_EFFECTIVE_FROM


def weighted_price_source(column_date: str) -> str:
    return "sales_funnel_history" if uses_order_price(column_date) else "prices_snapshot"


def index_daily_order_price(payload: Any, column_date: str) -> dict[int, dict[str, Any]]:
    """One accepted payload, exact operand date; never join independently latest metrics."""
    result: dict[int, dict[str, Any]] = {}
    for item in getattr(payload, "items", []) or []:
        if getattr(item, "date", None) != column_date:
            continue
        nm_id = getattr(item, "nm_id", None)
        if not isinstance(nm_id, int):
            continue
        row = result.setdefault(nm_id, {})
        key = getattr(item, "metric", None)
        if key in ("orderSum", "orderCount"):
            value = getattr(item, "value", None)
            # Conflicting duplicate observations are not a compatible operand pair.
            row[key] = value if key not in row or row[key] == value else None
    return result


def observed_order_price(
    lookup: dict[int, dict[str, Any]], nm_ids: Iterable[int],
) -> tuple[float | None, list[int], list[int]]:
    """Return the observed mean plus absent and invalid members of the same scope."""
    amount = count = 0.0
    missing, invalid = [], []
    for nm_id in nm_ids:
        if nm_id not in lookup:
            missing.append(nm_id)
            continue
        row = lookup[nm_id]
        pair = [row.get("orderSum"), row.get("orderCount")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or v < 0 for v in pair):
            invalid.append(nm_id)
            continue
        order_sum, order_count = pair
        if not float(order_count).is_integer() or (order_count == 0 and order_sum != 0):
            invalid.append(nm_id)
            continue
        amount += order_sum
        count += order_count
    value = (amount / count if not invalid and count > 0
             and math.isfinite(amount) and math.isfinite(count) else None)
    return value, missing, invalid


def weighted_price_presentation(*, slots, evaluator, current_date):
    cells = {}
    for slot in slots:
        day = slot.column_date
        if not uses_order_price(day):
            continue
        lookup = evaluator._slot_lookups(slot.slot_key).order_price_lookup
        members = [item.nm_id for item in evaluator.enabled_config]
        _, absent, invalid = observed_order_price(lookup, members)
        reason = (f"С {ORDER_PRICE_EFFECTIVE_FROM}: сумма заказов / количество заказов "
                  "за одну дату, по наблюдаемым заказам. Цена снимка не используется.")
        cell = {
            "aggregation_rule": ORDER_PRICE_AGGREGATION_RULE,
            "formula_effective_from": ORDER_PRICE_EFFECTIVE_FROM,
            "source_as_of_date": day,
            "quality_reason": reason,
            "completeness_state": "unknown_scope",
            "missing_sku_count": None,
            "calculation_scope": [f"SKU:{nm_id}" for nm_id in members],
            "calculation_missing_scope": [f"SKU:{nm_id}" for nm_id in sorted(set(absent + invalid))],
        }
        # Today's catalog is not evidence of the applicable catalog on an older date.
        if day == current_date:
            missing = sorted(set(absent + invalid))
            cell.update({
                "metric_scope_evidence": {
                    "operand_date": day,
                    "applicable_scope": [f"SKU:{nm_id}" for nm_id in members],
                    "sku_metric_keys": ["orderSum", "orderCount"],
                    "missing_scope": [f"SKU:{nm_id}" for nm_id in missing],
                },
                "completeness_state": "partial" if missing else "complete",
                "missing_sku_count": len(missing),
            })
        if absent or invalid:
            cell["quality_state"] = "partial"
            cell["quality_reason"] += (
                f" Без дневных данных: {len(absent)} SKU; "
                f"с неполной или некорректной парой: {len(invalid)} SKU."
            )
        cells[day] = cell
    return {WEIGHTED_PRICE_ROW_ID: cells} if cells else {}


def preserve_seller_price_history(
    plan: SheetVitrinaV1Envelope, *, previous_plan: SheetVitrinaV1Envelope,
    business_date: str, column_dates: Iterable[str] | None = None,
    unconfirmed_dates: Iterable[str] = (),
) -> SheetVitrinaV1Envelope:
    """Freeze published pre-cutover history, including blanks and its presentation."""
    cutoff = min(ORDER_PRICE_EFFECTIVE_FROM, business_date)
    applicable_dates = set(column_dates if column_dates is not None else plan.date_columns)
    shared_dates = set(plan.date_columns) & set(previous_plan.date_columns) & applicable_dates
    days = sorted(day for day in shared_dates if day < cutoff or day in unconfirmed_dates)
    closed_order_days = sorted(day for day in shared_dates if uses_order_price(day) and day < business_date)
    if not days and not closed_order_days:
        return plan
    data = next((s for s in plan.sheets if s.sheet_name == "DATA_VITRINA"), None)
    previous = next((s for s in previous_plan.sheets if s.sheet_name == "DATA_VITRINA"), None)
    if data is None or previous is None:
        return plan
    old_row = next((r for r in previous.rows if len(r) > 1 and r[1] == WEIGHTED_PRICE_ROW_ID), [])
    rows = []
    for raw in data.rows:
        row = list(raw)
        if len(row) > 1 and row[1] == WEIGHTED_PRICE_ROW_ID:
            for day in days:
                old_indexes = [i for i, label in enumerate(previous.header) if label == day]
                indexes = [i for i, label in enumerate(data.header) if label == day]
                for offset, index in enumerate(indexes):
                    old_index = old_indexes[min(offset, len(old_indexes) - 1)] if old_indexes else -1
                    while len(row) <= index:
                        row.append("")
                    row[index] = old_row[old_index] if 0 <= old_index < len(old_row) else ""
        rows.append(row)
    metadata = deepcopy(plan.metadata)
    cells = metadata.setdefault("server_cell_presentation", {}).setdefault(WEIGHTED_PRICE_ROW_ID, {})
    old_cells = previous_plan.metadata.get("server_cell_presentation", {}).get(WEIGHTED_PRICE_ROW_ID, {})
    for day in days:
        if day in old_cells:
            cells[day] = deepcopy(old_cells[day])
        else:
            cells.pop(day, None)
    for day in set(closed_order_days) - set(days):
        cell = cells.get(day, {})
        old_evidence = old_cells.get(day, {}).get("metric_scope_evidence", {})
        old_scope = old_evidence.get("applicable_scope")
        if (old_evidence.get("operand_date") == day and isinstance(old_scope, list)
                and set(old_scope) == set(cell.get("calculation_scope", []))):
            missing = cell.get("calculation_missing_scope", [])
            cell.update({
                "metric_scope_evidence": {**deepcopy(old_evidence), "missing_scope": list(missing)},
                "completeness_state": "partial" if missing else "complete",
                "missing_sku_count": len(missing),
            })
    metadata["weighted_seller_price_history_preserved_dates"] = sorted(
        set(metadata.get("weighted_seller_price_history_preserved_dates", [])) | set(days))
    return replace(plan, sheets=[replace(s, rows=rows) if s is data else s for s in plan.sheets],
                   metadata=metadata)


def extend_metrics_with_weighted_seller_price(
    metrics: Iterable[MetricV2Item],
) -> list[MetricV2Item]:
    """Append the new TOTAL identity without mutating either historical price key."""

    existing_metrics = list(metrics)
    if any(
        item.metric_key == WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY
        for item in existing_metrics
    ):
        return [replace(item, calc_type="metric", calc_ref=WEIGHTED_PRICE_CALC_REF)
                if item.metric_key == WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY else item
                for item in existing_metrics]
    return [
        *existing_metrics,
        MetricV2Item(
            metric_key=WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru=WEIGHTED_SELLER_PRICE_DISCOUNTED_LABEL_RU,
            calc_type="metric",
            calc_ref=WEIGHTED_PRICE_CALC_REF,
            show_in_data=True,
            format="rub",
            display_order=170,
            section="Цены",
        ),
    ]
