"""Server-owned quantity ledger for current ФФ stock balances."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
from typing import Any, Mapping
from uuid import uuid4

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.contracts.supplier_shipments import (
    LINE_TYPE_PRODUCT,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_MATCHED_BY_COMPATIBILITY,
)


CONTRACT_NAME = "sheet_vitrina_v1_ff_stock_ledger"
CONTRACT_VERSION = "v1"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

FF_STOCK_OPERATION_MANUAL_RECEIPT = "manual_receipt"
FF_STOCK_OPERATION_MANUAL_WRITEOFF = "manual_writeoff"
FF_STOCK_OPERATION_AUTO_RECEIPT = "auto_receipt"
FF_STOCK_OPERATION_AUTO_WRITEOFF = "auto_writeoff"
FF_STOCK_OPERATION_CORRECTION_RECEIPT = "correction_receipt"

FF_STOCK_SOURCE_MANUAL_EXCEL = "manual_excel"
FF_STOCK_SOURCE_SUPPLIER_SHIPMENT = "supplier_shipment"
FF_STOCK_SOURCE_WB_SUPPLY = "wb_supply"
FF_STOCK_SOURCE_RUNTIME_REPAIR = "runtime_repair"

FF_STOCK_LEDGER_SOURCE_KEY_PREFIX = "ff_stock_ledger"

WB_DEBIT_STATUS_IDS = {3, 4, 5, 6}
WB_SKIP_VIRTUAL_TYPE_ID = 5
WB_SKIP_TYPE_LABEL = "Допринято"

FF_STOCK_XLSX_HEADERS = [
    "barcode",
    "nmId",
    "SKU/название/комментарий",
    "группа",
    "количество",
]

_MANUAL_OPERATION_LABELS = {
    FF_STOCK_OPERATION_MANUAL_RECEIPT: "ручное оприходование",
    FF_STOCK_OPERATION_MANUAL_WRITEOFF: "ручное списание",
    FF_STOCK_OPERATION_AUTO_RECEIPT: "автооприходование",
    FF_STOCK_OPERATION_AUTO_WRITEOFF: "автосписание",
    FF_STOCK_OPERATION_CORRECTION_RECEIPT: "корректировка",
}


class FfStockLedgerBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory

    def get_status(self, *, operations_limit: int = 50) -> dict[str, Any]:
        registry_rows = self.current_balance_rows()
        operations = self.runtime.list_ff_stock_operations(limit=operations_limit)
        operations = [_with_operation_public_fields(operation) for operation in operations]
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ok",
            "registry": {
                "rows": registry_rows,
                "summary": _balance_summary(registry_rows),
            },
            "operations": operations,
        }

    def current_balance_rows(self) -> list[dict[str, Any]]:
        balances_by_nm = {
            int(item.get("nm_id") or 0): float(item.get("balance") or 0.0)
            for item in self.runtime.list_ff_stock_balances()
        }
        items = self._active_nomenclature_items()
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in items:
            nm_id = _optional_int(item.get("nm_id"))
            if nm_id is None or nm_id in seen:
                continue
            seen.add(nm_id)
            balance = float(balances_by_nm.get(nm_id, 0.0))
            rows.append(
                {
                    **_nomenclature_public_fields(item),
                    "current_stock_ff": balance,
                    "quantity": balance,
                    "negative_balance": balance < 0,
                    "warning": "Отрицательный остаток ФФ" if balance < 0 else "",
                }
            )
        return rows

    def current_balance_rows_for_active_skus(self, active_skus: list[tuple[int, str]]) -> list[dict[str, Any]]:
        balances_by_nm = {
            int(item.get("nm_id") or 0): float(item.get("balance") or 0.0)
            for item in self.runtime.list_ff_stock_balances()
        }
        nomenclature_by_nm = self._nomenclature_by_nm()
        rows: list[dict[str, Any]] = []
        for nm_id, sku_comment in active_skus:
            item = nomenclature_by_nm.get(int(nm_id), {})
            balance = float(balances_by_nm.get(int(nm_id), 0.0))
            rows.append(
                {
                    **_nomenclature_public_fields(item),
                    "nm_id": int(nm_id),
                    "sku": str(item.get("our_sku") or ""),
                    "sku_comment": str(sku_comment or item.get("nomenclature_name") or item.get("comment") or ""),
                    "current_stock_ff": balance,
                    "quantity": balance,
                    "negative_balance": balance < 0,
                    "warning": "Отрицательный остаток ФФ" if balance < 0 else "",
                }
            )
        return rows

    def export_current_balances_xlsx(self) -> tuple[bytes, str, str]:
        rows = [FF_STOCK_XLSX_HEADERS]
        rows.extend(
            [
                [
                    row.get("barcode") or "",
                    row.get("nm_id") or "",
                    row.get("sku_display") or "",
                    row.get("group_name") or "",
                    row.get("current_stock_ff") or 0,
                ]
                for row in self.current_balance_rows()
            ]
        )
        return (
            build_single_sheet_workbook_bytes("Остатки ФФ", rows),
            "sheet-vitrina-v1-ff-stock-balances.xlsx",
            XLSX_CONTENT_TYPE,
        )

    def parse_manual_operation_preview(
        self,
        workbook_bytes: bytes,
        *,
        operation_type: str,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        normalized_operation_type = _normalize_manual_operation_type(operation_type)
        if not workbook_bytes:
            raise ValueError("XLSX file is empty")
        filename = _safe_xlsx_filename(uploaded_filename or "ff-stock-operation.xlsx")
        rows = read_first_sheet_rows(workbook_bytes)
        parsed_lines, warnings, errors = self._parse_rows(rows)
        signed_lines = [
            {
                **line,
                "quantity_delta": line["quantity"] if normalized_operation_type == FF_STOCK_OPERATION_MANUAL_RECEIPT else -line["quantity"],
            }
            for line in parsed_lines
        ]
        summary = _preview_summary(signed_lines, warnings=warnings, errors=errors)
        preview_id = "ffsp_" + uuid4().hex[:20]
        preview = self.runtime.save_ff_stock_operation_preview(
            preview_id=preview_id,
            operation_type=normalized_operation_type,
            created_at=self.timestamp_factory(),
            uploaded_filename=filename,
            uploaded_content_type=str(uploaded_content_type or XLSX_CONTENT_TYPE),
            source_file_sha256=hashlib.sha256(workbook_bytes).hexdigest(),
            workbook_bytes=workbook_bytes,
            parsed_lines=signed_lines,
            summary=summary,
            warnings=warnings,
            errors=errors,
        )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "preview",
            "preview": preview,
            "apply_allowed": summary["error_count"] == 0 and summary["total_quantity_abs"] > 0,
        }

    def confirm_manual_operation(self, preview_id: str, *, created_by: str = "") -> dict[str, Any]:
        preview = self.runtime.load_ff_stock_operation_preview(preview_id, include_file_blob=True)
        if preview is None:
            raise ValueError(f"Операция ФФ preview не найдена: {preview_id}")
        errors = list(preview.get("errors") or [])
        if errors:
            raise ValueError("Нельзя применить документ ФФ с ошибками строк")
        lines = [dict(item) for item in preview.get("parsed_lines") or []]
        if not lines:
            raise ValueError("Нельзя применить пустой документ ФФ")
        operation_id = "ffso_" + uuid4().hex[:20]
        source_key = f"manual_excel:{operation_id}"
        operation = self.runtime.create_ff_stock_operation(
            operation_id=operation_id,
            operation_type=str(preview.get("operation_type") or ""),
            source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
            source_key=source_key,
            source_object_id=operation_id,
            source_object_label=str(preview.get("uploaded_filename") or ""),
            created_at=self.timestamp_factory(),
            created_by=created_by,
            warnings=list(preview.get("warnings") or []),
            diagnostics={"preview_id": preview_id, "summary": dict(preview.get("summary") or {})},
            source_filename=str(preview.get("uploaded_filename") or ""),
            source_content_type=str(preview.get("uploaded_content_type") or XLSX_CONTENT_TYPE),
            source_file_sha256=str(preview.get("source_file_sha256") or ""),
            source_file_bytes=bytes(preview.get("workbook_bytes") or b""),
            lines=lines,
        )
        self.runtime.delete_ff_stock_operation_preview(preview_id)
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "applied",
            "operation": _with_operation_public_fields(operation),
            "registry": {
                "rows": self.current_balance_rows(),
            },
        }

    def download_operation_source_file(self, operation_id: str) -> tuple[bytes, str, str]:
        operation = self.runtime.load_ff_stock_operation(operation_id, include_file_blob=True)
        if operation is None:
            raise ValueError(f"Операция ФФ не найдена: {operation_id}")
        body = bytes(operation.get("source_file_bytes") or b"")
        if not body:
            raise ValueError("Для этой операции ФФ исходный Excel-файл отсутствует")
        return (
            body,
            str(operation.get("source_filename") or f"{operation_id}.xlsx"),
            str(operation.get("source_content_type") or XLSX_CONTENT_TYPE),
        )

    def record_supplier_acceptance(self, shipment_detail: Mapping[str, Any]) -> dict[str, Any] | None:
        header = dict(shipment_detail.get("header") or {})
        shipment_id = str(header.get("shipment_id") or "").strip()
        if not shipment_id:
            return None
        source_key = f"supplier_shipment_acceptance:{shipment_id}"
        existing = self.runtime.load_ff_stock_operation_by_source_key(source_key)
        if existing is not None:
            existing["idempotent"] = True
            return existing
        lines, warnings = _supplier_shipment_lines(shipment_detail)
        label = str(header.get("invoice_no") or shipment_id)
        if header.get("invoice_date"):
            label = f"{label} от {header['invoice_date']}"
        return self.runtime.create_ff_stock_operation(
            operation_id="ffso_" + uuid4().hex[:20],
            operation_type=FF_STOCK_OPERATION_AUTO_RECEIPT,
            source_type=FF_STOCK_SOURCE_SUPPLIER_SHIPMENT,
            source_key=source_key,
            source_object_id=shipment_id,
            source_object_label=label,
            created_at=self.timestamp_factory(),
            created_by="system",
            warnings=warnings,
            diagnostics={"shipment_id": shipment_id},
            lines=lines,
        )

    def record_wb_supply_debits(self, records: list[Mapping[str, Any]]) -> dict[str, Any]:
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for record in records:
            result = self.record_wb_supply_debit(record)
            if not result:
                continue
            if result.get("operation_id") and not result.get("idempotent"):
                created.append(result)
            elif result.get("skip_reason"):
                skipped.append(result)
        return {
            "created_count": len(created),
            "created_operation_ids": [str(item.get("operation_id") or "") for item in created],
            "skipped_count": len(skipped),
            "skipped_total_quantity": sum(float(item.get("total_quantity") or 0.0) for item in skipped),
            "skipped_reasons": _count_by_key(skipped, "skip_reason"),
            "skipped": skipped[:20],
        }

    def record_wb_supply_debit(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        normalized = dict(record.get("normalized") or record)
        status_id = _optional_int(normalized.get("status_id"))
        if status_id not in WB_DEBIT_STATUS_IDS:
            return None
        if _optional_int(normalized.get("virtual_type_id")) == WB_SKIP_VIRTUAL_TYPE_ID:
            return {"skip_reason": "wb_supply_doprinato_virtual_type", "supply_id": str(normalized.get("supply_id") or "")}
        if str(normalized.get("type_label") or "").strip() == WB_SKIP_TYPE_LABEL:
            return {"skip_reason": "wb_supply_doprinato_type_label", "supply_id": str(normalized.get("supply_id") or "")}
        supply_id = str(normalized.get("supply_id") or record.get("supply_id") or "").strip()
        cache_key = str(normalized.get("cache_key") or record.get("cache_key") or supply_id).strip()
        source_key = f"wb_supply_debit:{cache_key or supply_id}"
        existing = self.runtime.load_ff_stock_operation_by_source_key(source_key)
        if existing is not None:
            existing["idempotent"] = True
            return existing
        raw_goods = record.get("raw_goods")
        if not isinstance(raw_goods, list):
            raw_goods = normalized.get("raw_goods")
        if not isinstance(raw_goods, list) or not raw_goods:
            return {"skip_reason": "wb_supply_goods_missing", "supply_id": supply_id, "source_key": source_key}
        lines, warnings = _wb_supply_goods_lines(raw_goods, self._nomenclature_by_nm())
        if not lines:
            return {"skip_reason": "wb_supply_goods_without_usable_qty", "supply_id": supply_id, "source_key": source_key}
        activation = self.runtime.load_ff_stock_activation_operation()
        if activation is None:
            return {
                "skip_reason": "wb_supply_ledger_not_activated",
                "supply_id": supply_id,
                "source_key": source_key,
                "total_quantity": sum(abs(float(item.get("quantity_delta") or 0.0)) for item in lines),
            }
        activation_dt = _parse_datetime_like(activation.get("created_at"))
        source_dt, source_dt_field = _wb_supply_business_timestamp(record=record, normalized=normalized)
        if activation_dt is None or source_dt is None:
            return {
                "skip_reason": "wb_supply_source_date_missing",
                "supply_id": supply_id,
                "source_key": source_key,
                "activation_created_at": str(activation.get("created_at") or ""),
            }
        if source_dt < activation_dt:
            return {
                "skip_reason": "wb_supply_before_ledger_activation",
                "supply_id": supply_id,
                "source_key": source_key,
                "source_timestamp": source_dt.isoformat(),
                "source_timestamp_field": source_dt_field,
                "activation_created_at": activation_dt.isoformat(),
                "activation_operation_id": str(activation.get("operation_id") or ""),
                "total_quantity": sum(abs(float(item.get("quantity_delta") or 0.0)) for item in lines),
            }
        negative_preview = _negative_balance_preview(lines, self.runtime.list_ff_stock_balances())
        if negative_preview:
            return {
                "skip_reason": "wb_supply_would_make_negative_balance",
                "supply_id": supply_id,
                "source_key": source_key,
                "source_timestamp": source_dt.isoformat(),
                "source_timestamp_field": source_dt_field,
                "activation_created_at": activation_dt.isoformat(),
                "negative_nm_ids": negative_preview[:20],
                "total_quantity": sum(abs(float(item.get("quantity_delta") or 0.0)) for item in lines),
            }
        total_quantity = sum(abs(float(item.get("quantity_delta") or 0.0)) for item in lines)
        if 0 < total_quantity < 250:
            warnings.append(f"WB-поставка меньше 250 шт: {total_quantity:g}")
        label = str(normalized.get("visible_number") or normalized.get("number_label") or supply_id or cache_key)
        return self.runtime.create_ff_stock_operation(
            operation_id="ffso_" + uuid4().hex[:20],
            operation_type=FF_STOCK_OPERATION_AUTO_WRITEOFF,
            source_type=FF_STOCK_SOURCE_WB_SUPPLY,
            source_key=source_key,
            source_object_id=supply_id or cache_key,
            source_object_label=label,
            created_at=self.timestamp_factory(),
            created_by="system",
            warnings=warnings,
            diagnostics={
                "cache_key": cache_key,
                "status_id": status_id,
                "virtual_type_id": normalized.get("virtual_type_id"),
                "type_label": normalized.get("type_label"),
                "source_timestamp": source_dt.isoformat(),
                "source_timestamp_field": source_dt_field,
                "ledger_activation_operation_id": str(activation.get("operation_id") or ""),
                "ledger_activation_created_at": activation_dt.isoformat(),
            },
            lines=lines,
        )

    def _parse_rows(self, rows: list[list[Any]]) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
        if not rows:
            raise ValueError("XLSX должен содержать строку заголовков")
        header = [str(item or "").strip() for item in rows[0]]
        indexes = _resolve_header_indexes(header)
        active_items = self._active_nomenclature_items()
        by_nm = {
            int(item["nm_id"]): item
            for item in active_items
            if _optional_int(item.get("nm_id")) is not None
        }
        by_barcode: dict[str, dict[str, Any]] = {}
        for item in active_items:
            for barcode in [item.get("barcode"), *(item.get("barcodes") or [])]:
                normalized_barcode = str(barcode or "").strip()
                if normalized_barcode and normalized_barcode not in by_barcode:
                    by_barcode[normalized_barcode] = item
        parsed: dict[int, dict[str, Any]] = {}
        warnings: list[str] = []
        errors: list[dict[str, Any]] = []
        duplicate_rows: dict[int, list[int]] = {}
        for row_index, row in enumerate(rows[1:], start=2):
            if _row_is_empty(row):
                continue
            barcode = _cell_text(_row_value(row, indexes["barcode"]))
            nm_id = _optional_int(_row_value(row, indexes["nm_id"]))
            quantity = _optional_float(_row_value(row, indexes["quantity"]))
            if quantity is None:
                errors.append({"row_index": row_index, "error": "Количество должно быть положительным числом"})
                continue
            if quantity < 0:
                errors.append({"row_index": row_index, "error": "Количество не должно быть отрицательным"})
                continue
            if quantity == 0:
                continue
            item = by_nm.get(nm_id) if nm_id is not None else None
            if item is None and barcode:
                item = by_barcode.get(barcode)
            if item is None:
                errors.append({"row_index": row_index, "error": "SKU не найден в активной номенклатуре"})
                continue
            matched_nm_id = _optional_int(item.get("nm_id"))
            if matched_nm_id is None:
                errors.append({"row_index": row_index, "error": "В найденной номенклатуре нет nmId"})
                continue
            if nm_id is not None and nm_id != matched_nm_id:
                errors.append({"row_index": row_index, "error": f"nmId не совпадает с barcode: {nm_id} != {matched_nm_id}"})
                continue
            if barcode and item.get("barcode") and barcode != item.get("barcode") and barcode not in (item.get("barcodes") or []):
                warnings.append(f"Строка {row_index}: barcode отличается от текущей номенклатуры")
            line = parsed.setdefault(
                matched_nm_id,
                {
                    **_nomenclature_public_fields(item),
                    "nm_id": matched_nm_id,
                    "quantity": 0.0,
                    "source_rows": [],
                    "raw": {},
                },
            )
            line["quantity"] += float(quantity)
            line["source_rows"].append(row_index)
            duplicate_rows.setdefault(matched_nm_id, []).append(row_index)
        for nm_id, row_indexes in duplicate_rows.items():
            if len(row_indexes) > 1:
                warnings.append(f"nmId {nm_id}: несколько строк объединены ({', '.join(str(item) for item in row_indexes)})")
        return list(parsed.values()), warnings, errors

    def _active_nomenclature_items(self) -> list[dict[str, Any]]:
        groups = {
            str(group.get("group_key") or ""): str(group.get("label") or group.get("group_key") or "")
            for group in self.runtime.list_sku_groups(include_inactive=True)
        }
        result: list[dict[str, Any]] = []
        for item in self.runtime.list_nomenclature_items(active_only=True):
            if bool(item.get("is_hidden")):
                continue
            normalized = dict(item)
            product_type = str(normalized.get("product_type") or "")
            normalized["group_name"] = groups.get(product_type, product_type)
            result.append(normalized)
        return result

    def _nomenclature_by_nm(self) -> dict[int, dict[str, Any]]:
        return {
            int(item["nm_id"]): item
            for item in self._active_nomenclature_items()
            if _optional_int(item.get("nm_id")) is not None
        }


def resolve_ff_stock_ledger_rows(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    active_skus: list[tuple[int, str]],
) -> tuple[list[Any], dict[str, Any]]:
    from packages.contracts.factory_order_supply import FactoryOrderStockFfRow

    block = FfStockLedgerBlock(runtime=runtime)
    balance_rows = block.current_balance_rows_for_active_skus(active_skus)
    rows = [
        FactoryOrderStockFfRow(
            nm_id=int(item["nm_id"]),
            sku_comment=str(item.get("sku_comment") or item.get("sku_display") or ""),
            stock_ff=float(item.get("current_stock_ff") or 0.0),
            snapshot_date=None,
            comment=FF_STOCK_LEDGER_SOURCE_KEY_PREFIX,
        )
        for item in balance_rows
    ]
    negative_rows = [item for item in balance_rows if bool(item.get("negative_balance"))]
    total_stock_ff = sum(float(item.get("current_stock_ff") or 0.0) for item in balance_rows)
    warnings: list[str] = []
    if negative_rows:
        warnings.append(f"Отрицательный остаток ФФ по {len(negative_rows)} SKU")
        warnings.append(
            "Остатки ФФ отрицательные/некорректные, рекомендации к поставке ограничены доступным ФФ-остатком"
        )
    if total_stock_ff <= 0 and active_skus:
        warnings.append("Остатки ФФ <= 0, рекомендации к поставке ограничены доступным ФФ-остатком")
    state = {
        "status": "ready",
        "source": FF_STOCK_LEDGER_SOURCE_KEY_PREFIX,
        "source_label_ru": "Остатки ФФ",
        "active_sku_count": len(active_skus),
        "covered_sku_count": len(rows),
        "total_stock_ff": total_stock_ff,
        "negative_sku_count": len(negative_rows),
        "warnings": warnings,
        "requires_manual_review": bool(negative_rows or (total_stock_ff <= 0 and active_skus)),
    }
    return rows, state


def _supplier_shipment_lines(shipment_detail: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    grouped: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []
    for line in shipment_detail.get("lines") or []:
        item = dict(line or {})
        if str(item.get("line_type") or "") != LINE_TYPE_PRODUCT:
            continue
        match_status = str(item.get("match_status") or "")
        nm_id = _optional_int(item.get("internal_nm_id"))
        quantity = _optional_float(item.get("qty"))
        if match_status not in {MATCH_STATUS_MATCHED, MATCH_STATUS_MATCHED_BY_COMPATIBILITY} or nm_id is None:
            warnings.append(f"Строка поставщика {item.get('source_no') or item.get('sort_order') or ''}: нет matched nmId")
            continue
        if quantity is None or quantity <= 0:
            warnings.append(f"Строка поставщика {item.get('source_no') or item.get('sort_order') or ''}: нет положительного количества")
            continue
        target = grouped.setdefault(
            nm_id,
            {
                "nm_id": nm_id,
                "barcode": "",
                "sku": str(item.get("internal_sku") or ""),
                "nomenclature_name": str(item.get("internal_name") or ""),
                "comment": str(item.get("comment") or ""),
                "group_name": str(item.get("product_type") or ""),
                "quantity_delta": 0.0,
                "raw": {},
            },
        )
        target["quantity_delta"] += float(quantity)
    return list(grouped.values()), warnings


def _wb_supply_goods_lines(raw_goods: list[Mapping[str, Any]], nomenclature_by_nm: Mapping[int, Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    grouped: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []
    for index, raw in enumerate(raw_goods, start=1):
        item = dict(raw or {})
        nm_id = _optional_int(_first_present(item, "nmID", "nmId", "nm_id"))
        quantity = _optional_float(_first_present(item, "quantity", "qty"))
        if nm_id is None:
            warnings.append(f"WB goods row {index}: нет nmId")
            continue
        if quantity is None or quantity <= 0:
            warnings.append(f"WB goods row {index}: нет положительного quantity")
            continue
        nomenclature = dict(nomenclature_by_nm.get(nm_id) or {})
        target = grouped.setdefault(
            nm_id,
            {
                **_nomenclature_public_fields(nomenclature),
                "nm_id": nm_id,
                "barcode": str(_first_present(item, "barcode", "barCode", "barcodeID") or nomenclature.get("barcode") or ""),
                "quantity_delta": 0.0,
                "raw": {},
            },
        )
        target["quantity_delta"] -= float(quantity)
    return list(grouped.values()), warnings


def _negative_balance_preview(
    lines: list[Mapping[str, Any]],
    current_balances: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    balances_by_nm = {
        int(item.get("nm_id") or 0): float(item.get("balance") or 0.0)
        for item in current_balances
    }
    result: list[dict[str, Any]] = []
    for line in lines:
        nm_id = _optional_int(line.get("nm_id"))
        if nm_id is None:
            continue
        delta = float(line.get("quantity_delta") or 0.0)
        next_balance = float(balances_by_nm.get(nm_id, 0.0)) + delta
        if next_balance < 0:
            result.append(
                {
                    "nm_id": nm_id,
                    "current_balance": float(balances_by_nm.get(nm_id, 0.0)),
                    "quantity_delta": delta,
                    "next_balance": next_balance,
                }
            )
    return result


def _count_by_key(items: list[Mapping[str, Any]], key: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "").strip()
        if not value:
            continue
        result[value] = result.get(value, 0) + 1
    return result


def _wb_supply_business_timestamp(
    *,
    record: Mapping[str, Any],
    normalized: Mapping[str, Any],
) -> tuple[datetime | None, str]:
    sources: list[tuple[str, Mapping[str, Any]]] = [
        ("normalized", normalized),
        ("record", record),
    ]
    for raw_key in ("raw_list", "raw_detail"):
        raw_value = record.get(raw_key)
        if isinstance(raw_value, Mapping):
            sources.append((raw_key, raw_value))
    fields = (
        "source_created_at",
        "createDate",
        "createdAt",
        "supply_date",
        "supplyDate",
        "fact_date",
        "factDate",
    )
    for source_name, source in sources:
        for field in fields:
            if field not in source:
                continue
            parsed = _parse_datetime_like(source.get(field))
            if parsed is not None:
                return parsed, f"{source_name}.{field}"
    return None, ""


def _parse_datetime_like(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed_date = date.fromisoformat(text[:10])
            except ValueError:
                return None
            parsed = datetime(parsed_date.year, parsed_date.month, parsed_date.day)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _nomenclature_public_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    sku = str(item.get("our_sku") or "").strip()
    name = str(item.get("nomenclature_name") or "").strip()
    comment = str(item.get("comment") or "").strip()
    display_parts = [part for part in (sku, name, comment) if part]
    return {
        "barcode": str(item.get("barcode") or item.get("primary_barcode") or "").strip(),
        "nm_id": _optional_int(item.get("nm_id")) or 0,
        "sku": sku,
        "nomenclature_name": name,
        "comment": comment,
        "sku_display": " / ".join(display_parts),
        "group_name": str(item.get("group_name") or item.get("product_type") or "").strip(),
    }


def _with_operation_public_fields(operation: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(operation)
    operation_type = str(payload.get("operation_type") or "")
    payload["operation_type_label"] = _MANUAL_OPERATION_LABELS.get(operation_type, operation_type)
    payload["download_path"] = (
        f"/v1/sheet-vitrina-v1/supply/ff-stocks/operations/{payload.get('operation_id')}/file"
        if payload.get("file_available")
        else ""
    )
    return payload


def _balance_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "sku_count": len(rows),
        "total_quantity": sum(float(item.get("current_stock_ff") or 0.0) for item in rows),
        "negative_sku_count": sum(1 for item in rows if bool(item.get("negative_balance"))),
    }


def _preview_summary(lines: list[Mapping[str, Any]], *, warnings: list[str], errors: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "sku_count": len({int(item.get("nm_id") or 0) for item in lines}),
        "total_quantity_delta": sum(float(item.get("quantity_delta") or 0.0) for item in lines),
        "total_quantity_abs": sum(abs(float(item.get("quantity_delta") or 0.0)) for item in lines),
        "warning_count": len(warnings),
        "error_count": len(errors),
    }


def _resolve_header_indexes(header: list[str]) -> dict[str, int]:
    normalized = {_normalize_header(value): index for index, value in enumerate(header)}
    aliases = {
        "barcode": {"barcode", "штрихкод", "шкbarcode", "шк"},
        "nm_id": {"nmid", "nm", "артикулwb"},
        "quantity": {"количество", "qty", "quantity"},
    }
    result: dict[str, int] = {}
    for key, candidates in aliases.items():
        for candidate in candidates:
            if candidate in normalized:
                result[key] = normalized[candidate]
                break
        if key not in result:
            raise ValueError(f"В XLSX нет обязательной колонки: {key}")
    return result


def _normalize_header(value: str) -> str:
    return "".join(char.casefold() for char in str(value or "") if char.isalnum())


def _normalize_manual_operation_type(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized in {FF_STOCK_OPERATION_MANUAL_RECEIPT, "receipt"}:
        return FF_STOCK_OPERATION_MANUAL_RECEIPT
    if normalized in {FF_STOCK_OPERATION_MANUAL_WRITEOFF, "writeoff"}:
        return FF_STOCK_OPERATION_MANUAL_WRITEOFF
    raise ValueError("Тип операции ФФ должен быть manual_receipt или manual_writeoff")


def _safe_xlsx_filename(value: str) -> str:
    filename = str(value or "").strip() or "ff-stock-operation.xlsx"
    if not filename.lower().endswith(".xlsx"):
        raise ValueError("ФФ stock upload accepts .xlsx files only")
    return filename.replace("/", "_").replace("\\", "_")


def _row_is_empty(row: list[Any]) -> bool:
    return not any(str(item or "").strip() for item in row)


def _row_value(row: list[Any], index: int) -> Any:
    return row[index] if 0 <= index < len(row) else None


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _optional_int(value: Any) -> int | None:
    if value in ("", None):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) not in ("", None):
            return mapping.get(key)
    return None


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
