"""1C stock-capital metric wiring for sheet_vitrina_v1."""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping

from packages.adapters.onec_stocks_block import ONEC_STOCKS_SMOKE_ACCOUNT_ID_ENV
from packages.contracts.registry_upload_bundle_v1 import MetricV2Item

ONEC_STOCKS_SOURCE_KEY = "onec_stocks"
ONEC_STOCKS_SOURCE_GROUP_ID = "onec_product_capital"
ONEC_STOCKS_SOURCE_GROUP_LABEL_RU = "1С / товарный капитал"
ONEC_STOCKS_ACCOUNT_ID_ENV = "ONEC_STOCKS_ACCOUNT_ID"
DEFAULT_ONEC_STOCKS_ACCOUNT_ID = "000000001"

ONEC_STOCKS_SECTION_RU = "1С / товарный капитал"
ONEC_STOCKS_STAGE_KEYS: tuple[str, ...] = (
    "CHINA_TO_FF",
    "FF_STOCK",
    "FF_TO_WB",
    "WB_STOCK",
)
ONEC_STOCKS_STAGE_LABELS_RU: Mapping[str, str] = {
    "CHINA_TO_FF": "Китай -> ФФ",
    "FF_STOCK": "ФФ",
    "FF_TO_WB": "ФФ -> WB",
    "WB_STOCK": "WB",
}
ONEC_STOCKS_STAGE_FIELDS: tuple[str, ...] = (
    "qty",
    "unit_cost_rub",
    "cost_total_rub",
)
ONEC_STOCKS_FIELD_LABELS_RU: Mapping[str, str] = {
    "qty": "кол-во",
    "unit_cost_rub": "себестоимость за ед., руб",
    "cost_total_rub": "капитал, руб",
}
ONEC_STOCKS_FIELD_FORMATS: Mapping[str, str] = {
    "qty": "integer",
    "unit_cost_rub": "rub",
    "cost_total_rub": "rub",
}
ONEC_STOCKS_SKU_TOTAL_QTY_METRIC_KEY = "onec_total_qty"
ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY = "onec_total_cost_rub"
ONEC_STOCKS_TOTAL_QTY_METRIC_KEY = "total_onec_total_qty"
ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY = "total_onec_total_cost_rub"
ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY = "onec_WB_STOCK_unit_cost_rub"
ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY = "proxy_profit_2_rub"
ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY = "total_proxy_profit_2_rub"
ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY = "proxy_margin_2_pct"
ONEC_PROXY_MARGIN_2_PCT_TOTAL_METRIC_KEY = "proxy_margin_2_pct_total"
ONEC_INVENTORY_CAPITAL_RETURN_PCT_METRIC_KEY = "inventory_capital_return_pct"
ONEC_INVENTORY_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY = "inventory_capital_return_pct_total"
ONEC_STOCKS_STAGE_TOTAL_UNIT_COST_FIELD = "unit_cost_rub"
ONEC_STOCKS_TOTAL_STAGE_METRIC_KEYS: tuple[str, ...] = tuple(
    f"{'avg' if field == ONEC_STOCKS_STAGE_TOTAL_UNIT_COST_FIELD else 'total'}"
    f"_onec_{stage_key}_{field}"
    for stage_key in ONEC_STOCKS_STAGE_KEYS
    for field in ONEC_STOCKS_STAGE_FIELDS
)
ONEC_STOCKS_TOTAL_METRIC_KEYS: tuple[str, ...] = (
    ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
    ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
    *ONEC_STOCKS_TOTAL_STAGE_METRIC_KEYS,
)
ONEC_STOCKS_DERIVED_TOTAL_METRIC_KEYS: tuple[str, ...] = (
    ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY,
    ONEC_PROXY_MARGIN_2_PCT_TOTAL_METRIC_KEY,
    ONEC_INVENTORY_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,
)
ONEC_STOCKS_SKU_STAGE_METRIC_KEYS: tuple[str, ...] = tuple(
    f"onec_{stage_key}_{field}"
    for stage_key in ONEC_STOCKS_STAGE_KEYS
    for field in ONEC_STOCKS_STAGE_FIELDS
)
ONEC_STOCKS_SKU_METRIC_KEYS: tuple[str, ...] = (
    *ONEC_STOCKS_SKU_STAGE_METRIC_KEYS,
    ONEC_STOCKS_SKU_TOTAL_QTY_METRIC_KEY,
    ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY,
)
ONEC_STOCKS_DERIVED_SKU_METRIC_KEYS: tuple[str, ...] = (
    ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
    ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY,
    ONEC_INVENTORY_CAPITAL_RETURN_PCT_METRIC_KEY,
)
ONEC_STOCKS_METRIC_KEYS: tuple[str, ...] = (
    *ONEC_STOCKS_TOTAL_METRIC_KEYS,
    *ONEC_STOCKS_DERIVED_TOTAL_METRIC_KEYS,
    *ONEC_STOCKS_SKU_METRIC_KEYS,
    *ONEC_STOCKS_DERIVED_SKU_METRIC_KEYS,
)

