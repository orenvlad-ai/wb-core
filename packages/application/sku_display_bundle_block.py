"""Application-слой блока sku display bundle."""

from typing import Any, Mapping

from packages.adapters.sku_display_bundle_block import SkuDisplayBundleSource
from packages.contracts.sku_display_bundle_block import (
    SkuDisplayBundleEmpty,
    SkuDisplayBundleEnvelope,
    SkuDisplayBundleItem,
    SkuDisplayBundleRequest,
    SkuDisplayBundleSuccess,
)


def transform_legacy_payload(payload: Mapping[str, Any]) -> SkuDisplayBundleEnvelope:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("legacy payload must contain data.rows list")

    items: list[SkuDisplayBundleItem] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ValueError("legacy row must be object")
        items.append(
            SkuDisplayBundleItem(
                nm_id=_require_int(row, "sku"),
                display_name=_require_non_empty_str(row, "comment"),
                group=_require_non_empty_str(row, "group"),
                enabled=_require_bool(row, "active"),
                display_order=index,
            )
        )

    if not items:
        return SkuDisplayBundleEnvelope(
            result=SkuDisplayBundleEmpty(
                kind="empty",
                count=0,
                items=[],
                detail="no sku display rows available",
            )
        )

    return SkuDisplayBundleEnvelope(
        result=SkuDisplayBundleSuccess(
            kind="success",
            count=len(items),
            items=items,
        )
    )


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{key} must be int")
    return value


def _require_non_empty_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be non-empty string")
    return value.strip()


def _require_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be bool")
    return value


class SkuDisplayBundleBlock:
    def __init__(self, source: SkuDisplayBundleSource) -> None:
        self._source = source

    def execute(self, request: SkuDisplayBundleRequest) -> SkuDisplayBundleEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
