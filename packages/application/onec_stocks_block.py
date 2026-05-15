"""Application layer for the bounded 1C/Soykasoft WB stocks source."""

from __future__ import annotations

from typing import Any, Mapping

from packages.adapters.onec_stocks_block import OnecStocksSource
from packages.contracts.onec_stocks_block import (
    ALLOWED_ONEC_CANONICAL_STAGE_CODES,
    ONEC_STOCKS_PARTIAL_FETCH_META_KEY,
    OnecCanonicalStageCode,
    OnecStocksEmpty,
    OnecStocksEnvelope,
    OnecStocksIncomplete,
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
        envelope = normalize_onec_stocks_payload(payload, stage_mapping=self._stage_mapping)
        partial_meta = _partial_fetch_metadata(payload)
        if partial_meta is None or envelope.result.kind != "success":
            return envelope

        covered_nm_ids = sorted({row.nm_id for row in envelope.result.items})
        requested_nm_ids = _partial_requested_nm_ids(
            request_nm_ids=request.nm_ids,
            partial_meta=partial_meta,
        )
        missing_nm_ids = sorted(
            set(_partial_missing_nm_ids(partial_meta)) | (set(requested_nm_ids) - set(covered_nm_ids))
        )
        requested_count = _positive_int(
            partial_meta.get("requested_count"),
            fallback=len(requested_nm_ids),
        )
        status_codes = _format_partial_status_codes(partial_meta.get("status_codes"))
        detail_parts = [
            "1C stocks current snapshot loaded partially",
            f"requested_count={requested_count}",
            f"covered_count={len(covered_nm_ids)}",
            f"missing_count={len(missing_nm_ids)}",
        ]
        if status_codes:
            detail_parts.append(f"status_codes={status_codes}")
        return OnecStocksEnvelope(
            result=OnecStocksIncomplete(
                kind="incomplete",
                meta=envelope.result.meta,
                item_count=envelope.result.item_count,
                stage_count=envelope.result.stage_count,
                dynamic_stage_names=envelope.result.dynamic_stage_names,
                items=envelope.result.items,
                requested_count=requested_count,
                covered_count=len(covered_nm_ids),
                missing_nm_ids=missing_nm_ids,
                detail="; ".join(detail_parts),
            )
        )


def _partial_fetch_metadata(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = payload.get(ONEC_STOCKS_PARTIAL_FETCH_META_KEY)
    return raw if isinstance(raw, Mapping) else None


def _partial_requested_nm_ids(
    *,
    request_nm_ids: list[int],
    partial_meta: Mapping[str, Any],
) -> list[int]:
    raw = partial_meta.get("requested_nm_ids")
    if isinstance(raw, list):
        parsed = _int_list(raw)
        if parsed:
            return parsed
    return sorted({int(item) for item in request_nm_ids})


def _partial_missing_nm_ids(partial_meta: Mapping[str, Any]) -> list[int]:
    raw = partial_meta.get("missing_nm_ids")
    return _int_list(raw) if isinstance(raw, list) else []


def _int_list(raw_items: list[Any]) -> list[int]:
    result: list[int] = []
    for item in raw_items:
        if isinstance(item, bool):
            continue
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        result.append(value)
    return sorted(set(result))


def _positive_int(value: Any, *, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _format_partial_status_codes(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    parts: list[str] = []
    for status_code, count in sorted(value.items(), key=lambda item: str(item[0])):
        code = str(status_code).strip()
        if not code:
            continue
        try:
            parsed_count = int(count)
        except (TypeError, ValueError):
            continue
        if parsed_count > 0:
            parts.append(f"{code}:{parsed_count}")
    return ",".join(parts)
