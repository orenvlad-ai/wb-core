"""Read-only closed-day stock report for the sheet_vitrina_v1 operator page."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from packages.application.demand_estimation import (
    estimate_availability_adjusted_demand,
    parse_sales_avg_period_days,
    sales_lookup_days as calculate_sales_lookup_days,
)
from packages.application.ff_stock_ledger import FfStockLedgerBlock
from packages.application.factory_order_sales_history import (
    SALES_HISTORY_SOURCE_KEY,
    describe_runtime_sales_history_coverage,
    load_runtime_sales_history_payloads,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_report_snapshot_selection import select_latest_ready_snapshot_dates
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    current_business_date_iso,
    default_business_as_of_date,
)
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope
from packages.contracts.supplier_shipments import (
    LINE_TYPE_PRODUCT,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_MATCHED_BY_BARCODE,
    MATCH_STATUS_MATCHED_BY_COMPATIBILITY,
    ORDER_STATUS_IN_TRANSIT,
    ORDER_STATUS_LABELS_RU,
    ORDER_STATUS_PRODUCTION,
)

TEMPORAL_SLOT_YESTERDAY_CLOSED = "yesterday_closed"
STOCK_ALERT_THRESHOLD = 50.0
PROMO_PARTICIPATION_METRIC_KEY = "promo_participation"
EPS = 1e-9
WB_SUPPLY_EXCLUDED_STATUS_IDS = {1, 2, 5}
WB_SUPPLY_STATUS_LABELS_RU = {
    1: "Не запланировано",
    2: "Запланировано",
    3: "Отгрузка разрешена",
    4: "Идёт приёмка",
    5: "Принято",
    6: "Отгружено на воротах",
}
WB_SUPPLY_EXCLUDED_STATUS_LABELS = {
    "принято",
    "запланировано",
    "не запланировано",
    "незапланировано",
}
SUPPLIER_SHIPMENT_CYCLE_STATUS_KEYS = (ORDER_STATUS_PRODUCTION, ORDER_STATUS_IN_TRANSIT)
SUPPLIER_SHIPMENT_LINE_MATCH_STATUSES = {
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_MATCHED_BY_BARCODE,
    MATCH_STATUS_MATCHED_BY_COMPATIBILITY,
}
STOCK_REPORT_DISTRICTS = (
    ("stock_ru_central", "Центральный"),
    ("stock_ru_northwest", "Северо-Западный"),
    ("stock_ru_volga", "Приволжский"),
    ("stock_ru_ural", "Уральский"),
    ("stock_ru_south_caucasus", "Юг/СКФО"),
)
REPORT_NOTES = (
    "По умолчанию отчёт использует previous closed business day через persisted ready snapshot и slot yesterday_closed.",
    "При explicit as_of_date route остаётся server-owned и читает именно requested closed business day, без upstream fetch.",
    "Строки отчёта строятся по всем active SKU из current config_v2; legacy threshold <50 больше не является критерием включения.",
    "Период усреднения продаж означает целевое число валидных торговых дней; отчёт читает только persisted sales_funnel_history.",
    "Участие в акции читается из canonical metric promo_participation: numeric >0 = Да, numeric 0 = Нет, missing = н/д.",
    "На произв. и в пути Китай читаются из current supplier shipment registry по product lines internal_nm_id -> qty: статусы production = На производстве, in_transit = В пути.",
    "Поставки ВБ считаются из current WB supplies cache по goods composition nmId -> quantity; исключены только статусы 1/2/5 = Не запланировано/Запланировано/Принято.",
    "Ост. ФФ читается из server-owned ФФ stock ledger current balances по active SKU.",
    "Ост. ВБ = прежний stock_total из WB stocks ready snapshot; semantics данных не меняется.",
    "Строка Итого агрегирует количественные остатки/поставки суммой, продажи/день — суммой SKU daily demand, дни — как aggregate stock / aggregate burn.",
    "Дней по округам считается по positive stock depletion между consecutive persisted ready snapshots; restock/increase и gaps не превращаются в расход.",
    "Merged bucket `ДВ и Сибирь` целиком исключён из текущего report contour: current truth не делит его на отдельный Дальний Восток и Сибирь.",
)


@dataclass(frozen=True)
class SnapshotSlotView:
    as_of_date: str
    slot_date: str
    sku_values: dict[int, dict[str, float | None]]


@dataclass(frozen=True)
class SalesSamplesWindow:
    date_from: str
    date_to: str
    samples_by_nm_id: dict[int, list[tuple[str, float]]]
    missing_date_count: int
    missing_pair_count: int
    available_date_count: int
    coverage_earliest_date: str | None
    coverage_latest_date: str | None
    coverage_snapshot_count: int


@dataclass(frozen=True)
class DistrictBurnEstimate:
    avg_daily_burn: float | None
    valid_day_count: int
    missing_day_count: int
    restock_day_count: int
    zero_depletion_day_count: int
    gap_day_count: int
    lookup_pair_count: int
    earliest_used_date: str
    latest_used_date: str
    warning: str


class SheetVitrinaV1StockReportBlock:
    """Build an operator-facing closed-day stock table from persisted server truth."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        as_of_date: str | None = None,
        sales_avg_period_days: int | str | None = None,
    ) -> dict[str, Any]:
        parsed_sales_avg_period_days = parse_sales_avg_period_days(sales_avg_period_days)
        parsed_sales_lookup_days = calculate_sales_lookup_days(parsed_sales_avg_period_days)
        business_date = date.fromisoformat(current_business_date_iso(self.now_factory()))
        current_business_date = business_date.isoformat()
        explicit_as_of_date = str(as_of_date or "").strip() or None
        requested_as_of_date = explicit_as_of_date or default_business_as_of_date(self.now_factory())
        date.fromisoformat(requested_as_of_date)
        effective_as_of_date = requested_as_of_date
        base_payload = {
            "status": "unavailable",
            "reason": "",
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "current_business_date": current_business_date,
            "requested_as_of_date": requested_as_of_date,
            "explicit_as_of_date": explicit_as_of_date,
            "report_date": effective_as_of_date,
            "threshold_lt": int(STOCK_ALERT_THRESHOLD),
            "sales_avg_period_days": parsed_sales_avg_period_days,
            "sales_lookup_days": parsed_sales_lookup_days,
            "notes": list(REPORT_NOTES),
            "warnings": [],
            "available_as_of_dates": [],
            "districts": [
                {
                    "metric_key": metric_key,
                    "label": label,
                }
                for metric_key, label in STOCK_REPORT_DISTRICTS
            ],
            "source_of_truth": {
                "read_model": "persisted_ready_snapshot",
                "sheet_name": "DATA_VITRINA",
                "snapshot_as_of_date": effective_as_of_date,
                "temporal_slot": TEMPORAL_SLOT_YESTERDAY_CLOSED,
                "slot_date": effective_as_of_date,
                "sales_history_source": SALES_HISTORY_SOURCE_KEY,
                "supplier_shipments_source": "sheet_vitrina_v1_supplier_shipments runtime registry",
                "supplier_shipments_quantity_source": "product lines internal_nm_id -> qty",
                "supplier_shipments_included_statuses": [
                    {
                        "code": ORDER_STATUS_PRODUCTION,
                        "label_ru": ORDER_STATUS_LABELS_RU[ORDER_STATUS_PRODUCTION],
                        "report_column": "на произв.",
                    },
                    {
                        "code": ORDER_STATUS_IN_TRANSIT,
                        "label_ru": ORDER_STATUS_LABELS_RU[ORDER_STATUS_IN_TRANSIT],
                        "report_column": "в пути Китай",
                    },
                ],
                "wb_supplies_source": "sheet_vitrina_v1_wb_supplies runtime cache",
                "wb_supplies_quantity_source": "raw_goods nmId -> quantity",
                "wb_supplies_excluded_status_ids": sorted(WB_SUPPLY_EXCLUDED_STATUS_IDS),
                "wb_supplies_excluded_status_labels": [
                    WB_SUPPLY_STATUS_LABELS_RU[1],
                    WB_SUPPLY_STATUS_LABELS_RU[2],
                    WB_SUPPLY_STATUS_LABELS_RU[5],
                ],
                "stock_ff_source": "ff_stock_ledger current balances",
                "district_burn_source": "persisted_ready_snapshot_consecutive_depletion",
            },
        }

        try:
            current_state = self.runtime.load_current_state()
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Отчёт по остаткам пока недоступен: {exc}",
            }

        if explicit_as_of_date is None:
            try:
                selection = select_latest_ready_snapshot_dates(
                    self.runtime,
                    requested_as_of_date=requested_as_of_date,
                    limit=1,
                )
            except ValueError as exc:
                return {
                    **base_payload,
                    "reason": f"Отчёт по остаткам пока недоступен: {exc}",
                }
            if selection.latest_as_of_date is None:
                return {
                    **base_payload,
                    "available_as_of_dates": list(selection.available_as_of_dates),
                    "reason": (
                        "Отчёт по остаткам пока недоступен: нет persisted ready snapshot "
                        f"не позднее {requested_as_of_date}"
                    ),
                }
            effective_as_of_date = selection.latest_as_of_date
            base_payload = {
                **base_payload,
                "report_date": effective_as_of_date,
                "available_as_of_dates": list(selection.available_as_of_dates),
                "source_of_truth": {
                    **base_payload["source_of_truth"],
                    "snapshot_as_of_date": effective_as_of_date,
                    "slot_date": effective_as_of_date,
                },
            }

        try:
            snapshot = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=effective_as_of_date)
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Отчёт по остаткам пока недоступен: отсутствует ready snapshot для {effective_as_of_date} ({exc})",
            }

        try:
            closed_view = _extract_closed_slot_view(snapshot, expected_closed_date=effective_as_of_date)
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Отчёт по остаткам пока недоступен: {exc}",
            }

        active_items = _active_config_items(current_state.config_v2)
        active_nm_ids = [int(item.nm_id) for item in active_items]
        active_sku_pairs = [(int(item.nm_id), str(getattr(item, "display_name"))) for item in active_items]
        nomenclature_by_nm = _load_active_nomenclature_by_nm(self.runtime)
        supplier_cycle_projection = _load_supplier_shipment_cycle_quantities_by_nm(self.runtime, active_nm_ids)
        wb_supplies_projection = _load_wb_supplies_inbound_by_nm(self.runtime, active_nm_ids)
        ff_stock_by_nm = _load_ff_stock_balances_by_nm(self.runtime, active_sku_pairs)
        demand_reference_date = date.fromisoformat(closed_view.slot_date) + timedelta(days=1)
        sales_window = _load_persisted_order_count_samples(
            runtime=self.runtime,
            date_from=(demand_reference_date - timedelta(days=parsed_sales_lookup_days)).isoformat(),
            date_to=closed_view.slot_date,
            nm_ids=active_nm_ids,
        )
        district_burn_by_key = _build_district_burn_lookup(
            runtime=self.runtime,
            report_date=closed_view.slot_date,
            nm_ids=active_nm_ids,
            sales_avg_period_days=parsed_sales_avg_period_days,
            sales_lookup_days=parsed_sales_lookup_days,
        )

        rows: list[dict[str, Any]] = []
        insufficient_sales_rows = 0
        insufficient_district_rows = 0
        for active_order, config_item in enumerate(active_items):
            nm_id = int(config_item.nm_id)
            sku_values = closed_view.sku_values.get(nm_id, {})
            stock_total = sku_values.get("stock_total")
            demand_estimate = estimate_availability_adjusted_demand(
                sales_window.samples_by_nm_id.get(nm_id, []),
                report_date=demand_reference_date,
                sales_avg_period_days=parsed_sales_avg_period_days,
                sales_lookup_days=parsed_sales_lookup_days,
            )
            if demand_estimate.valid_sales_day_count < parsed_sales_avg_period_days:
                insufficient_sales_rows += 1
            avg_sales_per_day = (
                float(demand_estimate.daily_demand_total)
                if demand_estimate.valid_sales_day_count > 0
                else None
            )
            days_left_total = _days_left(stock_total, avg_sales_per_day)

            districts: list[dict[str, Any]] = []
            zero_district_count = 0
            row_has_insufficient_district = False
            for metric_key, label in STOCK_REPORT_DISTRICTS:
                stock_value = sku_values.get(metric_key)
                if stock_value is not None and abs(float(stock_value)) <= EPS:
                    zero_district_count += 1
                burn_estimate = district_burn_by_key.get((nm_id, metric_key)) or _empty_district_burn_estimate(
                    sales_avg_period_days=parsed_sales_avg_period_days,
                )
                if burn_estimate.valid_day_count < parsed_sales_avg_period_days:
                    row_has_insufficient_district = True
                avg_daily_burn = (
                    burn_estimate.avg_daily_burn
                    if burn_estimate.avg_daily_burn and burn_estimate.avg_daily_burn > 0
                    else None
                )
                districts.append(
                    {
                        "metric_key": metric_key,
                        "label": label,
                        "stock": None if stock_value is None else float(stock_value),
                        "avg_daily_burn": None if avg_daily_burn is None else float(avg_daily_burn),
                        "days_left": _days_left(stock_value, avg_daily_burn),
                        "diagnostics": asdict(burn_estimate),
                    }
                )
            if row_has_insufficient_district:
                insufficient_district_rows += 1

            nomenclature_item = nomenclature_by_nm.get(nm_id, {})
            display_name = str(getattr(config_item, "display_name"))
            nomenclature_name = str(nomenclature_item.get("nomenclature_name") or "").strip()
            identity_name = nomenclature_name or display_name
            promotion_payload = _promotion_participation_payload(
                sku_values.get(PROMO_PARTICIPATION_METRIC_KEY)
            )
            supplier_cycle_quantities = supplier_cycle_projection["quantity_by_nm_id"].get(nm_id, {})
            supplier_production_qty = float(supplier_cycle_quantities.get(ORDER_STATUS_PRODUCTION, 0.0))
            supplier_in_transit_qty = float(supplier_cycle_quantities.get(ORDER_STATUS_IN_TRANSIT, 0.0))
            rows.append(
                {
                    "nm_id": nm_id,
                    "display_name": display_name,
                    "nomenclature_name": nomenclature_name,
                    "identity_label": f"{identity_name} · nmId {nm_id}",
                    "active_order": active_order,
                    "promotion_participation": promotion_payload["value"],
                    "promotion_participation_label": promotion_payload["label"],
                    "supplier_production_qty": supplier_production_qty,
                    "supplier_in_transit_qty": supplier_in_transit_qty,
                    "wb_supplies_inbound_qty": float(wb_supplies_projection["quantity_by_nm_id"].get(nm_id, 0.0)),
                    "stock_ff": float(ff_stock_by_nm.get(nm_id, 0.0)),
                    "stock_wb": None if stock_total is None else float(stock_total),
                    "stock_total": None if stock_total is None else float(stock_total),
                    "zero_district_count": zero_district_count,
                    "avg_sales_per_day": avg_sales_per_day,
                    "days_left_total": days_left_total,
                    "districts": districts,
                    "diagnostics": {
                        "sales": {
                            **asdict(demand_estimate),
                            "missing_sales_history_date_count": sales_window.missing_date_count,
                            "missing_sales_history_pair_count": sales_window.missing_pair_count,
                            "available_sales_history_date_count": sales_window.available_date_count,
                            "coverage_earliest_date": sales_window.coverage_earliest_date,
                            "coverage_latest_date": sales_window.coverage_latest_date,
                            "coverage_snapshot_count": sales_window.coverage_snapshot_count,
                        },
                        "promotion": promotion_payload["diagnostics"],
                        "supplier_shipments": {
                            "source": "supplier_shipments_registry",
                            "quantity_source": "product lines internal_nm_id -> qty",
                            "included_statuses": list(SUPPLIER_SHIPMENT_CYCLE_STATUS_KEYS),
                            "production_quantity": supplier_production_qty,
                            "in_transit_quantity": supplier_in_transit_qty,
                        },
                        "wb_supplies": {
                            "source": "wb_supplies_cache",
                            "excluded_status_ids": sorted(WB_SUPPLY_EXCLUDED_STATUS_IDS),
                            "quantity": float(wb_supplies_projection["quantity_by_nm_id"].get(nm_id, 0.0)),
                        },
                        "stock_ff": {
                            "source": "ff_stock_ledger",
                            "quantity": float(ff_stock_by_nm.get(nm_id, 0.0)),
                        },
                        "stock_wb": {
                            "source": "persisted_ready_snapshot.stock_total",
                            "metric_key": "stock_total",
                        },
                    },
                }
            )

        warnings = _build_report_warnings(
            sales_window=sales_window,
            active_row_count=len(rows),
            insufficient_sales_rows=insufficient_sales_rows,
            insufficient_district_rows=insufficient_district_rows,
            sales_avg_period_days=parsed_sales_avg_period_days,
        )
        summary_row = _build_stock_report_summary_row(rows)

        return {
            **base_payload,
            "status": "available",
            "report_date": closed_view.slot_date,
            "row_count": len(rows),
            "active_sku_count": len(rows),
            "rows": rows,
            "summary_row": summary_row,
            "warnings": warnings,
            "notes": list(REPORT_NOTES) + warnings,
            "sales_history_window": {
                "date_from": sales_window.date_from,
                "date_to": sales_window.date_to,
                "available_date_count": sales_window.available_date_count,
                "missing_date_count": sales_window.missing_date_count,
                "missing_pair_count": sales_window.missing_pair_count,
                "coverage_earliest_date": sales_window.coverage_earliest_date,
                "coverage_latest_date": sales_window.coverage_latest_date,
                "coverage_snapshot_count": sales_window.coverage_snapshot_count,
            },
            "supplier_shipments_cycle_summary": supplier_cycle_projection["summary"],
            "wb_supplies_inbound_summary": wb_supplies_projection["summary"],
            "source_of_truth": {
                **base_payload["source_of_truth"],
                "slot_date": closed_view.slot_date,
            },
        }


