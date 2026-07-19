"""Typed supply-planning zones and the authoritative Central warehouse registry.

Federal districts remain the canonical reporting contract.  This module owns the
smaller operational units used only by WB supply planning and warehouse history.
Warehouse identity is warehouseID-first; names and aliases are exact-only legacy
fallbacks when an ID is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from packages.contracts.wb_regional_supply import (
    DISTRICT_FAR_SIBERIA,
    DISTRICT_NORTHWEST,
    DISTRICT_SOUTH_CAUCASUS,
    DISTRICT_URAL,
    DISTRICT_VOLGA,
)


PLANNING_ZONE_CENTRAL_NORTH = "central_north"
PLANNING_ZONE_CENTRAL_EAST = "central_east"
PLANNING_ZONE_CENTRAL_SOUTH = "central_south"
CENTRAL_PLANNING_ZONE_KEYS = (
    PLANNING_ZONE_CENTRAL_NORTH,
    PLANNING_ZONE_CENTRAL_EAST,
    PLANNING_ZONE_CENTRAL_SOUTH,
)

SUPPLY_PLANNING_ZONE_KEYS = (
    *CENTRAL_PLANNING_ZONE_KEYS,
    DISTRICT_NORTHWEST,
    DISTRICT_VOLGA,
    DISTRICT_URAL,
    DISTRICT_SOUTH_CAUCASUS,
    DISTRICT_FAR_SIBERIA,
)

SUPPLY_PLANNING_ZONE_LABELS_RU = {
    PLANNING_ZONE_CENTRAL_NORTH: "ЦФО Север",
    PLANNING_ZONE_CENTRAL_EAST: "ЦФО Восток",
    PLANNING_ZONE_CENTRAL_SOUTH: "ЦФО Юг",
    DISTRICT_NORTHWEST: "Северо-Западный федеральный округ",
    DISTRICT_VOLGA: "Приволжский федеральный округ",
    DISTRICT_URAL: "Уральский федеральный округ",
    DISTRICT_SOUTH_CAUCASUS: "Южный и Северо-Кавказский федеральный округ",
    DISTRICT_FAR_SIBERIA: "Дальневосточный и Сибирский федеральный округ",
}

SUPPLY_PLANNING_ZONE_SHORT_LABELS_RU = {
    PLANNING_ZONE_CENTRAL_NORTH: "ЦФО Север",
    PLANNING_ZONE_CENTRAL_EAST: "ЦФО Восток",
    PLANNING_ZONE_CENTRAL_SOUTH: "ЦФО Юг",
    DISTRICT_NORTHWEST: "СЗФО",
    DISTRICT_VOLGA: "ПФО",
    DISTRICT_URAL: "УФО",
    DISTRICT_SOUTH_CAUCASUS: "ЮФО/СКФО",
    DISTRICT_FAR_SIBERIA: "ДВФО/СФО",
}

SUPPLY_PLANNING_ZONE_TO_STOCK_FIELD = {
    PLANNING_ZONE_CENTRAL_NORTH: "stock_ru_central_north",
    PLANNING_ZONE_CENTRAL_EAST: "stock_ru_central_east",
    PLANNING_ZONE_CENTRAL_SOUTH: "stock_ru_central_south",
    DISTRICT_NORTHWEST: "stock_ru_northwest",
    DISTRICT_VOLGA: "stock_ru_volga",
    DISTRICT_URAL: "stock_ru_ural",
    DISTRICT_SOUTH_CAUCASUS: "stock_ru_south_caucasus",
    DISTRICT_FAR_SIBERIA: "stock_ru_far_siberia",
}

WarehouseRole = Literal["primary", "reserve", "far_reserve"]
StorageKind = Literal["storage"]


@dataclass(frozen=True)
class SupplyPlanningWarehouse:
    warehouse_id: int
    canonical_name: str
    aliases: tuple[str, ...]
    federal_district_key: str
    planning_zone_key: str
    planning_zone_label: str
    role: WarehouseRole
    storage_kind: StorageKind
    recommendation_enabled: bool
    historical_mapping_enabled: bool
    blocked_reason: str | None
    metadata_version: str


WAREHOUSE_REGISTRY_VERSION = "central-storage-v1-2026-07-19"

CENTRAL_STORAGE_WAREHOUSES: tuple[SupplyPlanningWarehouse, ...] = (
    SupplyPlanningWarehouse(
        warehouse_id=301806,
        canonical_name="Тверь",
        aliases=(),
        federal_district_key="central",
        planning_zone_key=PLANNING_ZONE_CENTRAL_NORTH,
        planning_zone_label=SUPPLY_PLANNING_ZONE_LABELS_RU[PLANNING_ZONE_CENTRAL_NORTH],
        role="primary",
        storage_kind="storage",
        recommendation_enabled=True,
        historical_mapping_enabled=True,
        blocked_reason=None,
        metadata_version=WAREHOUSE_REGISTRY_VERSION,
    ),
    SupplyPlanningWarehouse(
        warehouse_id=301981,
        canonical_name="Владимир Воршинское",
        aliases=("Владимир (Воршинское)",),
        federal_district_key="central",
        planning_zone_key=PLANNING_ZONE_CENTRAL_EAST,
        planning_zone_label=SUPPLY_PLANNING_ZONE_LABELS_RU[PLANNING_ZONE_CENTRAL_EAST],
        role="primary",
        storage_kind="storage",
        recommendation_enabled=True,
        historical_mapping_enabled=True,
        blocked_reason=None,
        metadata_version=WAREHOUSE_REGISTRY_VERSION,
    ),
    SupplyPlanningWarehouse(
        warehouse_id=301760,
        canonical_name="Рязань (Тюшевское)",
        aliases=("Рязань Тюшевское",),
        federal_district_key="central",
        planning_zone_key=PLANNING_ZONE_CENTRAL_EAST,
        planning_zone_label=SUPPLY_PLANNING_ZONE_LABELS_RU[PLANNING_ZONE_CENTRAL_EAST],
        role="reserve",
        storage_kind="storage",
        recommendation_enabled=True,
        historical_mapping_enabled=True,
        blocked_reason=None,
        metadata_version=WAREHOUSE_REGISTRY_VERSION,
    ),
    SupplyPlanningWarehouse(
        warehouse_id=120762,
        canonical_name="Электросталь",
        aliases=(),
        federal_district_key="central",
        planning_zone_key=PLANNING_ZONE_CENTRAL_EAST,
        planning_zone_label=SUPPLY_PLANNING_ZONE_LABELS_RU[PLANNING_ZONE_CENTRAL_EAST],
        role="reserve",
        storage_kind="storage",
        recommendation_enabled=False,
        historical_mapping_enabled=True,
        blocked_reason="temporarily_blocked_after_warehouse_loss",
        metadata_version=WAREHOUSE_REGISTRY_VERSION,
    ),
    SupplyPlanningWarehouse(
        warehouse_id=301809,
        canonical_name="Котовск",
        aliases=(),
        federal_district_key="central",
        planning_zone_key=PLANNING_ZONE_CENTRAL_EAST,
        planning_zone_label=SUPPLY_PLANNING_ZONE_LABELS_RU[PLANNING_ZONE_CENTRAL_EAST],
        role="reserve",
        storage_kind="storage",
        recommendation_enabled=False,
        historical_mapping_enabled=True,
        blocked_reason="temporarily_blocked_for_new_recommendations",
        metadata_version=WAREHOUSE_REGISTRY_VERSION,
    ),
    SupplyPlanningWarehouse(
        warehouse_id=507,
        canonical_name="Коледино",
        aliases=(),
        federal_district_key="central",
        planning_zone_key=PLANNING_ZONE_CENTRAL_SOUTH,
        planning_zone_label=SUPPLY_PLANNING_ZONE_LABELS_RU[PLANNING_ZONE_CENTRAL_SOUTH],
        role="primary",
        storage_kind="storage",
        recommendation_enabled=True,
        historical_mapping_enabled=True,
        blocked_reason=None,
        metadata_version=WAREHOUSE_REGISTRY_VERSION,
    ),
    SupplyPlanningWarehouse(
        warehouse_id=206348,
        canonical_name="Тула",
        aliases=(),
        federal_district_key="central",
        planning_zone_key=PLANNING_ZONE_CENTRAL_SOUTH,
        planning_zone_label=SUPPLY_PLANNING_ZONE_LABELS_RU[PLANNING_ZONE_CENTRAL_SOUTH],
        role="reserve",
        storage_kind="storage",
        recommendation_enabled=True,
        historical_mapping_enabled=True,
        blocked_reason=None,
        metadata_version=WAREHOUSE_REGISTRY_VERSION,
    ),
    SupplyPlanningWarehouse(
        warehouse_id=301808,
        canonical_name="Воронеж",
        aliases=(),
        federal_district_key="central",
        planning_zone_key=PLANNING_ZONE_CENTRAL_SOUTH,
        planning_zone_label=SUPPLY_PLANNING_ZONE_LABELS_RU[PLANNING_ZONE_CENTRAL_SOUTH],
        role="far_reserve",
        storage_kind="storage",
        recommendation_enabled=True,
        historical_mapping_enabled=True,
        blocked_reason=None,
        metadata_version=WAREHOUSE_REGISTRY_VERSION,
    ),
)

CENTRAL_STORAGE_WAREHOUSES_BY_ID = {
    item.warehouse_id: item for item in CENTRAL_STORAGE_WAREHOUSES
}


def normalize_exact_warehouse_name(value: object) -> str:
    text = str(value or "").replace("\xa0", " ").replace("ё", "е").strip().casefold()
    return " ".join(text.split())


def _build_exact_name_index() -> dict[str, SupplyPlanningWarehouse]:
    result: dict[str, SupplyPlanningWarehouse] = {}
    ambiguous: set[str] = set()
    for item in CENTRAL_STORAGE_WAREHOUSES:
        for raw_name in (item.canonical_name, *item.aliases):
            name = normalize_exact_warehouse_name(raw_name)
            if not name:
                continue
            if name in result and result[name].warehouse_id != item.warehouse_id:
                ambiguous.add(name)
                continue
            result[name] = item
    for name in ambiguous:
        result.pop(name, None)
    return result


CENTRAL_STORAGE_WAREHOUSES_BY_EXACT_NAME = _build_exact_name_index()


def resolve_central_storage_warehouse(
    *,
    warehouse_id: object = None,
    warehouse_name: object = None,
    historical: bool = False,
) -> tuple[SupplyPlanningWarehouse | None, str]:
    """Return an explicit Central registry entry and its classification source.

    A supplied unknown ID never falls back to a possibly colliding name.  Name
    fallback is only for historical sources that genuinely omit warehouseID.
    """

    normalized_id = _optional_positive_int(warehouse_id)
    if normalized_id is not None:
        item = CENTRAL_STORAGE_WAREHOUSES_BY_ID.get(normalized_id)
        if item is None:
            return None, "warehouse_id_unclassified"
        if historical and not item.historical_mapping_enabled:
            return None, "historical_mapping_disabled"
        return item, "warehouse_id"
    if not historical:
        return None, "warehouse_id_missing"
    name = normalize_exact_warehouse_name(warehouse_name)
    item = CENTRAL_STORAGE_WAREHOUSES_BY_EXACT_NAME.get(name)
    if item is None:
        return None, "exact_name_unclassified"
    if not item.historical_mapping_enabled:
        return None, "historical_mapping_disabled"
    return item, "exact_name_or_alias"


def warehouse_name_exclusion_codes(value: object) -> tuple[str, ...]:
    """Exact semantic guards; never used to establish warehouse identity."""

    name = normalize_exact_warehouse_name(value)
    if not name:
        return ()
    codes: list[str] = []
    if re.match(r"^сц(?:\s|[:\-–—]|$)", name):
        codes.append("sorting_center_name")
    tokens = set(re.findall(r"[a-zа-я0-9]+", name, flags=re.IGNORECASE))
    if "сгт" in tokens:
        codes.append("sgt_warehouse")
    if "питание" in tokens:
        codes.append("specialized_food")
    if "горючее" in tokens:
        codes.append("specialized_fuel")
    if "шины" in tokens:
        codes.append("specialized_tires")
    return tuple(codes)


def _optional_positive_int(value: object) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
