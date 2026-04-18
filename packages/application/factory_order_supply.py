"""Server-owned factory-order supply block for the operator page."""

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
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.application.stocks_block import StocksBlock
from packages.business_time import current_business_date_iso
from packages.contracts.factory_order_supply import (
    DATASET_INBOUND_FACTORY_TO_FF,
    DATASET_INBOUND_FF_TO_WB,
    DATASET_STOCK_FF,
    FactoryOrderCalculationResult,
    FactoryOrderDatasetDeleteResult,
    FactoryOrderDatasetState,
    FactoryOrderInboundRow,
    FactoryOrderRecommendationRow,
    FactoryOrderSettings,
    FactoryOrderStatus,
    FactoryOrderStockFfRow,
    FactoryOrderSummary,
    FactoryOrderUploadResult,
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
    ],
    DATASET_INBOUND_FF_TO_WB: [
        "nmId",
        "Комментарий SKU",
        "Количество в пути",
        "Планируемая дата прихода на Wildberries",
        "Комментарий",
    ],
}
_RESULT_HEADERS = ["nmId", "Комментарий SKU", "Рекомендовано к заказу"]
_WEIGHT_COEFFICIENT = 0.08593
_VOLUME_DIVISOR = 204.38
_DEFAULT_SALES_AVG_PERIOD_DAYS = 7
_COVERAGE_CONTRACT_NOTE = (
    "Файлы «Товары в пути от фабрики» и «Товары в пути от ФФ на Wildberries» необязательны: "
    "если файл не загружен, соответствующий inbound считается как 0. "
    "В пути ФФ -> Wildberries учитываются только из отдельного загруженного шаблона, "
    "потому что в текущем wb-core нет другого authoritative source для этого члена формулы."
)


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
        return FactoryOrderStatus(
            status="ready" if last_result is not None else "idle",
            active_sku_count=len(active_skus),
            coverage_contract_note=self.sales_history.build_operator_note(_COVERAGE_CONTRACT_NOTE),
            datasets=datasets,
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
            rows.extend([[nm_id, sku_comment, "", "", ""] for nm_id, sku_comment in active_skus])
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
        )
        return FactoryOrderUploadResult(
            status="accepted",
            dataset=dataset_state,
            accepted_row_count=len(parsed_rows),
            ignored_row_count=ignored_row_count,
            message=f"Файл принят: {_DATASET_LABELS[dataset_type].lower()}",
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
        inbound_factory_rows = self._load_inbound_rows(DATASET_INBOUND_FACTORY_TO_FF)
        inbound_ff_to_wb_rows = self._load_inbound_rows(DATASET_INBOUND_FF_TO_WB)

        report_date = settings.report_date_override or current_business_date_iso(self.now_factory())
        horizon_days = (
            settings.prod_lead_time_days
            + settings.lead_time_factory_to_ff_days
            + settings.lead_time_ff_to_wb_days
        )
        report_date_obj = date.fromisoformat(report_date)
        horizon_end = report_date_obj + timedelta(days=horizon_days)
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
        inbound_factory_by_nm = _sum_inbound_rows_within_horizon(inbound_factory_rows, horizon_end)
        inbound_ff_to_wb_by_nm = _sum_inbound_rows_within_horizon(inbound_ff_to_wb_rows, horizon_end)

        result_rows: list[FactoryOrderRecommendationRow] = []
        for nm_id, sku_comment in active_skus:
            order_samples = order_counts_by_nm.get(nm_id, [])
            daily_demand_total = sum(order_samples) / len(order_samples) if order_samples else 0.0
            demand_horizon = daily_demand_total * horizon_days
            safety_mp_units = daily_demand_total * settings.safety_days_mp
            safety_ff_units = daily_demand_total * settings.safety_days_ff
            target_qty = demand_horizon + safety_mp_units + safety_ff_units
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
            coverage_contract_note=self.sales_history.build_operator_note(_COVERAGE_CONTRACT_NOTE),
            settings=settings,
            datasets=datasets,
            summary=summary,
            rows=result_rows,
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
        return FactoryOrderDatasetState(
            dataset_type=dataset_type,
            label_ru=_DATASET_LABELS[dataset_type],
            status="uploaded",
            uploaded_at=str(payload["uploaded_at"]),
            row_count=int(payload["row_count"]),
            required=_DATASET_REQUIRED[dataset_type],
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
            )
            for item in payload["rows"]
        ]

    def _load_last_result(self) -> FactoryOrderCalculationResult | None:
        payload = self.runtime.load_factory_order_result_state()
        if not isinstance(payload, dict):
            return None
        settings_payload = payload.get("settings") or {}
        summary_payload = payload.get("summary") or {}
        datasets_payload = payload.get("datasets") or {}
        rows_payload = payload.get("rows") or []
        return FactoryOrderCalculationResult(
            status=str(payload.get("status", "")),
            calculation_id=str(payload.get("calculation_id", "")),
            calculated_at=str(payload.get("calculated_at", "")),
            report_date=str(payload.get("report_date", "")),
            horizon_days=int(payload.get("horizon_days", 0)),
            coverage_contract_note=str(payload.get("coverage_contract_note", _COVERAGE_CONTRACT_NOTE)),
            settings=FactoryOrderSettings(
                prod_lead_time_days=int(settings_payload.get("prod_lead_time_days", 0)),
                lead_time_factory_to_ff_days=int(settings_payload.get("lead_time_factory_to_ff_days", 0)),
                lead_time_ff_to_wb_days=int(settings_payload.get("lead_time_ff_to_wb_days", 0)),
                safety_days_mp=int(settings_payload.get("safety_days_mp", 0)),
                safety_days_ff=int(settings_payload.get("safety_days_ff", 0)),
                order_batch_qty=int(settings_payload.get("order_batch_qty", 0)),
                report_date_override=(
                    str(settings_payload.get("report_date_override"))
                    if settings_payload.get("report_date_override")
                    else None
                ),
                sales_avg_period_days=int(settings_payload.get("sales_avg_period_days", _DEFAULT_SALES_AVG_PERIOD_DAYS)),
            ),
            datasets={
                key: FactoryOrderDatasetState(
                    dataset_type=str(value.get("dataset_type", key)),
                    label_ru=str(value.get("label_ru", _DATASET_LABELS.get(key, key))),
                    status=str(value.get("status", "missing")),
                    uploaded_at=str(value.get("uploaded_at")) if value.get("uploaded_at") else None,
                    row_count=int(value.get("row_count", 0)),
                    required=bool(value.get("required", _DATASET_REQUIRED.get(key, True))),
                    uploaded_filename=str(value.get("uploaded_filename")) if value.get("uploaded_filename") else None,
                    file_available=bool(value.get("file_available", False)),
                )
                for key, value in datasets_payload.items()
                if isinstance(value, Mapping)
            },
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
        if actual_headers != expected_headers:
            raise ValueError(
                "Неверные заголовки в XLSX. "
                f"Ожидались: {', '.join(expected_headers)}. "
                f"Получены: {', '.join(actual_headers) if actual_headers else 'пусто'}."
            )
        if dataset_type == DATASET_STOCK_FF:
            return self._parse_stock_ff_rows(workbook_rows[1:], active_skus), 0
        return self._parse_inbound_rows(dataset_type, workbook_rows[1:], active_skus)

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
    ) -> tuple[list[FactoryOrderInboundRow], int]:
        parsed_rows: list[FactoryOrderInboundRow] = []
        ignored_row_count = 0
        for row_index, row in enumerate(workbook_rows, start=2):
            padded = list(row[:5]) + [None] * max(0, 5 - len(row))
            if _row_is_empty(padded):
                continue
            nm_id = _parse_nm_id(padded[0], row_index=row_index, active_skus=active_skus)
            quantity_raw = padded[2]
            date_raw = padded[3]
            comment = _normalize_optional_text(padded[4])
            if _event_row_is_placeholder(quantity_raw, date_raw, comment):
                ignored_row_count += 1
                continue
            quantity = _parse_positive_number(
                quantity_raw,
                row_index=row_index,
                field_label="Количество в пути",
            )
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
            "Срок фабрика -> ФФ",
        ),
        lead_time_ff_to_wb_days=_parse_positive_int(
            payload.get("lead_time_ff_to_wb_days"),
            "Срок ФФ -> Wildberries",
        ),
        safety_days_mp=_parse_nonnegative_int(payload.get("safety_days_mp"), "Страховой запас MP"),
        safety_days_ff=_parse_nonnegative_int(payload.get("safety_days_ff"), "Страховой запас ФФ"),
        order_batch_qty=_parse_positive_int(payload.get("order_batch_qty"), "Кратность штук в коробке"),
        report_date_override=_parse_optional_date(payload.get("report_date_override"), row_index=None, field_label="Дата отчёта"),
        sales_avg_period_days=_parse_sales_avg_period_days(payload.get("sales_avg_period_days")),
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


def _sum_inbound_rows_within_horizon(rows: list[FactoryOrderInboundRow], horizon_end: date) -> dict[int, float]:
    totals: dict[int, float] = {}
    for row in rows:
        arrival_date = date.fromisoformat(row.planned_arrival_date)
        if arrival_date > horizon_end:
            continue
        totals[row.nm_id] = totals.get(row.nm_id, 0.0) + float(row.quantity)
    return totals


def _format_decimal(value: float) -> str:
    return f"{float(value):.2f}"