def list_active_sku_options(config_items: list[Any]) -> list[dict[str, Any]]:
    active_items = _active_config_items(config_items)
    options: list[dict[str, Any]] = []
    seen_nm_ids: set[int] = set()
    for item in active_items:
        nm_id = int(getattr(item, "nm_id"))
        if nm_id in seen_nm_ids:
            continue
        seen_nm_ids.add(nm_id)
        display_name = str(getattr(item, "display_name"))
        options.append(
            {
                "nm_id": nm_id,
                "display_name": display_name,
                "identity_label": f"{display_name} · nmId {nm_id}",
            }
        )
    return options


def _active_config_items(config_items: list[Any]) -> list[Any]:
    return sorted(
        [item for item in config_items if getattr(item, "enabled", False)],
        key=lambda item: getattr(item, "display_order", 0),
    )


def _extract_closed_slot_view(
    plan: SheetVitrinaV1Envelope,
    *,
    expected_closed_date: str,
) -> SnapshotSlotView:
    slot_index = None
    slot_date = ""
    for index, slot in enumerate(plan.temporal_slots):
        if slot.slot_key == TEMPORAL_SLOT_YESTERDAY_CLOSED:
            slot_index = index
            slot_date = slot.column_date
            break
    if slot_index is None:
        raise ValueError(f"ready snapshot {plan.as_of_date} does not contain yesterday_closed slot")
    if slot_date != expected_closed_date:
        raise ValueError(
            f"ready snapshot {plan.as_of_date} points yesterday_closed to {slot_date}, expected {expected_closed_date}"
        )

    data_sheet = next((item for item in plan.sheets if item.sheet_name == "DATA_VITRINA"), None)
    if data_sheet is None:
        raise ValueError(f"ready snapshot {plan.as_of_date} does not contain DATA_VITRINA")

    value_index = 2 + slot_index
    sku_values: dict[int, dict[str, float | None]] = {}
    for row in data_sheet.rows:
        if len(row) <= value_index:
            continue
        key = str(row[1] or "")
        value = _coerce_numeric(row[value_index])
        if not key.startswith("SKU:") or "|" not in key:
            continue
        scope_token, metric_key = key.split("|", 1)
        try:
            nm_id = int(scope_token.split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        sku_values.setdefault(nm_id, {})[metric_key] = value

    return SnapshotSlotView(
        as_of_date=plan.as_of_date,
        slot_date=slot_date,
        sku_values=sku_values,
    )


def _coerce_numeric(value: Any) -> float | None:
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _load_active_nomenclature_by_nm(runtime: RegistryUploadDbBackedRuntime) -> dict[int, dict[str, Any]]:
    try:
        items = runtime.list_nomenclature_items(active_only=True)
    except Exception:
        return {}
    by_nm: dict[int, dict[str, Any]] = {}
    for item in items:
        nm_id = _optional_int(item.get("nm_id"))
        if nm_id is None or nm_id in by_nm:
            continue
        by_nm[nm_id] = dict(item)
    return by_nm


def _load_ff_stock_balances_by_nm(
    runtime: RegistryUploadDbBackedRuntime,
    active_skus: list[tuple[int, str]],
) -> dict[int, float]:
    rows = FfStockLedgerBlock(runtime=runtime).current_balance_rows_for_active_skus(active_skus)
    balances: dict[int, float] = {}
    for row in rows:
        nm_id = _optional_int(row.get("nm_id"))
        if nm_id is None:
            continue
        balances[nm_id] = float(_optional_number(row.get("current_stock_ff")) or 0.0)
    return balances


def _load_supplier_shipment_cycle_quantities_by_nm(
    runtime: RegistryUploadDbBackedRuntime,
    active_nm_ids: list[int],
) -> dict[str, Any]:
    active_set = {int(nm_id) for nm_id in active_nm_ids}
    quantity_by_nm_id = {
        int(nm_id): {status_key: 0.0 for status_key in SUPPLIER_SHIPMENT_CYCLE_STATUS_KEYS}
        for nm_id in active_nm_ids
    }
    summary: dict[str, Any] = {
        "source": "sheet_vitrina_v1_supplier_shipments runtime registry",
        "quantity_source": "product lines internal_nm_id -> qty",
        "included_statuses": [
            {
                "code": status_key,
                "label_ru": ORDER_STATUS_LABELS_RU.get(status_key, status_key),
            }
            for status_key in SUPPLIER_SHIPMENT_CYCLE_STATUS_KEYS
        ],
        "status": "unavailable",
        "shipments_total": 0,
        "shipments_included": 0,
        "shipments_skipped_by_status": 0,
        "shipments_missing_detail": 0,
        "lines_counted": 0,
        "lines_skipped": 0,
        "lines_skipped_by_match_status": 0,
        "quantity_by_status": {status_key: 0.0 for status_key in SUPPLIER_SHIPMENT_CYCLE_STATUS_KEYS},
    }
    try:
        shipments = runtime.list_supplier_shipments()
    except Exception as exc:
        summary["reason"] = str(exc)
        return {"quantity_by_nm_id": quantity_by_nm_id, "summary": summary}

    summary["status"] = "available"
    summary["shipments_total"] = len(shipments)
    for shipment in shipments:
        if not isinstance(shipment, Mapping):
            summary["shipments_skipped_by_status"] += 1
            continue
        shipment_status = str(shipment.get("order_status") or ORDER_STATUS_PRODUCTION).strip()
        if shipment_status not in SUPPLIER_SHIPMENT_CYCLE_STATUS_KEYS:
            summary["shipments_skipped_by_status"] += 1
            continue
        shipment_id = str(shipment.get("shipment_id") or "").strip()
        if not shipment_id:
            summary["shipments_missing_detail"] += 1
            continue
        try:
            detail = runtime.load_supplier_shipment(shipment_id)
        except Exception:
            summary["shipments_missing_detail"] += 1
            continue
        if not isinstance(detail, Mapping):
            summary["shipments_missing_detail"] += 1
            continue
        lines = detail.get("lines")
        if not isinstance(lines, list):
            summary["shipments_missing_detail"] += 1
            continue
        summary["shipments_included"] += 1
        for line in lines:
            if not isinstance(line, Mapping):
                summary["lines_skipped"] += 1
                continue
            if str(line.get("line_type") or "") != LINE_TYPE_PRODUCT:
                summary["lines_skipped"] += 1
                continue
            match_status = str(line.get("match_status") or "")
            if match_status not in SUPPLIER_SHIPMENT_LINE_MATCH_STATUSES:
                summary["lines_skipped"] += 1
                summary["lines_skipped_by_match_status"] += 1
                continue
            nm_id = _optional_int(_first_value(line, "internal_nm_id", "nm_id", "nmId", "nmID"))
            quantity = _optional_number(_first_value(line, "qty", "quantity"))
            if nm_id is None or nm_id not in active_set or quantity is None or quantity <= 0:
                summary["lines_skipped"] += 1
                continue
            quantity_by_nm_id[nm_id][shipment_status] = (
                quantity_by_nm_id.get(nm_id, {}).get(shipment_status, 0.0) + float(quantity)
            )
            summary["quantity_by_status"][shipment_status] = (
                float(summary["quantity_by_status"].get(shipment_status, 0.0)) + float(quantity)
            )
            summary["lines_counted"] += 1
    return {"quantity_by_nm_id": quantity_by_nm_id, "summary": summary}


def _load_wb_supplies_inbound_by_nm(
    runtime: RegistryUploadDbBackedRuntime,
    active_nm_ids: list[int],
) -> dict[str, Any]:
    active_set = {int(nm_id) for nm_id in active_nm_ids}
    quantity_by_nm_id = {int(nm_id): 0.0 for nm_id in active_nm_ids}
    summary: dict[str, Any] = {
        "source": "sheet_vitrina_v1_wb_supplies runtime cache",
        "quantity_source": "raw_goods nmId -> quantity",
        "excluded_status_ids": sorted(WB_SUPPLY_EXCLUDED_STATUS_IDS),
        "excluded_status_labels": [
            WB_SUPPLY_STATUS_LABELS_RU[1],
            WB_SUPPLY_STATUS_LABELS_RU[2],
            WB_SUPPLY_STATUS_LABELS_RU[5],
        ],
        "records_total": 0,
        "records_included": 0,
        "records_excluded_by_status": 0,
        "records_without_goods": 0,
        "goods_rows_counted": 0,
        "goods_rows_skipped": 0,
        "included_status_ids": [],
        "unknown_status_records": 0,
    }
    included_status_ids: set[int] = set()
    try:
        records = runtime.list_wb_supplies_cache_records()
    except Exception as exc:
        summary["status"] = "unavailable"
        summary["reason"] = str(exc)
        return {"quantity_by_nm_id": quantity_by_nm_id, "summary": summary}
    summary["status"] = "available"
    summary["records_total"] = len(records)
    for record in records:
        normalized = record.get("normalized") if isinstance(record.get("normalized"), Mapping) else {}
        status_id = _optional_int(
            _first_value(normalized, "status_id", "statusID", "statusId")
            if isinstance(normalized, Mapping)
            else None
        )
        status_label = str(
            _first_value(normalized, "status_label", "statusLabel", "status")
            or _wb_supply_status_label(status_id)
        )
        if _wb_supply_status_is_excluded(status_id, status_label):
            summary["records_excluded_by_status"] += 1
            continue
        summary["records_included"] += 1
        if status_id is None:
            summary["unknown_status_records"] += 1
        else:
            included_status_ids.add(status_id)
        raw_goods = record.get("raw_goods")
        if not isinstance(raw_goods, list) and isinstance(normalized, Mapping):
            raw_goods = normalized.get("raw_goods")
        if not isinstance(raw_goods, list) or not raw_goods:
            summary["records_without_goods"] += 1
            continue
        for goods_row in raw_goods:
            if not isinstance(goods_row, Mapping):
                summary["goods_rows_skipped"] += 1
                continue
            nm_id = _optional_int(_first_value(goods_row, "nmID", "nmId", "nm_id", "nm"))
            quantity = _optional_number(_first_value(goods_row, "quantity", "qty"))
            if nm_id is None or nm_id not in active_set or quantity is None or quantity <= 0:
                summary["goods_rows_skipped"] += 1
                continue
            quantity_by_nm_id[nm_id] = quantity_by_nm_id.get(nm_id, 0.0) + float(quantity)
            summary["goods_rows_counted"] += 1
    summary["included_status_ids"] = sorted(included_status_ids)
    return {"quantity_by_nm_id": quantity_by_nm_id, "summary": summary}


def _build_stock_report_summary_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    total_avg_sales_per_day = _sum_row_numbers(rows, "avg_sales_per_day")
    total_stock_wb = _sum_row_numbers(rows, "stock_wb")
    districts: list[dict[str, Any]] = []
    for metric_key, label in STOCK_REPORT_DISTRICTS:
        district_rows = [_stock_report_row_district(row, metric_key) for row in rows]
        stock_sum = _sum_mapping_numbers(district_rows, "stock")
        burn_sum = _sum_mapping_numbers(district_rows, "avg_daily_burn")
        districts.append(
            {
                "metric_key": metric_key,
                "label": label,
                "stock": stock_sum,
                "avg_daily_burn": burn_sum,
                "days_left": _days_left(stock_sum, burn_sum),
                "diagnostics": {
                    "source": "summary_row_aggregate",
                    "row_count": len(rows),
                    "aggregation": "sum stock and burn, then stock / burn",
                },
            }
        )

    return {
        "is_summary": True,
        "nm_id": None,
        "display_name": "Итого",
        "nomenclature_name": "",
        "identity_label": "Итого",
        "active_order": -1,
        "promotion_participation": None,
        "promotion_participation_label": "—",
        "supplier_production_qty": _sum_row_numbers(rows, "supplier_production_qty"),
        "supplier_in_transit_qty": _sum_row_numbers(rows, "supplier_in_transit_qty"),
        "wb_supplies_inbound_qty": _sum_row_numbers(rows, "wb_supplies_inbound_qty"),
        "stock_ff": _sum_row_numbers(rows, "stock_ff"),
        "stock_wb": total_stock_wb,
        "stock_total": total_stock_wb,
        "zero_district_count": _sum_row_numbers(rows, "zero_district_count"),
        "avg_sales_per_day": total_avg_sales_per_day,
        "days_left_total": _days_left(total_stock_wb, total_avg_sales_per_day),
        "districts": districts,
        "diagnostics": {
            "source": "stock_report_summary_row",
            "row_count": len(rows),
            "days_left_total_formula": "sum(stock_wb) / sum(avg_sales_per_day)",
        },
    }


def _stock_report_row_district(row: Mapping[str, Any], metric_key: str) -> Mapping[str, Any]:
    districts = row.get("districts")
    if not isinstance(districts, list):
        return {}
    for district in districts:
        if isinstance(district, Mapping) and str(district.get("metric_key") or "") == metric_key:
            return district
    return {}


def _sum_row_numbers(rows: list[Mapping[str, Any]], key: str) -> float | None:
    return _sum_mapping_numbers(rows, key)


def _sum_mapping_numbers(rows: list[Mapping[str, Any]], key: str) -> float | None:
    total = 0.0
    seen = False
    for row in rows:
        value = _optional_number(row.get(key))
        if value is None:
            continue
        total += float(value)
        seen = True
    return total if seen else None


def _optional_int(value: Any) -> int | None:
    try:
        if value in ("", None):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_number(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric


def _first_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, ""):
            return mapping.get(key)
    return None


def _wb_supply_status_label(status_id: int | None) -> str:
    if status_id is None:
        return ""
    return WB_SUPPLY_STATUS_LABELS_RU.get(status_id, f"Статус {status_id}")


def _normalize_status_label(value: str) -> str:
    return " ".join(str(value or "").strip().lower().replace("ё", "е").split())


def _wb_supply_status_is_excluded(status_id: int | None, status_label: str) -> bool:
    if status_id in WB_SUPPLY_EXCLUDED_STATUS_IDS:
        return True
    return _normalize_status_label(status_label) in WB_SUPPLY_EXCLUDED_STATUS_LABELS


def _load_persisted_order_count_samples(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str,
    date_to: str,
    nm_ids: list[int],
) -> SalesSamplesWindow:
    payloads = load_runtime_sales_history_payloads(
        runtime=runtime,
        date_from=date_from,
        date_to=date_to,
    )
    coverage = describe_runtime_sales_history_coverage(runtime)
    samples_by_nm_id: dict[int, list[tuple[str, float]]] = {nm_id: [] for nm_id in nm_ids}
    missing_dates: set[str] = set()
    missing_pair_count = 0
    available_date_count = 0
    for snapshot_date in _iter_iso_dates(date_from, date_to):
        payload = payloads.get(snapshot_date)
        if payload is None or str(getattr(payload, "kind", "") or "") != "success":
            missing_dates.add(snapshot_date)
            missing_pair_count += len(nm_ids)
            continue
        available_date_count += 1
        order_counts = _collect_order_count_map(payload)
        for nm_id in nm_ids:
            if nm_id in order_counts:
                samples_by_nm_id[nm_id].append((snapshot_date, order_counts[nm_id]))
            else:
                missing_dates.add(snapshot_date)
                missing_pair_count += 1
    return SalesSamplesWindow(
        date_from=date_from,
        date_to=date_to,
        samples_by_nm_id=samples_by_nm_id,
        missing_date_count=len(missing_dates),
        missing_pair_count=missing_pair_count,
        available_date_count=available_date_count,
        coverage_earliest_date=coverage.earliest_available_date,
        coverage_latest_date=coverage.latest_available_date,
        coverage_snapshot_count=coverage.exact_date_snapshot_count,
    )


def _collect_order_count_map(payload: Any) -> dict[int, float]:
    out: dict[int, float] = {}
    for item in list(getattr(payload, "items", []) or []):
        metric = str(getattr(item, "metric", "") or "")
        nm_id = getattr(item, "nm_id", None)
        value = getattr(item, "value", None)
        if metric != "orderCount" or not isinstance(nm_id, int) or not isinstance(value, (int, float)):
            continue
        out[nm_id] = float(value)
    return out


def _build_district_burn_lookup(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    report_date: str,
    nm_ids: list[int],
    sales_avg_period_days: int,
    sales_lookup_days: int,
) -> dict[tuple[int, str], DistrictBurnEstimate]:
    if not nm_ids:
        return {}
    report_date_obj = date.fromisoformat(report_date)
    views = _load_historical_closed_slot_views(
        runtime=runtime,
        date_from=(report_date_obj - timedelta(days=sales_lookup_days + 1)).isoformat(),
        date_to=report_date,
    )
    pair_payloads: list[tuple[SnapshotSlotView, SnapshotSlotView]] = []
    gap_day_count = 0
    for previous, current in zip(views, views[1:]):
        previous_date = date.fromisoformat(previous.slot_date)
        current_date = date.fromisoformat(current.slot_date)
        if current_date != previous_date + timedelta(days=1):
            gap_day_count += max((current_date - previous_date).days - 1, 1)
            continue
        pair_payloads.append((previous, current))

    out: dict[tuple[int, str], DistrictBurnEstimate] = {}
    for nm_id in nm_ids:
        for metric_key, _ in STOCK_REPORT_DISTRICTS:
            out[(nm_id, metric_key)] = _estimate_district_burn(
                pair_payloads=pair_payloads,
                nm_id=nm_id,
                metric_key=metric_key,
                sales_avg_period_days=sales_avg_period_days,
                gap_day_count=gap_day_count,
            )
    return out


def _load_historical_closed_slot_views(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str,
    date_to: str,
) -> list[SnapshotSlotView]:
    views: list[SnapshotSlotView] = []
    for snapshot_date in runtime.list_sheet_vitrina_ready_snapshot_dates(
        date_from=date_from,
        date_to=date_to,
        descending=False,
    ):
        try:
            snapshot = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=snapshot_date)
            views.append(_extract_closed_slot_view(snapshot, expected_closed_date=snapshot_date))
        except ValueError:
            continue
    return sorted(views, key=lambda item: item.slot_date)


