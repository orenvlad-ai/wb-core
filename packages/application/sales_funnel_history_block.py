"""Application-слой блока sales funnel history."""

from typing import Any, Mapping

from packages.adapters.sales_funnel_history_block import SalesFunnelHistorySource
from packages.contracts.sales_funnel_history_block import (
    SalesFunnelHistoryEmpty,
    SalesFunnelHistoryEnvelope,
    SalesFunnelHistoryItem,
    SalesFunnelHistoryRequest,
    SalesFunnelHistorySuccess,
)


PERCENT_METRICS = {"addToCartConversion", "cartToOrderConversion", "buyoutPercent"}


def transform_legacy_payload(payload: Mapping[str, Any]) -> SalesFunnelHistoryEnvelope:
    date_from = _require_str(payload, "date_from")
    date_to = _require_str(payload, "date_to")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("legacy payload must contain data.rows list")

    latest: dict[tuple[str, int, str], tuple[str, float]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 5:
            raise ValueError("legacy row must be 5-item list")
        fetched_at, date, nm_id, metric, value = row
        if not isinstance(fetched_at, str) or not isinstance(date, str) or not isinstance(metric, str):
            raise ValueError("legacy row fields fetched_at/date/metric must be strings")
        if not isinstance(nm_id, int):
            raise ValueError("legacy row nm_id must be int")
        if not isinstance(value, (int, float)):
            raise ValueError("legacy row value must be numeric")
        key = (date, nm_id, metric)
        numeric = float(value) / 100.0 if metric in PERCENT_METRICS else float(value)
        prev = latest.get(key)
        if prev is None or fetched_at > prev[0]:
            latest[key] = (fetched_at, numeric)

    if not latest:
        return SalesFunnelHistoryEnvelope(
            result=SalesFunnelHistoryEmpty(
                kind="empty",
                date_from=date_from,
                date_to=date_to,
                count=0,
                items=[],
                detail="no history rows returned for requested nmIds",
            )
        )

    items = [
        SalesFunnelHistoryItem(date=date, nm_id=nm_id, metric=metric, value=value)
        for (date, nm_id, metric), (_, value) in sorted(latest.items())
    ]
    return SalesFunnelHistoryEnvelope(
        result=SalesFunnelHistorySuccess(
            kind="success",
            date_from=date_from,
            date_to=date_to,
            count=len(items),
            items=items,
        )
    )


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string")
    return value


class SalesFunnelHistoryBlock:
    def __init__(self, source: SalesFunnelHistorySource) -> None:
        self._source = source

    def execute(self, request: SalesFunnelHistoryRequest) -> SalesFunnelHistoryEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
