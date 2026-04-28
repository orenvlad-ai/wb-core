"""Server-owned WB regional supply block for the operator page."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Mapping
from uuid import uuid4

from packages.adapters.sales_funnel_history_block import HttpBackedSalesFunnelHistorySource
from packages.adapters.stocks_block import HttpBackedStocksSource
from packages.application.factory_order_sales_history import FactoryOrderAuthoritativeSalesHistory
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes
from packages.application.stocks_block import StocksBlock
from packages.business_time import current_business_date_iso
from packages.contracts.factory_order_supply import (
    DATASET_STOCK_FF,
    FactoryOrderDatasetState,
    FactoryOrderStockFfRow,
)
from packages.contracts.stocks_block import StocksRequest
from packages.contracts.wb_regional_supply import (
    DISTRICT_CENTRAL,
    DISTRICT_FAR_SIBERIA,
    DISTRICT_KEYS,
    DISTRICT_NORTHWEST,
    DISTRICT_SOUTH_CAUCASUS,
    DISTRICT_URAL,
    DISTRICT_VOLGA,
    WbRegionalSupplyCalculationResult,
    WbRegionalSupplyDistrictResult,
    WbRegionalSupplyDistrictRow,
    WbRegionalSupplySettings,
    WbRegionalSupplyStatus,
    WbRegionalSupplySummary,
)


_DISTRICT_SPECS = (
    (DISTRICT_CENTRAL, "Центральный федеральный округ", "stock_ru_central"),
    (DISTRICT_NORTHWEST, "Северо-Западный федеральный округ", "stock_ru_northwest"),
    (DISTRICT_VOLGA, "Приволжский федеральный округ", "stock_ru_volga"),
    (DISTRICT_URAL, "Уральский федеральный округ", "stock_ru_ural"),
    (DISTRICT_SOUTH_CAUCASUS, "Южный и Северо-Кавказский федеральный округ", "stock_ru_south_caucasus"),
    (DISTRICT_FAR_SIBERIA, "Дальневосточный и Сибирский федеральный округ", "stock_ru_far_siberia"),
)
_DISTRICT_NAME_BY_KEY = {key: name for key, name, _ in _DISTRICT_SPECS}
_DISTRICT_FIELD_BY_KEY = {key: field_name for key, _, field_name in _DISTRICT_SPECS}
_DISTRICT_ORDER_INDEX = {key: index for index, key in enumerate(DISTRICT_KEYS)}
_SHARED_STOCK_LABEL = "Остатки ФФ"
_DISTRICT_FILE_HEADERS = ["nmId", "SKU", "Количество к поставке", "Дефицит"]
_WEIGHT_COEFFICIENT = 0.08593
_VOLUME_DIVISOR = 204.38
_DEFAULT_SALES_AVG_PERIOD_DAYS = 14
_DEFAULT_CYCLE_SUPPLY_DAYS = 7
_METHODOLOGY_NOTE = (
    "Расчёт использует общий файл «Остатки ФФ» из этой же вкладки. "
    "Сервер берёт total orderCount по SKU и current stock rows по 6 федеральным округам; "
    "пока в wb-core нет отдельного authoritative district sales source, district daily demand "
    "раскладывается по текущей структуре региональных остатков, после чего применяется legacy "
    "box allocation against available stock_ff с truthfully рассчитанным deficit."
)


class WbRegionalSupplyBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        stocks_block: StocksBlock | None = None,
        sales_funnel_history_block: SalesFunnelHistoryBlock | None = None,
        now_factory: callable | None = None,
        timestamp_factory: callable | None = None,
    ) -> None:
        self.runtime = runtime
        self.stocks_block = stocks_block or StocksBlock(HttpBackedStocksSource())
        self.sales_funnel_history_block = sales_funnel_history_block or SalesFunnelHistoryBlock(
            HttpBackedSalesFunnelHistorySource()
        )
        self.now_factory = now_factory or _default_now_factory
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory
        self.sales_history = FactoryOrderAuthoritativeSalesHistory(
            runtime=self.runtime,
            sales_funnel_history_block=self.sales_funnel_history_block,
            now_factory=self.now_factory,
            timestamp_factory=self.timestamp_factory,
        )

    def build_status(self) -> WbRegionalSupplyStatus:
        active_skus = self._load_active_skus()
        shared_datasets = {DATASET_STOCK_FF: self._load_shared_stock_ff_state()}
        last_result = self._load_last_result()
        return WbRegionalSupplyStatus(
            status="ready" if last_result is not None else "idle",
            active_sku_count=len(active_skus),
            methodology_note=self.sales_history.build_operator_note(_METHODOLOGY_NOTE),
            shared_datasets=shared_datasets,
            last_result=last_result,
        )

    def calculate(self, settings_input: Mapping[str, Any]) -> WbRegionalSupplyCalculationResult:
        settings = _parse_settings(settings_input)
        active_skus = self._load_active_skus()
        if not active_skus:
            raise ValueError("current registry config_v2 does not contain enabled rows for расчёта")

        shared_state = self._load_shared_stock_ff_state()
        if shared_state.status != "uploaded":
            raise ValueError(
                "Для расчёта по федеральным округам нужен общий загруженный файл: Остатки ФФ"
            )
        shared_datasets = {DATASET_STOCK_FF: shared_state}
        stock_ff_rows = self._load_stock_ff_rows()
        report_date = settings.report_date_override or current_business_date_iso(self.now_factory())
        report_date_obj = date.fromisoformat(report_date)
        history_from = report_date_obj - timedelta(days=settings.sales_avg_period_days)
        history_to = report_date_obj - timedelta(days=1)

        nm_ids = [nm_id for nm_id, _ in active_skus]
        stock_response = self.stocks_block.execute(
            StocksRequest(
                snapshot_type="stocks",
                snapshot_date=report_date,
                nm_ids=nm_ids,
            )
        ).result
        if getattr(stock_response, "kind", "") != "success":
            missing = getattr(stock_response, "missing_nm_ids", [])
            raise ValueError(
                "authoritative district stock coverage incomplete for requested nmIds at report_date "
                f"{report_date}: " + ", ".join(str(item) for item in missing)
            )
        stock_items = {int(item.nm_id): item for item in getattr(stock_response, "items", [])}
        if set(stock_items) != set(nm_ids):
            missing = sorted(set(nm_ids) - set(stock_items))
            raise ValueError(
                "authoritative district stock coverage incomplete for requested nmIds at report_date "
                f"{report_date}: " + ", ".join(str(item) for item in missing)
            )

        order_counts_by_nm = self.sales_history.load_order_count_samples(
            date_from=history_from.isoformat(),
            date_to=history_to.isoformat(),
            nm_ids=nm_ids,
        )
        stock_ff_by_nm = {row.nm_id: float(row.stock_ff) for row in stock_ff_rows}
        district_rows_by_key: dict[str, list[WbRegionalSupplyDistrictRow]] = {key: [] for key in DISTRICT_KEYS}

        for nm_id, sku_comment in active_skus:
            stock_item = stock_items[nm_id]
            order_samples = order_counts_by_nm.get(nm_id, [])
            daily_demand_total = sum(order_samples) / len(order_samples) if order_samples else 0.0
            district_stock_by_key = {
                district_key: float(getattr(stock_item, _DISTRICT_FIELD_BY_KEY[district_key], 0.0) or 0.0)
                for district_key in DISTRICT_KEYS
            }
            district_daily_demand_by_key = _split_daily_demand_by_district(
                daily_demand_total=daily_demand_total,
                district_stock_by_key=district_stock_by_key,
            )
            full_recommendation_by_key: dict[str, int] = {}
            row_payloads_by_key: dict[str, dict[str, Any]] = {}
            for district_key in DISTRICT_KEYS:
                current_stock = district_stock_by_key[district_key]
                district_daily_demand = district_daily_demand_by_key[district_key]
                projected_stock_on_eta = max(
                    current_stock - (district_daily_demand * settings.lead_time_to_region_days),
                    0.0,
                )
                target_stock_after_arrival = district_daily_demand * (
                    settings.cycle_supply_days + settings.safety_days
                )
                raw_recommendation = max(target_stock_after_arrival - projected_stock_on_eta, 0.0)
                full_recommendation_qty = (
                    int(math.ceil(raw_recommendation / settings.order_batch_qty) * settings.order_batch_qty)
                    if raw_recommendation > 0
                    else 0
                )
                full_recommendation_by_key[district_key] = full_recommendation_qty
                row_payloads_by_key[district_key] = {
                    "nm_id": nm_id,
                    "sku_comment": sku_comment,
                    "current_stock": current_stock,
                    "projected_stock_on_eta": projected_stock_on_eta,
                    "target_stock_after_arrival": target_stock_after_arrival,
                    "daily_demand_total": daily_demand_total,
                    "district_daily_demand": district_daily_demand,
                    "full_recommendation_qty": full_recommendation_qty,
                }

            allocated_by_key = _allocate_boxes(
                full_recommendation_by_key=full_recommendation_by_key,
                district_daily_demand_by_key=district_daily_demand_by_key,
                projected_stock_by_key={
                    district_key: float(row_payloads_by_key[district_key]["projected_stock_on_eta"])
                    for district_key in DISTRICT_KEYS
                },
                available_stock_ff=float(stock_ff_by_nm.get(nm_id, 0.0)),
                order_batch_qty=settings.order_batch_qty,
            )
            for district_key in DISTRICT_KEYS:
                allocated_qty = int(allocated_by_key.get(district_key, 0))
                full_recommendation_qty = int(full_recommendation_by_key.get(district_key, 0))
                district_rows_by_key[district_key].append(
                    WbRegionalSupplyDistrictRow(
                        nm_id=nm_id,
                        sku_comment=sku_comment,
                        full_recommendation_qty=full_recommendation_qty,
                        allocated_qty=allocated_qty,
                        deficit_qty=max(full_recommendation_qty - allocated_qty, 0),
                        current_stock=float(row_payloads_by_key[district_key]["current_stock"]),
                        projected_stock_on_eta=float(
                            row_payloads_by_key[district_key]["projected_stock_on_eta"]
                        ),
                        target_stock_after_arrival=float(
                            row_payloads_by_key[district_key]["target_stock_after_arrival"]
                        ),
                        daily_demand_total=float(row_payloads_by_key[district_key]["daily_demand_total"]),
                        district_daily_demand=float(
                            row_payloads_by_key[district_key]["district_daily_demand"]
                        ),
                    )
                )

        districts = [
            WbRegionalSupplyDistrictResult(
                district_key=district_key,
                district_name_ru=_DISTRICT_NAME_BY_KEY[district_key],
                total_qty=sum(row.allocated_qty for row in district_rows_by_key[district_key]),
                deficit_qty=sum(row.deficit_qty for row in district_rows_by_key[district_key]),
                filename=f"{_DISTRICT_NAME_BY_KEY[district_key]}.xlsx",
                rows=district_rows_by_key[district_key],
            )
            for district_key in DISTRICT_KEYS
        ]
        total_qty = sum(item.total_qty for item in districts)
        result = WbRegionalSupplyCalculationResult(
            status="success",
            calculation_id=uuid4().hex,
            calculated_at=self.timestamp_factory(),
            report_date=report_date,
            horizon_days=settings.cycle_supply_days,
            active_sku_count=len(active_skus),
            methodology_note=self.sales_history.build_operator_note(_METHODOLOGY_NOTE),
            settings=settings,
            shared_datasets=shared_datasets,
            summary=WbRegionalSupplySummary(
                total_qty=total_qty,
                estimated_weight=round(total_qty * _WEIGHT_COEFFICIENT, 2),
                estimated_volume=round((total_qty * _WEIGHT_COEFFICIENT) / _VOLUME_DIVISOR, 2),
            ),
            districts=districts,
        )
        self._validate_result_consistency(result)
        for district in result.districts:
            self._build_district_workbook_bytes(district)
        self.runtime.save_wb_regional_supply_result_state(
            calculated_at=result.calculated_at,
            payload=asdict(result),
        )
        return result

    def download_district_recommendation(self, district_key: str) -> tuple[bytes, str]:
        normalized_key = str(district_key or "").strip().lower()
        if normalized_key not in _DISTRICT_NAME_BY_KEY:
            raise ValueError(f"Неизвестный федеральный округ: {district_key}")
        result = self._load_last_result()
        if result is None:
            raise ValueError("Результат расчёта по федеральным округам ещё не подготовлен")
        district = next((item for item in result.districts if item.district_key == normalized_key), None)
        if district is None:
            raise ValueError(f"В последнем результате нет округа: {district_key}")
        return self._build_district_workbook_bytes(district), district.filename

    def _load_active_skus(self) -> list[tuple[int, str]]:
        current_state = self.runtime.load_current_state()
        enabled = sorted(
            [item for item in current_state.config_v2 if item.enabled],
            key=lambda item: item.display_order,
        )
        return [(int(item.nm_id), str(item.display_name)) for item in enabled]

    def _load_shared_stock_ff_state(self) -> FactoryOrderDatasetState:
        payload = self.runtime.load_factory_order_dataset_state(DATASET_STOCK_FF)
        if payload is None:
            return FactoryOrderDatasetState(
                dataset_type=DATASET_STOCK_FF,
                label_ru=_SHARED_STOCK_LABEL,
                status="missing",
                uploaded_at=None,
                row_count=0,
                required=True,
            )
        return FactoryOrderDatasetState(
            dataset_type=DATASET_STOCK_FF,
            label_ru=_SHARED_STOCK_LABEL,
            status="uploaded",
            uploaded_at=str(payload["uploaded_at"]),
            row_count=int(payload["row_count"]),
            required=True,
            uploaded_filename=str(payload.get("uploaded_filename") or "") or None,
            file_available=bool(payload.get("file_available")),
        )

    def _load_stock_ff_rows(self) -> list[FactoryOrderStockFfRow]:
        payload = self.runtime.load_factory_order_dataset_state(DATASET_STOCK_FF)
        if payload is None:
            return []
        return [
            FactoryOrderStockFfRow(
                nm_id=int(item["nm_id"]),
                sku_comment=str(item.get("sku_comment", "") or ""),
                stock_ff=float(item["stock_ff"]),
                snapshot_date=str(item.get("snapshot_date") or "") or None,
                comment=str(item.get("comment", "") or ""),
            )
            for item in payload["rows"]
        ]

    def _load_last_result(self) -> WbRegionalSupplyCalculationResult | None:
        payload = self.runtime.load_wb_regional_supply_result_state()
        if not isinstance(payload, Mapping):
            return None
        settings_payload = payload.get("settings") or {}
        summary_payload = payload.get("summary") or {}
        shared_datasets_payload = payload.get("shared_datasets") or {}
        districts_payload = payload.get("districts") or []
        return WbRegionalSupplyCalculationResult(
            status=str(payload.get("status", "")),
            calculation_id=str(payload.get("calculation_id", "")),
            calculated_at=str(payload.get("calculated_at", "")),
            report_date=str(payload.get("report_date", "")),
            horizon_days=int(payload.get("horizon_days", 0)),
            active_sku_count=int(payload.get("active_sku_count", 0)),
            methodology_note=str(payload.get("methodology_note", _METHODOLOGY_NOTE)),
            settings=WbRegionalSupplySettings(
                sales_avg_period_days=int(
                    settings_payload.get("sales_avg_period_days", _DEFAULT_SALES_AVG_PERIOD_DAYS)
                ),
                cycle_supply_days=int(
                    settings_payload.get(
                        "cycle_supply_days",
                        settings_payload.get("supply_horizon_days", 0),
                    )
                ),
                lead_time_to_region_days=int(settings_payload.get("lead_time_to_region_days", 0)),
                safety_days=int(settings_payload.get("safety_days", 0)),
                order_batch_qty=int(settings_payload.get("order_batch_qty", 0)),
                report_date_override=(
                    str(settings_payload.get("report_date_override"))
                    if settings_payload.get("report_date_override")
                    else None
                ),
            ),
            shared_datasets={
                key: FactoryOrderDatasetState(
                    dataset_type=str(value.get("dataset_type", key)),
                    label_ru=str(value.get("label_ru", _SHARED_STOCK_LABEL)),
                    status=str(value.get("status", "missing")),
                    uploaded_at=str(value.get("uploaded_at")) if value.get("uploaded_at") else None,
                    row_count=int(value.get("row_count", 0)),
                    required=bool(value.get("required", True)),
                    uploaded_filename=str(value.get("uploaded_filename")) if value.get("uploaded_filename") else None,
                    file_available=bool(value.get("file_available", False)),
                )
                for key, value in shared_datasets_payload.items()
                if isinstance(value, Mapping)
            },
            summary=WbRegionalSupplySummary(
                total_qty=int(summary_payload.get("total_qty", 0)),
                estimated_weight=float(summary_payload.get("estimated_weight", 0.0)),
                estimated_volume=float(summary_payload.get("estimated_volume", 0.0)),
            ),
            districts=[
                WbRegionalSupplyDistrictResult(
                    district_key=str(item.get("district_key", "")),
                    district_name_ru=str(
                        item.get("district_name_ru", _DISTRICT_NAME_BY_KEY.get(str(item.get("district_key", "")), ""))
                    ),
                    total_qty=int(item.get("total_qty", 0)),
                    deficit_qty=int(item.get("deficit_qty", 0)),
                    filename=str(item.get("filename", "")),
                    rows=[
                        WbRegionalSupplyDistrictRow(
                            nm_id=int(row.get("nm_id", 0)),
                            sku_comment=str(row.get("sku_comment", "")),
                            full_recommendation_qty=int(row.get("full_recommendation_qty", 0)),
                            allocated_qty=int(row.get("allocated_qty", 0)),
                            deficit_qty=int(row.get("deficit_qty", 0)),
                            current_stock=float(row.get("current_stock", 0.0)),
                            projected_stock_on_eta=float(row.get("projected_stock_on_eta", 0.0)),
                            target_stock_after_arrival=float(row.get("target_stock_after_arrival", 0.0)),
                            daily_demand_total=float(row.get("daily_demand_total", 0.0)),
                            district_daily_demand=float(row.get("district_daily_demand", 0.0)),
                        )
                        for row in item.get("rows", [])
                        if isinstance(row, Mapping)
                    ],
                )
                for item in districts_payload
                if isinstance(item, Mapping)
            ],
        )

    def _validate_result_consistency(self, result: WbRegionalSupplyCalculationResult) -> None:
        total_from_districts = 0
        for district in result.districts:
            if district.district_key not in _DISTRICT_NAME_BY_KEY:
                raise ValueError(f"district result contains unsupported key: {district.district_key}")
            district_total = sum(row.allocated_qty for row in district.rows)
            district_deficit = sum(row.deficit_qty for row in district.rows)
            if district_total != district.total_qty:
                raise ValueError(
                    f"district summary mismatch for {district.district_key}: total_qty={district.total_qty}, rows={district_total}"
                )
            if district_deficit != district.deficit_qty:
                raise ValueError(
                    f"district summary mismatch for {district.district_key}: deficit_qty={district.deficit_qty}, rows={district_deficit}"
                )
            for row in district.rows:
                if row.allocated_qty > row.full_recommendation_qty:
                    raise ValueError(
                        f"allocated_qty exceeds full_recommendation_qty for {district.district_key} nmId={row.nm_id}"
                    )
                if row.deficit_qty != max(row.full_recommendation_qty - row.allocated_qty, 0):
                    raise ValueError(
                        f"deficit_qty mismatch for {district.district_key} nmId={row.nm_id}"
                    )
            total_from_districts += district_total
        if total_from_districts != result.summary.total_qty:
            raise ValueError(
                f"summary total mismatch: summary.total_qty={result.summary.total_qty}, districts={total_from_districts}"
            )

    def _build_district_workbook_bytes(self, district: WbRegionalSupplyDistrictResult) -> bytes:
        rows: list[list[Any]] = [
            ["Федеральный округ", district.district_name_ru, "", ""],
            [],
            _DISTRICT_FILE_HEADERS,
        ]
        rows.extend(
            [
                [row.nm_id, row.sku_comment, row.allocated_qty, row.deficit_qty]
                for row in district.rows
                if row.allocated_qty > 0 or row.deficit_qty > 0
            ]
        )
        sheet_name = _truncate_sheet_name(district.district_name_ru)
        return build_single_sheet_workbook_bytes(sheet_name, rows)


def _default_now_factory() -> datetime:
    return datetime.now(timezone.utc)


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_settings(payload: Mapping[str, Any]) -> WbRegionalSupplySettings:
    return WbRegionalSupplySettings(
        sales_avg_period_days=_parse_sales_avg_period_days(payload.get("sales_avg_period_days")),
        cycle_supply_days=_parse_cycle_supply_days(payload),
        lead_time_to_region_days=_parse_positive_int(
            payload.get("lead_time_to_region_days"),
            "Срок доставки до склада Wildberries",
        ),
        safety_days=_parse_nonnegative_int(payload.get("safety_days"), "Страховой запас"),
        order_batch_qty=_parse_positive_int(payload.get("order_batch_qty"), "Кратность штук в коробке"),
        report_date_override=_parse_optional_date(
            payload.get("report_date_override"),
            field_label="Дата расчёта",
        ),
    )


def _parse_sales_avg_period_days(value: Any) -> int:
    if value in ("", None):
        return _DEFAULT_SALES_AVG_PERIOD_DAYS
    try:
        numeric = int(str(value).strip())
    except ValueError as exc:
        raise ValueError("Период усреднения продаж должен быть целым числом") from exc
    if numeric <= 0:
        return _DEFAULT_SALES_AVG_PERIOD_DAYS
    return numeric


def _parse_cycle_supply_days(payload: Mapping[str, Any]) -> int:
    raw_value = payload.get("cycle_supply_days")
    if raw_value in ("", None):
        raw_value = payload.get("supply_horizon_days")
    if raw_value in ("", None):
        return _DEFAULT_CYCLE_SUPPLY_DAYS
    try:
        numeric = int(str(raw_value).strip())
    except ValueError as exc:
        raise ValueError("Цикл поставок должен быть целым числом") from exc
    if numeric <= 0:
        return _DEFAULT_CYCLE_SUPPLY_DAYS
    return numeric


def _parse_positive_int(value: Any, label: str) -> int:
    try:
        numeric = int(str(value).strip())
    except Exception as exc:
        raise ValueError(f"{label} должен быть целым числом") from exc
    if numeric <= 0:
        raise ValueError(f"{label} должен быть больше нуля")
    return numeric


def _parse_nonnegative_int(value: Any, label: str) -> int:
    try:
        numeric = int(str(value).strip())
    except Exception as exc:
        raise ValueError(f"{label} должен быть целым числом") from exc
    if numeric < 0:
        raise ValueError(f"{label} не может быть отрицательным")
    return numeric


def _parse_optional_date(value: Any, *, field_label: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    for parser in (_parse_iso_date, _parse_dotted_date, _parse_iso_datetime_date):
        parsed = parser(normalized)
        if parsed is not None:
            return parsed
    raise ValueError(f"{field_label} должна быть датой в формате YYYY-MM-DD или DD.MM.YYYY")


def _parse_iso_date(value: str) -> str | None:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _parse_iso_datetime_date(value: str) -> str | None:
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        return None


def _parse_dotted_date(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def _split_daily_demand_by_district(
    *,
    daily_demand_total: float,
    district_stock_by_key: Mapping[str, float],
) -> dict[str, float]:
    if daily_demand_total <= 0:
        return {key: 0.0 for key in DISTRICT_KEYS}
    positive_stock_total = sum(max(float(district_stock_by_key.get(key, 0.0)), 0.0) for key in DISTRICT_KEYS)
    if positive_stock_total <= 0:
        equal_share = daily_demand_total / len(DISTRICT_KEYS)
        return {key: equal_share for key in DISTRICT_KEYS}
    return {
        key: daily_demand_total * (max(float(district_stock_by_key.get(key, 0.0)), 0.0) / positive_stock_total)
        for key in DISTRICT_KEYS
    }


def _allocate_boxes(
    *,
    full_recommendation_by_key: Mapping[str, int],
    district_daily_demand_by_key: Mapping[str, float],
    projected_stock_by_key: Mapping[str, float],
    available_stock_ff: float,
    order_batch_qty: int,
) -> dict[str, int]:
    allocated = {key: 0 for key in DISTRICT_KEYS}
    total_full = sum(max(int(full_recommendation_by_key.get(key, 0)), 0) for key in DISTRICT_KEYS)
    ff_allocatable = int(math.floor(max(available_stock_ff, 0.0) / order_batch_qty) * order_batch_qty)
    if ff_allocatable <= 0 or total_full <= 0:
        return allocated
    if ff_allocatable >= total_full:
        return {key: max(int(full_recommendation_by_key.get(key, 0)), 0) for key in DISTRICT_KEYS}

    remaining = ff_allocatable
    while remaining >= order_batch_qty:
        candidates = [
            key
            for key in DISTRICT_KEYS
            if allocated[key] < max(int(full_recommendation_by_key.get(key, 0)), 0)
        ]
        if not candidates:
            break
        chosen = min(
            candidates,
            key=lambda key: (
                _coverage_days(
                    projected_stock=projected_stock_by_key.get(key, 0.0),
                    allocated_qty=allocated[key],
                    avg_day=district_daily_demand_by_key.get(key, 0.0),
                ),
                -float(district_daily_demand_by_key.get(key, 0.0)),
                _DISTRICT_ORDER_INDEX[key],
            ),
        )
        allocated[chosen] += order_batch_qty
        remaining -= order_batch_qty
    return allocated


def _coverage_days(*, projected_stock: float, allocated_qty: int, avg_day: float) -> float:
    if avg_day <= 0:
        return float("inf")
    return (max(float(projected_stock), 0.0) + float(allocated_qty)) / float(avg_day)


def _truncate_sheet_name(value: str) -> str:
    normalized = str(value or "").strip() or "Рекомендация"
    return normalized[:31]
