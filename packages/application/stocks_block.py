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
    StocksWarehouseRow,
)
from packages.contracts.wb_supply_planning_zones import (
    PLANNING_ZONE_CENTRAL_EAST,
    PLANNING_ZONE_CENTRAL_NORTH,
    PLANNING_ZONE_CENTRAL_SOUTH,
    resolve_central_storage_warehouse,
    warehouse_name_exclusion_codes,
)


ELEKTROSTAL_WAREHOUSE_ID = 120762


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
            "in_way_to_client": 0.0,
            "in_way_from_client": 0.0,
            "stock_ru_central": 0.0,
            "stock_ru_northwest": 0.0,
            "stock_ru_volga": 0.0,
            "stock_ru_ural": 0.0,
            "stock_ru_south_caucasus": 0.0,
            "stock_ru_far_siberia": 0.0,
            "stock_ru_central_north": 0.0,
            "stock_ru_central_east": 0.0,
            "stock_ru_central_south": 0.0,
        }
    )
    unmapped_region_totals: dict[str, float] = defaultdict(float)
    warehouse_rows_by_nm: dict[int, list[StocksWarehouseRow]] = defaultdict(list)

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
                "in_way_to_client": 0.0,
                "in_way_from_client": 0.0,
                "stock_ru_central": 0.0,
                "stock_ru_northwest": 0.0,
                "stock_ru_volga": 0.0,
                "stock_ru_ural": 0.0,
                "stock_ru_south_caucasus": 0.0,
                "stock_ru_far_siberia": 0.0,
                "stock_ru_central_north": 0.0,
                "stock_ru_central_east": 0.0,
                "stock_ru_central_south": 0.0,
            }
            warehouse_rows_by_nm[nm_id] = []
        if latest_ts_by_nm.get(nm_id) != snapshot_ts:
            continue

        stock_count = _require_float(row, "stockCount")
        in_way_to_client = _optional_float(row, "inWayToClient", default=0.0)
        in_way_from_client = _optional_float(row, "inWayFromClient", default=0.0)
        # Network-backed current snapshots reject every negative field at the
        # adapter boundary.  Preserve legacy/fake negative ``stockCount`` long
        # enough for the warehouse cutover guard to report its established,
        # source-specific error; in-way fields have no such legacy contract.
        if min(in_way_to_client, in_way_from_client) < 0:
            raise ValueError(f"stocks in-way quantities must be non-negative for nmId {nm_id}")
        aggregated[nm_id]["stock_total"] += stock_count
        aggregated[nm_id]["in_way_to_client"] += in_way_to_client
        aggregated[nm_id]["in_way_from_client"] += in_way_from_client
        region_name = _require_str(row, "regionName")
        normalized_region_name = _normalize_region_name(region_name)
        metric_key = REGION_TO_FIELD.get(normalized_region_name)
        if metric_key:
            aggregated[nm_id][metric_key] += stock_count
        elif abs(stock_count) > 0:
            unmapped_region_totals[region_name] += stock_count

        warehouse_name = str(row.get("warehouseName") or "").strip()
        warehouse_id = _optional_non_negative_int(
            row.get("warehouseId", row.get("warehouseID", row.get("warehouse_id")))
        )
        registry_item, classification_source = resolve_central_storage_warehouse(
            warehouse_id=warehouse_id,
            warehouse_name=warehouse_name,
            historical=True,
        )
        exclusion_codes = warehouse_name_exclusion_codes(warehouse_name)
        planning_zone_key: str | None = None
        classification_status = "outside_central_planning"
        if registry_item is not None:
            planning_zone_key = registry_item.planning_zone_key
            planning_field = {
                PLANNING_ZONE_CENTRAL_NORTH: "stock_ru_central_north",
                PLANNING_ZONE_CENTRAL_EAST: "stock_ru_central_east",
                PLANNING_ZONE_CENTRAL_SOUTH: "stock_ru_central_south",
            }[planning_zone_key]
            aggregated[nm_id][planning_field] += stock_count
            classification_status = "mapped"
        elif normalized_region_name == "Центральный":
            if exclusion_codes:
                classification_status = "excluded"
            else:
                classification_status = "unmapped"
        warehouse_rows_by_nm[nm_id].append(
            StocksWarehouseRow(
                nm_id=nm_id,
                warehouse_id=warehouse_id,
                warehouse_name=warehouse_name,
                region_name=region_name,
                quantity=stock_count,
                in_way_to_client=in_way_to_client,
                in_way_from_client=in_way_from_client,
                planning_zone_key=planning_zone_key,
                classification_status=classification_status,
                classification_source=classification_source,
                exclusion_codes=exclusion_codes,
            )
        )

    covered_nm_ids = sorted(aggregated.keys())
    reconciliation = {
        "legacy_central_total": 0.0,
        "central_planning_zone_total": 0.0,
        "central_unmapped_total": 0.0,
        "central_excluded_total": 0.0,
    }
    for nm_id in covered_nm_ids:
        for item in warehouse_rows_by_nm.get(nm_id, []):
            if _normalize_region_name(item.region_name) == "Центральный":
                reconciliation["legacy_central_total"] += item.quantity
            if item.planning_zone_key:
                reconciliation["central_planning_zone_total"] += item.quantity
            elif item.classification_status == "excluded":
                reconciliation["central_excluded_total"] += item.quantity
            elif item.classification_status == "unmapped":
                reconciliation["central_unmapped_total"] += item.quantity
    missing_nm_ids = sorted(set(requested_nm_ids) - set(covered_nm_ids))
    missing_are_zero = bool(data.get("pagination_complete")) and bool(data.get("missing_nm_ids_are_zero"))
    if missing_nm_ids and missing_are_zero:
        for nm_id in missing_nm_ids:
            aggregated[nm_id]
        covered_nm_ids = sorted(aggregated.keys())
        missing_nm_ids = []
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
            in_way_to_client=aggregated[nm_id]["in_way_to_client"],
            in_way_from_client=aggregated[nm_id]["in_way_from_client"],
            wb_contour_total=(
                aggregated[nm_id]["stock_total"]
                + aggregated[nm_id]["in_way_to_client"]
                + aggregated[nm_id]["in_way_from_client"]
            ),
            stock_ru_central_north=aggregated[nm_id]["stock_ru_central_north"],
            stock_ru_central_east=aggregated[nm_id]["stock_ru_central_east"],
            stock_ru_central_south=aggregated[nm_id]["stock_ru_central_south"],
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
            warehouse_rows=[
                row
                for nm_id in covered_nm_ids
                for row in warehouse_rows_by_nm.get(nm_id, [])
            ],
            planning_reconciliation={
                **{key: round(float(value), 6) for key, value in reconciliation.items()},
                "difference": round(
                    float(reconciliation["legacy_central_total"])
                    - float(reconciliation["central_planning_zone_total"])
                    - float(reconciliation["central_unmapped_total"])
                    - float(reconciliation["central_excluded_total"]),
                    6,
                ),
            },
            fetched_at=str(data.get("fetched_at") or ""),
            pagination_complete=bool(data.get("pagination_complete")),
            raw_rows_digest=str(data.get("raw_rows_digest") or ""),
        )
    )


