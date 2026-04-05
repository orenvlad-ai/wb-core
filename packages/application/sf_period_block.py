"""Application-слой блока sf period."""

from typing import Any, Mapping

from packages.adapters.sf_period_block import SfPeriodSource
from packages.contracts.sf_period_block import (
    SfPeriodEnvelope,
    SfPeriodItem,
    SfPeriodRequest,
    SfPeriodSuccess,
)


def transform_legacy_payload(payload: Mapping[str, Any]) -> SfPeriodEnvelope:
    """Преобразует legacy payload в target contract shape."""

    snapshot_date = _require_str(payload, "snapshot_date")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    products = data.get("products")
    if not isinstance(products, list):
        raise ValueError("legacy payload must contain data.products list")

    items = [_build_item(product) for product in products]
    return SfPeriodEnvelope(
        result=SfPeriodSuccess(
            kind="success",
            snapshot_date=snapshot_date,
            count=len(items),
            items=items,
        )
    )


def _build_item(product_row: Mapping[str, Any]) -> SfPeriodItem:
    product = product_row.get("product")
    if not isinstance(product, Mapping):
        raise ValueError("product row must contain product object")

    statistic = product_row.get("statistic")
    if not isinstance(statistic, Mapping):
        raise ValueError("product row must contain statistic object")

    selected = statistic.get("selected")
    if not isinstance(selected, Mapping):
        raise ValueError("statistic must contain selected object")

    return SfPeriodItem(
        nm_id=_require_int(product, "nmId"),
        localization_percent=_require_int(selected, "localizationPercent"),
        feedback_rating=_require_float(product, "feedbackRating"),
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


class SfPeriodBlock:
    """Минимальный application-slice для sf period."""

    def __init__(self, source: SfPeriodSource) -> None:
        self._source = source

    def execute(self, request: SfPeriodRequest) -> SfPeriodEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
