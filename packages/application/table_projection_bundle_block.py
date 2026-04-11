"""Application-слой блока table projection bundle."""

from typing import Any, Mapping, Optional

from packages.adapters.table_projection_bundle_block import TableProjectionBundleSource
from packages.contracts.table_projection_bundle_block import (
    TableProjectionBundleEmpty,
    TableProjectionBundleEnvelope,
    TableProjectionBundleItem,
    TableProjectionBundleRequest,
    TableProjectionBundleSuccess,
    TableProjectionSourceStatus,
)


def transform_input_bundle(payload: Mapping[str, Any]) -> TableProjectionBundleEnvelope:
    upstream = payload.get("upstream")
    if not isinstance(upstream, Mapping):
        raise ValueError("projection payload must contain upstream object")

    sku_bundle = _require_mapping(upstream, "sku_display_bundle")
    sku_result = _require_mapping(sku_bundle, "result")
    sku_status = _build_source_status("sku_display_bundle", sku_result, [])
    sku_items = _extract_sku_items(sku_result)

    if not sku_items:
        return TableProjectionBundleEnvelope(
            result=TableProjectionBundleEmpty(
                kind="empty",
                as_of_date=None,
                count=0,
                items=[],
                source_statuses=[sku_status],
                detail="no sku rows available for projection",
            )
        )

    requested_nm_ids = [item["nm_id"] for item in sku_items]
    source_statuses = [sku_status]

    source_keys = [
        "web_source_snapshot",
        "seller_funnel_snapshot",
        "prices_snapshot",
        "sf_period",
        "spp",
        "ads_bids",
        "stocks",
        "ads_compact",
        "fin_report_daily",
        "sales_funnel_history",
    ]

    source_results: dict[str, Mapping[str, Any]] = {}
    for source_key in source_keys:
        bundle = upstream.get(source_key)
        if not isinstance(bundle, Mapping):
            continue
        result = bundle.get("result")
        if not isinstance(result, Mapping):
            continue
        source_results[source_key] = result
        source_statuses.append(_build_source_status(source_key, result, requested_nm_ids))

    search_lookup = _index_by_nm_id(source_results.get("web_source_snapshot"))
    funnel_lookup = _index_by_nm_id(source_results.get("seller_funnel_snapshot"))
    prices_lookup = _index_by_nm_id(source_results.get("prices_snapshot"))
    sf_period_lookup = _index_by_nm_id(source_results.get("sf_period"))
    spp_lookup = _index_by_nm_id(source_results.get("spp"))
    ads_bids_lookup = _index_by_nm_id(source_results.get("ads_bids"))
    stocks_lookup = _index_by_nm_id(source_results.get("stocks"))
    ads_compact_lookup = _index_by_nm_id(source_results.get("ads_compact"))
    fin_lookup = _index_by_nm_id(source_results.get("fin_report_daily"))
    history_lookup = _group_history_by_nm_id(source_results.get("sales_funnel_history"))

    items: list[TableProjectionBundleItem] = []
    for sku in sku_items:
        nm_id = sku["nm_id"]
        items.append(
            TableProjectionBundleItem(
                nm_id=nm_id,
                display_name=sku["display_name"],
                group=sku["group"],
                enabled=sku["enabled"],
                display_order=sku["display_order"],
                web_source={
                    "search_analytics": _build_source_item_summary(
                        source_results.get("web_source_snapshot"),
                        search_lookup.get(nm_id),
                        "date_from",
                        "date_to",
                        ["views_current", "ctr_current", "orders_current", "position_avg"],
                    ),
                    "seller_funnel_daily": _build_source_item_summary(
                        source_results.get("seller_funnel_snapshot"),
                        funnel_lookup.get(nm_id),
                        "date",
                        None,
                        ["view_count", "open_card_count", "ctr"],
                    ),
                },
                official_api={
                    "prices": _build_source_item_summary(
                        source_results.get("prices_snapshot"),
                        prices_lookup.get(nm_id),
                        "snapshot_date",
                        None,
                        ["price_seller", "price_seller_discounted"],
                    ),
                    "sf_period": _build_source_item_summary(
                        source_results.get("sf_period"),
                        sf_period_lookup.get(nm_id),
                        "snapshot_date",
                        None,
                        ["localization_percent", "feedback_rating"],
                    ),
                    "spp": _build_source_item_summary(
                        source_results.get("spp"),
                        spp_lookup.get(nm_id),
                        "snapshot_date",
                        None,
                        ["spp"],
                    ),
                    "ads_bids": _build_source_item_summary(
                        source_results.get("ads_bids"),
                        ads_bids_lookup.get(nm_id),
                        "snapshot_date",
                        None,
                        ["ads_bid_search", "ads_bid_recommendations"],
                    ),
                    "stocks": _build_source_item_summary(
                        source_results.get("stocks"),
                        stocks_lookup.get(nm_id),
                        "snapshot_date",
                        None,
                        ["stock_total"],
                    ),
                    "ads_compact": _build_source_item_summary(
                        source_results.get("ads_compact"),
                        ads_compact_lookup.get(nm_id),
                        "snapshot_date",
                        None,
                        ["ads_views", "ads_clicks", "ads_orders", "ads_sum_price"],
                    ),
                    "fin_report_daily": _build_source_item_summary(
                        source_results.get("fin_report_daily"),
                        fin_lookup.get(nm_id),
                        "snapshot_date",
                        None,
                        ["fin_storage_fee", "fin_commission", "fin_buyout_rub"],
                    ),
                },
                history_summary=_build_history_summary(source_results.get("sales_funnel_history"), history_lookup.get(nm_id)),
            )
        )

    return TableProjectionBundleEnvelope(
        result=TableProjectionBundleSuccess(
            kind="success",
            as_of_date=_resolve_as_of_date(source_statuses),
            count=len(items),
            items=items,
            source_statuses=source_statuses,
        )
    )


