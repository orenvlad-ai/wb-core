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
    AdsCompactPartial,
)
from packages.contracts.source_attempt_diagnostics import SourceAttemptError, unknown_diagnostics


def transform_legacy_payload(payload: Mapping[str, Any]) -> AdsCompactEnvelope:
    snapshot_date = _require_str(payload, "snapshot_date")
    source = payload.get("source")
    diagnostics = {**unknown_diagnostics("ads_compact"), **(dict(source) if isinstance(source, Mapping) else {})}
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
                diagnostics=diagnostics,
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
                ads_cpc=rec["ads_sum"] / ads_clicks if ads_clicks > 0 else (None if diagnostics.get("partial_observation_contract") else 0.0),
                ads_ctr=ads_clicks / ads_views if ads_views > 0 else (None if diagnostics.get("partial_observation_contract") else 0.0),
                ads_cr=rec["ads_orders"] / ads_clicks if ads_clicks > 0 else (None if diagnostics.get("partial_observation_contract") else 0.0),
            )
        )

    if diagnostics.get("partial_observation_contract") == "ads_partial_observed_v1":
        requested = payload.get("requested_nm_ids")
        if not isinstance(requested, list) or any(type(n) is not int or n <= 0 for n in requested):
            raise ValueError("partial Ads requires its exact requested scope")
        return AdsCompactEnvelope(result=AdsCompactPartial(
            kind="incomplete", snapshot_date=snapshot_date, count=len(items), items=items,
            requested_count=len(set(requested)), covered_count=len(items),
            missing_nm_ids=sorted(set(requested) - set(grouped)),
            detail="ads_partial_observed; unknown contributions are not zero; affected SKU scope is unknown",
            diagnostics=diagnostics,
        ))
    return AdsCompactEnvelope(
        result=AdsCompactSuccess(
            kind="success",
            snapshot_date=snapshot_date,
            count=len(items),
            items=items,
            diagnostics=diagnostics,
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
        try:
            return transform_legacy_payload(payload)
        except Exception as exc:
            diagnostics = payload.get("source")
            if not isinstance(diagnostics, Mapping):
                diagnostics = unknown_diagnostics("ads_compact")
            raise SourceAttemptError("ads_compact_transform_failed", diagnostics) from exc
