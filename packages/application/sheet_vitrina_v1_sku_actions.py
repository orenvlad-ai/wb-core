"""Metric catalog extension for confirmed SKU operator actions and buyer price."""

from __future__ import annotations

from typing import Iterable

from packages.contracts.registry_upload_bundle_v1 import MetricV2Item


SELLER_PRICE_CHANGE_RUB_METRIC_KEY = "seller_price_change_rub"
ADVERTISING_BID_CHANGE_RUB_METRIC_KEY = "advertising_bid_change_rub"
BUYER_PRICE_RUB_METRIC_KEY = "buyer_price_rub"
SKU_ACTION_METRIC_KEYS = {
    SELLER_PRICE_CHANGE_RUB_METRIC_KEY,
    ADVERTISING_BID_CHANGE_RUB_METRIC_KEY,
    BUYER_PRICE_RUB_METRIC_KEY,
}


def extend_metrics_with_sku_action_metrics(metrics: Iterable[MetricV2Item]) -> list[MetricV2Item]:
    existing = list(metrics)
    keys = {item.metric_key for item in existing}
    additions = [
        MetricV2Item(
            metric_key=SELLER_PRICE_CHANGE_RUB_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru="Изменение нашей цены, ₽",
            calc_type="metric",
            calc_ref=SELLER_PRICE_CHANGE_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=525,
            section="Цены",
        ),
        MetricV2Item(
            metric_key=ADVERTISING_BID_CHANGE_RUB_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru="Изменение рекламной ставки, ₽",
            calc_type="metric",
            calc_ref=ADVERTISING_BID_CHANGE_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=625,
            section="Реклама",
        ),
        MetricV2Item(
            metric_key=BUYER_PRICE_RUB_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru="Цена для покупателя, ₽",
            calc_type="metric",
            calc_ref=BUYER_PRICE_RUB_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=515,
            section="Цены",
        ),
    ]
    return [*existing, *(item for item in additions if item.metric_key not in keys)]
