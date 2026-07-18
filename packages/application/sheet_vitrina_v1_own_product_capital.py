"""Metric catalog for WebCore-owned invested product capital."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from packages.contracts.registry_upload_bundle_v1 import MetricV2Item


OWN_PRODUCT_CAPITAL_SOURCE_KEY = "own_product_capital"
OWN_PRODUCT_CAPITAL_SOURCE_GROUP_ID = "webcore_product_capital"
OWN_PRODUCT_CAPITAL_SOURCE_GROUP_LABEL_RU = "WebCore"
OWN_PRODUCT_CAPITAL_SECTION_RU = "Товарный капитал — наши данные"

OWN_PRODUCT_CAPITAL_STAGES: tuple[str, ...] = (
    "PRODUCTION",
    "PRODUCTION_TO_FF",
    "FF",
    "FF_TO_WB",
    "WB",
    "WB_ACCEPTANCE_DISCREPANCY",
)
OWN_PRODUCT_CAPITAL_STAGE_LABELS_RU: Mapping[str, str] = {
    "PRODUCTION": "На производстве",
    "PRODUCTION_TO_FF": "Китай → FF",
    "FF": "На ФФ",
    "FF_TO_WB": "ФФ → WB",
    "WB": "На WB",
    "WB_ACCEPTANCE_DISCREPANCY": "Расхождения приёмки WB",
}
OWN_PRODUCT_CAPITAL_STAGE_FIELDS: tuple[str, ...] = (
    "capital_rub",
    "qty",
    "paid_equivalent_qty",
    "unit_cost_rub",
    "cost_coverage_pct",
    "confirmed_share_pct",
)
OWN_PRODUCT_CAPITAL_FIELD_LABELS_RU: Mapping[str, str] = {
    "capital_rub": "всего капитал, ₽",
    "qty": "всего количество, шт",
    "paid_equivalent_qty": "оплаченный эквивалент, шт",
    "unit_cost_rub": "средневзвешенная стоимость, ₽/шт",
    "cost_coverage_pct": "покрыто себестоимостью, %",
    "confirmed_share_pct": "подтверждено, %",
}
OWN_PRODUCT_CAPITAL_FIELD_FORMATS: Mapping[str, str] = {
    "capital_rub": "rub",
    "qty": "number",
    "paid_equivalent_qty": "number",
    "unit_cost_rub": "rub",
    "cost_coverage_pct": "percent",
    "confirmed_share_pct": "percent",
}

OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY = "own_underaccepted_wb_qty"
OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_METRIC_KEY = "own_underaccepted_wb_unit_cost_rub"
OWN_UNDERACCEPTED_WB_QTY_TOTAL_METRIC_KEY = "total_own_underaccepted_wb_qty"
OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_TOTAL_METRIC_KEY = "total_own_underaccepted_wb_unit_cost_rub"

OWN_TOTAL_QTY_METRIC_KEY = "own_total_product_qty"
OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY = "own_total_paid_equivalent_qty"
OWN_TOTAL_CAPITAL_RUB_METRIC_KEY = "own_total_product_capital_rub"
OWN_AVG_COST_RUB_METRIC_KEY = "own_avg_product_cost_rub"
OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY = "own_total_confirmed_share_pct"
OWN_CAPITAL_RETURN_PCT_METRIC_KEY = "own_inventory_capital_return_pct"

OWN_TOTAL_QTY_TOTAL_METRIC_KEY = "total_own_total_product_qty"
OWN_TOTAL_PAID_EQUIVALENT_QTY_TOTAL_METRIC_KEY = "total_own_total_paid_equivalent_qty"
OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY = "total_own_total_product_capital_rub"
OWN_AVG_COST_RUB_TOTAL_METRIC_KEY = "total_own_avg_product_cost_rub"
OWN_TOTAL_CONFIRMED_SHARE_PCT_TOTAL_METRIC_KEY = "total_own_total_confirmed_share_pct"
OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY = "total_own_inventory_capital_return_pct"


def own_stage_metric_key(stage: str, field: str) -> str:
    return f"own_capital_{stage}_{field}"


def own_stage_total_metric_key(stage: str, field: str) -> str:
    prefix = "avg" if field in {"unit_cost_rub", "confirmed_share_pct"} else "total"
    return f"{prefix}_own_capital_{stage}_{field}"


OWN_PRODUCT_CAPITAL_SKU_STAGE_METRIC_KEYS: tuple[str, ...] = tuple(
    own_stage_metric_key(stage, field)
    for stage in OWN_PRODUCT_CAPITAL_STAGES
    for field in OWN_PRODUCT_CAPITAL_STAGE_FIELDS
)
OWN_PRODUCT_CAPITAL_TOTAL_STAGE_METRIC_KEYS: tuple[str, ...] = tuple(
    own_stage_total_metric_key(stage, field)
    for stage in OWN_PRODUCT_CAPITAL_STAGES
    for field in OWN_PRODUCT_CAPITAL_STAGE_FIELDS
)
OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS: tuple[str, ...] = (
    *OWN_PRODUCT_CAPITAL_SKU_STAGE_METRIC_KEYS,
    OWN_TOTAL_QTY_METRIC_KEY,
    OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY,
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    OWN_AVG_COST_RUB_METRIC_KEY,
    OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY,
    OWN_CAPITAL_RETURN_PCT_METRIC_KEY,
    OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY,
    OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_METRIC_KEY,
)
OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS: tuple[str, ...] = (
    *OWN_PRODUCT_CAPITAL_TOTAL_STAGE_METRIC_KEYS,
    OWN_TOTAL_QTY_TOTAL_METRIC_KEY,
    OWN_TOTAL_PAID_EQUIVALENT_QTY_TOTAL_METRIC_KEY,
    OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY,
    OWN_AVG_COST_RUB_TOTAL_METRIC_KEY,
    OWN_TOTAL_CONFIRMED_SHARE_PCT_TOTAL_METRIC_KEY,
    OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,
    OWN_UNDERACCEPTED_WB_QTY_TOTAL_METRIC_KEY,
    OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_TOTAL_METRIC_KEY,
)
OWN_PRODUCT_CAPITAL_METRIC_KEYS: tuple[str, ...] = (
    *OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS,
    *OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS,
)


def extend_metrics_with_own_product_capital_metrics(
    metrics: Iterable[MetricV2Item],
) -> list[MetricV2Item]:
    existing = list(metrics)
    existing_keys = {item.metric_key for item in existing}
    additions = [item for item in build_own_product_capital_metric_items() if item.metric_key not in existing_keys]
    return [*existing, *additions]


def build_own_product_capital_metric_items() -> list[MetricV2Item]:
    items: list[MetricV2Item] = []
    order = 1210
    for scope in ("TOTAL", "SKU"):
        for stage in OWN_PRODUCT_CAPITAL_STAGES:
            stage_label = OWN_PRODUCT_CAPITAL_STAGE_LABELS_RU[stage]
            for field in OWN_PRODUCT_CAPITAL_STAGE_FIELDS:
                metric_key = (
                    own_stage_total_metric_key(stage, field)
                    if scope == "TOTAL"
                    else own_stage_metric_key(stage, field)
                )
                calc_ref = own_stage_metric_key(stage, field)
                items.append(
                    MetricV2Item(
                        metric_key=metric_key,
                        enabled=True,
                        scope=scope,
                        label_ru=f"{stage_label}: {OWN_PRODUCT_CAPITAL_FIELD_LABELS_RU[field]}",
                        calc_type="metric",
                        calc_ref=calc_ref,
                        show_in_data=True,
                        format=OWN_PRODUCT_CAPITAL_FIELD_FORMATS[field],
                        display_order=order,
                        section=OWN_PRODUCT_CAPITAL_SECTION_RU,
                    )
                )
                order += 10

        underaccepted = (
            (
                OWN_UNDERACCEPTED_WB_QTY_TOTAL_METRIC_KEY,
                OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY,
                "Недопринято WB: количество, шт",
                "number",
            ),
            (
                OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_TOTAL_METRIC_KEY,
                OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_METRIC_KEY,
                "Недопринято WB: средняя себестоимость, ₽/шт",
                "rub",
            ),
        ) if scope == "TOTAL" else (
            (
                OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY,
                OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY,
                "Недопринято WB: количество, шт",
                "number",
            ),
            (
                OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_METRIC_KEY,
                OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_METRIC_KEY,
                "Недопринято WB: средняя себестоимость, ₽/шт",
                "rub",
            ),
        )
        for metric_key, calc_ref, label, value_format in underaccepted:
            items.append(
                MetricV2Item(
                    metric_key=metric_key,
                    enabled=True,
                    scope=scope,
                    label_ru=label,
                    calc_type="metric",
                    calc_ref=calc_ref,
                    show_in_data=True,
                    format=value_format,
                    display_order=order,
                    section=OWN_PRODUCT_CAPITAL_SECTION_RU,
                )
            )
            order += 10

        totals = (
            (OWN_TOTAL_QTY_TOTAL_METRIC_KEY, OWN_TOTAL_QTY_METRIC_KEY, "всего товаров, шт", "number"),
            (
                OWN_TOTAL_PAID_EQUIVALENT_QTY_TOTAL_METRIC_KEY,
                OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY,
                "оплаченный эквивалент всего, шт",
                "number",
            ),
            (
                OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY,
                OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
                "товарный капитал всего, ₽",
                "rub",
            ),
            (
                OWN_AVG_COST_RUB_TOTAL_METRIC_KEY,
                OWN_AVG_COST_RUB_METRIC_KEY,
                "себестоимость средняя, ₽/шт",
                "rub",
            ),
            (
                OWN_TOTAL_CONFIRMED_SHARE_PCT_TOTAL_METRIC_KEY,
                OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY,
                "подтверждено всего, %",
                "percent",
            ),
            (
                OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,
                OWN_CAPITAL_RETURN_PCT_METRIC_KEY,
                "рентабельность товарного капитала, %",
                "percent",
            ),
        ) if scope == "TOTAL" else (
            (OWN_TOTAL_QTY_METRIC_KEY, OWN_TOTAL_QTY_METRIC_KEY, "всего товаров, шт", "number"),
            (
                OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY,
                OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY,
                "оплаченный эквивалент всего, шт",
                "number",
            ),
            (
                OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
                OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
                "товарный капитал всего, ₽",
                "rub",
            ),
            (
                OWN_AVG_COST_RUB_METRIC_KEY,
                OWN_AVG_COST_RUB_METRIC_KEY,
                "себестоимость средняя, ₽/шт",
                "rub",
            ),
            (
                OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY,
                OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY,
                "подтверждено всего, %",
                "percent",
            ),
            (
                OWN_CAPITAL_RETURN_PCT_METRIC_KEY,
                OWN_CAPITAL_RETURN_PCT_METRIC_KEY,
                "рентабельность товарного капитала, %",
                "percent",
            ),
        )
        for metric_key, calc_ref, label, value_format in totals:
            items.append(
                MetricV2Item(
                    metric_key=metric_key,
                    enabled=True,
                    scope=scope,
                    label_ru=label,
                    calc_type="metric",
                    calc_ref=calc_ref,
                    show_in_data=True,
                    format=value_format,
                    display_order=order,
                    section=OWN_PRODUCT_CAPITAL_SECTION_RU,
                )
            )
            order += 10
    return items


def is_own_product_capital_sku_metric_key(metric_key: str) -> bool:
    return str(metric_key or "").strip() in set(OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS)


def own_product_capital_metric_value(metric_key: str, row: Mapping[str, Any] | None) -> float | None:
    if row is None or not is_own_product_capital_sku_metric_key(metric_key):
        return None
    value = row.get(str(metric_key))
    if value in {None, ""}:
        return None
    return float(value)


def own_stage_total_components(metric_key: str) -> tuple[str, str] | None:
    normalized = str(metric_key or "").strip()
    for stage in OWN_PRODUCT_CAPITAL_STAGES:
        if normalized == own_stage_total_metric_key(stage, "unit_cost_rub"):
            return (
                own_stage_metric_key(stage, "capital_rub"),
                own_stage_metric_key(stage, "paid_equivalent_qty"),
            )
        if normalized == own_stage_total_metric_key(stage, "confirmed_share_pct"):
            return own_stage_metric_key(stage, "confirmed_qty"), own_stage_metric_key(stage, "qty")
        if normalized == own_stage_total_metric_key(stage, "cost_coverage_pct"):
            return own_stage_metric_key(stage, "cost_covered_qty"), own_stage_metric_key(stage, "qty")
    return None
