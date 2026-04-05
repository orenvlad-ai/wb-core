"""Application-слой блока web-source snapshot."""

from typing import Any, Mapping

from packages.adapters.web_source_snapshot_block import WebSourceSnapshotSource
from packages.contracts.web_source_snapshot_block import (
    WebSourceSnapshotEnvelope,
    WebSourceSnapshotItem,
    WebSourceSnapshotNotFound,
    WebSourceSnapshotRequest,
    WebSourceSnapshotSuccess,
)


def transform_legacy_payload(payload: Mapping[str, Any]) -> WebSourceSnapshotEnvelope:
    """Преобразует legacy payload в target contract shape."""

    if "detail" in payload:
        detail = payload["detail"]
        if not isinstance(detail, str):
            raise ValueError("legacy not-found payload must contain string detail")
        return WebSourceSnapshotEnvelope(
            result=WebSourceSnapshotNotFound(kind="not_found", detail=detail)
        )

    items_raw = payload.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("legacy success payload must contain items list")

    items = [_build_item(item) for item in items_raw]
    return WebSourceSnapshotEnvelope(
        result=WebSourceSnapshotSuccess(
            kind="success",
            date_from=_require_str(payload, "date_from"),
            date_to=_require_str(payload, "date_to"),
            count=_require_int(payload, "count"),
            items=items,
        )
    )


def _build_item(item: Mapping[str, Any]) -> WebSourceSnapshotItem:
    return WebSourceSnapshotItem(
        nm_id=_require_int(item, "nm_id"),
        views_current=_require_int(item, "views_current"),
        ctr_current=_require_int(item, "ctr_current"),
        orders_current=_require_int(item, "orders_current"),
        position_avg=_require_int(item, "position_avg"),
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


class WebSourceSnapshotBlock:
    """Минимальный application-slice для чистой трансформации."""

    def __init__(self, source: WebSourceSnapshotSource) -> None:
        self._source = source

    def execute(self, request: WebSourceSnapshotRequest) -> WebSourceSnapshotEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
