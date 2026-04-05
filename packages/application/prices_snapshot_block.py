"""Application-слой блока prices snapshot."""

from typing import Any, Mapping

from packages.adapters.prices_snapshot_block import PricesSnapshotSource
from packages.contracts.prices_snapshot_block import (
    PricesSnapshotEmpty,
    PricesSnapshotEnvelope,
    PricesSnapshotItem,
    PricesSnapshotRequest,
    PricesSnapshotSuccess,
)


def transform_legacy_payload(payload: Mapping[str, Any]) -> PricesSnapshotEnvelope:
    """Преобразует legacy payload в target contract shape."""

    snapshot_date = _require_str(payload, "snapshot_date")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    goods = data.get("listGoods")
    if not isinstance(goods, list):
        raise ValueError("legacy payload must contain data.listGoods list")

    if not goods:
        return PricesSnapshotEnvelope(
            result=PricesSnapshotEmpty(
                kind="empty",
                snapshot_date=snapshot_date,
                count=0,
                items=[],
                detail="no goods returned for requested nmIds",
            )
        )

    items = [_build_item(good) for good in goods]
    return PricesSnapshotEnvelope(
        result=PricesSnapshotSuccess(
            kind="success",
            snapshot_date=snapshot_date,
            count=len(items),
            items=items,
        )
    )


def _build_item(good: Mapping[str, Any]) -> PricesSnapshotItem:
    nm_id = _require_int(good, "nmID")
    sizes = good.get("sizes")
    if not isinstance(sizes, list):
        raise ValueError("good.sizes must be list")

    if not sizes:
        return PricesSnapshotItem(
            nm_id=nm_id,
            price_seller=0,
            price_seller_discounted=0,
        )

    min_price = None
    min_discounted = None
    for size in sizes:
        if not isinstance(size, Mapping):
            raise ValueError("size item must be object")
        price = _to_int(size.get("price"))
        discounted_price = _to_int(size.get("discountedPrice"))
        min_price = price if min_price is None else min(min_price, price)
        min_discounted = (
            discounted_price
            if min_discounted is None
            else min(min_discounted, discounted_price)
        )

    return PricesSnapshotItem(
        nm_id=nm_id,
        price_seller=min_price if min_price is not None else 0,
        price_seller_discounted=min_discounted if min_discounted is not None else 0,
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


def _to_int(value: Any) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return 0
    return numeric


class PricesSnapshotBlock:
    """Минимальный application-slice для prices snapshot."""

    def __init__(self, source: PricesSnapshotSource) -> None:
        self._source = source

    def execute(self, request: PricesSnapshotRequest) -> PricesSnapshotEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
