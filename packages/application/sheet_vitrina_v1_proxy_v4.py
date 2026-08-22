from __future__ import annotations

from typing import Iterable

from packages.contracts.registry_upload_bundle_v1 import MetricV2Item


PROXY_V4_PROFIT_RUB_METRIC_KEY = "proxy_profit_4_rub"
PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY = "total_proxy_profit_4_rub"
PROXY_V4_MARGIN_PCT_METRIC_KEY = "proxy_margin_4_pct"
PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY = "proxy_margin_4_pct_total"
PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY = "proxy_margin_per_unit_rub"
PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY = "proxy_margin_per_unit_rub_total"

PROXY_V4_PROFIT_LABEL_RU = "Proxy прибыль 4"
PROXY_V4_MARGIN_LABEL_RU = "Прокси маржинальность 4"
PROXY_V4_MARGIN_PER_UNIT_LABEL_RU = "Средняя маржа на единицу"

PROXY_V4_SKU_METRIC_KEYS: tuple[str, ...] = (
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
)
PROXY_V4_TOTAL_METRIC_KEYS: tuple[str, ...] = (
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
)
PROXY_V4_METRIC_KEYS: tuple[str, ...] = (
    *PROXY_V4_SKU_METRIC_KEYS,
    *PROXY_V4_TOTAL_METRIC_KEYS,
)


def extend_metrics_with_proxy_v4(
    metrics: Iterable[MetricV2Item],
) -> list[MetricV2Item]:
    """Append the three public V4 logical metric pairs without registry duplication."""

    existing_metrics = list(metrics)
    existing = {item.metric_key for item in existing_metrics}
    additions: list[MetricV2Item] = []

    def append(item: MetricV2Item) -> None:
        if item.metric_key not in existing:
            existing.add(item.metric_key)
            additions.append(item)

    append(
        MetricV2Item(
            metric_key=PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru=PROXY_V4_PROFIT_LABEL_RU,
            calc_type="metric",
            calc_ref=f"aggregate:sum:{PROXY_V4_PROFIT_RUB_METRIC_KEY}",
            show_in_data=True,
            format="rub",
            display_order=27,
            section="Экономика",
        )
    )
    append(
        MetricV2Item(
            metric_key=PROXY_V4_PROFIT_RUB_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru=PROXY_V4_PROFIT_LABEL_RU,
            calc_type="metric",
            calc_ref=PROXY_V4_PROFIT_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=28,
            section="Экономика",
        )
    )
    append(
        MetricV2Item(
            metric_key=PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru=PROXY_V4_MARGIN_LABEL_RU,
            calc_type="metric",
            calc_ref=PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
            show_in_data=True,
            format="percent",
            display_order=29,
            section="Экономика",
        )
    )
    append(
        MetricV2Item(
            metric_key=PROXY_V4_MARGIN_PCT_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru=PROXY_V4_MARGIN_LABEL_RU,
            calc_type="metric",
            calc_ref=PROXY_V4_MARGIN_PCT_METRIC_KEY,
            show_in_data=True,
            format="percent",
            display_order=30,
            section="Экономика",
        )
    )
    append(
        MetricV2Item(
            metric_key=PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru=PROXY_V4_MARGIN_PER_UNIT_LABEL_RU,
            calc_type="metric",
            calc_ref=PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub_per_unit",
            display_order=31,
            section="Экономика",
        )
    )
    append(
        MetricV2Item(
            metric_key=PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru=PROXY_V4_MARGIN_PER_UNIT_LABEL_RU,
            calc_type="metric",
            calc_ref=PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub_per_unit",
            display_order=32,
            section="Экономика",
        )
    )
    return [*existing_metrics, *additions]
