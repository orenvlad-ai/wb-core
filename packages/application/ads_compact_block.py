"""Application-слой блока ads compact."""

from collections import defaultdict
from typing import Any, Mapping

from packages.adapters.ads_compact_block import AdsCompactSource
from packages.contracts.ads_compact_block import (
    AdsCompactEmpty,
    AdsCompactEnvelope,
    AdsCompactItem,
    AdsCompactRequest,
    AdsCompactSuccess,
)


def transform_legacy_payload(payload: Mapping[str, Any]) -> AdsCompactEnvelope:
    snapshot_date = _require_str(payload, "snapshot_date")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("legacy payload must contain data.rows list")

    grouped: dict[int, dict[str, float]] = defaultdict(
        lambda: {
            "ads_views": 0.0,
            "ads_clicks": 0.0,
            "ads_atbs": 0.0,
            "ads_orders": 0.0,
            "ads_sum": 0.0,
            "ads_sum_price": 0.0,
        }
    )

    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("legacy row must be object")
        if _require_str(row, "snapshot_date") != snapshot_date:
            continue

        nm_id = _require_int(row, "nmId")
        acc = grouped[nm_id]
        acc["ads_views"] += _require_float(row, "ads_views")
        acc["ads_clicks"] += _require_float(row, "ads_clicks")
        acc["ads_atbs"] += _require_float(row, "ads_atbs")
        acc["ads_orders"] += _require_float(row, "ads_orders")
        acc["ads_sum"] += _require_float(row, "ads_sum")
        acc["ads_sum_price"] += _require_float(row, "ads_sum_price")

    if not grouped:
        return AdsCompactEnvelope(
            result=AdsCompactEmpty(
                kind="empty",
                snapshot_date=snapshot_date,
                count=0,
                items=[],
                detail="no compact ads rows returned for requested nmIds",
            )
        )

    items = []
    for nm_id in sorted(grouped):
        rec = grouped[nm_id]
        ads_clicks = rec["ads_clicks"]
        ads_views = rec["ads_views"]
        items.append(
            AdsCompactItem(
                nm_id=nm_id,
                ads_views=ads_views,
                ads_clicks=ads_clicks,
                ads_atbs=rec["ads_atbs"],
                ads_orders=rec["ads_orders"],
                ads_sum=rec["ads_sum"],
                ads_sum_price=rec["ads_sum_price"],
                ads_cpc=rec["ads_sum"] / ads_clicks if ads_clicks > 0 else 0.0,
                ads_ctr=ads_clicks / ads_views if ads_views > 0 else 0.0,
                ads_cr=rec["ads_orders"] / ads_clicks if ads_clicks > 0 else 0.0,
            )
        )

    return AdsCompactEnvelope(
        result=AdsCompactSuccess(
            kind="success",
            snapshot_date=snapshot_date,
            count=len(items),
            items=items,
        )
    )


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


def _require_float(payload: Mapping[str, Any], key: str) -> float:
    value = payload.get(key)
    if not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    return float(value)


class AdsCompactBlock:
    def __init__(self, source: AdsCompactSource) -> None:
        self._source = source

    def execute(self, request: AdsCompactRequest) -> AdsCompactEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