def _estimate_district_burn(
    *,
    pair_payloads: list[tuple[SnapshotSlotView, SnapshotSlotView]],
    nm_id: int,
    metric_key: str,
    sales_avg_period_days: int,
    gap_day_count: int,
) -> DistrictBurnEstimate:
    valid_samples: list[tuple[str, float]] = []
    missing_day_count = 0
    restock_day_count = 0
    zero_depletion_day_count = 0
    lookup_pair_count = 0
    for previous, current in reversed(pair_payloads):
        if len(valid_samples) >= sales_avg_period_days:
            break
        lookup_pair_count += 1
        previous_stock = previous.sku_values.get(nm_id, {}).get(metric_key)
        current_stock = current.sku_values.get(nm_id, {}).get(metric_key)
        if previous_stock is None or current_stock is None:
            missing_day_count += 1
            continue
        depletion = float(previous_stock) - float(current_stock)
        if depletion > EPS:
            valid_samples.append((current.slot_date, depletion))
        elif depletion < -EPS:
            restock_day_count += 1
        else:
            zero_depletion_day_count += 1

    values = [value for _, value in valid_samples]
    avg_daily_burn = sum(values) / len(values) if values else None
    used_dates = sorted(snapshot_date for snapshot_date, _ in valid_samples)
    warning = ""
    if len(valid_samples) < sales_avg_period_days:
        warning = (
            f"Собрано {len(valid_samples)} district depletion days из {sales_avg_period_days}; "
            "дней хватит по округу считается только при positive depletion history."
        )
    return DistrictBurnEstimate(
        avg_daily_burn=avg_daily_burn,
        valid_day_count=len(valid_samples),
        missing_day_count=missing_day_count,
        restock_day_count=restock_day_count,
        zero_depletion_day_count=zero_depletion_day_count,
        gap_day_count=gap_day_count,
        lookup_pair_count=lookup_pair_count,
        earliest_used_date=used_dates[0] if used_dates else "",
        latest_used_date=used_dates[-1] if used_dates else "",
        warning=warning,
    )


