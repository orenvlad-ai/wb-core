"""Application-слой блока spp."""

from typing import Any, Mapping

from packages.adapters.spp_block import SppSource
from packages.contracts.spp_block import SppEmpty, SppEnvelope, SppItem, SppRequest, SppSuccess


def transform_legacy_payload(payload: Mapping[str, Any]) -> SppEnvelope:
    """Преобразует legacy payload в target contract shape."""

    snapshot_date = _require_str(payload, "snapshot_date")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    items_raw = data.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("legacy payload must contain data.items list")

    if not items_raw:
        return SppEnvelope(
            result=SppEmpty(
                kind="empty",
                snapshot_date=snapshot_date,
                count=0,
                items=[],
                detail="no sales rows returned for requested nmIds and snapshot date",
            )
        )

    items = [_build_item(item) for item in items_raw]
    return SppEnvelope(
        result=SppSuccess(
            kind="success",
            snapshot_date=snapshot_date,
            count=len(items),
            items=items,
        )
    )


def _build_item(item: Mapping[str, Any]) -> SppItem:
    return SppItem(
        nm_id=_require_int(item, "nmId"),
        spp=_require_float(item, "spp_avg"),
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


class SppBlock:
    """Минимальный application-slice для spp."""

    def __init__(self, source: SppSource) -> None:
        self._source = source

    def execute(self, request: SppRequest) -> SppEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