def parse_excluded_wb_warehouse_ids(
    payload: Mapping[str, Any],
    *,
    allow_legacy_elektrostal: bool = True,
) -> tuple[int, ...]:
    """Parse stable warehouse identities without accepting duplicate/browser-only state."""

    value = payload.get("excluded_wb_warehouse_ids")
    if value in (None, ""):
        if allow_legacy_elektrostal and _parse_bool(payload.get("exclude_elektrostal_stock")):
            return (ELEKTROSTAL_WAREHOUSE_ID,)
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("excluded_wb_warehouse_ids must be an array of warehouseId")
    parsed: list[int] = []
    seen: set[int] = set()
    for raw in value:
        if isinstance(raw, bool):
            raise ValueError("excluded_wb_warehouse_ids must contain integer warehouseId values")
        try:
            warehouse_id = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("excluded_wb_warehouse_ids must contain integer warehouseId values") from exc
        if warehouse_id < 0:
            raise ValueError("excluded_wb_warehouse_ids cannot contain negative warehouseId")
        if warehouse_id in seen:
            raise ValueError(f"duplicate excluded warehouseId: {warehouse_id}")
        seen.add(warehouse_id)
        parsed.append(warehouse_id)
    return tuple(sorted(parsed))


def build_wb_warehouse_exclusion(
    *,
    items: list[StocksItem],
    warehouse_rows: list[StocksWarehouseRow],
    excluded_warehouse_ids: tuple[int, ...],
    snapshot_date: str,
    fetched_at: str,
    pagination_complete: bool,
    raw_rows_digest: str,
    require_complete: bool = False,
) -> dict[str, Any]:
    """Build one calculation-only exclusion contract for both supply calculators."""

    if not pagination_complete and (excluded_warehouse_ids or require_complete):
        raise ValueError(
            "Нельзя выбрать исключаемые склады: официальный снимок WB неполный"
        )
    selected = set(excluded_warehouse_ids)
    actual_by_nm = {int(item.nm_id): max(float(item.stock_total), 0.0) for item in items}
    excluded_by_nm: dict[int, float] = defaultdict(float)
    options_by_id: dict[int, dict[str, Any]] = {}
    selected_seen: set[int] = set()
    for row in warehouse_rows:
        warehouse_id = row.warehouse_id
        if warehouse_id is None:
            continue
        quantity = max(float(row.quantity), 0.0)
        to_client = max(float(row.in_way_to_client), 0.0)
        from_client = max(float(row.in_way_from_client), 0.0)
        option = options_by_id.setdefault(
            int(warehouse_id),
            {
                "warehouse_id": int(warehouse_id),
                "warehouse_name": str(row.warehouse_name or f"warehouseId {warehouse_id}"),
                "stock_quantity": 0.0,
                "in_way_to_client": 0.0,
                "in_way_from_client": 0.0,
                "total_contour": 0.0,
                "temporarily_missing": False,
            },
        )
        option["stock_quantity"] += quantity
        option["in_way_to_client"] += to_client
        option["in_way_from_client"] += from_client
        option["total_contour"] += quantity + to_client + from_client
        if int(warehouse_id) in selected:
            selected_seen.add(int(warehouse_id))
            excluded_by_nm[int(row.nm_id)] += quantity

    options = []
    for warehouse_id, option in sorted(
        options_by_id.items(),
        key=lambda item: (str(item[1]["warehouse_name"]).casefold(), item[0]),
    ):
        if float(option["total_contour"]) <= 0 and warehouse_id not in selected:
            continue
        options.append(
            {
                **option,
                "stock_quantity": round(float(option["stock_quantity"]), 6),
                "in_way_to_client": round(float(option["in_way_to_client"]), 6),
                "in_way_from_client": round(float(option["in_way_from_client"]), 6),
                "total_contour": round(float(option["total_contour"]), 6),
                "selected": warehouse_id in selected,
            }
        )
    missing_selected = sorted(selected - selected_seen)
    for warehouse_id in missing_selected:
        options.append(
            {
                "warehouse_id": warehouse_id,
                "warehouse_name": f"warehouseId {warehouse_id}",
                "stock_quantity": 0.0,
                "in_way_to_client": 0.0,
                "in_way_from_client": 0.0,
                "total_contour": 0.0,
                "temporarily_missing": True,
                "selected": True,
                "message": "Склад временно отсутствует в текущем снимке",
            }
        )

    by_nm_id: dict[str, dict[str, Any]] = {}
    for nm_id, actual in sorted(actual_by_nm.items()):
        raw_excluded = max(float(excluded_by_nm.get(nm_id, 0.0)), 0.0)
        excluded = min(actual, raw_excluded)
        by_nm_id[str(nm_id)] = {
            "nm_id": nm_id,
            "actual_stock_total_mp": round(actual, 6),
            "excluded_stock_total_mp": round(excluded, 6),
            "effective_stock_total_mp": round(max(actual - excluded, 0.0), 6),
            "over_exclusion": round(max(raw_excluded - actual, 0.0), 6),
            "reconciliation_difference": round(
                actual - max(actual - excluded, 0.0) - excluded,
                6,
            ),
        }
    actual_total = sum(float(row["actual_stock_total_mp"]) for row in by_nm_id.values())
    excluded_total = sum(float(row["excluded_stock_total_mp"]) for row in by_nm_id.values())
    effective_total = sum(float(row["effective_stock_total_mp"]) for row in by_nm_id.values())
    return {
        "contract_name": "wb_warehouse_calculation_exclusion",
        "contract_version": 1,
        "excluded_wb_warehouse_ids": list(excluded_warehouse_ids),
        "snapshot_date": snapshot_date,
        "fetched_at": fetched_at,
        "pagination_complete": bool(pagination_complete),
        "raw_rows_digest": raw_rows_digest,
        "options": options,
        "temporarily_missing_selected_ids": missing_selected,
        "actual_stock_total_mp": round(actual_total, 6),
        "excluded_stock_total_mp": round(excluded_total, 6),
        "effective_stock_total_mp": round(effective_total, 6),
        "reconciliation_difference": round(actual_total - excluded_total - effective_total, 6),
        "by_nm_id": by_nm_id,
    }


