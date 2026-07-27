"""Stable Vitrina metric family for fact and incident-aware WB stock."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from packages.contracts.registry_upload_bundle_v1 import MetricV2Item


INCIDENT_STOCK_SECTION_RU = "Остатки WB — инциденты"
INCIDENT_STOCK_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("total", "stock_total", ""),
    ("central", "stock_ru_central", " — Центральный"),
    ("northwest", "stock_ru_northwest", " — Северо-Запад"),
    ("volga", "stock_ru_volga", " — Поволжье"),
    ("south_caucasus", "stock_ru_south_caucasus", " — Юг и Кавказ"),
    ("ural", "stock_ru_ural", " — Урал"),
    ("far_siberia", "stock_ru_far_siberia", " — Сибирь и Дальний Восток"),
)
INCIDENT_STOCK_SHORT_REGION_LABELS: dict[str, str] = {
    "total": "всего",
    "central": "Центр",
    "northwest": "СЗ",
    "volga": "Поволжье",
    "south_caucasus": "Юг+СКФО",
    "ural": "Урал",
    "far_siberia": "ДВ+Сибирь",
}
INCIDENT_STOCK_VARIANTS: tuple[tuple[str, str], ...] = (
    ("fact", "Остаток WB — факт, шт"),
    ("incident", "Остаток инц.:"),
    ("effective", "Остаток без инц.:"),
)


def incident_stock_metric_key(variant: str, region_key: str = "total") -> str:
    suffix = "" if region_key == "total" else f"_{region_key}"
    return f"wb_stock_{variant}_qty{suffix}"


def incident_stock_total_metric_key(variant: str, region_key: str = "total") -> str:
    return f"total_{incident_stock_metric_key(variant, region_key)}"


INCIDENT_STOCK_SKU_METRIC_KEYS = tuple(
    incident_stock_metric_key(variant, region)
    for variant, _label in INCIDENT_STOCK_VARIANTS
    for region, _source, _suffix in INCIDENT_STOCK_FIELDS
)
INCIDENT_STOCK_TOTAL_METRIC_KEYS = tuple(
    incident_stock_total_metric_key(variant, region)
    for variant, _label in INCIDENT_STOCK_VARIANTS
    for region, _source, _suffix in INCIDENT_STOCK_FIELDS
)
INCIDENT_STOCK_METRIC_KEYS = (
    *INCIDENT_STOCK_SKU_METRIC_KEYS,
    *INCIDENT_STOCK_TOTAL_METRIC_KEYS,
)


def extend_metrics_with_incident_stock_metrics(
    metrics: Iterable[MetricV2Item],
) -> list[MetricV2Item]:
    existing = list(metrics)
    keys = {item.metric_key for item in existing}
    additions: list[MetricV2Item] = []
    display_order = 890
    for scope in ("TOTAL", "SKU"):
        for variant, label in INCIDENT_STOCK_VARIANTS:
            for region, _source, suffix in INCIDENT_STOCK_FIELDS:
                sku_key = incident_stock_metric_key(variant, region)
                metric_key = (
                    incident_stock_total_metric_key(variant, region)
                    if scope == "TOTAL"
                    else sku_key
                )
                additions.append(
                    MetricV2Item(
                        metric_key=metric_key,
                        enabled=True,
                        scope=scope,
                        label_ru=(
                            f"{label} {INCIDENT_STOCK_SHORT_REGION_LABELS[region]}"
                            if variant in {"incident", "effective"}
                            else f"{label}{suffix}"
                        ),
                        calc_type="metric",
                        calc_ref=sku_key,
                        show_in_data=True,
                        format="number",
                        display_order=display_order,
                        section=INCIDENT_STOCK_SECTION_RU,
                    )
                )
                display_order += 1
    return [*existing, *(item for item in additions if item.metric_key not in keys)]


def incident_stock_value(
    metric_key: str,
    projection_row: Mapping[str, Any] | None,
) -> float | None:
    if projection_row is None:
        return None
    normalized = metric_key.removeprefix("total_")
    for variant, _label in INCIDENT_STOCK_VARIANTS:
        for region, source_field, _suffix in INCIDENT_STOCK_FIELDS:
            if normalized != incident_stock_metric_key(variant, region):
                continue
            prefix = {
                "fact": "actual",
                "incident": "excluded",
                "effective": "effective",
            }[variant]
            field = "stock_total_mp" if source_field == "stock_total" else source_field
            value = projection_row.get(f"{prefix}_{field}")
            return None if value is None else float(value)
    return None


def is_incident_stock_metric_key(metric_key: str) -> bool:
    return metric_key in INCIDENT_STOCK_METRIC_KEYS
