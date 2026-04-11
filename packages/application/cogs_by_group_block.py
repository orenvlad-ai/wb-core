"""Application-слой блока cogs by group."""

from typing import Any, Mapping

from packages.adapters.cogs_by_group_block import CogsByGroupSource
from packages.contracts.cogs_by_group_block import (
    CogsByGroupEmpty,
    CogsByGroupEnvelope,
    CogsByGroupItem,
    CogsByGroupRequest,
    CogsByGroupSuccess,
)


def transform_legacy_payload(payload: Mapping[str, Any]) -> CogsByGroupEnvelope:
    date_from = _require_str(payload, "date_from")
    date_to = _require_str(payload, "date_to")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("legacy payload must contain data.rows list")

    items: list[CogsByGroupItem] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("legacy row must be object")
        items.append(
            CogsByGroupItem(
                date=_require_str(row, "date"),
                nm_id=_require_int(row, "nmId"),
                cost_price_rub=_require_float(row, "cost_price_rub"),
            )
        )

    items.sort(key=lambda item: (item.date, item.nm_id))

    if not items:
        return CogsByGroupEnvelope(
            result=CogsByGroupEmpty(
                kind="empty",
                date_from=date_from,
                date_to=date_to,
                count=0,
                items=[],
                detail="no cogs rows returned for requested nmIds",
            )
        )

    return CogsByGroupEnvelope(
        result=CogsByGroupSuccess(
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


class CogsByGroupBlock:
    def __init__(self, source: CogsByGroupSource) -> None:
        self._source = source

    def execute(self, request: CogsByGroupRequest) -> CogsByGroupEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