def _empty_district_burn_estimate(*, sales_avg_period_days: int) -> DistrictBurnEstimate:
    return DistrictBurnEstimate(
        avg_daily_burn=None,
        valid_day_count=0,
        missing_day_count=0,
        restock_day_count=0,
        zero_depletion_day_count=0,
        gap_day_count=0,
        lookup_pair_count=0,
        earliest_used_date="",
        latest_used_date="",
        warning=(
            f"Собрано 0 district depletion days из {sales_avg_period_days}; "
            "дней хватит по округу считается только при positive depletion history."
        ),
    )


def _promotion_participation_payload(value: float | None) -> dict[str, Any]:
    if value is None:
        return {
            "value": None,
            "label": "н/д",
            "diagnostics": {
                "metric_key": PROMO_PARTICIPATION_METRIC_KEY,
                "status": "missing",
            },
        }
    # Canonical mapping: numeric >0 means participates, numeric 0 means not participating.
    participates = float(value) > 0
    diagnostics_status = "participates" if participates else "not_participating"
    if float(value) < 0:
        diagnostics_status = "unexpected_negative_treated_as_not_participating"
    return {
        "value": participates,
        "label": "Да" if participates else "Нет",
        "diagnostics": {
            "metric_key": PROMO_PARTICIPATION_METRIC_KEY,
            "status": diagnostics_status,
            "raw_value": float(value),
        },
    }


