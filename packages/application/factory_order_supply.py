"""Server-owned factory-order supply block for the operator page."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Mapping
from uuid import uuid4

from packages.adapters.sales_funnel_history_block import HttpBackedSalesFunnelHistorySource
from packages.adapters.stocks_block import HttpBackedStocksSource
from packages.application.factory_order_sales_history import FactoryOrderAuthoritativeSalesHistory
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.application.stocks_block import StocksBlock
from packages.business_time import current_business_date_iso
from packages.contracts.factory_order_supply import (
    DATASET_INBOUND_FACTORY_TO_FF,
    DATASET_INBOUND_FF_TO_WB,
    DATASET_STOCK_FF,
    FACTORY_INBOUND_SOURCE_MANUAL_EXCEL,
    FACTORY_INBOUND_SOURCE_SUPPLIER_REGISTRY,
    SUPPLIER_REGISTRY_FACTORY_TO_FF_ACCEPTANCE_DAYS,
    FactoryOrderCalculationResult,
    FactoryOrderDatasetDeleteResult,
    FactoryOrderDatasetState,
    FactoryOrderEffectiveInboundRow,
    FactoryOrderInboundRow,
    FactoryOrderInboundShipmentSummary,
    FactoryOrderRecommendationRow,
    FactoryOrderSettings,
    FactoryOrderStatus,
    FactoryOrderStockFfRow,
    FactoryOrderSummary,
    FactoryOrderSupplierRegistryDiagnostics,
    FactoryOrderSupplierRegistryInboundState,
    FactoryOrderSupplierRegistryShipmentSummary,
    FactoryOrderUploadResult,
)
from packages.contracts.supplier_shipments import (
    LINE_TYPE_PRODUCT,
    MATCH_STATUS_AMBIGUOUS,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_MATCHED_BY_COMPATIBILITY,
    MATCH_STATUS_UNMATCHED,
)
from packages.contracts.stocks_block import StocksRequest


_DATASET_LABELS = {
    DATASET_STOCK_FF: "Остатки ФФ",
    DATASET_INBOUND_FACTORY_TO_FF: "Товары в пути от фабрики",
    DATASET_INBOUND_FF_TO_WB: "Товары в пути от ФФ на Wildberries",
}
_DATASET_REQUIRED = {
    DATASET_STOCK_FF: True,
    DATASET_INBOUND_FACTORY_TO_FF: False,
    DATASET_INBOUND_FF_TO_WB: False,
}
_DATASET_FILENAMES = {
    DATASET_STOCK_FF: "sheet-vitrina-v1-factory-order-stock-ff-template.xlsx",
    DATASET_INBOUND_FACTORY_TO_FF: "sheet-vitrina-v1-factory-order-inbound-factory-template.xlsx",
    DATASET_INBOUND_FF_TO_WB: "sheet-vitrina-v1-factory-order-inbound-ff-to-wb-template.xlsx",
}
_DATASET_SHEET_NAMES = {
    DATASET_STOCK_FF: "Остатки ФФ",
    DATASET_INBOUND_FACTORY_TO_FF: "В пути от фабрики",
    DATASET_INBOUND_FF_TO_WB: "В пути ФФ -> WB",
}
_TEMPLATE_HEADERS = {
    DATASET_STOCK_FF: ["nmId", "Комментарий SKU", "Остаток ФФ", "Дата остатка", "Комментарий"],
    DATASET_INBOUND_FACTORY_TO_FF: [
        "nmId",
        "Комментарий SKU",
        "Количество в пути",
        "Планируемая дата прихода на ФФ",
        "Комментарий",
        "Поставка",
    ],
    DATASET_INBOUND_FF_TO_WB: [
        "nmId",
        "Комментарий SKU",
        "Количество в пути",
        "Планируемая дата прихода на Wildberries",
        "Комментарий",
    ],
}
_LEGACY_INBOUND_FACTORY_HEADERS = _TEMPLATE_HEADERS[DATASET_INBOUND_FACTORY_TO_FF][:-1]
_RESULT_HEADERS = ["nmId", "Комментарий SKU", "Рекомендовано к заказу"]
_WEIGHT_COEFFICIENT = 0.08593
_VOLUME_DIVISOR = 204.38
_DEFAULT_SALES_AVG_PERIOD_DAYS = 14
_DEFAULT_CYCLE_ORDER_DAYS = 14
_COVERAGE_CONTRACT_NOTE = (
    "Файлы «Товары в пути от фабрики» и «Товары в пути от ФФ на Wildberries» необязательны: "
    "если файл не загружен, соответствующий inbound считается как 0. "
    "Строки inbound с количеством 0 принимаются, но игнорируются и не влияют на coverage. "
    "Inbound считается только если его effective arrival date попадает в окно от даты отчёта "
    "до полного target window: производство + фабрика→ФФ + ФФ→WB + safety MP + safety ФФ + цикл заказа. "
    "В пути ФФ -> Wildberries учитываются только из отдельного загруженного шаблона, "
    "потому что в текущем wb-core нет другого authoritative source для этого члена формулы. "
    "Для «Товары в пути от фабрики» оператор может выбрать manual Excel или read-only source из supplier registry; "
    "supplier registry uses shipment_date + 30 days as current bounded factory-to-FF acceptance default."
)


@dataclass(frozen=True)
class _SupplierRegistryShipmentFactorySummary:
    shipment_id: str
    shipment_label: str
    invoice_no: str
    invoice_date: str
    total_product_quantity: float
    shipment_date: str
    calculated_acceptance_date: str
    product_line_count: int
    matched_line_count: int
    unmatched_line_count: int
    ambiguous_line_count: int
    missing_shipment_date_line_count: int
    invalid_quantity_line_count: int
    usable_line_count: int
    usable_quantity: float


class FactoryOrderSupplyBlock:
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

    def build_status(self) -> FactoryOrderStatus:
        active_skus = self._load_active_skus()
        datasets = {dataset_type: self._load_dataset_state(dataset_type) for dataset_type in _DATASET_LABELS}
        last_result = self._load_last_result()
        supplier_registry_state = self._build_supplier_registry_inbound_state()
        factory_inbound_source = (
            last_result.factory_inbound_source
            if last_result is not None
            else FACTORY_INBOUND_SOURCE_MANUAL_EXCEL
        )
        return FactoryOrderStatus(
            status="ready" if last_result is not None else "idle",
            active_sku_count=len(active_skus),
            coverage_contract_note=self.sales_history.build_operator_note(_COVERAGE_CONTRACT_NOTE),
            factory_inbound_source=factory_inbound_source,
            datasets=datasets,
            manual_factory_inbound_dataset=datasets[DATASET_INBOUND_FACTORY_TO_FF],
            supplier_registry_inbound_summary=supplier_registry_state,
            last_result=last_result,
        )

    def build_template(self, dataset_type: str) -> tuple[bytes, str]:
        active_skus = self._load_active_skus()
        if not active_skus:
            raise ValueError("current registry config_v2 does not contain enabled rows for template generation")
        rows: list[list[Any]] = [_TEMPLATE_HEADERS[dataset_type]]
        if dataset_type == DATASET_STOCK_FF:
            snapshot_date = current_business_date_iso(self.now_factory())
            rows.extend(
                [[nm_id, sku_comment, 0, snapshot_date, ""] for nm_id, sku_comment in active_skus]
            )
        else:
            rows.extend(
                [
                    [nm_id, sku_comment] + [""] * (len(_TEMPLATE_HEADERS[dataset_type]) - 2)
                    for nm_id, sku_comment in active_skus
                ]
            )
        return (
            build_single_sheet_workbook_bytes(_DATASET_SHEET_NAMES[dataset_type], rows),
            _DATASET_FILENAMES[dataset_type],
        )

    def upload_dataset(
        self,
        dataset_type: str,
        workbook_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> FactoryOrderUploadResult:
        active_skus = dict(self._load_active_skus())
        workbook_rows = read_first_sheet_rows(workbook_bytes)
        parsed_rows, ignored_row_count = self._parse_dataset_rows(
            dataset_type=dataset_type,
            workbook_rows=workbook_rows,
            active_skus=active_skus,
        )
        shipment_summary = (
            _build_inbound_shipment_summary(
                [row for row in parsed_rows if isinstance(row, FactoryOrderInboundRow)]
            )
            if dataset_type == DATASET_INBOUND_FACTORY_TO_FF
            else ()
        )
        uploaded_at = self.timestamp_factory()
        normalized_filename = _normalize_uploaded_filename(uploaded_filename, dataset_type=dataset_type)
        normalized_content_type = (
            str(uploaded_content_type or "").strip()
            or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        self.runtime.save_factory_order_dataset_state(
            dataset_type=dataset_type,
            uploaded_at=uploaded_at,
            rows=[asdict(item) for item in parsed_rows],
            uploaded_filename=normalized_filename,
            uploaded_content_type=normalized_content_type,
            workbook_bytes=workbook_bytes,
        )
        dataset_state = FactoryOrderDatasetState(
            dataset_type=dataset_type,
            label_ru=_DATASET_LABELS[dataset_type],
            status="uploaded",
            uploaded_at=uploaded_at,
            row_count=len(parsed_rows),
            required=_DATASET_REQUIRED[dataset_type],
            uploaded_filename=normalized_filename,
            file_available=True,
            shipment_summary=shipment_summary,
        )
        return FactoryOrderUploadResult(
            status="accepted",
            dataset=dataset_state,
            accepted_row_count=len(parsed_rows),
            ignored_row_count=ignored_row_count,
            message=f"Файл принят: {_DATASET_LABELS[dataset_type].lower()}",
            shipment_summary=shipment_summary,
        )

    def delete_dataset(self, dataset_type: str) -> FactoryOrderDatasetDeleteResult:
        deleted = self.runtime.delete_factory_order_dataset_state(dataset_type)
        dataset_state = self._load_dataset_state(dataset_type)
        if not deleted:
            return FactoryOrderDatasetDeleteResult(
                status="missing",
                dataset=dataset_state,
                message=f"Файл уже отсутствует: {_DATASET_LABELS[dataset_type].lower()}",
            )
        return FactoryOrderDatasetDeleteResult(
            status="deleted",
            dataset=dataset_state,
            message=f"Файл удалён: {_DATASET_LABELS[dataset_type].lower()}",
        )

    def download_uploaded_dataset(self, dataset_type: str) -> tuple[bytes, str, str]:
        payload = self.runtime.load_factory_order_dataset_state(dataset_type, include_file_blob=True)
        if payload is None:
            raise ValueError(f"Текущий загруженный файл отсутствует: {_DATASET_LABELS[dataset_type].lower()}")
        workbook_bytes = bytes(payload.get("workbook_bytes") or b"")
        if not workbook_bytes:
            raise ValueError(
                "Исходный загруженный XLSX ещё не сохранён в текущем runtime. "
                "Загрузите файл повторно, чтобы он стал доступен для скачивания."
            )
        return (
            workbook_bytes,
            str(payload.get("uploaded_filename") or _DATASET_FILENAMES[dataset_type]),
            str(payload.get("uploaded_content_type") or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )

    def calculate(self, settings_input: Mapping[str, Any]) -> FactoryOrderCalculationResult:
        settings = _parse_settings(settings_input)
        active_skus = self._load_active_skus()
        if not active_skus:
            raise ValueError("current registry config_v2 does not contain enabled rows for расчёта")

        datasets = {dataset_type: self._load_dataset_state(dataset_type) for dataset_type in _DATASET_LABELS}
        missing_required = [
            state.label_ru
            for state in datasets.values()
            if state.required and state.status != "uploaded"
        ]
        if missing_required:
            raise ValueError(
                "Для расчёта не хватает загруженных файлов: " + ", ".join(missing_required)
            )

        stock_ff_rows = self._load_stock_ff_rows()
        factory_inbound_source = settings.factory_inbound_source
        supplier_registry_state = self._build_supplier_registry_inbound_state()
        inbound_factory_rows = (
            self._supplier_registry_inbound_rows(supplier_registry_state)
            if factory_inbound_source == FACTORY_INBOUND_SOURCE_SUPPLIER_REGISTRY
            else self._load_inbound_rows(DATASET_INBOUND_FACTORY_TO_FF)
        )
        inbound_ff_to_wb_rows = self._load_inbound_rows(DATASET_INBOUND_FF_TO_WB)

        report_date = settings.report_date_override or current_business_date_iso(self.now_factory())
        horizon_days = (
            settings.prod_lead_time_days
            + settings.lead_time_factory_to_ff_days
            + settings.lead_time_ff_to_wb_days
        )
        target_window_days = (
            horizon_days
            + settings.safety_days_mp
            + settings.safety_days_ff
            + settings.cycle_order_days
        )
        report_date_obj = date.fromisoformat(report_date)
        inbound_window_end = report_date_obj + timedelta(days=target_window_days)
        stock_snapshot_date = current_business_date_iso(self.now_factory())

        nm_ids = [nm_id for nm_id, _ in active_skus]
        stock_response = self.stocks_block.execute(
            StocksRequest(
                snapshot_type="stocks",
                snapshot_date=stock_snapshot_date,
                nm_ids=nm_ids,
            )
        ).result
        if getattr(stock_response, "kind", "") != "success":
            missing = getattr(stock_response, "missing_nm_ids", [])
            raise ValueError(
                "authoritative stock_total coverage incomplete for requested nmIds: "
                + ", ".join(str(item) for item in missing)
            )
        stock_total_by_nm = {item.nm_id: float(item.stock_total) for item in getattr(stock_response, "items", [])}
        if set(stock_total_by_nm) != set(nm_ids):
            missing = sorted(set(nm_ids) - set(stock_total_by_nm))
            raise ValueError(
                "authoritative stock_total coverage incomplete for requested nmIds: "
                + ", ".join(str(item) for item in missing)
            )

        history_from = report_date_obj - timedelta(days=settings.sales_avg_period_days)
        history_to = report_date_obj - timedelta(days=1)
        order_counts_by_nm = self.sales_history.load_order_count_samples(
            date_from=history_from.isoformat(),
            date_to=history_to.isoformat(),
            nm_ids=nm_ids,
        )

        stock_ff_by_nm = {row.nm_id: float(row.stock_ff) for row in stock_ff_rows}
        effective_inbound_factory_rows = _effective_inbound_rows_within_window(
            inbound_factory_rows,
            report_date_obj,
            inbound_window_end,
            source=factory_inbound_source,
            projected_days=settings.lead_time_ff_to_wb_days,
        )
        inbound_factory_by_nm = _sum_effective_inbound_rows(effective_inbound_factory_rows)
        inbound_ff_to_wb_by_nm = _sum_effective_inbound_rows(
            _effective_inbound_rows_within_window(
                inbound_ff_to_wb_rows,
                report_date_obj,
                inbound_window_end,
                source=DATASET_INBOUND_FF_TO_WB,
            )
        )
        result_warnings = tuple(supplier_registry_state.warnings) if factory_inbound_source == FACTORY_INBOUND_SOURCE_SUPPLIER_REGISTRY else ()
        if (
            factory_inbound_source == FACTORY_INBOUND_SOURCE_SUPPLIER_REGISTRY
            and not effective_inbound_factory_rows
        ):
            result_warnings = result_warnings + (
                "Источник supplier registry выбран, но usable matched supplier rows внутри расчётного окна = 0.",
            )

        result_rows: list[FactoryOrderRecommendationRow] = []
        for nm_id, sku_comment in active_skus:
            order_samples = order_counts_by_nm.get(nm_id, [])
            daily_demand_total = sum(order_samples) / len(order_samples) if order_samples else 0.0
            demand_horizon = daily_demand_total * horizon_days
            safety_mp_units = daily_demand_total * settings.safety_days_mp
            safety_ff_units = daily_demand_total * settings.safety_days_ff
            cycle_order_units = daily_demand_total * settings.cycle_order_days
            target_qty = demand_horizon + safety_mp_units + safety_ff_units + cycle_order_units
            stock_total_mp = float(stock_total_by_nm.get(nm_id, 0.0))
            stock_ff = float(stock_ff_by_nm.get(nm_id, 0.0))
            inbound_factory_to_ff = float(inbound_factory_by_nm.get(nm_id, 0.0))
            inbound_ff_to_wb = float(inbound_ff_to_wb_by_nm.get(nm_id, 0.0))
            coverage_qty = stock_total_mp + stock_ff + inbound_factory_to_ff + inbound_ff_to_wb
            shortage_qty = max(target_qty - coverage_qty, 0.0)
            recommended_order_qty = (
                int(math.ceil(shortage_qty / settings.order_batch_qty) * settings.order_batch_qty)
                if shortage_qty > 0
                else 0
            )
            result_rows.append(
                FactoryOrderRecommendationRow(
                    nm_id=nm_id,
                    sku_comment=sku_comment,
                    recommended_order_qty=recommended_order_qty,
                    daily_demand_total=daily_demand_total,
                    target_qty=target_qty,
                    coverage_qty=coverage_qty,
                    shortage_qty=shortage_qty,
                    stock_total_mp=stock_total_mp,
                    stock_ff=stock_ff,
                    inbound_factory_to_ff=inbound_factory_to_ff,
                    inbound_ff_to_wb=inbound_ff_to_wb,
                )
            )

        total_qty = sum(item.recommended_order_qty for item in result_rows)
        summary = FactoryOrderSummary(
            total_qty=total_qty,
            estimated_weight=round(total_qty * _WEIGHT_COEFFICIENT, 2),
            estimated_volume=round((total_qty * _WEIGHT_COEFFICIENT) / _VOLUME_DIVISOR, 2),
        )
        result = FactoryOrderCalculationResult(
            status="success",
            calculation_id=uuid4().hex,
            calculated_at=self.timestamp_factory(),
            report_date=report_date,
            horizon_days=horizon_days,
            target_window_days=target_window_days,
            inbound_window_end=inbound_window_end.isoformat(),
            coverage_contract_note=self.sales_history.build_operator_note(_COVERAGE_CONTRACT_NOTE),
            settings=settings,
            factory_inbound_source=factory_inbound_source,
            datasets=datasets,
            manual_factory_inbound_dataset=datasets[DATASET_INBOUND_FACTORY_TO_FF],
            supplier_registry_inbound_summary=supplier_registry_state,
            effective_inbound_factory_to_ff=effective_inbound_factory_rows,
            summary=summary,
            rows=result_rows,
            warnings=result_warnings,
        )
        self.runtime.save_factory_order_result_state(
            calculated_at=result.calculated_at,
            payload=asdict(result),
        )
        return result

    def download_recommendation(self) -> tuple[bytes, str]:
        result = self._load_last_result()
        if result is None:
            raise ValueError("Результат расчёта ещё не подготовлен")
        workbook_rows: list[list[Any]] = [_RESULT_HEADERS]
        workbook_rows.extend(
            [[item.nm_id, item.sku_comment, item.recommended_order_qty] for item in result.rows]
        )
        workbook_rows.append([])
        workbook_rows.extend(
            [
                ["Общее количество", "", result.summary.total_qty],
                ["Расчётный вес", "", _format_decimal(result.summary.estimated_weight)],
                ["Расчётный объём", "", _format_decimal(result.summary.estimated_volume)],
            ]
        )
        filename = f"sheet-vitrina-v1-factory-order-recommendation-{result.report_date}.xlsx"
        return (
            build_single_sheet_workbook_bytes("Рекомендация", workbook_rows),
            filename,
        )

    def _load_active_skus(self) -> list[tuple[int, str]]:
        current_state = self.runtime.load_current_state()
        enabled = sorted(
            [item for item in current_state.config_v2 if item.enabled],
            key=lambda item: item.display_order,
        )
        return [(int(item.nm_id), str(item.display_name)) for item in enabled]

    def _load_dataset_state(self, dataset_type: str) -> FactoryOrderDatasetState:
        payload = self.runtime.load_factory_order_dataset_state(dataset_type)
        if payload is None:
            return FactoryOrderDatasetState(
                dataset_type=dataset_type,
                label_ru=_DATASET_LABELS[dataset_type],
                status="missing",
                uploaded_at=None,
                row_count=0,
                required=_DATASET_REQUIRED[dataset_type],
            )
        shipment_summary = (
            _build_inbound_shipment_summary(self._load_inbound_rows(DATASET_INBOUND_FACTORY_TO_FF))
            if dataset_type == DATASET_INBOUND_FACTORY_TO_FF
            else ()
        )
        return FactoryOrderDatasetState(
            dataset_type=dataset_type,
            label_ru=_DATASET_LABELS[dataset_type],
            status="uploaded",
            uploaded_at=str(payload["uploaded_at"]),
            row_count=int(payload["row_count"]),
            required=_DATASET_REQUIRED[dataset_type],
            uploaded_filename=str(payload.get("uploaded_filename") or "") or None,
            file_available=bool(payload.get("file_available")),
            shipment_summary=shipment_summary,
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

    def _load_inbound_rows(self, dataset_type: str) -> list[FactoryOrderInboundRow]:
        payload = self.runtime.load_factory_order_dataset_state(dataset_type)
        if payload is None:
            return []
        return [
            FactoryOrderInboundRow(
                nm_id=int(item["nm_id"]),
                sku_comment=str(item.get("sku_comment", "") or ""),
                quantity=float(item["quantity"]),
                planned_arrival_date=str(item["planned_arrival_date"]),
                comment=str(item.get("comment", "") or ""),
                shipment_name=str(item.get("shipment_name", "") or ""),
            )
            for item in payload["rows"]
        ]

    def _build_supplier_registry_inbound_state(self) -> FactoryOrderSupplierRegistryInboundState:
        summaries: list[FactoryOrderSupplierRegistryShipmentSummary] = []
        diagnostics = {
            "shipment_count": 0,
            "product_line_count": 0,
            "matched_line_count": 0,
            "unmatched_line_count": 0,
            "ambiguous_line_count": 0,
            "missing_shipment_date_line_count": 0,
            "invalid_quantity_line_count": 0,
            "usable_line_count": 0,
            "usable_quantity": 0.0,
        }
        warnings: list[str] = []
        for shipment in self.runtime.list_supplier_shipments():
            shipment_id = str(shipment.get("shipment_id") or "").strip()
            if not shipment_id:
                continue
            diagnostics["shipment_count"] += 1
            try:
                detail = self.runtime.load_supplier_shipment(shipment_id)
            except Exception as exc:  # pragma: no cover - defensive diagnostics
                warnings.append(f"Supplier shipment {shipment_id} skipped: {exc}")
                continue
            if not detail:
                warnings.append(f"Supplier shipment {shipment_id} skipped: detail is missing.")
                continue
            header = dict(detail.get("header") or {})
            lines = [dict(item) for item in detail.get("lines") or []]
            shipment_date = str(header.get("shipment_date") or "").strip()
            calculated_acceptance_date = (
                (date.fromisoformat(shipment_date) + timedelta(days=SUPPLIER_REGISTRY_FACTORY_TO_FF_ACCEPTANCE_DAYS)).isoformat()
                if _is_iso_date(shipment_date)
                else ""
            )
            shipment_summary = _summarize_supplier_shipment_for_factory_inbound(
                header=header,
                lines=lines,
                calculated_acceptance_date=calculated_acceptance_date,
            )
            diagnostics["product_line_count"] += shipment_summary.product_line_count
            diagnostics["matched_line_count"] += shipment_summary.matched_line_count
            diagnostics["unmatched_line_count"] += shipment_summary.unmatched_line_count
            diagnostics["ambiguous_line_count"] += shipment_summary.ambiguous_line_count
            diagnostics["missing_shipment_date_line_count"] += shipment_summary.missing_shipment_date_line_count
            diagnostics["invalid_quantity_line_count"] += shipment_summary.invalid_quantity_line_count
            diagnostics["usable_line_count"] += shipment_summary.usable_line_count
            diagnostics["usable_quantity"] += shipment_summary.usable_quantity
            if shipment_summary.unmatched_line_count:
                warnings.append(
                    f"{shipment_summary.shipment_label}: unmatched product lines skipped = {shipment_summary.unmatched_line_count}."
                )
            if shipment_summary.ambiguous_line_count:
                warnings.append(
                    f"{shipment_summary.shipment_label}: ambiguous product lines skipped = {shipment_summary.ambiguous_line_count}."
                )
            if shipment_summary.missing_shipment_date_line_count:
                warnings.append(
                    f"{shipment_summary.shipment_label}: product lines skipped because shipment_date is missing = {shipment_summary.missing_shipment_date_line_count}."
                )
            if shipment_summary.invalid_quantity_line_count:
                warnings.append(
                    f"{shipment_summary.shipment_label}: product lines skipped because quantity is missing/invalid = {shipment_summary.invalid_quantity_line_count}."
                )
            summaries.append(
                FactoryOrderSupplierRegistryShipmentSummary(
                    shipment_id=shipment_summary.shipment_id,
                    shipment_label=shipment_summary.shipment_label,
                    invoice_no=shipment_summary.invoice_no,
                    invoice_date=shipment_summary.invoice_date,
                    total_product_quantity=shipment_summary.total_product_quantity,
                    shipment_date=shipment_summary.shipment_date,
                    calculated_acceptance_date=shipment_summary.calculated_acceptance_date,
                    matched_line_count=shipment_summary.matched_line_count,
                    unmatched_line_count=shipment_summary.unmatched_line_count,
                    ambiguous_line_count=shipment_summary.ambiguous_line_count,
                    missing_shipment_date_line_count=shipment_summary.missing_shipment_date_line_count,
                    usable_quantity=shipment_summary.usable_quantity,
                )
            )
        diagnostics_payload = FactoryOrderSupplierRegistryDiagnostics(
            shipment_count=int(diagnostics["shipment_count"]),
            product_line_count=int(diagnostics["product_line_count"]),
            matched_line_count=int(diagnostics["matched_line_count"]),
            unmatched_line_count=int(diagnostics["unmatched_line_count"]),
            ambiguous_line_count=int(diagnostics["ambiguous_line_count"]),
            missing_shipment_date_line_count=int(diagnostics["missing_shipment_date_line_count"]),
            invalid_quantity_line_count=int(diagnostics["invalid_quantity_line_count"]),
            usable_line_count=int(diagnostics["usable_line_count"]),
            usable_quantity=round(float(diagnostics["usable_quantity"]), 2),
        )
        status = "ready" if diagnostics_payload.usable_line_count > 0 else "empty"
        return FactoryOrderSupplierRegistryInboundState(
            source=FACTORY_INBOUND_SOURCE_SUPPLIER_REGISTRY,
            status=status,
            acceptance_days=SUPPLIER_REGISTRY_FACTORY_TO_FF_ACCEPTANCE_DAYS,
            shipment_summary=tuple(summaries),
            diagnostics=diagnostics_payload,
            warnings=tuple(warnings),
        )

    def _supplier_registry_inbound_rows(
        self,
        state: FactoryOrderSupplierRegistryInboundState,
    ) -> list[FactoryOrderInboundRow]:
        active_skus = dict(self._load_active_skus())
        rows: list[FactoryOrderInboundRow] = []
        for shipment_summary in state.shipment_summary:
            if not shipment_summary.calculated_acceptance_date:
                continue
            detail = self.runtime.load_supplier_shipment(shipment_summary.shipment_id)
            if not detail:
                continue
            header = dict(detail.get("header") or {})
            shipment_label = _supplier_shipment_label(header)
            for line in detail.get("lines") or []:
                if not _supplier_line_is_usable_factory_inbound(line, has_valid_shipment_date=True):
                    continue
                nm_id = int(line.get("internal_nm_id"))
                rows.append(
                    FactoryOrderInboundRow(
                        nm_id=nm_id,
                        sku_comment=active_skus.get(nm_id) or str(line.get("internal_name") or ""),
                        quantity=float(line.get("qty") or 0.0),
                        planned_arrival_date=shipment_summary.calculated_acceptance_date,
                        comment="supplier_registry",
                        shipment_name=shipment_label,
                    )
                )
        return rows

    def _load_last_result(self) -> FactoryOrderCalculationResult | None:
        payload = self.runtime.load_factory_order_result_state()
        if not isinstance(payload, dict):
            return None
        settings_payload = payload.get("settings") or {}
        summary_payload = payload.get("summary") or {}
        datasets_payload = payload.get("datasets") or {}
        rows_payload = payload.get("rows") or []
        datasets = {
            key: FactoryOrderDatasetState(
                dataset_type=str(value.get("dataset_type", key)),
                label_ru=str(value.get("label_ru", _DATASET_LABELS.get(key, key))),
                status=str(value.get("status", "missing")),
                uploaded_at=str(value.get("uploaded_at")) if value.get("uploaded_at") else None,
                row_count=int(value.get("row_count", 0)),
                required=bool(value.get("required", _DATASET_REQUIRED.get(key, True))),
                uploaded_filename=str(value.get("uploaded_filename")) if value.get("uploaded_filename") else None,
                file_available=bool(value.get("file_available", False)),
                shipment_summary=_parse_shipment_summary_payload(value.get("shipment_summary")),
            )
            for key, value in datasets_payload.items()
            if isinstance(value, Mapping)
        }
        supplier_registry_state = _parse_supplier_registry_inbound_state(
            payload.get("supplier_registry_inbound_summary")
        )
        manual_dataset = datasets.get(DATASET_INBOUND_FACTORY_TO_FF) or self._load_dataset_state(DATASET_INBOUND_FACTORY_TO_FF)
        return FactoryOrderCalculationResult(
            status=str(payload.get("status", "")),
            calculation_id=str(payload.get("calculation_id", "")),
            calculated_at=str(payload.get("calculated_at", "")),
            report_date=str(payload.get("report_date", "")),
            horizon_days=int(payload.get("horizon_days", 0)),
            target_window_days=int(payload.get("target_window_days", payload.get("horizon_days", 0))),
            inbound_window_end=str(payload.get("inbound_window_end", "") or ""),
            coverage_contract_note=str(payload.get("coverage_contract_note", _COVERAGE_CONTRACT_NOTE)),
            settings=FactoryOrderSettings(
                prod_lead_time_days=int(settings_payload.get("prod_lead_time_days", 0)),
                lead_time_factory_to_ff_days=int(settings_payload.get("lead_time_factory_to_ff_days", 0)),
                lead_time_ff_to_wb_days=int(settings_payload.get("lead_time_ff_to_wb_days", 0)),
                safety_days_mp=int(settings_payload.get("safety_days_mp", 0)),
                safety_days_ff=int(settings_payload.get("safety_days_ff", 0)),
                cycle_order_days=int(settings_payload.get("cycle_order_days", 0)),
                order_batch_qty=int(settings_payload.get("order_batch_qty", 0)),
                report_date_override=(
                    str(settings_payload.get("report_date_override"))
                    if settings_payload.get("report_date_override")
                    else None
                ),
                sales_avg_period_days=int(settings_payload.get("sales_avg_period_days", _DEFAULT_SALES_AVG_PERIOD_DAYS)),
                factory_inbound_source=_normalize_factory_inbound_source(
                    settings_payload.get("factory_inbound_source", payload.get("factory_inbound_source"))
                ),
            ),
            factory_inbound_source=_normalize_factory_inbound_source(payload.get("factory_inbound_source")),
            datasets=datasets,
            manual_factory_inbound_dataset=manual_dataset,
            supplier_registry_inbound_summary=supplier_registry_state,
            effective_inbound_factory_to_ff=_parse_effective_inbound_payload(
                payload.get("effective_inbound_factory_to_ff")
            ),
            summary=FactoryOrderSummary(
                total_qty=int(summary_payload.get("total_qty", 0)),
                estimated_weight=float(summary_payload.get("estimated_weight", 0.0)),
                estimated_volume=float(summary_payload.get("estimated_volume", 0.0)),
            ),
            rows=[
                FactoryOrderRecommendationRow(
                    nm_id=int(item.get("nm_id", 0)),
                    sku_comment=str(item.get("sku_comment", "")),
                    recommended_order_qty=int(item.get("recommended_order_qty", 0)),
                    daily_demand_total=float(item.get("daily_demand_total", 0.0)),
                    target_qty=float(item.get("target_qty", 0.0)),
                    coverage_qty=float(item.get("coverage_qty", 0.0)),
                    shortage_qty=float(item.get("shortage_qty", 0.0)),
                    stock_total_mp=float(item.get("stock_total_mp", 0.0)),
                    stock_ff=float(item.get("stock_ff", 0.0)),
                    inbound_factory_to_ff=float(item.get("inbound_factory_to_ff", 0.0)),
                    inbound_ff_to_wb=float(item.get("inbound_ff_to_wb", 0.0)),
                )
                for item in rows_payload
                if isinstance(item, Mapping)
            ],
            warnings=tuple(str(item) for item in payload.get("warnings", []) if str(item or "").strip()),
        )

    def _parse_dataset_rows(
        self,
        *,
        dataset_type: str,
        workbook_rows: list[list[Any]],
        active_skus: dict[int, str],
    ) -> tuple[list[FactoryOrderStockFfRow | FactoryOrderInboundRow], int]:
        if not workbook_rows:
            raise ValueError("XLSX-файл пустой")
        actual_headers = [str(value or "").strip() for value in workbook_rows[0]]
        expected_headers = _TEMPLATE_HEADERS[dataset_type]
        has_shipment_column = dataset_type == DATASET_INBOUND_FACTORY_TO_FF and actual_headers == expected_headers
        if dataset_type == DATASET_INBOUND_FACTORY_TO_FF and actual_headers == _LEGACY_INBOUND_FACTORY_HEADERS:
            has_shipment_column = False
        elif actual_headers != expected_headers:
            raise ValueError(
                "Неверные заголовки в XLSX. "
                f"Ожидались: {', '.join(expected_headers)}. "
                f"Получены: {', '.join(actual_headers) if actual_headers else 'пусто'}."
            )
        if dataset_type == DATASET_STOCK_FF:
            return self._parse_stock_ff_rows(workbook_rows[1:], active_skus), 0
        return self._parse_inbound_rows(
            dataset_type,
            workbook_rows[1:],
            active_skus,
            has_shipment_column=has_shipment_column,
        )

    def _parse_stock_ff_rows(
        self,
        workbook_rows: list[list[Any]],
        active_skus: dict[int, str],
    ) -> list[FactoryOrderStockFfRow]:
        parsed_rows: list[FactoryOrderStockFfRow] = []
        seen_nm_ids: set[int] = set()
        for row_index, row in enumerate(workbook_rows, start=2):
            padded = list(row[:5]) + [None] * max(0, 5 - len(row))
            if _row_is_empty(padded):
                continue
            nm_id = _parse_nm_id(padded[0], row_index=row_index, active_skus=active_skus)
            if nm_id in seen_nm_ids:
                raise ValueError(f"Строка {row_index}: повторяющийся nmId в остатках ФФ: {nm_id}")
            seen_nm_ids.add(nm_id)
            stock_ff = _parse_nonnegative_number(padded[2], row_index=row_index, field_label="Остаток ФФ")
            snapshot_date = _parse_optional_date(padded[3], row_index=row_index, field_label="Дата остатка")
            parsed_rows.append(
                FactoryOrderStockFfRow(
                    nm_id=nm_id,
                    sku_comment=_normalize_uploaded_comment(padded[1], fallback=active_skus[nm_id]),
                    stock_ff=stock_ff,
                    snapshot_date=snapshot_date,
                    comment=_normalize_optional_text(padded[4]),
                )
            )
        if set(active_skus) != set(row.nm_id for row in parsed_rows):
            missing = sorted(set(active_skus) - {row.nm_id for row in parsed_rows})
            raise ValueError(
                "В остатках ФФ не хватает активных SKU: " + ", ".join(str(item) for item in missing)
            )
        return parsed_rows

    def _parse_inbound_rows(
        self,
        dataset_type: str,
        workbook_rows: list[list[Any]],
        active_skus: dict[int, str],
        *,
        has_shipment_column: bool,
    ) -> tuple[list[FactoryOrderInboundRow], int]:
        parsed_rows: list[FactoryOrderInboundRow] = []
        ignored_row_count = 0
        column_count = 6 if has_shipment_column else 5
        for row_index, row in enumerate(workbook_rows, start=2):
            padded = list(row[:column_count]) + [None] * max(0, column_count - len(row))
            if _row_is_empty(padded):
                continue
            nm_id = _parse_nm_id(padded[0], row_index=row_index, active_skus=active_skus)
            quantity_raw = padded[2]
            date_raw = padded[3]
            comment = _normalize_optional_text(padded[4])
            if _event_row_is_placeholder(quantity_raw, date_raw, comment):
                ignored_row_count += 1
                continue
            quantity = _parse_nonnegative_number(
                quantity_raw,
                row_index=row_index,
                field_label="Количество в пути",
            )
            if quantity == 0:
                ignored_row_count += 1
                continue
            planned_arrival_date = _parse_required_date(
                date_raw,
                row_index=row_index,
                field_label=(
                    "Планируемая дата прихода на ФФ"
                    if dataset_type == DATASET_INBOUND_FACTORY_TO_FF
                    else "Планируемая дата прихода на Wildberries"
                ),
            )
            parsed_rows.append(
                FactoryOrderInboundRow(
                    nm_id=nm_id,
                    sku_comment=_normalize_uploaded_comment(padded[1], fallback=active_skus[nm_id]),
                    quantity=quantity,
                    planned_arrival_date=planned_arrival_date,
                    comment=comment,
                    shipment_name=_normalize_optional_text(padded[5]) if has_shipment_column else "",
                )
            )
        return parsed_rows, ignored_row_count


def _default_now_factory() -> datetime:
    return datetime.now(timezone.utc)


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_settings(payload: Mapping[str, Any]) -> FactoryOrderSettings:
    return FactoryOrderSettings(
        prod_lead_time_days=_parse_positive_int(payload.get("prod_lead_time_days"), "Срок производства"),
        lead_time_factory_to_ff_days=_parse_positive_int(
            payload.get("lead_time_factory_to_ff_days"),
            "Доставка фабрики -> ФФ",
        ),
        lead_time_ff_to_wb_days=_parse_positive_int(
            payload.get("lead_time_ff_to_wb_days"),
            "Доставка ФФ -> Wildberries",
        ),
        safety_days_mp=_parse_nonnegative_int(payload.get("safety_days_mp"), "Страховой запас MP"),
        safety_days_ff=_parse_nonnegative_int(payload.get("safety_days_ff"), "Страховой запас ФФ"),
        cycle_order_days=_parse_cycle_order_days(payload.get("cycle_order_days")),
        order_batch_qty=_parse_positive_int(payload.get("order_batch_qty"), "Кратность штук в коробке"),
        report_date_override=_parse_optional_date(payload.get("report_date_override"), row_index=None, field_label="Дата отчёта"),
        sales_avg_period_days=_parse_sales_avg_period_days(payload.get("sales_avg_period_days")),
        factory_inbound_source=_parse_factory_inbound_source(payload.get("factory_inbound_source")),
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


def _parse_factory_inbound_source(value: Any) -> str:
    normalized = str(value or "").strip() or FACTORY_INBOUND_SOURCE_MANUAL_EXCEL
    if normalized not in {FACTORY_INBOUND_SOURCE_MANUAL_EXCEL, FACTORY_INBOUND_SOURCE_SUPPLIER_REGISTRY}:
        raise ValueError(
            "Источник товаров в пути от фабрики должен быть manual_excel или supplier_registry"
        )
    return normalized


def _normalize_factory_inbound_source(value: Any) -> str:
    try:
        return _parse_factory_inbound_source(value)
    except ValueError:
        return FACTORY_INBOUND_SOURCE_MANUAL_EXCEL


def _parse_cycle_order_days(value: Any) -> int:
    if value in ("", None):
        return _DEFAULT_CYCLE_ORDER_DAYS
    try:
        numeric = int(str(value).strip())
    except ValueError as exc:
        raise ValueError("Цикл заказов должен быть целым числом") from exc
    if numeric <= 0:
        return _DEFAULT_CYCLE_ORDER_DAYS
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


def _parse_nm_id(value: Any, *, row_index: int, active_skus: Mapping[int, str]) -> int:
    try:
        nm_id = int(str(value).strip())
    except Exception as exc:
        raise ValueError(f"Строка {row_index}: nmId должен быть целым числом") from exc
    if nm_id not in active_skus:
        raise ValueError(f"Строка {row_index}: nmId {nm_id} отсутствует в активном списке SKU")
    return nm_id


def _parse_nonnegative_number(value: Any, *, row_index: int, field_label: str) -> float:
    number = _parse_number(value, row_index=row_index, field_label=field_label)
    if number < 0:
        raise ValueError(f"Строка {row_index}: {field_label} не может быть отрицательным")
    return number


def _parse_positive_number(value: Any, *, row_index: int, field_label: str) -> float:
    number = _parse_number(value, row_index=row_index, field_label=field_label)
    if number <= 0:
        raise ValueError(f"Строка {row_index}: {field_label} должен быть больше нуля")
    return number


def _parse_number(value: Any, *, row_index: int, field_label: str) -> float:
    normalized = _cell_text(value).replace(",", ".")
    if not normalized:
        raise ValueError(f"Строка {row_index}: {field_label} обязателен")
    try:
        return float(normalized)
    except ValueError as exc:
        raise ValueError(f"Строка {row_index}: {field_label} должен быть числом") from exc


def _parse_required_date(value: Any, *, row_index: int, field_label: str) -> str:
    parsed = _parse_optional_date(value, row_index=row_index, field_label=field_label)
    if not parsed:
        raise ValueError(f"Строка {row_index}: {field_label} обязательна")
    return parsed


def _parse_optional_date(value: Any, *, row_index: int | None, field_label: str) -> str | None:
    normalized = _cell_text(value)
    if not normalized:
        return None
    for parser in (_parse_iso_date, _parse_dotted_date, _parse_iso_datetime_date):
        parsed = parser(normalized)
        if parsed is not None:
            return parsed
    prefix = f"Строка {row_index}: " if row_index is not None else ""
    raise ValueError(f"{prefix}{field_label} должна быть датой в формате YYYY-MM-DD или DD.MM.YYYY")


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


def _normalize_optional_text(value: Any) -> str:
    return _cell_text(value)


def _normalize_uploaded_comment(value: Any, *, fallback: str) -> str:
    normalized = _normalize_optional_text(value)
    return normalized or fallback


def _row_is_empty(row: list[Any]) -> bool:
    return all(_cell_text(value) == "" for value in row)


def _event_row_is_placeholder(quantity_value: Any, date_value: Any, comment_value: str) -> bool:
    return _cell_text(quantity_value) == "" and _cell_text(date_value) == "" and comment_value == ""


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_uploaded_filename(value: str | None, *, dataset_type: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return _DATASET_FILENAMES[dataset_type]
    normalized = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return normalized or _DATASET_FILENAMES[dataset_type]


def _build_inbound_shipment_summary(
    rows: list[FactoryOrderInboundRow],
) -> tuple[FactoryOrderInboundShipmentSummary, ...]:
    totals: dict[tuple[str, str], float] = {}
    ordered_keys: list[tuple[str, str]] = []
    for row in rows:
        shipment_name = _normalize_optional_text(row.shipment_name)
        acceptance_date = str(row.planned_arrival_date or "").strip()
        if not acceptance_date:
            continue
        key = (shipment_name, acceptance_date)
        if key not in totals:
            ordered_keys.append(key)
            totals[key] = 0.0
        totals[key] += float(row.quantity)

    fallback_index = 1
    summary: list[FactoryOrderInboundShipmentSummary] = []
    for shipment_name, acceptance_date in ordered_keys:
        shipment_label = shipment_name
        if not shipment_label:
            shipment_label = f"Поставка №{fallback_index}"
            fallback_index += 1
        summary.append(
            FactoryOrderInboundShipmentSummary(
                shipment=shipment_label,
                total_quantity=round(totals[(shipment_name, acceptance_date)], 2),
                acceptance_date=acceptance_date,
            )
        )
    return tuple(summary)


def _parse_shipment_summary_payload(value: Any) -> tuple[FactoryOrderInboundShipmentSummary, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    summary: list[FactoryOrderInboundShipmentSummary] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        summary.append(
            FactoryOrderInboundShipmentSummary(
                shipment=str(item.get("shipment", "") or ""),
                total_quantity=float(item.get("total_quantity", 0.0)),
                acceptance_date=str(item.get("acceptance_date", "") or ""),
            )
        )
    return tuple(summary)


def _parse_supplier_registry_inbound_state(value: Any) -> FactoryOrderSupplierRegistryInboundState:
    if not isinstance(value, Mapping):
        return _empty_supplier_registry_inbound_state()
    diagnostics_payload = value.get("diagnostics") if isinstance(value.get("diagnostics"), Mapping) else {}
    summaries = []
    for item in value.get("shipment_summary") or []:
        if not isinstance(item, Mapping):
            continue
        summaries.append(
            FactoryOrderSupplierRegistryShipmentSummary(
                shipment_id=str(item.get("shipment_id", "") or ""),
                shipment_label=str(item.get("shipment_label", "") or ""),
                invoice_no=str(item.get("invoice_no", "") or ""),
                invoice_date=str(item.get("invoice_date", "") or ""),
                total_product_quantity=float(item.get("total_product_quantity", 0.0)),
                shipment_date=str(item.get("shipment_date", "") or ""),
                calculated_acceptance_date=str(item.get("calculated_acceptance_date", "") or ""),
                matched_line_count=int(item.get("matched_line_count", 0)),
                unmatched_line_count=int(item.get("unmatched_line_count", 0)),
                ambiguous_line_count=int(item.get("ambiguous_line_count", 0)),
                missing_shipment_date_line_count=int(item.get("missing_shipment_date_line_count", 0)),
                usable_quantity=float(item.get("usable_quantity", 0.0)),
            )
        )
    diagnostics = FactoryOrderSupplierRegistryDiagnostics(
        shipment_count=int(diagnostics_payload.get("shipment_count", 0)),
        product_line_count=int(diagnostics_payload.get("product_line_count", 0)),
        matched_line_count=int(diagnostics_payload.get("matched_line_count", 0)),
        unmatched_line_count=int(diagnostics_payload.get("unmatched_line_count", 0)),
        ambiguous_line_count=int(diagnostics_payload.get("ambiguous_line_count", 0)),
        missing_shipment_date_line_count=int(diagnostics_payload.get("missing_shipment_date_line_count", 0)),
        invalid_quantity_line_count=int(diagnostics_payload.get("invalid_quantity_line_count", 0)),
        usable_line_count=int(diagnostics_payload.get("usable_line_count", 0)),
        usable_quantity=float(diagnostics_payload.get("usable_quantity", 0.0)),
    )
    return FactoryOrderSupplierRegistryInboundState(
        source=FACTORY_INBOUND_SOURCE_SUPPLIER_REGISTRY,
        status=str(value.get("status", "empty") or "empty"),
        acceptance_days=int(value.get("acceptance_days", SUPPLIER_REGISTRY_FACTORY_TO_FF_ACCEPTANCE_DAYS)),
        shipment_summary=tuple(summaries),
        diagnostics=diagnostics,
        warnings=tuple(str(item) for item in value.get("warnings", []) if str(item or "").strip()),
    )


def _empty_supplier_registry_inbound_state() -> FactoryOrderSupplierRegistryInboundState:
    return FactoryOrderSupplierRegistryInboundState(
        source=FACTORY_INBOUND_SOURCE_SUPPLIER_REGISTRY,
        status="empty",
        acceptance_days=SUPPLIER_REGISTRY_FACTORY_TO_FF_ACCEPTANCE_DAYS,
        shipment_summary=(),
        diagnostics=FactoryOrderSupplierRegistryDiagnostics(
            shipment_count=0,
            product_line_count=0,
            matched_line_count=0,
            unmatched_line_count=0,
            ambiguous_line_count=0,
            missing_shipment_date_line_count=0,
            invalid_quantity_line_count=0,
            usable_line_count=0,
            usable_quantity=0.0,
        ),
        warnings=(),
    )


def _parse_effective_inbound_payload(value: Any) -> list[FactoryOrderEffectiveInboundRow]:
    if not isinstance(value, list):
        return []
    rows: list[FactoryOrderEffectiveInboundRow] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        rows.append(
            FactoryOrderEffectiveInboundRow(
                source=str(item.get("source", "") or ""),
                nm_id=int(item.get("nm_id", 0)),
                sku_comment=str(item.get("sku_comment", "") or ""),
                quantity=float(item.get("quantity", 0.0)),
                planned_arrival_date=str(item.get("planned_arrival_date", "") or ""),
                effective_arrival_date=str(item.get("effective_arrival_date", "") or ""),
                shipment_name=str(item.get("shipment_name", "") or ""),
                comment=str(item.get("comment", "") or ""),
            )
        )
    return rows


def _effective_inbound_rows_within_window(
    rows: list[FactoryOrderInboundRow],
    report_date: date,
    inbound_window_end: date,
    *,
    source: str,
    projected_days: int = 0,
) -> list[FactoryOrderEffectiveInboundRow]:
    effective_rows: list[FactoryOrderEffectiveInboundRow] = []
    for row in rows:
        effective_arrival_date = date.fromisoformat(row.planned_arrival_date) + timedelta(days=projected_days)
        if effective_arrival_date < report_date or effective_arrival_date > inbound_window_end:
            continue
        effective_rows.append(
            FactoryOrderEffectiveInboundRow(
                source=source,
                nm_id=row.nm_id,
                sku_comment=row.sku_comment,
                quantity=float(row.quantity),
                planned_arrival_date=row.planned_arrival_date,
                effective_arrival_date=effective_arrival_date.isoformat(),
                shipment_name=row.shipment_name,
                comment=row.comment,
            )
        )
    return effective_rows


def _sum_effective_inbound_rows(rows: list[FactoryOrderEffectiveInboundRow]) -> dict[int, float]:
    totals: dict[int, float] = {}
    for row in rows:
        totals[row.nm_id] = totals.get(row.nm_id, 0.0) + float(row.quantity)
    return totals


def _summarize_supplier_shipment_for_factory_inbound(
    *,
    header: Mapping[str, Any],
    lines: list[Mapping[str, Any]],
    calculated_acceptance_date: str,
) -> _SupplierRegistryShipmentFactorySummary:
    shipment_id = str(header.get("shipment_id") or "").strip()
    shipment_date = str(header.get("shipment_date") or "").strip()
    has_valid_shipment_date = bool(calculated_acceptance_date)
    product_lines = [item for item in lines if item.get("line_type") == LINE_TYPE_PRODUCT]
    matched_line_count = 0
    unmatched_line_count = 0
    ambiguous_line_count = 0
    missing_shipment_date_line_count = 0
    invalid_quantity_line_count = 0
    usable_line_count = 0
    usable_quantity = 0.0
    total_product_quantity = 0.0
    for line in product_lines:
        qty = _optional_positive_line_quantity(line.get("qty"))
        if qty is not None:
            total_product_quantity += qty
        match_status = str(line.get("match_status") or "").strip()
        if match_status in {MATCH_STATUS_MATCHED, MATCH_STATUS_MATCHED_BY_COMPATIBILITY} and _optional_int(line.get("internal_nm_id")) is not None:
            matched_line_count += 1
        elif match_status == MATCH_STATUS_AMBIGUOUS:
            ambiguous_line_count += 1
        elif match_status == MATCH_STATUS_UNMATCHED:
            unmatched_line_count += 1
        else:
            unmatched_line_count += 1
        if not has_valid_shipment_date:
            missing_shipment_date_line_count += 1
            continue
        if qty is None:
            invalid_quantity_line_count += 1
            continue
        if _supplier_line_is_usable_factory_inbound(line, has_valid_shipment_date=has_valid_shipment_date):
            usable_line_count += 1
            usable_quantity += qty
    header_total = _optional_positive_line_quantity(header.get("product_qty_total"))
    return _SupplierRegistryShipmentFactorySummary(
        shipment_id=shipment_id,
        shipment_label=_supplier_shipment_label(header),
        invoice_no=str(header.get("invoice_no") or ""),
        invoice_date=str(header.get("invoice_date") or ""),
        total_product_quantity=round(header_total if header_total is not None else total_product_quantity, 2),
        shipment_date=shipment_date,
        calculated_acceptance_date=calculated_acceptance_date,
        product_line_count=len(product_lines),
        matched_line_count=matched_line_count,
        unmatched_line_count=unmatched_line_count,
        ambiguous_line_count=ambiguous_line_count,
        missing_shipment_date_line_count=missing_shipment_date_line_count,
        invalid_quantity_line_count=invalid_quantity_line_count,
        usable_line_count=usable_line_count,
        usable_quantity=round(usable_quantity, 2),
    )


def _supplier_line_is_usable_factory_inbound(line: Mapping[str, Any], *, has_valid_shipment_date: bool) -> bool:
    if not has_valid_shipment_date:
        return False
    if line.get("line_type") != LINE_TYPE_PRODUCT:
        return False
    if str(line.get("match_status") or "").strip() not in {MATCH_STATUS_MATCHED, MATCH_STATUS_MATCHED_BY_COMPATIBILITY}:
        return False
    if _optional_int(line.get("internal_nm_id")) is None:
        return False
    return _optional_positive_line_quantity(line.get("qty")) is not None


def _supplier_shipment_label(header: Mapping[str, Any]) -> str:
    invoice_no = str(header.get("invoice_no") or "").strip()
    if invoice_no:
        return invoice_no
    shipment_id = str(header.get("shipment_id") or "").strip()
    return shipment_id or "supplier shipment"


def _optional_positive_line_quantity(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _format_decimal(value: float) -> str:
    return f"{float(value):.2f}"