DEFAULT_ONEC_STAGE_MAPPING: Mapping[str, str] = {
    "CHINA_TO_FF": "CHINA_TO_FF",
    "CN_TO_RU_TRANSIT": "CHINA_TO_FF",
    "В_пути": "CHINA_TO_FF",
    "В пути": "CHINA_TO_FF",
    "В пути Китай-ФФ": "CHINA_TO_FF",
    "В пути Китай -> ФФ": "CHINA_TO_FF",
    "FF_STOCK": "FF_STOCK",
    "Фулфиллмент": "FF_STOCK",
    "ФФ": "FF_STOCK",
    "FF_TO_WB": "FF_TO_WB",
    "FF_TO_WB_TRANSIT": "FF_TO_WB",
    "ФФ -> WB": "FF_TO_WB",
    "ФФ-ВБ": "FF_TO_WB",
    "В пути ФФ-ВБ": "FF_TO_WB",
    "WB_STOCK": "WB_STOCK",
    "ВБ": "WB_STOCK",
    "WB": "WB_STOCK",
}
_ONEC_ALLOWED_STAGE_SET = set(ONEC_STOCKS_STAGE_KEYS)


def resolve_onec_stocks_account_id() -> str:
    return (
        os.environ.get(ONEC_STOCKS_ACCOUNT_ID_ENV, "").strip()
        or os.environ.get(ONEC_STOCKS_SMOKE_ACCOUNT_ID_ENV, "").strip()
        or DEFAULT_ONEC_STOCKS_ACCOUNT_ID
    )


def extend_metrics_with_onec_stock_metrics(metrics: Iterable[MetricV2Item]) -> list[MetricV2Item]:
    existing = list(metrics)
    existing_keys = {item.metric_key for item in existing}
    return existing + [
        item for item in build_onec_stock_metric_items() if item.metric_key not in existing_keys
    ]


