"""Application-слой блока seller funnel snapshot."""

from typing import Any, Mapping

from packages.adapters.seller_funnel_snapshot_block import SellerFunnelSnapshotSource
from packages.contracts.seller_funnel_snapshot_block import (
    SellerFunnelSnapshotEnvelope,
    SellerFunnelSnapshotItem,
    SellerFunnelSnapshotNotFound,
    SellerFunnelSnapshotRequest,
    SellerFunnelSnapshotSuccess,
)


def transform_legacy_payload(payload: Mapping[str, Any]) -> SellerFunnelSnapshotEnvelope:
    """Преобразует legacy payload в target contract shape."""

    if "detail" in payload:
        detail = payload["detail"]
        if not isinstance(detail, str):
            raise ValueError("legacy not-found payload must contain string detail")
        return SellerFunnelSnapshotEnvelope(
            result=SellerFunnelSnapshotNotFound(kind="not_found", detail=detail)
        )

    items_raw = payload.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("legacy success payload must contain items list")

    items = [_build_item(item) for item in items_raw]
    return SellerFunnelSnapshotEnvelope(
        result=SellerFunnelSnapshotSuccess(
            kind="success",
            date=_require_str(payload, "date"),
            count=_require_int(payload, "count"),
            items=items,
        )
    )


def _build_item(item: Mapping[str, Any]) -> SellerFunnelSnapshotItem:
    return SellerFunnelSnapshotItem(
        nm_id=_require_int(item, "nm_id"),
        name=_require_str(item, "name"),
        vendor_code=_require_str(item, "vendor_code"),
        view_count=_require_int(item, "view_count"),
        open_card_count=_require_int(item, "open_card_count"),
        ctr=_require_int(item, "ctr"),
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


class SellerFunnelSnapshotBlock:
    """Минимальный application-slice для seller funnel snapshot."""

    def __init__(self, source: SellerFunnelSnapshotSource) -> None:
        self._source = source

    def execute(self, request: SellerFunnelSnapshotRequest) -> SellerFunnelSnapshotEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)

