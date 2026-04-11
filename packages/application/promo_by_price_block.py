"""Application-слой блока promo by price."""

from typing import Any, Mapping

from packages.adapters.promo_by_price_block import PromoByPriceSource
from packages.contracts.promo_by_price_block import (
    PromoByPriceEmpty,
    PromoByPriceEnvelope,
    PromoByPriceItem,
    PromoByPriceRequest,
    PromoByPriceSuccess,
)


def transform_legacy_payload(payload: Mapping[str, Any]) -> PromoByPriceEnvelope:
    date_from = _require_str(payload, "date_from")
    date_to = _require_str(payload, "date_to")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("legacy payload must contain data.rows list")

    items: list[PromoByPriceItem] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("legacy row must be object")
        items.append(
            PromoByPriceItem(
                date=_require_str(row, "date"),
                nm_id=_require_int(row, "nmId"),
                promo_count_by_price=_require_float(row, "promo_count_by_price"),
                promo_entry_price_best=_require_float(row, "promo_entry_price_best"),
                promo_participation=_require_float(row, "promo_participation"),
            )
        )

    items.sort(key=lambda item: (item.date, item.nm_id))

    if not items:
        return PromoByPriceEnvelope(
            result=PromoByPriceEmpty(
                kind="empty",
                date_from=date_from,
                date_to=date_to,
                count=0,
                items=[],
                detail="no promo rows returned for requested nmIds",
            )
        )

    return PromoByPriceEnvelope(
        result=PromoByPriceSuccess(
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


class PromoByPriceBlock:
    def __init__(self, source: PromoByPriceSource) -> None:
        self._source = source

    def execute(self, request: PromoByPriceRequest) -> PromoByPriceEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