def build_elektrostal_stock_override(
    *,
    items: list[StocksItem],
    warehouse_rows: list[StocksWarehouseRow],
    enabled: bool,
) -> dict[str, Any]:
    """Build a calculation-only, non-negative incident adjustment.

    The source snapshot and historical rows remain untouched.  The exact
    warehouse ID is the only identity accepted for the adjustment.
    """

    excluded_by_nm: dict[int, float] = defaultdict(float)
    if enabled:
        for row in warehouse_rows:
            if row.warehouse_id == ELEKTROSTAL_WAREHOUSE_ID:
                excluded_by_nm[int(row.nm_id)] += max(float(row.quantity), 0.0)
    result: dict[str, Any] = {}
    for item in items:
        actual = max(float(item.stock_ru_central), 0.0)
        excluded = min(actual, max(float(excluded_by_nm.get(int(item.nm_id), 0.0)), 0.0))
        effective = max(actual - excluded, 0.0)
        result[str(int(item.nm_id))] = {
            "warehouse_id": ELEKTROSTAL_WAREHOUSE_ID,
            "enabled": bool(enabled),
            "actual_central_stock": round(actual, 6),
            "excluded_elektrostal_stock": round(excluded, 6),
            "effective_central_stock": round(effective, 6),
            "reason": (
                "Электросталь исключена только из текущего расчёта"
                if enabled and excluded > 0
                else "Электросталь не исключалась из текущего расчёта"
            ),
        }
    total_actual = sum(float(item["actual_central_stock"]) for item in result.values())
    total_excluded = sum(float(item["excluded_elektrostal_stock"]) for item in result.values())
    return {
        "enabled": bool(enabled),
        "warehouse_id": ELEKTROSTAL_WAREHOUSE_ID,
        "warehouse_name": "Электросталь",
        "reason": (
            "Электросталь исключена только из текущего расчёта"
            if enabled
            else "Инцидент Электростали не включён"
        ),
        "actual_central_stock": round(total_actual, 6),
        "excluded_elektrostal_stock": round(total_excluded, 6),
        "effective_central_stock": round(max(total_actual - total_excluded, 0.0), 6),
        "by_nm_id": result,
        "idempotent": True,
    }


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да"}


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


def _optional_non_negative_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


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


def _optional_float(payload: Mapping[str, Any], key: str, *, default: float) -> float:
    value = payload.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
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

    def fetch_payload(self, request: StocksRequest) -> Mapping[str, Any]:
        """Fetch one canonical source payload without changing its adapter semantics."""

        return self._source.fetch(request)

    def execute(self, request: StocksRequest) -> StocksEnvelope:
        payload = self.fetch_payload(request)
        return transform_legacy_payload(payload)
