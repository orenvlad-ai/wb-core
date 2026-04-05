"""Application-слой блока ads bids."""

from collections import defaultdict
from typing import Any, Mapping

from packages.adapters.ads_bids_block import AdsBidsSource
from packages.contracts.ads_bids_block import (
    AdsBidsEmpty,
    AdsBidsEnvelope,
    AdsBidsItem,
    AdsBidsRequest,
    AdsBidsSuccess,
)


def transform_legacy_payload(payload: Mapping[str, Any]) -> AdsBidsEnvelope:
    """Преобразует legacy payload в target contract shape."""

    snapshot_date = _require_str(payload, "snapshot_date")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("legacy payload must contain data.rows list")

    normalized_rows = [row for row in rows if isinstance(row, Mapping)]
    if not normalized_rows:
        return AdsBidsEnvelope(
            result=AdsBidsEmpty(
                kind="empty",
                snapshot_date=snapshot_date,
                count=0,
                items=[],
                detail="no active bid rows returned for requested nmIds",
            )
        )

    latest_fetched_at = max(_require_str(row, "fetched_at") for row in normalized_rows)
    max_by_nm: dict[int, dict[str, float]] = defaultdict(
        lambda: {"search": 0.0, "recommendations": 0.0}
    )

    for row in normalized_rows:
        if _require_str(row, "fetched_at") != latest_fetched_at:
            continue
        if _require_str(row, "snapshot_date") != snapshot_date:
            continue

        nm_id = _require_int(row, "nmId")
        placement = _require_str(row, "placement")
        bid_rub = _require_float(row, "bid_kopecks") / 100.0

        if placement == "search":
            max_by_nm[nm_id]["search"] = max(max_by_nm[nm_id]["search"], bid_rub)
        elif placement == "recommendations":
            max_by_nm[nm_id]["recommendations"] = max(
                max_by_nm[nm_id]["recommendations"], bid_rub
            )

    if not max_by_nm:
        return AdsBidsEnvelope(
            result=AdsBidsEmpty(
                kind="empty",
                snapshot_date=snapshot_date,
                count=0,
                items=[],
                detail="no active bid rows returned for requested nmIds",
            )
        )

    items = [
        AdsBidsItem(
            nm_id=nm_id,
            ads_bid_search=max_by_nm[nm_id]["search"],
            ads_bid_recommendations=max_by_nm[nm_id]["recommendations"],
        )
        for nm_id in sorted(max_by_nm.keys())
    ]
    return AdsBidsEnvelope(
        result=AdsBidsSuccess(
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


class AdsBidsBlock:
    """Минимальный application-slice для ads bids."""

    def __init__(self, source: AdsBidsSource) -> None:
        self._source = source

    def execute(self, request: AdsBidsRequest) -> AdsBidsEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
