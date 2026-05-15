"""Application layer for the bounded 1C/Soykasoft WB stocks source."""

from __future__ import annotations

from typing import Any, Mapping

from packages.adapters.onec_stocks_block import OnecStocksSource
from packages.contracts.onec_stocks_block import (
    ALLOWED_ONEC_CANONICAL_STAGE_CODES,
    OnecCanonicalStageCode,
    OnecStocksEmpty,
    OnecStocksEnvelope,
    OnecStocksItem,
    OnecStocksMeta,
    OnecStocksNormalizedStage,
    OnecStocksParsedPayload,
    OnecStocksRequest,
    OnecStocksStage,
    OnecStocksSuccess,
)


def parse_onec_stocks_payload(payload: Mapping[str, Any]) -> OnecStocksParsedPayload:
    """Parse the raw 1C response without assuming a fixed stage enum."""

    meta_payload = _require_mapping(payload, "meta")
    meta = OnecStocksMeta(
        version=_require_str(meta_payload, "version"),
        marketplace=_require_str(meta_payload, "marketplace"),
        account_id=_require_str(meta_payload, "account_id"),
        date=_require_str(meta_payload, "date"),
        generated_at=_require_str(meta_payload, "generated_at"),
        currency=_require_str(meta_payload, "currency"),
    )

    raw_items = _require_list(payload, "items")
    items: list[OnecStocksItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise ValueError("items[] must contain objects")
        stages_payload = _require_mapping(raw_item, "stages")
        stages: dict[str, OnecStocksStage] = {}
        for stage_name, stage_payload in stages_payload.items():
            if not isinstance(stage_name, str) or not stage_name.strip():
                raise ValueError("stage name must be a non-empty string")
            if not isinstance(stage_payload, Mapping):
                raise ValueError(f"stage {stage_name!r} must be an object")
            normalized_stage_name = stage_name.strip()
            stages[normalized_stage_name] = OnecStocksStage(
                stage_name=normalized_stage_name,
                qty=_require_number(stage_payload, "qty"),
                unit_cost_rub=_require_number(stage_payload, "unit_cost_rub"),
                cost_total_rub=_require_number(stage_payload, "cost_total_rub"),
            )

        sizes = raw_item.get("sizes", [])
        if sizes is None:
            sizes = []
        if not isinstance(sizes, list) or not all(isinstance(item, Mapping) for item in sizes):
            raise ValueError("items[].sizes must be a list of objects when present")

        items.append(
            OnecStocksItem(
                nm_id=_require_str_or_int(raw_item, "nmId"),
                product_1c_id=_require_str(raw_item, "product_1c_id"),
                vendor_code=_require_str(raw_item, "vendor_code"),
                name=_require_str(raw_item, "name"),
                stages=stages,
                sizes=[dict(item) for item in sizes],
            )
        )

    return OnecStocksParsedPayload(meta=meta, items=items)


def normalize_onec_stocks_payload(
    payload: Mapping[str, Any],
    *,
    stage_mapping: Mapping[str, OnecCanonicalStageCode | str] | None = None,
) -> OnecStocksEnvelope:
    """Flatten parsed items to stage rows while preserving dynamic 1C stage names."""

    parsed = parse_onec_stocks_payload(payload)
    normalized_mapping = _normalize_stage_mapping(stage_mapping)
    rows: list[OnecStocksNormalizedStage] = []
    dynamic_stage_names: list[str] = []
    seen_stage_names: set[str] = set()

    for item in parsed.items:
        nm_id = _parse_nm_id(item.nm_id)
        for stage_name, stage in item.stages.items():
            if stage_name not in seen_stage_names:
                seen_stage_names.add(stage_name)
                dynamic_stage_names.append(stage_name)
            rows.append(
                OnecStocksNormalizedStage(
                    account_id=parsed.meta.account_id,
                    date=parsed.meta.date,
                    generated_at=parsed.meta.generated_at,
                    currency=parsed.meta.currency,
                    nm_id=nm_id,
                    source_nm_id=item.nm_id,
                    product_1c_id=item.product_1c_id,
                    vendor_code=item.vendor_code,
                    name=item.name,
                    stage_name=stage.stage_name,
                    canonical_stage_code=normalized_mapping.get(stage_name),
                    qty=stage.qty,
                    unit_cost_rub=stage.unit_cost_rub,
                    cost_total_rub=stage.cost_total_rub,
                )
            )

    if not parsed.items:
        return OnecStocksEnvelope(
            result=OnecStocksEmpty(
                kind="empty",
                meta=parsed.meta,
                item_count=0,
                stage_count=0,
                dynamic_stage_names=[],
                items=[],
                detail="1C stocks response contains no items",
            )
        )

    return OnecStocksEnvelope(
        result=OnecStocksSuccess(
            kind="success",
            meta=parsed.meta,
            item_count=len(parsed.items),
            stage_count=len(rows),
            dynamic_stage_names=dynamic_stage_names,
            items=rows,
        )
    )


def _normalize_stage_mapping(
    stage_mapping: Mapping[str, OnecCanonicalStageCode | str] | None,
) -> dict[str, OnecCanonicalStageCode]:
    if stage_mapping is None:
        return {}

    normalized: dict[str, OnecCanonicalStageCode] = {}
    allowed_codes = set(ALLOWED_ONEC_CANONICAL_STAGE_CODES)
    for source_stage_name, canonical_code in stage_mapping.items():
        if not isinstance(source_stage_name, str) or not source_stage_name.strip():
            raise ValueError("stage mapping source_stage_name must be a non-empty string")
        if canonical_code not in allowed_codes:
            raise ValueError(f"unsupported 1C canonical stage code: {canonical_code!r}")
        normalized[source_stage_name.strip()] = canonical_code  # type: ignore[assignment]
    return normalized


def _require_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be object")
    return value


def _require_list(payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be list")
    return value


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string")
    return value


def _require_str_or_int(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be string or int")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"{key} must be string or int")


def _require_number(payload: Mapping[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    return float(value)


def _parse_nm_id(value: str) -> int:
    if not value.isdigit():
        raise ValueError(f"nmId must contain only digits for normalized output: {value!r}")
    return int(value)


class OnecStocksBlock:
    """Application slice for parser + normalization over a 1C stocks source."""

    def __init__(
        self,
        source: OnecStocksSource,
        *,
        stage_mapping: Mapping[str, OnecCanonicalStageCode | str] | None = None,
    ) -> None:
        self._source = source
        self._stage_mapping = stage_mapping

    def execute(self, request: OnecStocksRequest) -> OnecStocksEnvelope:
        payload = self._source.fetch(request)
        return normalize_onec_stocks_payload(payload, stage_mapping=self._stage_mapping)
