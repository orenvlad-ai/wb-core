"""Canonical server-owned source groups for the Web Vitrina."""

from __future__ import annotations

from typing import Any

from packages.application.sheet_vitrina_v1_onec_stocks import (
    ONEC_STOCKS_SOURCE_GROUP_ID,
    ONEC_STOCKS_SOURCE_GROUP_LABEL_RU,
    ONEC_STOCKS_SOURCE_KEY,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_PRODUCT_CAPITAL_SOURCE_GROUP_ID,
    OWN_PRODUCT_CAPITAL_SOURCE_GROUP_LABEL_RU,
    OWN_PRODUCT_CAPITAL_SOURCE_KEY,
)


WEB_VITRINA_SOURCE_GROUPS: dict[str, dict[str, Any]] = {
    "wb_api": {
        "label_ru": "WB API",
        "source_keys": (
            "sales_funnel_history",
            "sf_period",
            "spp",
            "stocks",
            "ads_compact",
            "fin_report_daily",
            "prices_snapshot",
            "ads_bids",
        ),
    },
    "seller_portal_bot": {
        "label_ru": "Seller Portal / бот",
        "source_keys": (
            "seller_funnel_snapshot",
            "web_source_snapshot",
            "promo_by_price",
        ),
    },
    "wb_public_card_bot": {
        "label_ru": "WB public card / бот",
        "source_keys": ("spp_proxy",),
    },
    "other_sources": {
        "label_ru": "Прочие источники",
        "source_keys": ("cost_price", "sku_action_events"),
    },
    ONEC_STOCKS_SOURCE_GROUP_ID: {
        "label_ru": ONEC_STOCKS_SOURCE_GROUP_LABEL_RU,
        "source_keys": (ONEC_STOCKS_SOURCE_KEY,),
    },
    OWN_PRODUCT_CAPITAL_SOURCE_GROUP_ID: {
        "label_ru": OWN_PRODUCT_CAPITAL_SOURCE_GROUP_LABEL_RU,
        "source_keys": (OWN_PRODUCT_CAPITAL_SOURCE_KEY,),
    },
}

WEB_VITRINA_SOURCE_GROUP_ORDER = (
    "wb_api",
    ONEC_STOCKS_SOURCE_GROUP_ID,
    OWN_PRODUCT_CAPITAL_SOURCE_GROUP_ID,
    "seller_portal_bot",
    "wb_public_card_bot",
    "other_sources",
)

WEB_VITRINA_SOURCE_KEY_TO_GROUP = {
    source_key: group_id
    for group_id, group in WEB_VITRINA_SOURCE_GROUPS.items()
    for source_key in group["source_keys"]
}


def active_source_expectations(source_keys: set[str] | None = None) -> list[dict[str, str]]:
    """Return the stable group/source order, optionally restricted to persisted sources."""

    restricted = set(source_keys or ())
    rows: list[dict[str, str]] = []
    for group_id in WEB_VITRINA_SOURCE_GROUP_ORDER:
        group = WEB_VITRINA_SOURCE_GROUPS[group_id]
        for source_key in group["source_keys"]:
            if restricted and source_key not in restricted:
                continue
            rows.append(
                {
                    "source_group_id": group_id,
                    "source_group_label": str(group["label_ru"]),
                    "source_key": str(source_key),
                }
            )
    if restricted:
        known = {row["source_key"] for row in rows}
        for source_key in sorted(restricted - known):
            rows.append(
                {
                    "source_group_id": "unclassified",
                    "source_group_label": "Unclassified",
                    "source_key": source_key,
                }
            )
    return rows