def build_onec_stock_metric_items() -> list[MetricV2Item]:
    items = [
        MetricV2Item(
            metric_key=ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru="1С: всего товаров, шт",
            calc_type="metric",
            calc_ref=ONEC_STOCKS_SKU_TOTAL_QTY_METRIC_KEY,
            show_in_data=True,
            format="integer",
            display_order=1030,
            section=ONEC_STOCKS_SECTION_RU,
        ),
        MetricV2Item(
            metric_key=ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru="1С: товарный капитал всего, руб",
            calc_type="metric",
            calc_ref=ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=1040,
            section=ONEC_STOCKS_SECTION_RU,
        ),
        MetricV2Item(
            metric_key=ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru="Прокси прибыль 2 всего, ₽",
            calc_type="metric",
            calc_ref=ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=21,
            section="Экономика",
        ),
        MetricV2Item(
            metric_key=ONEC_PROXY_MARGIN_2_PCT_TOTAL_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru="Прокси маржинальность 2 всего, %",
            calc_type="metric",
            calc_ref=ONEC_PROXY_MARGIN_2_PCT_TOTAL_METRIC_KEY,
            show_in_data=True,
            format="percent",
            display_order=22,
            section="Экономика",
        ),
        MetricV2Item(
            metric_key=ONEC_INVENTORY_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru="Рентабельность товарных остатков всего, %",
            calc_type="metric",
            calc_ref=ONEC_INVENTORY_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,
            show_in_data=True,
            format="percent",
            display_order=23,
            section="Экономика",
        ),
    ]
    order = 1050
    for stage_key in ONEC_STOCKS_STAGE_KEYS:
        stage_label = ONEC_STOCKS_STAGE_LABELS_RU[stage_key]
        for field in ONEC_STOCKS_STAGE_FIELDS:
            total_metric_key = onec_stage_total_metric_key(stage_key, field)
            label = (
                f"1С {stage_label}: средневзвешенная себестоимость за ед., руб"
                if field == ONEC_STOCKS_STAGE_TOTAL_UNIT_COST_FIELD
                else f"1С {stage_label}: всего {ONEC_STOCKS_FIELD_LABELS_RU[field]}"
            )
            items.append(
                MetricV2Item(
                    metric_key=total_metric_key,
                    enabled=True,
                    scope="TOTAL",
                    label_ru=label,
                    calc_type="metric",
                    calc_ref=onec_stage_metric_key(stage_key, field),
                    show_in_data=True,
                    format=ONEC_STOCKS_FIELD_FORMATS[field],
                    display_order=order,
                    section=ONEC_STOCKS_SECTION_RU,
                )
            )
            order += 10
    for stage_key in ONEC_STOCKS_STAGE_KEYS:
        stage_label = ONEC_STOCKS_STAGE_LABELS_RU[stage_key]
        for field in ONEC_STOCKS_STAGE_FIELDS:
            items.append(
                MetricV2Item(
                    metric_key=onec_stage_metric_key(stage_key, field),
                    enabled=True,
                    scope="SKU",
                    label_ru=f"1С {stage_label}: {ONEC_STOCKS_FIELD_LABELS_RU[field]}",
                    calc_type="metric",
                    calc_ref=onec_stage_metric_key(stage_key, field),
                    show_in_data=True,
                    format=ONEC_STOCKS_FIELD_FORMATS[field],
                    display_order=order,
                    section=ONEC_STOCKS_SECTION_RU,
                )
            )
            order += 10
    items.extend(
        [
            MetricV2Item(
                metric_key=ONEC_STOCKS_SKU_TOTAL_QTY_METRIC_KEY,
                enabled=True,
                scope="SKU",
                label_ru="1С всего товаров, шт",
                calc_type="metric",
                calc_ref=ONEC_STOCKS_SKU_TOTAL_QTY_METRIC_KEY,
                show_in_data=True,
                format="integer",
                display_order=order,
                section=ONEC_STOCKS_SECTION_RU,
            ),
            MetricV2Item(
                metric_key=ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY,
                enabled=True,
                scope="SKU",
                label_ru="1С товарный капитал всего, руб",
                calc_type="metric",
                calc_ref=ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY,
                show_in_data=True,
                format="rub",
                display_order=order + 10,
                section=ONEC_STOCKS_SECTION_RU,
            ),
        ]
    )
    items.extend(
        [
            MetricV2Item(
                metric_key=ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
                enabled=True,
                scope="SKU",
                label_ru="Прокси прибыль 2",
                calc_type="metric",
                calc_ref=ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
                show_in_data=True,
                format="rub",
                display_order=501,
                section="Экономика",
            ),
            MetricV2Item(
                metric_key=ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY,
                enabled=True,
                scope="SKU",
                label_ru="Прокси маржинальность 2, %",
                calc_type="metric",
                calc_ref=ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY,
                show_in_data=True,
                format="percent",
                display_order=502,
                section="Экономика",
            ),
            MetricV2Item(
                metric_key=ONEC_INVENTORY_CAPITAL_RETURN_PCT_METRIC_KEY,
                enabled=True,
                scope="SKU",
                label_ru="Рентабельность товарных остатков, %",
                calc_type="metric",
                calc_ref=ONEC_INVENTORY_CAPITAL_RETURN_PCT_METRIC_KEY,
                show_in_data=True,
                format="percent",
                display_order=503,
                section="Экономика",
            ),
        ]
    )
    return items


def onec_stage_metric_key(stage_key: str, field: str) -> str:
    return f"onec_{stage_key}_{field}"


def onec_stage_total_metric_key(stage_key: str, field: str) -> str:
    prefix = "avg" if field == ONEC_STOCKS_STAGE_TOTAL_UNIT_COST_FIELD else "total"
    return f"{prefix}_onec_{stage_key}_{field}"


def onec_weighted_unit_cost_components(metric_key: str) -> tuple[str, str] | None:
    normalized = str(metric_key or "").strip()
    for stage_key in ONEC_STOCKS_STAGE_KEYS:
        if normalized == onec_stage_total_metric_key(stage_key, ONEC_STOCKS_STAGE_TOTAL_UNIT_COST_FIELD):
            return (
                onec_stage_metric_key(stage_key, "cost_total_rub"),
                onec_stage_metric_key(stage_key, "qty"),
            )
    return None


def is_onec_stock_metric_key(metric_key: str) -> bool:
    return str(metric_key or "").strip() in set(ONEC_STOCKS_METRIC_KEYS)


def is_onec_stock_sku_metric_key(metric_key: str) -> bool:
    return str(metric_key or "").strip() in set(ONEC_STOCKS_SKU_METRIC_KEYS)


