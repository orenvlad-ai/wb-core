from __future__ import annotations

from typing import Iterable

from packages.contracts.registry_upload_bundle_v1 import MetricV2Item


OUR_WB_COST_OPENING_DATE = "2026-07-01"

OUR_WB_UNIT_COST_RUB_METRIC_KEY = "our_wb_unit_cost_rub"
TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY = "total_our_wb_unit_cost_rub"
OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY = "our_wb_cost_confirmed_share_pct"
TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY = "total_our_wb_cost_confirmed_share_pct"
OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY = "proxy_profit_3_rub"
OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY = "total_proxy_profit_3_rub"
OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY = "proxy_margin_3_pct"
OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY = "proxy_margin_3_pct_total"
SALES_WITHOUT_COST_RUB_METRIC_KEY = "sales_without_cost_rub"
TOTAL_SALES_WITHOUT_COST_RUB_METRIC_KEY = "total_sales_without_cost_rub"
OUR_WB_ARCHIVED_METRIC_KEYS: tuple[str, ...] = (
    OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
    TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
)

OUR_WB_UNIT_COST_RUB_LABEL = "Себестоимость наша, ₽/шт"
SALES_WITHOUT_COST_RUB_LABEL = "Продажи без себестоимости, ₽"
OUR_WB_COST_CONFIRMED_SHARE_PCT_LABEL = "Доля подтверждённой себестоимости, %"
OUR_WB_PROXY_PROFIT_3_RUB_LABEL = "proxy прибыль 3"
OUR_WB_PROXY_MARGIN_3_PCT_LABEL = "Прокси маржинальность 3, %"
OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_LABEL = "Прокси маржинальность 3 всего, %"


def extend_metrics_with_our_wb_cost_metrics(metrics: Iterable[MetricV2Item]) -> list[MetricV2Item]:
    """Append management proxy WB cost metrics to the live vitrina metric catalog."""

    existing_metrics = list(metrics)
    existing = {metric.metric_key for metric in existing_metrics}
    additions: list[MetricV2Item] = []

    def _append(metric: MetricV2Item) -> None:
        if metric.metric_key not in existing:
            existing.add(metric.metric_key)
            additions.append(metric)

    _append(
        MetricV2Item(
            metric_key=SALES_WITHOUT_COST_RUB_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru=SALES_WITHOUT_COST_RUB_LABEL,
            calc_type="metric",
            calc_ref=SALES_WITHOUT_COST_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=23,
            section="Экономика",
        )
    )
    _append(
        MetricV2Item(
            metric_key=TOTAL_SALES_WITHOUT_COST_RUB_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru=SALES_WITHOUT_COST_RUB_LABEL,
            calc_type="metric",
            calc_ref="aggregate:sum:sales_without_cost_rub",
            show_in_data=True,
            format="rub",
            display_order=22,
            section="Экономика",
        )
    )
    _append(
        MetricV2Item(
            metric_key=OUR_WB_UNIT_COST_RUB_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru=OUR_WB_UNIT_COST_RUB_LABEL,
            calc_type="metric",
            calc_ref=OUR_WB_UNIT_COST_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=1095,
            section="Товарный капитал",
        )
    )
    _append(
        MetricV2Item(
            metric_key=OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru=OUR_WB_COST_CONFIRMED_SHARE_PCT_LABEL,
            calc_type="metric",
            calc_ref=OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
            show_in_data=True,
            format="percent",
            display_order=1105,
            section="Технический архив",
        )
    )
    _append(
        MetricV2Item(
            metric_key=TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru=OUR_WB_COST_CONFIRMED_SHARE_PCT_LABEL,
            calc_type="metric",
            calc_ref="aggregate:sum_confirmed_qty:stock_qty",
            show_in_data=True,
            format="percent",
            display_order=1100,
            section="Технический архив",
        )
    )
    _append(
        MetricV2Item(
            metric_key=TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru=OUR_WB_UNIT_COST_RUB_LABEL,
            calc_type="metric",
            calc_ref="aggregate:weighted_avg:our_wb_unit_cost_rub:stock_qty",
            show_in_data=True,
            format="rub",
            display_order=1090,
            section="Товарный капитал",
        )
    )
    _append(
        MetricV2Item(
            metric_key=OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru=OUR_WB_PROXY_PROFIT_3_RUB_LABEL,
            calc_type="metric",
            calc_ref=OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=25,
            section="Экономика",
        )
    )
    _append(
        MetricV2Item(
            metric_key=OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru=OUR_WB_PROXY_PROFIT_3_RUB_LABEL,
            calc_type="metric",
            calc_ref="aggregate:sum:proxy_profit_3_rub",
            show_in_data=True,
            format="rub",
            display_order=24,
            section="Экономика",
        )
    )
    _append(
        MetricV2Item(
            metric_key=OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru=OUR_WB_PROXY_MARGIN_3_PCT_LABEL,
            calc_type="metric",
            calc_ref=OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
            show_in_data=True,
            format="percent",
            display_order=26,
            section="Экономика",
        )
    )
    _append(
        MetricV2Item(
            metric_key=OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru=OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_LABEL,
            calc_type="metric",
            calc_ref=OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
            show_in_data=True,
            format="percent",
            display_order=25,
            section="Экономика",
        )
    )

    return [*existing_metrics, *additions]
