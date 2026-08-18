"""Explicit order-weighted TOTAL seller-price metric for Web Vitrina."""

from __future__ import annotations

from typing import Iterable

from packages.contracts.registry_upload_bundle_v1 import MetricV2Item


SELLER_PRICE_DISCOUNTED_METRIC_KEY = "price_seller_discounted"
LEGACY_AVG_SELLER_PRICE_DISCOUNTED_METRIC_KEY = "avg_price_seller_discounted"
WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY = "weighted_price_seller_discounted"
SELLER_PRICE_ORDER_WEIGHT_METRIC_KEY = "orderCount"
WEIGHTED_SELLER_PRICE_DISCOUNTED_LABEL_RU = "Цена продавца взвеш."
WEIGHTED_SELLER_PRICE_DISCOUNTED_AGGREGATION_RULE = (
    "SUM(price_seller_discounted * orderCount) / SUM(orderCount)"
)


def extend_metrics_with_weighted_seller_price(
    metrics: Iterable[MetricV2Item],
) -> list[MetricV2Item]:
    """Append the new TOTAL identity without mutating either historical price key."""

    existing_metrics = list(metrics)
    if any(
        item.metric_key == WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY
        for item in existing_metrics
    ):
        return existing_metrics
    return [
        *existing_metrics,
        MetricV2Item(
            metric_key=WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru=WEIGHTED_SELLER_PRICE_DISCOUNTED_LABEL_RU,
            calc_type="metric",
            calc_ref=(
                "aggregate:positive_weight_fail_closed:"
                f"{SELLER_PRICE_DISCOUNTED_METRIC_KEY}:"
                f"{SELLER_PRICE_ORDER_WEIGHT_METRIC_KEY}"
            ),
            show_in_data=True,
            format="rub",
            display_order=170,
            section="Цены",
        ),
    ]
