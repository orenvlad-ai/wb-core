"""Application-слой блока stocks."""

from collections import defaultdict
import re
from typing import Any, Mapping

from packages.adapters.stocks_block import StocksSource
from packages.contracts.stocks_block import (
    StocksEnvelope,
    StocksIncomplete,
    StocksItem,
    StocksRequest,
    StocksSuccess,
)


REGION_TO_FIELD = {
    "Центральный": "stock_ru_central",
    "Северо-Западный": "stock_ru_northwest",
    "Приволжский": "stock_ru_volga",
    "Уральский": "stock_ru_ural",
    "Дальневосточный и Сибирский": "stock_ru_far_siberia",
    "Южный и Северо-Кавказский": "stock_ru_south_caucasus",
}


def transform_legacy_payload(payload: Mapping[str, Any]) -> StocksEnvelope:
    """Преобразует legacy payload в target contract shape."""

    snapshot_date = _require_str(payload, "snapshot_date")
    requested_nm_ids = _require_int_list(payload, "requested_nm_ids")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("legacy payload must contain data.rows list")

    normalized_rows = [row for row in rows if isinstance(row, Mapping)]
    latest_ts_by_nm: dict[int, str] = {}
    aggregated: dict[int, dict[str, float]] = defaultdict(
        lambda: {
            "stock_total": 0.0,
            "stock_ru_central": 0.0,
            "stock_ru_northwest": 0.0,
            "stock_ru_volga": 0.0,
            "stock_ru_ural": 0.0,
            "stock_ru_south_caucasus": 0.0,
            "stock_ru_far_siberia": 0.0,
        }
    )
    unmapped_region_totals: dict[str, float] = defaultdict(float)

    for row in normalized_rows:
        if _require_str(row, "snapshot_date") != snapshot_date:
            continue

        nm_id = _require_int(row, "nmId")
        snapshot_ts = _require_str(row, "snapshot_ts")
        current = latest_ts_by_nm.get(nm_id)
        if current is None or snapshot_ts > current:
            latest_ts_by_nm[nm_id] = snapshot_ts
            aggregated[nm_id] = {
                "stock_total": 0.0,
                "stock_ru_central": 0.0,
                "stock_ru_northwest": 0.0,
                "stock_ru_volga": 0.0,
                "stock_ru_ural": 0.0,
                "stock_ru_south_caucasus": 0.0,
                "stock_ru_far_siberia": 0.0,
            }
        if latest_ts_by_nm.get(nm_id) != snapshot_ts:
            continue

        stock_count = _require_float(row, "stockCount")
        aggregated[nm_id]["stock_total"] += stock_count
        region_name = _require_str(row, "regionName")
        metric_key = REGION_TO_FIELD.get(_normalize_region_name(region_name))
        if metric_key:
            aggregated[nm_id][metric_key] += stock_count
        elif abs(stock_count) > 0:
            unmapped_region_totals[region_name] += stock_count

    covered_nm_ids = sorted(aggregated.keys())
    missing_nm_ids = sorted(set(requested_nm_ids) - set(covered_nm_ids))
    if missing_nm_ids:
        return StocksEnvelope(
            result=StocksIncomplete(
                kind="incomplete",
                snapshot_date=snapshot_date,
                requested_count=len(requested_nm_ids),
                covered_count=len(covered_nm_ids),
                missing_nm_ids=missing_nm_ids,
                detail="stocks snapshot coverage is incomplete for requested nmIds",
            )
        )

    items = [
        StocksItem(
            nm_id=nm_id,
            stock_total=aggregated[nm_id]["stock_total"],
            stock_ru_central=aggregated[nm_id]["stock_ru_central"],
            stock_ru_northwest=aggregated[nm_id]["stock_ru_northwest"],
            stock_ru_volga=aggregated[nm_id]["stock_ru_volga"],
            stock_ru_ural=aggregated[nm_id]["stock_ru_ural"],
            stock_ru_south_caucasus=aggregated[nm_id]["stock_ru_south_caucasus"],
            stock_ru_far_siberia=aggregated[nm_id]["stock_ru_far_siberia"],
        )
        for nm_id in covered_nm_ids
    ]
    return StocksEnvelope(
        result=StocksSuccess(
            kind="success",
            snapshot_date=snapshot_date,
            count=len(items),
            items=items,
            detail=_build_unmapped_detail(unmapped_region_totals),
        )
    )


def _normalize_region_name(value: str) -> str:
    normalized = str(value).replace("\xa0", " ").replace("ё", "е").strip()
    normalized = normalized.replace("+", " и ")
    normalized = re.sub(r"\s+", " ", normalized)
    if normalized.endswith(" ФО"):
        normalized = normalized[:-3].strip()
    return normalized


def _build_unmapped_detail(unmapped_region_totals: Mapping[str, float]) -> str:
    nonzero = {
        region: float(quantity)
        for region, quantity in unmapped_region_totals.items()
        if abs(float(quantity)) > 0
    }
    if not nonzero:
        return ""
    parts = [
        f"{region}={_format_quantity(nonzero[region])}"
        for region in sorted(nonzero)
    ]
    total_quantity = sum(nonzero.values())
    return (
        "unmapped stocks quantity outside configured district map="
        f"{_format_quantity(total_quantity)}; regions: {', '.join(parts)}"
    )


def _format_quantity(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(round(float(value), 6))


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


def _require_int_list(payload: Mapping[str, Any], key: str) -> list[int]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, int) for item in value):
        raise ValueError(f"{key} must be list[int]")
    return value


class StocksBlock:
    """Минимальный application-slice для stocks."""

    def __init__(self, source: StocksSource) -> None:
        self._source = source

    def execute(self, request: StocksRequest) -> StocksEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