def _days_left(stock: float | None, daily_burn: float | None) -> float | None:
    if stock is None or daily_burn is None or daily_burn <= EPS:
        return None
    return float(stock) / float(daily_burn)


def _build_report_warnings(
    *,
    sales_window: SalesSamplesWindow,
    active_row_count: int,
    insufficient_sales_rows: int,
    insufficient_district_rows: int,
    sales_avg_period_days: int,
) -> list[str]:
    warnings: list[str] = []
    if sales_window.missing_date_count:
        warnings.append(
            "Sales history coverage partial: "
            f"missing dates={sales_window.missing_date_count}, missing SKU/date pairs={sales_window.missing_pair_count}."
        )
    if active_row_count and insufficient_sales_rows:
        warnings.append(
            f"Сред. продаж/день рассчитан по неполному valid-day покрытию для {insufficient_sales_rows} из {active_row_count} SKU "
            f"(target={sales_avg_period_days})."
        )
    if active_row_count and insufficient_district_rows:
        warnings.append(
            f"District days-left имеет неполное positive depletion покрытие для {insufficient_district_rows} из {active_row_count} SKU "
            f"(target={sales_avg_period_days}); missing/restock/zero-depletion дни не фальсифицируются как расход."
        )
    return warnings


def _iter_iso_dates(date_from: str, date_to: str) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if start > end:
        return []
    values: list[str] = []
    current = start
    while current <= end:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return values