def build_onec_stocks_lookup(payload: Any | None) -> dict[int, dict[str, float]]:
    if payload is None:
        return {}
    items = getattr(payload, "items", None)
    if not isinstance(items, list):
        return {}

    result: dict[int, dict[str, float]] = {}
    for item in items:
        nm_id = getattr(item, "nm_id", None)
        if not isinstance(nm_id, int):
            continue
        stage_key = normalize_onec_stage_code(
            getattr(item, "canonical_stage_code", None)
            or getattr(item, "stage_name", None)
        )
        if stage_key is None:
            continue
        row = result.setdefault(nm_id, {})
        qty = _to_float(getattr(item, "qty", None))
        cost = _to_float(getattr(item, "cost_total_rub", None))
        previous_qty = row.get(onec_stage_metric_key(stage_key, "qty"), 0.0)
        previous_cost = row.get(onec_stage_metric_key(stage_key, "cost_total_rub"), 0.0)
        next_qty = previous_qty + qty
        next_cost = previous_cost + cost
        row[onec_stage_metric_key(stage_key, "qty")] = next_qty
        row[onec_stage_metric_key(stage_key, "cost_total_rub")] = next_cost
        row[onec_stage_metric_key(stage_key, "unit_cost_rub")] = (
            next_cost / next_qty if next_qty else _to_float(getattr(item, "unit_cost_rub", None))
        )

    for row in result.values():
        row[ONEC_STOCKS_SKU_TOTAL_QTY_METRIC_KEY] = sum(
            float(row.get(onec_stage_metric_key(stage_key, "qty"), 0.0) or 0.0)
            for stage_key in ONEC_STOCKS_STAGE_KEYS
        )
        row[ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY] = sum(
            float(row.get(onec_stage_metric_key(stage_key, "cost_total_rub"), 0.0) or 0.0)
            for stage_key in ONEC_STOCKS_STAGE_KEYS
        )
    return result


def summarize_onec_stage_bucket_coverage(payload: Any | None) -> dict[str, Any]:
    if payload is None:
        return {}
    items = getattr(payload, "items", None)
    if not isinstance(items, list):
        return {}

    stage_row_counts: dict[str, int] = {}
    stage_qty: dict[str, float] = {}
    stage_cost_total_rub: dict[str, float] = {}
    raw_stage_names: set[str] = set()
    unmapped_stage_names: set[str] = set()
    nm_ids_with_rows: set[int] = set()
    nm_ids_by_stage: dict[str, set[int]] = {}

    for item in items:
        nm_id = getattr(item, "nm_id", None)
        stage_name = str(getattr(item, "stage_name", "") or "").strip()
        if stage_name:
            raw_stage_names.add(stage_name)
        if isinstance(nm_id, int):
            nm_ids_with_rows.add(nm_id)
        stage_key = normalize_onec_stage_code(
            getattr(item, "canonical_stage_code", None)
            or getattr(item, "stage_name", None)
        )
        if stage_key is None:
            if stage_name:
                unmapped_stage_names.add(stage_name)
            continue
        stage_row_counts[stage_key] = stage_row_counts.get(stage_key, 0) + 1
        stage_qty[stage_key] = stage_qty.get(stage_key, 0.0) + _to_float(getattr(item, "qty", None))
        stage_cost_total_rub[stage_key] = stage_cost_total_rub.get(stage_key, 0.0) + _to_float(
            getattr(item, "cost_total_rub", None)
        )
        if isinstance(nm_id, int):
            nm_ids_by_stage.setdefault(stage_key, set()).add(nm_id)

    covered_stage_buckets = [stage_key for stage_key in ONEC_STOCKS_STAGE_KEYS if stage_key in stage_row_counts]
    missing_stage_buckets = [stage_key for stage_key in ONEC_STOCKS_STAGE_KEYS if stage_key not in stage_row_counts]
    return {
        "item_count": len(items),
        "covered_nm_id_count": len(nm_ids_with_rows),
        "covered_stage_buckets": covered_stage_buckets,
        "missing_stage_buckets": missing_stage_buckets,
        "stage_row_counts": {
            stage_key: stage_row_counts[stage_key]
            for stage_key in covered_stage_buckets
        },
        "stage_nm_id_counts": {
            stage_key: len(nm_ids_by_stage.get(stage_key, set()))
            for stage_key in covered_stage_buckets
        },
        "stage_qty": {
            stage_key: round(stage_qty.get(stage_key, 0.0), 6)
            for stage_key in covered_stage_buckets
        },
        "stage_cost_total_rub": {
            stage_key: round(stage_cost_total_rub.get(stage_key, 0.0), 6)
            for stage_key in covered_stage_buckets
        },
        "raw_stage_names": sorted(raw_stage_names),
        "unmapped_stage_names": sorted(unmapped_stage_names),
    }


def normalize_onec_stage_code(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    mapped = str(DEFAULT_ONEC_STAGE_MAPPING.get(text, text)).strip()
    if mapped in _ONEC_ALLOWED_STAGE_SET:
        return mapped
    return None


def resolve_onec_stock_metric_value(
    metric_key: str,
    row: Mapping[str, float] | None,
) -> float | None:
    normalized = str(metric_key or "").strip()
    if normalized not in set(ONEC_STOCKS_SKU_METRIC_KEYS):
        return None
    if row is None:
        return None
    value = row.get(normalized)
    return float(value) if value is not None else None


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0