def _extract_sku_items(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = result.get("items")
    if not isinstance(items, list):
        raise ValueError("sku display result.items must be list")

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("sku display item must be object")
        out.append(
            {
                "nm_id": _require_int(item, "nm_id"),
                "display_name": _require_str(item, "display_name"),
                "group": _require_str(item, "group"),
                "enabled": _require_bool(item, "enabled"),
                "display_order": _require_int(item, "display_order"),
            }
        )
    out.sort(key=lambda row: row["display_order"])
    return out


def _build_source_status(
    source_key: str,
    result: Mapping[str, Any],
    requested_nm_ids: list[int],
) -> TableProjectionSourceStatus:
    items = result.get("items")
    covered_nm_ids: set[int] = set()
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            nm_id = item.get("nm_id")
            if isinstance(nm_id, int):
                covered_nm_ids.add(nm_id)

    requested = set(requested_nm_ids)
    if not requested and source_key == "sku_display_bundle":
        requested = covered_nm_ids

    extra: Optional[dict[str, Any]] = None
    storage_total = result.get("storage_total")
    if isinstance(storage_total, Mapping):
        fee_total = storage_total.get("fin_storage_fee_total")
        if isinstance(fee_total, (int, float)):
            extra = {"fin_storage_fee_total": float(fee_total)}

    return TableProjectionSourceStatus(
        source_key=source_key,
        kind=_require_str(result, "kind"),
        freshness=_resolve_freshness(result),
        requested_count=len(requested),
        covered_count=len(covered_nm_ids),
        missing_nm_ids=sorted(requested - covered_nm_ids),
        snapshot_date=_optional_str(result, "snapshot_date"),
        date=_optional_str(result, "date"),
        date_from=_optional_str(result, "date_from"),
        date_to=_optional_str(result, "date_to"),
        extra=extra,
    )


def _build_source_item_summary(
    result: Optional[Mapping[str, Any]],
    item: Optional[Mapping[str, Any]],
    primary_date_key: str,
    secondary_date_key: Optional[str],
    fields: list[str],
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "kind": "missing" if item is None else "present",
        "freshness": _resolve_freshness(result) if isinstance(result, Mapping) else None,
    }
    if isinstance(result, Mapping):
        primary_value = result.get(primary_date_key)
        if isinstance(primary_value, str):
            summary[primary_date_key] = primary_value
        if secondary_date_key:
            secondary_value = result.get(secondary_date_key)
            if isinstance(secondary_value, str):
                summary[secondary_date_key] = secondary_value
    if item is None:
        return summary

    for field in fields:
        value = item.get(field)
        if isinstance(value, (int, float)):
            summary[field] = float(value)
        elif isinstance(value, str):
            summary[field] = value
    return summary


def _build_history_summary(
    result: Optional[Mapping[str, Any]],
    items: Optional[list[Mapping[str, Any]]],
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "kind": "missing" if not items else "present",
        "freshness": _resolve_freshness(result) if isinstance(result, Mapping) else None,
        "date_from": _optional_str(result, "date_from") if isinstance(result, Mapping) else None,
        "date_to": _optional_str(result, "date_to") if isinstance(result, Mapping) else None,
    }
    if not items:
        summary["metric_count"] = 0
        summary["metrics_present"] = []
        return summary

    metrics = sorted({_require_str(item, "metric") for item in items})
    last_history_date = max(_require_str(item, "date") for item in items)
    summary["last_history_date"] = last_history_date
    summary["metric_count"] = len(metrics)
    summary["metrics_present"] = metrics
    return summary


def _group_history_by_nm_id(result: Optional[Mapping[str, Any]]) -> dict[int, list[Mapping[str, Any]]]:
    lookup: dict[int, list[Mapping[str, Any]]] = {}
    if not isinstance(result, Mapping):
        return lookup
    items = result.get("items")
    if not isinstance(items, list):
        return lookup
    for item in items:
        if not isinstance(item, Mapping):
            continue
        nm_id = item.get("nm_id")
        if not isinstance(nm_id, int):
            continue
        lookup.setdefault(nm_id, []).append(item)
    return lookup


def _index_by_nm_id(result: Optional[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    lookup: dict[int, Mapping[str, Any]] = {}
    if not isinstance(result, Mapping):
        return lookup
    items = result.get("items")
    if not isinstance(items, list):
        return lookup
    for item in items:
        if not isinstance(item, Mapping):
            continue
        nm_id = item.get("nm_id")
        if isinstance(nm_id, int):
            lookup[nm_id] = item
    return lookup


def _resolve_as_of_date(source_statuses: list[TableProjectionSourceStatus]) -> Optional[str]:
    values = [status.freshness for status in source_statuses if isinstance(status.freshness, str)]
    return max(values) if values else None


def _resolve_freshness(result: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not isinstance(result, Mapping):
        return None
    for key in ("snapshot_date", "date", "date_to"):
        value = result.get(key)
        if isinstance(value, str):
            return value
    return None


def _require_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be object")
    return value


def _optional_str(payload: Mapping[str, Any], key: str) -> Optional[str]:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string")
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{key} must be int")
    return value


def _require_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be bool")
    return value


class TableProjectionBundleBlock:
    def __init__(self, source: TableProjectionBundleSource) -> None:
        self._source = source

    def execute(self, request: TableProjectionBundleRequest) -> TableProjectionBundleEnvelope:
        payload = self._source.fetch(request)
        return transform_input_bundle(payload)
