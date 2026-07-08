"""Runtime-only WB supplies calculation overlay helpers."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any, Mapping

from packages.contracts.factory_order_supply import FactoryOrderInboundRow, FactoryOrderStockFfRow
from packages.contracts.wb_regional_supply import DISTRICT_KEYS, DISTRICT_LABELS_RU


ELIGIBLE_WB_SUPPLY_STATUS_IDS = {3, 4, 6}
INELIGIBLE_WB_SUPPLY_STATUS_IDS = {1, 2, 5}
DISTRICT_UNMAPPED = "unmapped"
WB_SUPPLY_OVERLAY_REQUEST_KEYS = (
    "selected_wb_supply_ids",
    "selected_wb_supplies",
    "wb_supply_ids",
)

DISTRICT_SHORT_LABELS_RU = {
    "central": "ЦФО",
    "northwest": "СЗФО",
    "volga": "ПФО",
    "ural": "УрФО",
    "south_caucasus": "Юг+СК",
    "far_siberia": "Сиб+ДВ",
}

_MANUAL_WAREHOUSE_DISTRICT_FALLBACKS: dict[str, tuple[str, str]] = {
    "Коледино": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Электросталь": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Обухово": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Домодедово-2": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Домодедово 2: Питание": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Домодедово: Шины": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Склад Истра": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Истра": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Климовск СГТ": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Подольск": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Подольск 3": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Подольск 4": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Чашниково": ("central", "manual_known_wb_warehouse: Moscow region"),
    "Тула": ("central", "manual_known_wb_warehouse: Tula Oblast"),
    "Краснодар (Тихорецкая)": ("south_caucasus", "manual_known_wb_warehouse: Krasnodar Krai"),
    "Невинномысск": ("south_caucasus", "manual_known_wb_warehouse: Stavropol Krai"),
    "Склад Шушары": ("northwest", "manual_known_wb_warehouse: Saint Petersburg"),
    "Санкт-Петербург (Уткина Заводь)": ("northwest", "manual_known_wb_warehouse: Saint Petersburg"),
    "Казань": ("volga", "manual_known_wb_warehouse: Tatarstan"),
    "Новосемейкино": ("volga", "manual_known_wb_warehouse: Samara Oblast"),
    "СЦ Саратов Зоринский": ("volga", "manual_known_wb_warehouse: Saratov Oblast"),
    "СЦ Самара": ("volga", "manual_known_wb_warehouse: Samara Oblast"),
    "Пенза СГТ": ("volga", "manual_known_wb_warehouse: Penza Oblast"),
    "Пермь: Горючее": ("volga", "manual_known_wb_warehouse: Perm Krai"),
    "Екатеринбург - Перспективная 14": ("ural", "manual_known_wb_warehouse: Sverdlovsk Oblast"),
    "Екатеринбург - Испытателей 14г": ("ural", "manual_known_wb_warehouse: Sverdlovsk Oblast"),
    "Нижний Тагил: Индустриальная СГТ": ("ural", "manual_known_wb_warehouse: Sverdlovsk Oblast"),
    "Новосибирск": ("far_siberia", "manual_known_wb_warehouse: Novosibirsk Oblast"),
    "СЦ Барнаул": ("far_siberia", "manual_known_wb_warehouse: Altai Krai"),
    "СЦ Новокузнецк": ("far_siberia", "manual_known_wb_warehouse: Kemerovo Oblast"),
    "Склад Владивосток": ("far_siberia", "manual_known_wb_warehouse: Primorsky Krai"),
}

_DATE_FIELD_CANDIDATES = (
    "supply_date",
    "supplyDate",
    "delivery_date",
    "deliveryDate",
    "planned_delivery_date",
    "plannedDeliveryDate",
    "planned_supply_date",
    "plannedSupplyDate",
    "shipment_date",
    "shipmentDate",
    "shipping_date",
    "shippingDate",
    "ship_date",
    "shipDate",
    "fact_date",
    "factDate",
    "actual_date",
    "actualDate",
    "allowed_shipment_date",
    "allowedShipmentDate",
    "allowed_delivery_date",
    "allowedDeliveryDate",
    "acceptance_date",
    "acceptanceDate",
    "unloading_date",
    "unloadingDate",
)


def district_filter_options() -> list[dict[str, str]]:
    return [
        {
            "district_key": key,
            "value": key,
            "label": DISTRICT_SHORT_LABELS_RU[key],
            "district_name_ru": DISTRICT_LABELS_RU[key],
        }
        for key in DISTRICT_KEYS
    ]


def parse_selected_wb_supply_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw: Any = None
    for key in WB_SUPPLY_OVERLAY_REQUEST_KEYS:
        if key in payload:
            raw = payload.get(key)
            break
    if raw in (None, ""):
        return ()
    if isinstance(raw, str):
        raw_values: list[Any] = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, Mapping):
        raw_values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        raw_values = list(raw)
    else:
        raw_values = [raw]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        if isinstance(item, Mapping):
            value = (
                item.get("supply_id")
                or item.get("wb_supply_id")
                or item.get("cache_key")
                or item.get("id")
            )
        else:
            value = item
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def convert_raw_district_to_key(value: Any) -> str | None:
    normalized = _normalize_text(value)
    if not normalized:
        return None
    compact = normalized.replace(" ", "")
    if "централь" in normalized or compact in {"цфо", "central", "centralfo"}:
        return "central"
    if (
        "северо запад" in normalized
        or "северозапад" in compact
        or compact in {"сзфо", "northwest", "northwestern"}
    ):
        return "northwest"
    if "приволж" in normalized or "поволж" in normalized or compact in {"пфо", "volga"}:
        return "volga"
    if "ураль" in normalized or compact in {"урфо", "ural"}:
        return "ural"
    if (
        "южн" in normalized
        or "кавказ" in normalized
        or compact in {"юфо", "скфо", "south", "southern", "caucasus", "northcaucasus"}
    ):
        return "south_caucasus"
    if (
        "сибир" in normalized
        or "дальневост" in normalized
        or compact in {"сфо", "двфо", "siberia", "siberian", "fareast", "fareastern"}
    ):
        return "far_siberia"
    return None


def build_warehouse_district_mapping(
    *,
    warehouse_rows: list[Mapping[str, Any]] | None = None,
    supply_rows: list[Mapping[str, Any]] | None = None,
    office_rows: list[Mapping[str, Any]] | None = None,
    tariff_rows: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    target_names = _collect_target_warehouse_names(supply_rows or [])
    offices_by_name = _build_reference_index(
        office_rows or [],
        source="marketplace_offices",
        name_keys=("name", "officeName", "office_name", "warehouseName", "warehouse_name"),
        district_keys=("federalDistrict", "federal_district", "district", "districtName"),
    )
    tariffs_by_name = _build_reference_index(
        tariff_rows or [],
        source="tariffs_box",
        name_keys=("warehouseName", "warehouse_name", "name"),
        district_keys=("geoName", "geo_name", "federalDistrict", "federal_district"),
    )
    manual_by_name = _build_manual_fallback_index()
    trusted_cached_by_name = _build_trusted_cached_source_index(supply_rows or [])
    by_name: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    unmapped_warehouses: list[str] = []
    for normalized_name, display_name in sorted(target_names.items()):
        mapping = (
            offices_by_name.get(normalized_name)
            or tariffs_by_name.get(normalized_name)
            or manual_by_name.get(normalized_name)
            or trusted_cached_by_name.get(normalized_name)
        )
        if mapping:
            by_name[normalized_name] = mapping
        else:
            unmapped_warehouses.append(display_name)
            warnings.append(f"Склад WB не сопоставлен с расчётным округом: {display_name}")
    return {
        "by_normalized_name": by_name,
        "warnings": warnings,
        "unmapped_warehouses": unmapped_warehouses,
        "unmapped_warehouse_count": len(unmapped_warehouses),
        "source_warehouse_count": len(target_names),
        "district_options": district_filter_options(),
    }


def augment_supply_row_with_district(row: Mapping[str, Any], mapping: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    target = district_source_warehouse_for_supply(result)
    result["district_source_warehouse_id"] = target["warehouse_id"]
    result["district_source_warehouse_name"] = target["warehouse_name"]
    result["district_source_warehouse_role"] = target["warehouse_role"]
    result["district_source_warehouse_evidence"] = target["warehouse_evidence"]
    normalized_name = _normalize_warehouse_name(target["warehouse_name"])
    by_name = mapping.get("by_normalized_name") if isinstance(mapping, Mapping) else {}
    mapped = by_name.get(normalized_name) if isinstance(by_name, Mapping) and normalized_name else None
    if isinstance(mapped, Mapping):
        district_key = str(mapped.get("district_key") or "").strip()
        result["district_key"] = district_key
        result["warehouse_district_key"] = district_key
        result["district_label_ru"] = DISTRICT_LABELS_RU.get(district_key, "")
        result["district_short_label_ru"] = DISTRICT_SHORT_LABELS_RU.get(district_key, "")
        result["district_mapping_status"] = "mapped"
        result["district_mapping_source"] = str(mapped.get("source") or "")
        result["district_mapping_evidence"] = str(mapped.get("evidence") or "")
        result["district_mapping_confidence"] = str(mapped.get("confidence") or "name_exact")
        result["district_warehouse_id"] = target["warehouse_id"]
        result["district_warehouse_name"] = target["warehouse_name"]
        result["district_source_warehouse_id"] = target["warehouse_id"]
        result["district_source_warehouse_name"] = target["warehouse_name"]
        return result

    result["district_key"] = DISTRICT_UNMAPPED
    result["warehouse_district_key"] = DISTRICT_UNMAPPED
    result["district_label_ru"] = ""
    result["district_short_label_ru"] = ""
    result["district_mapping_status"] = "unmapped"
    result["district_mapping_source"] = "unmapped"
    result["district_mapping_evidence"] = (
        "warehouse_name=" + str(target["warehouse_name"] or "").strip()
        if target["warehouse_name"]
        else "warehouse_name_absent"
    )
    result["district_mapping_confidence"] = "none"
    result["district_warehouse_id"] = target["warehouse_id"]
    result["district_warehouse_name"] = target["warehouse_name"]
    return result


def target_warehouse_for_supply(row: Mapping[str, Any]) -> dict[str, str]:
    return district_source_warehouse_for_supply(row)


def district_source_warehouse_for_supply(row: Mapping[str, Any]) -> dict[str, str]:
    source_id = str(row.get("district_source_warehouse_id") or "").strip()
    source_name = str(row.get("district_source_warehouse_name") or "").strip()
    if source_name:
        return {
            "warehouse_id": source_id,
            "warehouse_name": source_name,
            "warehouse_role": str(row.get("district_source_warehouse_role") or "planned"),
            "warehouse_evidence": str(row.get("district_source_warehouse_evidence") or "cached.district_source_warehouse_name"),
        }
    target_id = str(row.get("target_warehouse_id") or "").strip()
    target_name = str(row.get("target_warehouse_name") or "").strip()
    if target_name:
        return {
            "warehouse_id": target_id,
            "warehouse_name": target_name,
            "warehouse_role": "target",
            "warehouse_evidence": "normalized.target_warehouse_name",
        }
    planned_id = str(row.get("planned_warehouse_id") or "").strip()
    planned_name = str(row.get("planned_warehouse_name") or "").strip()
    if planned_name:
        return {
            "warehouse_id": planned_id,
            "warehouse_name": planned_name,
            "warehouse_role": "planned",
            "warehouse_evidence": "normalized.planned_warehouse_name",
        }
    warehouse_name = str(row.get("warehouse_name") or "").strip()
    if warehouse_name:
        return {
            "warehouse_id": str(row.get("warehouse_id") or "").strip(),
            "warehouse_name": warehouse_name,
            "warehouse_role": "planned",
            "warehouse_evidence": "normalized.warehouse_name",
        }
    warehouse_from_name = str(row.get("warehouse_from_name") or "").strip()
    if warehouse_from_name:
        return {
            "warehouse_id": str(row.get("warehouse_id") or "").strip(),
            "warehouse_name": warehouse_from_name,
            "warehouse_role": "planned",
            "warehouse_evidence": "normalized.warehouse_from_name",
        }
    warehouse_display = str(row.get("warehouse_display") or "").strip()
    if "→" in warehouse_display:
        return {
            "warehouse_id": str(row.get("warehouse_id") or "").strip(),
            "warehouse_name": warehouse_display.split("→", 1)[0].strip(),
            "warehouse_role": "planned",
            "warehouse_evidence": "normalized.warehouse_display.from",
        }
    warehouse_to_name = str(row.get("warehouse_to_name") or "").strip()
    transit_warehouse_name = str(row.get("transit_warehouse_name") or "").strip()
    actual_warehouse_name = str(row.get("actual_warehouse_name") or row.get("warehouse_actual_name") or "").strip()
    if warehouse_to_name:
        return {
            "warehouse_id": str(row.get("transit_warehouse_id") or row.get("actual_warehouse_id") or "").strip(),
            "warehouse_name": warehouse_to_name,
            "warehouse_role": "transit_fallback",
            "warehouse_evidence": "normalized.warehouse_to_name",
        }
    if transit_warehouse_name:
        return {
            "warehouse_id": str(row.get("transit_warehouse_id") or "").strip(),
            "warehouse_name": transit_warehouse_name,
            "warehouse_role": "transit_fallback",
            "warehouse_evidence": "normalized.transit_warehouse_name",
        }
    if actual_warehouse_name:
        return {
            "warehouse_id": str(row.get("actual_warehouse_id") or "").strip(),
            "warehouse_name": actual_warehouse_name,
            "warehouse_role": "actual_fallback",
            "warehouse_evidence": "normalized.actual_warehouse_name",
        }
    return {
        "warehouse_id": "",
        "warehouse_name": warehouse_display,
        "warehouse_role": "display_fallback",
        "warehouse_evidence": "normalized.warehouse_display",
    }


def build_wb_supply_overlay_options(
    *,
    runtime: Any,
    active_skus: list[tuple[int, str]],
    warehouse_district_mapping: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    records = runtime.list_wb_supplies_cache_records()
    rows = [dict(record.get("normalized") or {}) for record in records]
    mapping = warehouse_district_mapping or build_warehouse_district_mapping(
        warehouse_rows=runtime.list_wb_supplies_warehouses(),
        supply_rows=rows,
    )
    active_sku_map = {int(nm_id): str(label) for nm_id, label in active_skus}
    excluded_status_count = 0
    options: list[dict[str, Any]] = []
    for record in records:
        normalized = record.get("normalized") if isinstance(record.get("normalized"), Mapping) else {}
        status_id = _optional_int(normalized.get("status_id"))
        status_label = str(normalized.get("status_label") or _status_label(status_id))
        if _record_is_doprinato(record, normalized) or not _status_is_eligible(status_id, status_label):
            excluded_status_count += 1
            continue
        options.append(_candidate_from_record(record, active_sku_map=active_sku_map, mapping=mapping))
    options.sort(key=_overlay_option_sort_key)
    return {
        "status": "ready",
        "source": "wb_supplies_cache",
        "read_only": True,
        "eligible_status_ids": sorted(ELIGIBLE_WB_SUPPLY_STATUS_IDS),
        "ineligible_status_ids": sorted(INELIGIBLE_WB_SUPPLY_STATUS_IDS),
        "district_options": district_filter_options(),
        "options": options,
        "summary": {
            "total": len(options),
            "eligible": sum(1 for item in options if item.get("eligible_for_overlay")),
            "disabled": sum(1 for item in options if item.get("disabled")),
            "excluded_by_status": excluded_status_count,
            "unmapped": sum(1 for item in options if item.get("district_key") == DISTRICT_UNMAPPED),
            "unmapped_warehouse_count": int(mapping.get("unmapped_warehouse_count") or 0),
        },
        "warnings": mapping.get("warnings", []),
        "warning_details": {
            "unmapped_warehouse_count": int(mapping.get("unmapped_warehouse_count") or 0),
            "unmapped_warehouses": list(mapping.get("unmapped_warehouses") or []),
        },
    }


def build_selected_wb_supply_overlay(
    *,
    runtime: Any,
    selected_supply_ids: tuple[str, ...],
    active_skus: list[tuple[int, str]],
    warehouse_district_mapping: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not selected_supply_ids:
        return {
            "status": "empty",
            "source": "wb_supplies_cache",
            "read_only": True,
            "selected_supply_ids": [],
            "selected_supply_count": 0,
            "selected_supplies": [],
            "events": [],
            "qty_by_nm_id": {},
            "total_selected_qty": 0.0,
            "skipped": [],
            "warnings": [],
            "mapping_warnings": [],
        }
    active_sku_map = {int(nm_id): str(label) for nm_id, label in active_skus}
    all_records = runtime.list_wb_supplies_cache_records()
    rows = [dict(record.get("normalized") or {}) for record in all_records]
    mapping = warehouse_district_mapping or build_warehouse_district_mapping(
        warehouse_rows=runtime.list_wb_supplies_warehouses(),
        supply_rows=rows,
    )
    records_by_key = _records_by_identity(all_records)
    selected_supplies: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []
    selected_qty_by_nm: dict[int, float] = {}
    for requested_id in selected_supply_ids:
        record = records_by_key.get(requested_id)
        if record is None:
            skipped.append(
                {
                    "supply_id": requested_id,
                    "reason": "supply_not_found",
                    "reason_ru": "выбранная WB-поставка не найдена в cache",
                }
            )
            warnings.append(f"Выбранная WB-поставка не найдена в cache: {requested_id}")
            continue
        candidate = _candidate_from_record(record, active_sku_map=active_sku_map, mapping=mapping)
        selected_supplies.append(candidate)
        if candidate.get("disabled"):
            reasons = list(candidate.get("disabled_reasons") or [])
            skipped.append(
                {
                    "supply_id": candidate["supply_id"],
                    "reason": "disabled_supply",
                    "reason_ru": "; ".join(reasons) or "поставка недоступна для расчёта",
                    "disabled_reasons": reasons,
                }
            )
            warnings.append(
                "WB-поставка "
                + str(candidate.get("number_label") or candidate.get("supply_id"))
                + " не учтена: "
                + ("; ".join(reasons) or "поставка недоступна для расчёта")
            )
            continue
        qty_by_nm = {
            int(nm_id): float(qty)
            for nm_id, qty in (candidate.get("quantity_by_nm_id") or {}).items()
        }
        for nm_id, quantity in qty_by_nm.items():
            if quantity <= 0:
                continue
            selected_qty_by_nm[nm_id] = selected_qty_by_nm.get(nm_id, 0.0) + quantity
            events.append(
                {
                    "supply_id": str(candidate.get("supply_id") or ""),
                    "wb_supply_id": str(candidate.get("wb_supply_id") or ""),
                    "preorder_id": str(candidate.get("preorder_id") or ""),
                    "status_id": candidate.get("status_id"),
                    "status_label": str(candidate.get("status_label") or ""),
                    "selected_date": str(candidate.get("selected_date") or ""),
                    "date_evidence": str(candidate.get("date_evidence") or ""),
                    "date_source_field": str(candidate.get("date_source_field") or ""),
                    "warehouse_id": str(candidate.get("warehouse_id") or ""),
                    "warehouse_name": str(candidate.get("warehouse_name") or ""),
                    "warehouse_display": str(candidate.get("warehouse_display") or ""),
                    "district_source_warehouse_id": str(candidate.get("district_source_warehouse_id") or ""),
                    "district_source_warehouse_name": str(candidate.get("district_source_warehouse_name") or ""),
                    "district_source_warehouse_role": str(candidate.get("district_source_warehouse_role") or ""),
                    "district_source_warehouse_evidence": str(candidate.get("district_source_warehouse_evidence") or ""),
                    "district_key": str(candidate.get("district_key") or DISTRICT_UNMAPPED),
                    "district_label_ru": str(candidate.get("district_label_ru") or ""),
                    "district_mapping_source": str(candidate.get("district_mapping_source") or ""),
                    "district_mapping_evidence": str(candidate.get("district_mapping_evidence") or ""),
                    "district_mapping_confidence": str(candidate.get("district_mapping_confidence") or ""),
                    "nm_id": int(nm_id),
                    "sku_comment": active_sku_map.get(int(nm_id), ""),
                    "quantity": float(quantity),
                }
            )
    return {
        "status": "applied" if selected_supply_ids else "empty",
        "source": "wb_supplies_cache",
        "read_only": True,
        "selected_supply_ids": list(selected_supply_ids),
        "selected_supply_count": len(selected_supply_ids),
        "selected_supplies": selected_supplies,
        "events": events,
        "qty_by_nm_id": {str(nm_id): qty for nm_id, qty in sorted(selected_qty_by_nm.items())},
        "total_selected_qty": round(sum(selected_qty_by_nm.values()), 4),
        "skipped": skipped,
        "warnings": warnings,
        "mapping_warnings": mapping.get("warnings", []),
    }


def apply_stock_ff_overlay(
    *,
    stock_ff_rows: list[FactoryOrderStockFfRow],
    active_skus: list[tuple[int, str]],
    overlay: Mapping[str, Any],
    deduct_selected_supplies: bool = True,
) -> tuple[list[FactoryOrderStockFfRow], dict[str, Any], tuple[str, ...]]:
    qty_by_nm = {int(nm_id): float(qty) for nm_id, qty in _numeric_items(overlay.get("qty_by_nm_id"))}
    stock_rows_by_nm = {row.nm_id: row for row in stock_ff_rows}
    effective_rows: list[FactoryOrderStockFfRow] = []
    by_nm: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    total_base = 0.0
    total_selected = 0.0
    total_effective = 0.0
    total_over_reserved = 0.0
    for nm_id, sku_comment in active_skus:
        row = stock_rows_by_nm.get(int(nm_id))
        base_stock = float(row.stock_ff if row is not None else 0.0)
        selected_qty = float(qty_by_nm.get(int(nm_id), 0.0))
        effective_stock = (
            base_stock
            if selected_qty <= 0 or not deduct_selected_supplies
            else max(base_stock - selected_qty, 0.0)
        )
        over_reserved = max(selected_qty - base_stock, 0.0) if deduct_selected_supplies else 0.0
        total_base += base_stock
        total_selected += selected_qty
        total_effective += effective_stock
        total_over_reserved += over_reserved
        if over_reserved > 0:
            warnings.append(
                "nmId "
                + str(nm_id)
                + ": выбранные WB-поставки превышают текущий остаток ФФ на "
                + _format_qty(over_reserved)
                + " шт.; ФФ принят как 0."
            )
        source_row = row or FactoryOrderStockFfRow(
            nm_id=int(nm_id),
            sku_comment=sku_comment,
            stock_ff=0.0,
            snapshot_date=None,
            comment="",
        )
        effective_rows.append(
            FactoryOrderStockFfRow(
                nm_id=int(nm_id),
                sku_comment=source_row.sku_comment or sku_comment,
                stock_ff=effective_stock,
                snapshot_date=source_row.snapshot_date,
                comment=source_row.comment,
            )
        )
        by_nm[str(nm_id)] = {
            "nm_id": int(nm_id),
            "sku_comment": source_row.sku_comment or sku_comment,
            "base_stock_ff": base_stock,
            "selected_wb_supply_qty": selected_qty,
            "effective_stock_ff": effective_stock,
            "over_reserved_qty": over_reserved,
        }
    diagnostics = {
        "by_nm_id": by_nm,
        "total_base_stock_ff": total_base,
        "total_selected_wb_supply_qty": total_selected,
        "total_effective_stock_ff": total_effective,
        "total_over_reserved_qty": total_over_reserved,
        "stock_deduction_applied": bool(deduct_selected_supplies),
    }
    return effective_rows, diagnostics, tuple(warnings)


def factory_inbound_overlay_rows(
    *,
    overlay: Mapping[str, Any],
    report_date: date,
    inbound_window_end: date,
) -> tuple[list[FactoryOrderInboundRow], dict[str, Any], tuple[str, ...]]:
    rows: list[FactoryOrderInboundRow] = []
    added_by_nm: dict[int, float] = {}
    outside_window: list[dict[str, Any]] = []
    warnings: list[str] = []
    for event in overlay.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        selected_date = _parse_date_value(event.get("selected_date"))
        if selected_date is None or selected_date < report_date or selected_date > inbound_window_end:
            outside_window.append(dict(event))
            continue
        nm_id = int(event.get("nm_id"))
        quantity = float(event.get("quantity") or 0.0)
        if quantity <= 0:
            continue
        added_by_nm[nm_id] = added_by_nm.get(nm_id, 0.0) + quantity
        rows.append(
            FactoryOrderInboundRow(
                nm_id=nm_id,
                sku_comment=str(event.get("sku_comment") or ""),
                quantity=quantity,
                planned_arrival_date=selected_date.isoformat(),
                comment="selected_wb_supply_overlay",
                shipment_name=str(event.get("supply_id") or ""),
            )
        )
    if outside_window:
        warnings.append(
            "Часть выбранных WB-поставок не попала в inbound window factory-order и не добавлена в coverage: "
            + str(len(outside_window))
            + " строк состава."
        )
    diagnostics = {
        "added_inbound_ff_to_wb_by_nm_id": {str(nm_id): qty for nm_id, qty in sorted(added_by_nm.items())},
        "added_inbound_ff_to_wb_qty_total": round(sum(added_by_nm.values()), 4),
        "outside_inbound_window_events": outside_window,
        "inbound_window": {
            "report_date": report_date.isoformat(),
            "inbound_window_end": inbound_window_end.isoformat(),
        },
    }
    return rows, diagnostics, tuple(warnings)


def regional_overlay_quantities(
    *,
    overlay: Mapping[str, Any],
) -> tuple[dict[int, dict[str, float]], dict[str, Any], tuple[str, ...]]:
    qty_by_nm_district: dict[int, dict[str, float]] = {}
    by_district: dict[str, float] = {key: 0.0 for key in DISTRICT_KEYS}
    unmapped_events: list[dict[str, Any]] = []
    warnings: list[str] = []
    for event in overlay.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        district_key = str(event.get("district_key") or "").strip()
        quantity = float(event.get("quantity") or 0.0)
        if quantity <= 0:
            continue
        if district_key not in DISTRICT_KEYS:
            unmapped_events.append(dict(event))
            continue
        nm_id = int(event.get("nm_id"))
        qty_by_nm_district.setdefault(nm_id, {})
        qty_by_nm_district[nm_id][district_key] = qty_by_nm_district[nm_id].get(district_key, 0.0) + quantity
        by_district[district_key] += quantity
    if unmapped_events:
        warnings.append(
            "WB regional overlay: часть выбранных WB-поставок не учтена по округам, потому что склад не сопоставлен: "
            + str(len(unmapped_events))
            + " строк состава."
        )
    diagnostics = {
        "added_qty_by_district": {key: by_district[key] for key in DISTRICT_KEYS},
        "added_qty_total": round(sum(by_district.values()), 4),
        "unmapped_events": unmapped_events,
    }
    return qty_by_nm_district, diagnostics, tuple(warnings)


def overlay_to_public_payload(
    *,
    overlay: Mapping[str, Any],
    stock_ff_diagnostics: Mapping[str, Any],
    factory_order_diagnostics: Mapping[str, Any] | None = None,
    wb_regional_diagnostics: Mapping[str, Any] | None = None,
    extra_warnings: tuple[str, ...] = (),
) -> dict[str, Any]:
    warnings = [
        *[str(item) for item in overlay.get("warnings") or [] if item],
        *[str(item) for item in extra_warnings if item],
    ]
    payload = dict(overlay)
    payload["stock_ff"] = dict(stock_ff_diagnostics)
    payload["warnings"] = warnings
    if factory_order_diagnostics is not None:
        payload["factory_order"] = dict(factory_order_diagnostics)
    if wb_regional_diagnostics is not None:
        payload["wb_regional"] = dict(wb_regional_diagnostics)
    return payload


def _candidate_from_record(
    record: Mapping[str, Any],
    *,
    active_sku_map: Mapping[int, str],
    mapping: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = augment_supply_row_with_district(dict(record.get("normalized") or {}), mapping)
    raw_list = record.get("raw_list") if isinstance(record.get("raw_list"), Mapping) else normalized.get("raw_list")
    raw_detail = record.get("raw_detail") if isinstance(record.get("raw_detail"), Mapping) else normalized.get("raw_detail")
    raw_goods = record.get("raw_goods") if isinstance(record.get("raw_goods"), list) else normalized.get("raw_goods")
    if not isinstance(raw_list, Mapping):
        raw_list = {}
    if not isinstance(raw_detail, Mapping):
        raw_detail = {}
    if not isinstance(raw_goods, list):
        raw_goods = None
    target_warehouse = target_warehouse_for_supply(normalized)
    selected_date, date_evidence, date_source_field = _select_supply_operational_date(
        normalized=normalized,
        raw_detail=raw_detail,
        raw_list=raw_list,
    )
    quantity_by_nm, skipped_goods = _quantity_by_active_nm_id(raw_goods, active_sku_map=active_sku_map)
    usable_total_qty = sum(quantity_by_nm.values())
    status_id = _optional_int(normalized.get("status_id"))
    status_label = str(normalized.get("status_label") or _status_label(status_id))
    disabled_reasons: list[str] = []
    if _record_is_doprinato(record, normalized):
        disabled_reasons.append("тип «Допринято» не учитывается")
    if not _status_is_eligible(status_id, status_label):
        disabled_reasons.append(_status_disabled_reason(status_id, status_label))
    if not selected_date:
        disabled_reasons.append("нет расчётной даты поставки")
    if raw_goods is None or not raw_goods:
        disabled_reasons.append("нет состава поставки")
    elif usable_total_qty <= 0:
        disabled_reasons.append("нет usable active SKU quantity")
    supply_id = str(normalized.get("supply_id") or record.get("supply_id") or "").strip()
    return {
        "supply_id": supply_id,
        "cache_key": str(normalized.get("cache_key") or record.get("cache_key") or "").strip(),
        "wb_supply_id": str(normalized.get("wb_supply_id") or record.get("wb_supply_id") or "").strip(),
        "preorder_id": str(normalized.get("preorder_id") or record.get("preorder_id") or "").strip(),
        "number_label": str(normalized.get("number_label") or normalized.get("visible_number") or supply_id),
        "status_id": status_id,
        "status_label": status_label,
        "selected_date": selected_date,
        "date_evidence": date_evidence,
        "date_source_field": date_source_field,
        "warehouse_id": target_warehouse["warehouse_id"],
        "warehouse_name": target_warehouse["warehouse_name"],
        "warehouse_display": str(normalized.get("warehouse_display") or target_warehouse["warehouse_name"]),
        "district_source_warehouse_id": str(normalized.get("district_source_warehouse_id") or target_warehouse["warehouse_id"]),
        "district_source_warehouse_name": str(normalized.get("district_source_warehouse_name") or target_warehouse["warehouse_name"]),
        "district_source_warehouse_role": str(normalized.get("district_source_warehouse_role") or target_warehouse.get("warehouse_role") or ""),
        "district_source_warehouse_evidence": str(
            normalized.get("district_source_warehouse_evidence") or target_warehouse.get("warehouse_evidence") or ""
        ),
        "district_key": str(normalized.get("district_key") or DISTRICT_UNMAPPED),
        "district_label_ru": str(normalized.get("district_label_ru") or ""),
        "district_short_label_ru": str(normalized.get("district_short_label_ru") or ""),
        "district_mapping_status": str(normalized.get("district_mapping_status") or ""),
        "district_mapping_source": str(normalized.get("district_mapping_source") or ""),
        "district_mapping_evidence": str(normalized.get("district_mapping_evidence") or ""),
        "district_mapping_confidence": str(normalized.get("district_mapping_confidence") or ""),
        "usable_sku_count": len(quantity_by_nm),
        "usable_total_qty": usable_total_qty,
        "quantity_by_nm_id": {str(nm_id): qty for nm_id, qty in sorted(quantity_by_nm.items())},
        "skipped_goods": skipped_goods,
        "skipped_goods_count": len(skipped_goods),
        "disabled": bool(disabled_reasons),
        "eligible_for_overlay": not disabled_reasons,
        "disabled_reasons": disabled_reasons,
    }


def _select_supply_operational_date(
    *,
    normalized: Mapping[str, Any],
    raw_detail: Mapping[str, Any],
    raw_list: Mapping[str, Any],
) -> tuple[str, str, str]:
    sources = (("normalized", normalized), ("detail", raw_detail), ("list", raw_list))
    for source_name, source in sources:
        for field_name in _DATE_FIELD_CANDIDATES:
            if field_name not in source:
                continue
            parsed = _parse_date_value(source.get(field_name))
            if parsed is not None:
                return parsed.isoformat(), f"{source_name}.{field_name}", field_name
    return "", "", ""


def _quantity_by_active_nm_id(
    raw_goods: list[Any] | None,
    *,
    active_sku_map: Mapping[int, str],
) -> tuple[dict[int, float], list[dict[str, Any]]]:
    quantity_by_nm: dict[int, float] = {}
    skipped: list[dict[str, Any]] = []
    for index, item in enumerate(raw_goods or []):
        if not isinstance(item, Mapping):
            skipped.append({"raw_index": index, "reason": "invalid_goods_row"})
            continue
        nm_id = _optional_int(_first_value(item, "nmID", "nmId", "nm_id"))
        quantity = _optional_number(_first_value(item, "quantity", "qty"))
        if nm_id is None:
            skipped.append({"raw_index": index, "reason": "missing_nm_id", "quantity": quantity})
            continue
        if nm_id not in active_sku_map:
            skipped.append(
                {
                    "raw_index": index,
                    "reason": "nm_id_not_active",
                    "nm_id": nm_id,
                    "quantity": quantity,
                }
            )
            continue
        if quantity is None or quantity <= 0:
            skipped.append(
                {
                    "raw_index": index,
                    "reason": "missing_or_nonpositive_quantity",
                    "nm_id": nm_id,
                    "quantity": quantity,
                }
            )
            continue
        quantity_by_nm[nm_id] = quantity_by_nm.get(nm_id, 0.0) + quantity
    return quantity_by_nm, skipped


def _records_by_identity(records: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for record in records:
        normalized = record.get("normalized") if isinstance(record.get("normalized"), Mapping) else {}
        for key in (
            record.get("supply_id"),
            record.get("cache_key"),
            record.get("wb_supply_id"),
            record.get("preorder_id"),
            normalized.get("supply_id"),
            normalized.get("cache_key"),
            normalized.get("wb_supply_id"),
            normalized.get("preorder_id"),
        ):
            text = str(key or "").strip()
            if not text:
                continue
            result.setdefault(text, record)
            if text.startswith("supply:"):
                result.setdefault(text.removeprefix("supply:"), record)
            elif text.startswith("preorder:"):
                result.setdefault(text.removeprefix("preorder:"), record)
            else:
                result.setdefault(f"supply:{text}", record)
                result.setdefault(f"preorder:{text}", record)
    return result


def _build_reference_index(
    rows: list[Mapping[str, Any]],
    *,
    source: str,
    name_keys: tuple[str, ...],
    district_keys: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        name = _first_string(item, *name_keys)
        normalized_name = _normalize_warehouse_name(name)
        if not normalized_name:
            continue
        raw_district = _first_value(item, *district_keys)
        district_key = (
            str(raw_district)
            if str(raw_district or "").strip() in DISTRICT_KEYS
            else convert_raw_district_to_key(raw_district)
        )
        if district_key not in DISTRICT_KEYS:
            continue
        result.setdefault(
            normalized_name,
            {
                "warehouse_name": name,
                "district_key": district_key,
                "district_label_ru": DISTRICT_LABELS_RU[district_key],
                "district_short_label_ru": DISTRICT_SHORT_LABELS_RU[district_key],
                "source": source,
                "evidence": f"{source}.name={name}; raw_district={raw_district}",
                "confidence": "name_exact",
            },
        )
    return result


def _build_manual_fallback_index() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, (district_key, evidence) in _MANUAL_WAREHOUSE_DISTRICT_FALLBACKS.items():
        normalized_name = _normalize_warehouse_name(name)
        if not normalized_name or district_key not in DISTRICT_KEYS:
            continue
        result.setdefault(
            normalized_name,
            {
                "warehouse_name": name,
                "district_key": district_key,
                "district_label_ru": DISTRICT_LABELS_RU[district_key],
                "district_short_label_ru": DISTRICT_SHORT_LABELS_RU[district_key],
                "source": "manual_known_wb_warehouse",
                "evidence": f"manual_known_wb_warehouse.name={name}; {evidence}",
                "confidence": "manual_known_name",
            },
        )
    return result


def _build_trusted_cached_source_index(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        district_key = str(row.get("district_key") or row.get("warehouse_district_key") or "").strip()
        if district_key not in DISTRICT_KEYS:
            continue
        source_name = str(row.get("district_source_warehouse_name") or "").strip()
        if not source_name:
            continue
        source = str(row.get("district_mapping_source") or "").strip()
        if source in {"", "unmapped"}:
            continue
        normalized_name = _normalize_warehouse_name(source_name)
        if not normalized_name:
            continue
        result.setdefault(
            normalized_name,
            {
                "warehouse_name": source_name,
                "district_key": district_key,
                "district_label_ru": DISTRICT_LABELS_RU[district_key],
                "district_short_label_ru": DISTRICT_SHORT_LABELS_RU[district_key],
                "source": "cached_verified_source_warehouse",
                "evidence": (
                    "cached_verified_source_warehouse.name="
                    + source_name
                    + "; source="
                    + source
                    + "; evidence="
                    + str(row.get("district_mapping_evidence") or "")
                ),
                "confidence": "cached_verified_source",
            },
        )
    return result


def _collect_target_warehouse_names(
    supply_rows: list[Mapping[str, Any]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in supply_rows:
        if not isinstance(row, Mapping):
            continue
        target = district_source_warehouse_for_supply(row)
        name = str(target.get("warehouse_name") or "").strip()
        normalized = _normalize_warehouse_name(name)
        if normalized:
            result.setdefault(normalized, name)
    return result


def _status_is_eligible(status_id: int | None, label: str) -> bool:
    del label
    return status_id in ELIGIBLE_WB_SUPPLY_STATUS_IDS


def _status_disabled_reason(status_id: int | None, label: str) -> str:
    if status_id == 1:
        return "статус «Не запланировано» не учитывается"
    if status_id == 2:
        return "статус «Запланировано» не учитывается: это бронь слота WB"
    if status_id == 5:
        return "статус «Принято» не учитывается"
    return "статус не входит в eligible WB-поставки: " + (label or _status_label(status_id))


def _is_doprinato_supply(normalized: Mapping[str, Any]) -> bool:
    virtual_type_id = _optional_int(_first_value(normalized, "virtual_type_id", "virtualTypeID"))
    if virtual_type_id == 5:
        return True
    type_label = _normalize_text(_first_value(normalized, "type_label", "typeLabel"))
    return type_label == "допринято"


def _record_is_doprinato(record: Mapping[str, Any], normalized: Mapping[str, Any]) -> bool:
    if _is_doprinato_supply(normalized):
        return True
    for key in ("raw_list", "raw_detail"):
        raw = record.get(key)
        if isinstance(raw, Mapping) and _is_doprinato_supply(raw):
            return True
    return False


def _status_label(status_id: int | None) -> str:
    labels = {
        1: "Не запланировано",
        2: "Запланировано",
        3: "Отгрузка разрешена",
        4: "Идёт приёмка",
        5: "Принято",
        6: "Отгружено на воротах",
    }
    return labels.get(status_id, f"Статус {status_id}" if status_id is not None else "—")


def _overlay_option_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    parsed = _parse_date_value(item.get("selected_date"))
    date_key = parsed.toordinal() if parsed is not None else 0
    disabled_rank = 1 if item.get("disabled") else 0
    return (disabled_rank, -date_key, str(item.get("number_label") or item.get("supply_id") or ""))


def _numeric_items(value: Any) -> list[tuple[int, float]]:
    if not isinstance(value, Mapping):
        return []
    result: list[tuple[int, float]] = []
    for key, raw_value in value.items():
        nm_id = _optional_int(key)
        quantity = _optional_number(raw_value)
        if nm_id is not None and quantity is not None:
            result.append((nm_id, quantity))
    return result


def _normalize_warehouse_name(value: Any) -> str:
    normalized = _normalize_text(value)
    return re.sub(r"\bсклад\b", "", normalized).strip()


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date_value(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    date_part = raw[:10]
    try:
        return date.fromisoformat(date_part)
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).date()
        except ValueError:
            continue
    return None


def _format_qty(value: float) -> str:
    if abs(value - round(value)) < 0.005:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _first_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _first_string(mapping: Mapping[str, Any], *keys: str) -> str:
    value = _first_value(mapping, *keys)
    return str(value or "").strip()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
