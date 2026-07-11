"""Server-owned quantity ledger for current ФФ stock balances."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
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
FF_STOCK_SOURCE_TARGETED_RECONCILIATION = "wb_supply_targeted_reconciliation"

FF_STOCK_LEDGER_SOURCE_KEY_PREFIX = "ff_stock_ledger"
FF_STOCK_OPERATION_DEFAULT_PAGE_SIZE = 50
FF_STOCK_OPERATION_PAGE_SIZES = (50, 100, 200)

WB_DEBIT_STATUS_IDS = {3, 4, 5, 6}
WB_SKIP_VIRTUAL_TYPE_ID = 5
WB_SKIP_TYPE_LABEL = "Допринято"
TARGETED_WB_RECONCILIATION_REASON = "targeted_pre_activation_remediation"
TARGETED_WB_RECONCILIATION_PLAN_VERSION = "v2"
TARGETED_WB_RECONCILIATION_SUPPLY_ID = "40561872"
TARGETED_WB_RECONCILIATION_EXPECTED_SKU_COUNT = 13
TARGETED_WB_RECONCILIATION_EXPECTED_DEBIT = 31_500.0
TARGETED_WB_RECONCILIATION_EXPECTED_TOTAL_BEFORE = 38_250.0
TARGETED_WB_RECONCILIATION_EXPECTED_TOTAL_AFTER = 6_750.0
TARGETED_WB_RECONCILIATION_ORDINARY_BLOCKERS = (
    "wb_supply_before_auto_writeoff_checkpoint",
    "wb_supply_before_ledger_activation",
)


class TargetedWbSupplyReconciliationError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

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

    def get_status(
        self,
        *,
        operations_limit: Any = FF_STOCK_OPERATION_DEFAULT_PAGE_SIZE,
        operations_page: Any = 1,
        operations_offset: Any | None = None,
        show_technical_archive: bool = True,
    ) -> dict[str, Any]:
        registry_rows = self.current_balance_rows()
        checkpoint = self.runtime.load_ff_stock_wb_auto_writeoff_checkpoint()
        archive_cutoff_created_at = str((checkpoint or {}).get("created_at") or "").strip()
        limit = _normalize_operation_page_size(operations_limit)
        include_archive = bool(show_technical_archive)
        total_count = self.runtime.count_ff_stock_operations(
            include_technical_archive=include_archive,
            archive_cutoff_created_at=archive_cutoff_created_at,
        )
        total_all_count = self.runtime.count_ff_stock_operations(include_technical_archive=True)
        page_count = max(1, (total_count + limit - 1) // limit)
        if operations_offset is not None:
            offset = _normalize_operation_offset(operations_offset)
            if total_count:
                offset = min(offset, (page_count - 1) * limit)
            page = (offset // limit) + 1
        else:
            page = min(_normalize_operation_page(operations_page), page_count)
            offset = (page - 1) * limit
        operations = self.runtime.list_ff_stock_operations(
            limit=limit,
            offset=offset,
            include_technical_archive=include_archive,
            archive_cutoff_created_at=archive_cutoff_created_at,
        )
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
            "operations_page": {
                "limit": limit,
                "allowed_page_sizes": list(FF_STOCK_OPERATION_PAGE_SIZES),
                "offset": offset,
                "current_offset": offset,
                "page": page,
                "current_page": page,
                "page_count": page_count,
                "total_count": total_count,
                "total": total_count,
                "total_all_count": total_all_count,
                "hidden_archive_count": max(0, total_all_count - total_count),
                "has_next": offset + limit < total_count,
                "has_previous": offset > 0,
                "show_technical_archive": include_archive,
                "archive_cutoff_created_at": archive_cutoff_created_at,
            },
            "wb_auto_writeoff_checkpoint": checkpoint,
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

    def ensure_wb_supply_auto_writeoff_checkpoint(
        self,
        records: list[Mapping[str, Any]],
        *,
        reason: str,
        created_by: str = "system",
    ) -> dict[str, Any]:
        existing = self.runtime.load_ff_stock_wb_auto_writeoff_checkpoint()
        if existing is not None:
            existing["idempotent"] = True
            return existing
        cache_keys: set[str] = set()
        source_keys: set[str] = set()
        supply_ids: set[str] = set()
        source_timestamps: list[datetime] = []
        supply_dates: list[str] = []
        for record in records:
            normalized = dict(record.get("normalized") or record)
            cache_key, supply_id, source_key = _wb_supply_debit_identity(record=record, normalized=normalized)
            if cache_key:
                cache_keys.add(cache_key)
            if source_key:
                source_keys.add(source_key)
            if supply_id:
                supply_ids.add(supply_id)
            source_dt, _ = _wb_supply_business_timestamp(record=record, normalized=normalized)
            if source_dt is not None:
                source_timestamps.append(source_dt)
            supply_date = str(
                _first_present(normalized, "supply_date", "supplyDate")
                or _first_present(record, "supply_date", "supplyDate")
                or ""
            ).strip()[:10]
            if supply_date:
                supply_dates.append(supply_date)
        watermark_source_created_at = _format_utc_z(max(source_timestamps)) if source_timestamps else ""
        return self.runtime.save_ff_stock_wb_auto_writeoff_checkpoint(
            checkpoint_id="ffswc_" + uuid4().hex[:20],
            created_at=self.timestamp_factory(),
            created_by=created_by,
            reason=reason,
            baseline_cache_keys=sorted(cache_keys),
            baseline_source_keys=sorted(source_keys),
            baseline_supply_ids=sorted(supply_ids),
            watermark_source_created_at=watermark_source_created_at,
            watermark_supply_date=max(supply_dates) if supply_dates else "",
            diagnostics={
                "baseline_input_record_count": len(records),
                "baseline_cache_key_count": len(cache_keys),
                "baseline_source_key_count": len(source_keys),
                "baseline_supply_id_count": len(supply_ids),
            },
        )

    def record_wb_supply_debit(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        normalized = dict(record.get("normalized") or record)
        status_id = _optional_int(normalized.get("status_id"))
        if status_id not in WB_DEBIT_STATUS_IDS:
            return None
        if _optional_int(normalized.get("virtual_type_id")) == WB_SKIP_VIRTUAL_TYPE_ID:
            return self._record_own_capital_doprinato(record, normalized)
        if str(normalized.get("type_label") or "").strip() == WB_SKIP_TYPE_LABEL:
            return self._record_own_capital_doprinato(record, normalized)
        cache_key, supply_id, source_key = _wb_supply_debit_identity(record=record, normalized=normalized)
        if not source_key:
            return {"skip_reason": "wb_supply_identity_missing", "supply_id": supply_id}
        existing = self.runtime.load_ff_stock_operation_by_source_key(source_key)
        if existing is not None:
            existing["idempotent"] = True
            existing["own_product_capital"] = self._record_own_capital_wb_supply(record, normalized)
            return existing
        raw_goods = record.get("raw_goods")
        if not isinstance(raw_goods, list):
            raw_goods = normalized.get("raw_goods")
        if not isinstance(raw_goods, list) or not raw_goods:
            return {"skip_reason": "wb_supply_goods_missing", "supply_id": supply_id, "source_key": source_key}
        lines, warnings = _wb_supply_goods_lines(raw_goods, self._nomenclature_by_nm())
        if warnings:
            return {
                "skip_reason": "wb_supply_goods_atomic_matching_blocked",
                "supply_id": supply_id,
                "source_key": source_key,
                "problem_rows": warnings[:50],
            }
        if not lines:
            return {"skip_reason": "wb_supply_goods_without_usable_qty", "supply_id": supply_id, "source_key": source_key}
        total_quantity = sum(abs(float(item.get("quantity_delta") or 0.0)) for item in lines)
        checkpoint = self.runtime.load_ff_stock_wb_auto_writeoff_checkpoint()
        if checkpoint is None:
            return {
                "skip_reason": "wb_supply_auto_writeoff_checkpoint_missing",
                "supply_id": supply_id,
                "source_key": source_key,
                "total_quantity": total_quantity,
            }
        source_dt, source_dt_field = _wb_supply_business_timestamp(record=record, normalized=normalized)
        checkpoint_dt = _parse_datetime_like(checkpoint.get("created_at"))
        if checkpoint_dt is None:
            return {
                "skip_reason": "wb_supply_auto_writeoff_checkpoint_invalid",
                "supply_id": supply_id,
                "source_key": source_key,
                "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
                "checkpoint_created_at": str(checkpoint.get("created_at") or ""),
                "total_quantity": total_quantity,
            }
        if source_dt is None:
            return {
                "skip_reason": "wb_supply_source_date_missing",
                "supply_id": supply_id,
                "source_key": source_key,
                "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
                "checkpoint_created_at": checkpoint_dt.isoformat(),
                "total_quantity": total_quantity,
            }
        baseline_match_fields = _wb_supply_checkpoint_match_fields(
            checkpoint=checkpoint,
            cache_key=cache_key,
            supply_id=supply_id,
            source_key=source_key,
        )
        if baseline_match_fields or source_dt <= checkpoint_dt:
            return {
                "skip_reason": "wb_supply_before_auto_writeoff_checkpoint",
                "supply_id": supply_id,
                "source_key": source_key,
                "cache_key": cache_key,
                "source_timestamp": source_dt.isoformat(),
                "source_timestamp_field": source_dt_field,
                "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
                "checkpoint_created_at": checkpoint_dt.isoformat(),
                "checkpoint_match_fields": baseline_match_fields,
                "total_quantity": total_quantity,
            }
        activation = self.runtime.load_ff_stock_activation_operation()
        if activation is None:
            return {
                "skip_reason": "wb_supply_ledger_not_activated",
                "supply_id": supply_id,
                "source_key": source_key,
                "total_quantity": total_quantity,
            }
        activation_dt = _parse_datetime_like(activation.get("created_at"))
        if activation_dt is None:
            return {
                "skip_reason": "wb_supply_ledger_activation_invalid",
                "supply_id": supply_id,
                "source_key": source_key,
                "activation_created_at": str(activation.get("created_at") or ""),
                "total_quantity": total_quantity,
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
                "total_quantity": total_quantity,
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
                "total_quantity": total_quantity,
            }
        if 0 < total_quantity < 250:
            warnings.append(f"WB-поставка меньше 250 шт: {total_quantity:g}")
        label = str(normalized.get("visible_number") or normalized.get("number_label") or supply_id or cache_key)
        operation = self.runtime.create_ff_stock_operation(
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
                "auto_writeoff_checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
                "auto_writeoff_checkpoint_created_at": checkpoint_dt.isoformat(),
                "ledger_activation_operation_id": str(activation.get("operation_id") or ""),
                "ledger_activation_created_at": activation_dt.isoformat(),
            },
            lines=lines,
        )
        operation["own_product_capital"] = self._record_own_capital_wb_supply(record, normalized)
        return operation

    def _record_own_capital_wb_supply(
        self,
        record: Mapping[str, Any],
        normalized: Mapping[str, Any],
        *,
        recalculate: bool = True,
    ) -> dict[str, Any]:
        from packages.application.own_product_capital import OwnProductCapitalBlock

        capital = OwnProductCapitalBlock(runtime=self.runtime, timestamp_factory=self.timestamp_factory)
        if not capital.has_events():
            return {"status": "skipped", "reason": "no_paid_capital_events"}
        raw_goods = record.get("raw_goods")
        if not isinstance(raw_goods, list):
            raw_goods = normalized.get("raw_goods")
        if not isinstance(raw_goods, list) or not raw_goods:
            return {"status": "blocked", "reason": "wb_supply_goods_missing"}
        sent, accepted, problems = _wb_capital_quantities(raw_goods)
        if problems:
            return {"status": "blocked", "reason": "wb_supply_goods_atomic_matching_blocked", "problem_rows": problems}
        supply_id = str(_first_present(normalized, "supply_id", "wb_supply_id", "preorder_id") or "").strip()
        business_dt, _ = _wb_supply_business_timestamp(record=record, normalized=normalized)
        if business_dt is None:
            return {"status": "blocked", "reason": "wb_supply_source_date_missing"}
        warehouse = str(_first_present(normalized, "warehouse_name", "warehouseName", "warehouse") or "")
        destination = str(
            _first_present(
                normalized,
                "destination_name",
                "target_warehouse_name",
                "targetWarehouseName",
                "warehouse_name",
            )
            or ""
        )
        try:
            status_id = _optional_int(normalized.get("status_id"))
            if status_id in {4, 5}:
                if status_id == 5:
                    missing = sorted(nm_id for nm_id in sent if nm_id not in accepted)
                    if missing:
                        raise ValueError(
                            f"final accepted quantity is missing for nmID {missing}"
                        )
                acceptance_dt = _wb_acceptance_business_timestamp(record=record, normalized=normalized)
                if acceptance_dt is None:
                    raise ValueError("WB acceptance fact date is missing")
                result = capital.record_ordinary_wb_supply_acceptance(
                    supply_id=supply_id,
                    writeoff_date=business_dt.date().isoformat(),
                    acceptance_date=acceptance_dt.date().isoformat(),
                    sent_quantities_by_nm=sent,
                    accepted_quantities_by_nm=accepted,
                    warehouse=warehouse,
                    destination=destination,
                    known_nm_ids=self._nomenclature_by_nm().keys(),
                    expenses_complete=False,
                    final=status_id == 5,
                    recalculate=recalculate,
                )
            else:
                result = capital.record_ff_writeoff(
                    supply_id=supply_id,
                    effective_date=business_dt.date().isoformat(),
                    sent_quantities_by_nm=sent,
                    warehouse=warehouse,
                    destination=destination,
                    known_nm_ids=self._nomenclature_by_nm().keys(),
                    expenses_complete=False,
                )
            capital.resolve_blockers(source_identity=supply_id)
            return result
        except ValueError as exc:
            capital._record_blocker(  # noqa: SLF001 - same bounded application contour
                code="wb_capital_movement_blocked",
                source_identity=supply_id,
                details={"reason": str(exc)},
            )
            return {"status": "blocked", "reason": str(exc)}

    def _record_own_capital_doprinato(
        self,
        record: Mapping[str, Any],
        normalized: Mapping[str, Any],
        *,
        recalculate: bool = True,
    ) -> dict[str, Any]:
        from packages.application.own_product_capital import OwnProductCapitalBlock

        supply_id = str(_first_present(normalized, "supply_id", "wb_supply_id", "preorder_id") or "")
        capital = OwnProductCapitalBlock(runtime=self.runtime, timestamp_factory=self.timestamp_factory)
        if not capital.has_events():
            return {"skip_reason": "wb_supply_doprinato_without_paid_capital", "supply_id": supply_id}
        raw_goods = record.get("raw_goods")
        if not isinstance(raw_goods, list):
            raw_goods = normalized.get("raw_goods")
        sent, accepted, problems = _wb_capital_quantities(raw_goods if isinstance(raw_goods, list) else [])
        quantities = accepted or sent
        if problems or not quantities:
            return {
                "skip_reason": "wb_supply_doprinato_goods_blocked",
                "supply_id": supply_id,
                "problem_rows": problems or ["goods quantities missing"],
            }
        business_dt = _wb_acceptance_business_timestamp(record=record, normalized=normalized)
        if business_dt is None:
            business_dt, _ = _wb_supply_business_timestamp(record=record, normalized=normalized)
        if business_dt is None:
            return {"skip_reason": "wb_supply_doprinato_date_missing", "supply_id": supply_id}
        warehouse = str(_first_present(normalized, "warehouse_name", "warehouseName", "warehouse") or "")
        destination = str(
            _first_present(normalized, "destination_name", "target_warehouse_name", "targetWarehouseName", "warehouse_name")
            or ""
        )
        original_supply_id = str(
            _first_present(
                normalized,
                "original_supply_id",
                "originalSupplyID",
                "parent_supply_id",
                "parentSupplyID",
                "upstream_supply_id",
            )
            or ""
        )
        try:
            result = capital.reconcile_doprinato(
                reconciliation_supply_id=supply_id,
                effective_date=business_dt.date().isoformat(),
                quantities_by_nm=quantities,
                warehouse=warehouse,
                destination=destination,
                original_supply_id=original_supply_id or None,
                recalculate=recalculate,
            )
            capital.resolve_blockers(source_identity=supply_id)
            return {"skip_reason": "wb_supply_doprinato_reconciled_no_ff_writeoff", "supply_id": supply_id, "result": result}
        except ValueError as exc:
            capital._record_blocker(  # noqa: SLF001 - same bounded application contour
                code="wb_doprinato_reconciliation_blocked",
                source_identity=supply_id,
                details={"reason": str(exc)},
            )
            return {"skip_reason": "wb_supply_doprinato_reconciliation_blocked", "supply_id": supply_id, "reason": str(exc)}

    def materialize_own_product_capital_history(
        self,
        *,
        date_to: str,
        recalculate: bool = True,
    ) -> dict[str, Any]:
        """Materialize capital movements only where persisted FF/WB evidence exists."""
        records = self.runtime.list_wb_supplies_cache_records()
        ordinary: list[tuple[datetime, Mapping[str, Any], Mapping[str, Any]]] = []
        doprinato: list[tuple[datetime, Mapping[str, Any], Mapping[str, Any]]] = []
        skipped_without_ledger_evidence = 0
        for record in records:
            normalized = dict(record.get("normalized") or record)
            if _optional_int(normalized.get("status_id")) not in WB_DEBIT_STATUS_IDS:
                continue
            business_dt, _ = _wb_supply_business_timestamp(
                record=record,
                normalized=normalized,
            )
            if business_dt is None or business_dt.date().isoformat() > str(date_to):
                continue
            item = (business_dt, record, normalized)
            if (
                _optional_int(normalized.get("virtual_type_id")) == WB_SKIP_VIRTUAL_TYPE_ID
                or str(normalized.get("type_label") or "").strip() == WB_SKIP_TYPE_LABEL
            ):
                doprinato.append(item)
                continue
            _, _, source_key = _wb_supply_debit_identity(
                record=record,
                normalized=normalized,
            )
            if not source_key or self.runtime.load_ff_stock_operation_by_source_key(source_key) is None:
                skipped_without_ledger_evidence += 1
                continue
            ordinary.append(item)

        diagnostics: list[dict[str, Any]] = []
        materialized = 0
        for _, record, normalized in sorted(ordinary, key=lambda item: item[0]):
            result = self._record_own_capital_wb_supply(
                record,
                normalized,
                recalculate=False,
            )
            materialized += 1
            if str(result.get("status") or "") == "blocked":
                diagnostics.append(
                    {
                        "supply_id": str(normalized.get("supply_id") or ""),
                        "reason": str(result.get("reason") or "blocked"),
                    }
                )
        for _, record, normalized in sorted(doprinato, key=lambda item: item[0]):
            result = self._record_own_capital_doprinato(
                record,
                normalized,
                recalculate=False,
            )
            materialized += 1
            if str(result.get("skip_reason") or "").endswith("_blocked"):
                diagnostics.append(
                    {
                        "supply_id": str(normalized.get("supply_id") or ""),
                        "reason": str(result.get("reason") or result.get("skip_reason") or "blocked"),
                    }
                )
        if recalculate and materialized:
            from packages.application.own_product_capital import OwnProductCapitalBlock

            OwnProductCapitalBlock(
                runtime=self.runtime,
                timestamp_factory=self.timestamp_factory,
            ).recalculate()
        return {
            "status": "blocked" if diagnostics else "ok",
            "persisted_supply_count": materialized,
            "skipped_without_ledger_evidence_count": skipped_without_ledger_evidence,
            "blocker_count": len(diagnostics),
            "blockers": diagnostics,
        }

    def plan_targeted_wb_supply_reconciliation(self, supply_id: str) -> dict[str, Any]:
        """Build the read-only v2 plan for the one checkpoint plus pre-activation incident."""
        requested_supply_id = str(supply_id or "").strip()
        if requested_supply_id != TARGETED_WB_RECONCILIATION_SUPPLY_ID:
            raise TargetedWbSupplyReconciliationError(
                "invalid_supply_id",
                f"Targeted reconciliation is bounded to WB supply_id {TARGETED_WB_RECONCILIATION_SUPPLY_ID}",
            )
        record = self.runtime.load_wb_supply_record(requested_supply_id)
        if record is None:
            raise TargetedWbSupplyReconciliationError(
                "wb_supply_not_found",
                f"WB supply {requested_supply_id} was not found in the server-owned cache",
            )
        normalized = dict(record.get("normalized") or {})
        cache_key, resolved_supply_id, source_key = _wb_supply_debit_identity(record=record, normalized=normalized)
        canonical_cache_key = f"supply:{requested_supply_id}"
        canonical_source_key = f"wb_supply_debit:{canonical_cache_key}"
        blockers: list[dict[str, Any]] = []
        if (
            resolved_supply_id != requested_supply_id
            or str(record.get("supply_id") or "") != requested_supply_id
            or str(record.get("wb_supply_id") or normalized.get("wb_supply_id") or "") != requested_supply_id
        ):
            blockers.append(
                {
                    "code": "wb_supply_identity_mismatch",
                    "expected_supply_id": requested_supply_id,
                    "resolved_supply_id": resolved_supply_id,
                    "cache_supply_id": str(record.get("supply_id") or ""),
                    "wb_supply_id": str(record.get("wb_supply_id") or normalized.get("wb_supply_id") or ""),
                }
            )
        if cache_key != canonical_cache_key or str(normalized.get("cache_key") or "") != canonical_cache_key:
            blockers.append(
                {
                    "code": "wb_supply_cache_key_not_canonical",
                    "expected_cache_key": canonical_cache_key,
                    "actual_cache_key": cache_key,
                    "normalized_cache_key": str(normalized.get("cache_key") or ""),
                }
            )
        if source_key != canonical_source_key:
            blockers.append(
                {
                    "code": "wb_supply_source_key_not_canonical",
                    "expected_source_key": canonical_source_key,
                    "actual_source_key": source_key,
                }
            )

        existing = self.runtime.load_ff_stock_operation_by_source_key(canonical_source_key)
        if existing is not None:
            existing_fingerprint = str((existing.get("diagnostics") or {}).get("dry_run_fingerprint") or "")
            return {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "plan_version": TARGETED_WB_RECONCILIATION_PLAN_VERSION,
                "status": "already_applied",
                "apply_allowed": False,
                "idempotent": True,
                "fingerprint": existing_fingerprint,
                "supply": {
                    "supply_id": requested_supply_id,
                    "cache_key": canonical_cache_key,
                    "source_key": canonical_source_key,
                    "status_id": _optional_int(normalized.get("status_id")),
                    "status_label": str(normalized.get("status_label") or ""),
                },
                "operation": existing,
                "human_summary": f"WB-поставка № {requested_supply_id} уже списана операцией {existing.get('operation_id')}",
            }

        status_id = _optional_int(normalized.get("status_id"))
        if status_id not in WB_DEBIT_STATUS_IDS:
            blockers.append(
                {
                    "code": "wb_supply_status_not_debit_eligible",
                    "status_id": status_id,
                    "allowed_status_ids": sorted(WB_DEBIT_STATUS_IDS),
                }
            )
        if _optional_int(normalized.get("virtual_type_id")) == WB_SKIP_VIRTUAL_TYPE_ID:
            blockers.append({"code": "wb_supply_doprinato_virtual_type", "virtual_type_id": WB_SKIP_VIRTUAL_TYPE_ID})
        if str(normalized.get("type_label") or "").strip() == WB_SKIP_TYPE_LABEL:
            blockers.append({"code": "wb_supply_doprinato_type_label", "type_label": WB_SKIP_TYPE_LABEL})

        raw_goods = record.get("raw_goods")
        operation_lines: list[dict[str, Any]] = []
        goods_warnings: list[str] = []
        nomenclature_by_nm = self._nomenclature_by_nm()
        if not isinstance(raw_goods, list) or not raw_goods:
            blockers.append({"code": "wb_supply_goods_missing"})
            raw_goods = []
        else:
            operation_lines, goods_warnings, goods_errors = _targeted_wb_supply_goods_lines(
                raw_goods,
                nomenclature_by_nm,
            )
            blockers.extend(goods_errors)
            if not operation_lines:
                blockers.append({"code": "wb_supply_goods_without_usable_qty"})

        checkpoint = self.runtime.load_ff_stock_wb_auto_writeoff_checkpoint()
        checkpoint_reason = ""
        checkpoint_match_fields: list[str] = []
        checkpoint_dt = _parse_datetime_like((checkpoint or {}).get("created_at"))
        source_dt, source_dt_field = _wb_supply_business_timestamp(record=record, normalized=normalized)
        if checkpoint is None:
            blockers.append({"code": "wb_supply_auto_writeoff_checkpoint_missing"})
        elif checkpoint_dt is None:
            blockers.append(
                {
                    "code": "wb_supply_auto_writeoff_checkpoint_invalid",
                    "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
                    "created_at": str(checkpoint.get("created_at") or ""),
                }
            )
        elif source_dt is None:
            blockers.append({"code": "wb_supply_source_date_missing"})
        else:
            checkpoint_match_fields = _wb_supply_checkpoint_match_fields(
                checkpoint=checkpoint,
                cache_key=cache_key,
                supply_id=resolved_supply_id,
                source_key=source_key,
            )
            required_checkpoint_match_fields = {"cache_key", "source_key", "supply_id"}
            if set(checkpoint_match_fields) == required_checkpoint_match_fields:
                checkpoint_reason = "wb_supply_before_auto_writeoff_checkpoint"
            else:
                blockers.append(
                    {
                        "code": "targeted_checkpoint_baseline_match_required",
                        "required_match_fields": sorted(required_checkpoint_match_fields),
                        "actual_match_fields": checkpoint_match_fields,
                    }
                )

        activation = self.runtime.load_ff_stock_activation_operation()
        activation_operation_id = str((activation or {}).get("operation_id") or "")
        activation_dt = _parse_datetime_like((activation or {}).get("created_at"))
        activation_reason = ""
        if activation is None:
            blockers.append({"code": "wb_supply_ledger_not_activated"})
        elif not activation_operation_id or activation_dt is None:
            blockers.append(
                {
                    "code": "wb_supply_ledger_activation_invalid",
                    "operation_id": activation_operation_id,
                    "created_at": str(activation.get("created_at") or ""),
                }
            )
        elif source_dt is not None and source_dt < activation_dt:
            activation_reason = "wb_supply_before_ledger_activation"
        elif source_dt is not None:
            blockers.append(
                {
                    "code": "targeted_pre_activation_remediation_not_applicable",
                    "source_timestamp": source_dt.isoformat(),
                    "activation_created_at": activation_dt.isoformat(),
                }
            )

        balance_rows = self.runtime.list_ff_stock_balances()
        balances_by_nm = {int(item.get("nm_id") or 0): float(item.get("balance") or 0.0) for item in balance_rows}
        sku_rows: list[dict[str, Any]] = []
        shortages: list[dict[str, Any]] = []
        for line in sorted(operation_lines, key=lambda item: int(item.get("nm_id") or 0)):
            nm_id = int(line.get("nm_id") or 0)
            debit_quantity = abs(float(line.get("quantity_delta") or 0.0))
            current_balance = float(balances_by_nm.get(nm_id, 0.0))
            projected_balance = current_balance - debit_quantity
            sku_row = {
                "nm_id": nm_id,
                "nmID": nm_id,
                "barcode": str(line.get("barcode") or ""),
                "sku": str(line.get("sku") or ""),
                "nomenclature_name": str(line.get("nomenclature_name") or ""),
                "current_balance": current_balance,
                "debit_quantity": debit_quantity,
                "projected_balance": projected_balance,
                "expected_balance": projected_balance,
            }
            sku_rows.append(sku_row)
            if projected_balance < -1e-9:
                shortages.append(
                    {
                        "nm_id": nm_id,
                        "nmID": nm_id,
                        "current_balance": current_balance,
                        "required_debit": debit_quantity,
                        "projected_balance": projected_balance,
                        "expected_balance": projected_balance,
                    }
                )
        if shortages:
            blockers.append({"code": "wb_supply_would_make_negative_balance", "skus": shortages})

        total_before = sum(float(item.get("balance") or 0.0) for item in balance_rows)
        total_debit = sum(float(item.get("debit_quantity") or 0.0) for item in sku_rows)
        total_after = total_before - total_debit
        if len(sku_rows) != TARGETED_WB_RECONCILIATION_EXPECTED_SKU_COUNT:
            blockers.append(
                {
                    "code": "target_supply_sku_count_changed",
                    "expected": TARGETED_WB_RECONCILIATION_EXPECTED_SKU_COUNT,
                    "actual": len(sku_rows),
                }
            )
        if abs(total_debit - TARGETED_WB_RECONCILIATION_EXPECTED_DEBIT) > 1e-9:
            blockers.append(
                {
                    "code": "target_supply_debit_quantity_changed",
                    "expected": TARGETED_WB_RECONCILIATION_EXPECTED_DEBIT,
                    "actual": total_debit,
                }
            )
        if abs(total_before - TARGETED_WB_RECONCILIATION_EXPECTED_TOTAL_BEFORE) > 1e-9:
            blockers.append(
                {
                    "code": "target_ff_stock_total_before_changed",
                    "expected": TARGETED_WB_RECONCILIATION_EXPECTED_TOTAL_BEFORE,
                    "actual": total_before,
                }
            )
        if abs(total_after - TARGETED_WB_RECONCILIATION_EXPECTED_TOTAL_AFTER) > 1e-9:
            blockers.append(
                {
                    "code": "target_ff_stock_total_after_changed",
                    "expected": TARGETED_WB_RECONCILIATION_EXPECTED_TOTAL_AFTER,
                    "actual": total_after,
                }
            )
        supply_guard = _targeted_supply_guard(record=record, normalized=normalized)
        expected_balances = {str(item["nm_id"]): item["current_balance"] for item in sku_rows}
        active_nomenclature_guard = {
            str(item["nm_id"]): _targeted_nomenclature_guard(nomenclature_by_nm[int(item["nm_id"])])
            for item in sku_rows
            if int(item["nm_id"]) in nomenclature_by_nm
        }
        checkpoint_guard = {
            "checkpoint_id": str((checkpoint or {}).get("checkpoint_id") or ""),
            "created_at": str((checkpoint or {}).get("created_at") or ""),
            "baseline_cache_keys": list((checkpoint or {}).get("baseline_cache_keys") or []),
            "baseline_source_keys": list((checkpoint or {}).get("baseline_source_keys") or []),
            "baseline_supply_ids": list((checkpoint or {}).get("baseline_supply_ids") or []),
        }
        activation_guard = {
            "operation_id": activation_operation_id,
            "created_at": str((activation or {}).get("created_at") or ""),
        }
        bypassed_ordinary_blockers = [
            reason
            for reason in TARGETED_WB_RECONCILIATION_ORDINARY_BLOCKERS
            if reason in {checkpoint_reason, activation_reason}
        ]
        if bypassed_ordinary_blockers != list(TARGETED_WB_RECONCILIATION_ORDINARY_BLOCKERS):
            blockers.append(
                {
                    "code": "targeted_required_ordinary_blockers_not_matched",
                    "expected": list(TARGETED_WB_RECONCILIATION_ORDINARY_BLOCKERS),
                    "actual": bypassed_ordinary_blockers,
                }
            )
        fingerprint_payload = {
            "plan_version": TARGETED_WB_RECONCILIATION_PLAN_VERSION,
            "reason": TARGETED_WB_RECONCILIATION_REASON,
            "supply_guard": supply_guard,
            "source_key": canonical_source_key,
            "checkpoint_guard": checkpoint_guard,
            "checkpoint_reason": checkpoint_reason,
            "checkpoint_match_fields": checkpoint_match_fields,
            "activation_guard": activation_guard,
            "activation_reason": activation_reason,
            "bypassed_ordinary_blockers": bypassed_ordinary_blockers,
            "active_nomenclature_guard": active_nomenclature_guard,
            "expected_balances": expected_balances,
            "expected_ledger_totals": {
                "before": total_before,
                "delta": -total_debit,
                "after": total_after,
            },
            "operation_lines": operation_lines,
        }
        fingerprint = _stable_reconciliation_fingerprint(fingerprint_payload)
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "plan_version": TARGETED_WB_RECONCILIATION_PLAN_VERSION,
            "status": "dry_run" if not blockers else "blocked",
            "apply_allowed": not blockers,
            "fingerprint": fingerprint,
            "reason": TARGETED_WB_RECONCILIATION_REASON,
            "supply": {
                "supply_id": requested_supply_id,
                "cache_key": canonical_cache_key,
                "source_key": canonical_source_key,
                "preorder_id": str(record.get("preorder_id") or normalized.get("preorder_id") or ""),
                "status_id": status_id,
                "status_label": str(normalized.get("status_label") or ""),
                "source_created_at": str(normalized.get("source_created_at") or ""),
                "supply_date": str(normalized.get("supply_date") or ""),
                "source_timestamp": source_dt.isoformat() if source_dt is not None else "",
                "source_timestamp_field": source_dt_field,
                "sku_count": len(sku_rows),
            },
            "checkpoint": {
                **checkpoint_guard,
                "ordinary_path_reason": checkpoint_reason,
                "match_fields": checkpoint_match_fields,
                "bypass_scope": "only_supply_40561872_checkpoint_and_pre_activation",
            },
            "ledger_activation": {
                **activation_guard,
                "ordinary_path_reason": activation_reason,
            },
            "bypassed_ordinary_blockers": bypassed_ordinary_blockers,
            "skus": sku_rows,
            "totals": {
                "before": total_before,
                "debit": total_debit,
                "after": total_after,
            },
            "warnings": goods_warnings,
            "blockers": blockers,
            "apply_guards": fingerprint_payload,
            "human_summary": (
                f"WB-поставка № {requested_supply_id}: {len(sku_rows)} SKU, "
                f"списание {total_debit:g}, остаток {total_before:g} -> {total_after:g}"
            ),
        }

    def apply_targeted_wb_supply_reconciliation(
        self,
        supply_id: str,
        *,
        apply: bool,
        confirmation_fingerprint: str,
        created_by: str,
    ) -> dict[str, Any]:
        requested_supply_id = str(supply_id or "").strip()
        if not apply:
            raise TargetedWbSupplyReconciliationError(
                "explicit_apply_required",
                "Targeted reconciliation apply requires an explicit apply flag",
            )
        if requested_supply_id != TARGETED_WB_RECONCILIATION_SUPPLY_ID:
            raise TargetedWbSupplyReconciliationError(
                "invalid_supply_id",
                f"Targeted reconciliation is bounded to WB supply_id {TARGETED_WB_RECONCILIATION_SUPPLY_ID}",
            )
        canonical_source_key = f"wb_supply_debit:supply:{requested_supply_id}"
        existing = self.runtime.load_ff_stock_operation_by_source_key(canonical_source_key)
        if existing is not None:
            existing_fingerprint = str((existing.get("diagnostics") or {}).get("dry_run_fingerprint") or "")
            if not confirmation_fingerprint or confirmation_fingerprint != existing_fingerprint:
                raise TargetedWbSupplyReconciliationError(
                    "stale_or_invalid_fingerprint",
                    "Confirmation fingerprint does not match the existing targeted operation",
                    details={"expected": existing_fingerprint, "provided": str(confirmation_fingerprint or "")},
                )
            existing["idempotent"] = True
            return {
                "status": "already_applied",
                "idempotent": True,
                "operation": existing,
                "post_run_reconciliation": self._targeted_post_run_reconciliation(existing),
            }
        plan = self.plan_targeted_wb_supply_reconciliation(requested_supply_id)
        actual_fingerprint = str(plan.get("fingerprint") or "")
        if not confirmation_fingerprint or confirmation_fingerprint != actual_fingerprint:
            raise TargetedWbSupplyReconciliationError(
                "stale_or_invalid_fingerprint",
                "Dry-run fingerprint is stale or does not match the current plan",
                details={"expected": actual_fingerprint, "provided": str(confirmation_fingerprint or "")},
            )
        if not plan.get("apply_allowed"):
            raise TargetedWbSupplyReconciliationError(
                "targeted_reconciliation_blocked",
                "Current targeted reconciliation plan is blocked",
                details=plan.get("blockers") or [],
            )
        guards = dict(plan.get("apply_guards") or {})
        operation = self.runtime.create_ff_stock_operation_guarded(
            operation_id="ffso_" + uuid4().hex[:20],
            operation_type=FF_STOCK_OPERATION_AUTO_WRITEOFF,
            source_type=FF_STOCK_SOURCE_WB_SUPPLY,
            source_key=canonical_source_key,
            source_object_id=requested_supply_id,
            source_object_label=f"WB-поставка № {requested_supply_id} · targeted pre-activation remediation",
            created_at=self.timestamp_factory(),
            created_by=str(created_by or "operator").strip() or "operator",
            warnings=list(plan.get("warnings") or []),
            diagnostics={
                "reason": TARGETED_WB_RECONCILIATION_REASON,
                "plan_version": TARGETED_WB_RECONCILIATION_PLAN_VERSION,
                "remediation": "targeted_pre_activation_remediation",
                "supply_id": requested_supply_id,
                "cache_key": f"supply:{requested_supply_id}",
                "source_key": canonical_source_key,
                "dry_run_fingerprint": actual_fingerprint,
                "supply_timestamp": str((plan.get("supply") or {}).get("source_timestamp") or ""),
                "activation_operation_id": str((plan.get("ledger_activation") or {}).get("operation_id") or ""),
                "activation_created_at": str((plan.get("ledger_activation") or {}).get("created_at") or ""),
                "bypassed_ordinary_blockers": list(plan.get("bypassed_ordinary_blockers") or []),
                "checkpoint": dict(plan.get("checkpoint") or {}),
                "totals": dict(plan.get("totals") or {}),
            },
            lines=[dict(item) for item in guards.get("operation_lines") or []],
            expected_balances={int(key): float(value) for key, value in dict(guards.get("expected_balances") or {}).items()},
            expected_supply_guard=dict(guards.get("supply_guard") or {}),
            expected_checkpoint=dict(guards.get("checkpoint_guard") or {}),
            expected_activation=dict(guards.get("activation_guard") or {}),
            expected_active_nomenclature={
                int(key): dict(value)
                for key, value in dict(guards.get("active_nomenclature_guard") or {}).items()
            },
            expected_ledger_totals=dict(guards.get("expected_ledger_totals") or {}),
        )
        return {
            "status": "applied" if not operation.get("idempotent") else "already_applied",
            "idempotent": bool(operation.get("idempotent")),
            "fingerprint": actual_fingerprint,
            "operation": operation,
            "post_run_reconciliation": self._targeted_post_run_reconciliation(operation),
        }

    def plan_targeted_wb_supply_reversal(self, supply_id: str) -> dict[str, Any]:
        requested_supply_id = str(supply_id or "").strip()
        if requested_supply_id != TARGETED_WB_RECONCILIATION_SUPPLY_ID:
            raise TargetedWbSupplyReconciliationError(
                "invalid_supply_id",
                f"Targeted reconciliation is bounded to WB supply_id {TARGETED_WB_RECONCILIATION_SUPPLY_ID}",
            )
        canonical_source_key = f"wb_supply_debit:supply:{requested_supply_id}"
        original = self.runtime.load_ff_stock_operation_by_source_key(canonical_source_key)
        if original is None:
            raise TargetedWbSupplyReconciliationError(
                "targeted_operation_not_found",
                f"Targeted WB debit for supply {requested_supply_id} was not found",
            )
        diagnostics = dict(original.get("diagnostics") or {})
        if (
            str(original.get("operation_type") or "") != FF_STOCK_OPERATION_AUTO_WRITEOFF
            or str(original.get("source_type") or "") != FF_STOCK_SOURCE_WB_SUPPLY
            or str(original.get("source_object_id") or "") != requested_supply_id
            or str(diagnostics.get("reason") or "") != TARGETED_WB_RECONCILIATION_REASON
        ):
            raise TargetedWbSupplyReconciliationError(
                "operation_not_targeted_reconciliation",
                "Only a targeted pre-activation remediation operation can be reversed by this path",
            )
        reversal_source_key = f"wb_supply_debit_reversal:supply:{requested_supply_id}"
        existing = self.runtime.load_ff_stock_operation_by_source_key(reversal_source_key)
        if existing is not None:
            existing_fingerprint = str((existing.get("diagnostics") or {}).get("dry_run_fingerprint") or "")
            existing["idempotent"] = True
            return {
                "status": "already_reversed",
                "apply_allowed": False,
                "idempotent": True,
                "fingerprint": existing_fingerprint,
                "operation": existing,
            }
        original_with_lines = self.runtime.load_ff_stock_operation(str(original.get("operation_id") or "")) or {}
        original_lines = [dict(item) for item in original_with_lines.get("lines") or []]
        if not original_lines:
            raise TargetedWbSupplyReconciliationError("targeted_operation_lines_missing", "Original targeted debit has no lines")
        reversal_lines = [
            {
                **line,
                "quantity_delta": abs(float(line.get("quantity_delta") or 0.0)),
                "raw": {"compensates_operation_id": str(original.get("operation_id") or "")},
            }
            for line in original_lines
        ]
        balance_rows = self.runtime.list_ff_stock_balances()
        balances = {int(item.get("nm_id") or 0): float(item.get("balance") or 0.0) for item in balance_rows}
        expected_balances = {str(int(line["nm_id"])): balances.get(int(line["nm_id"]), 0.0) for line in reversal_lines}
        total_before = sum(balances.values())
        fingerprint_payload = {
            "plan_version": TARGETED_WB_RECONCILIATION_PLAN_VERSION,
            "reason": "targeted_pre_activation_remediation_reversal",
            "supply_id": requested_supply_id,
            "original_operation_id": str(original.get("operation_id") or ""),
            "original_source_key": canonical_source_key,
            "reversal_source_key": reversal_source_key,
            "expected_balances": expected_balances,
            "expected_ledger_totals": {
                "before": total_before,
                "delta": sum(float(item.get("quantity_delta") or 0.0) for item in reversal_lines),
                "after": total_before + sum(float(item.get("quantity_delta") or 0.0) for item in reversal_lines),
            },
            "operation_lines": reversal_lines,
        }
        fingerprint = _stable_reconciliation_fingerprint(fingerprint_payload)
        total_receipt = sum(float(item.get("quantity_delta") or 0.0) for item in reversal_lines)
        return {
            "status": "reversal_dry_run",
            "apply_allowed": True,
            "fingerprint": fingerprint,
            "supply_id": requested_supply_id,
            "original_operation_id": str(original.get("operation_id") or ""),
            "original_source_key": canonical_source_key,
            "reversal_source_key": reversal_source_key,
            "totals": {"before": total_before, "receipt": total_receipt, "after": total_before + total_receipt},
            "apply_guards": fingerprint_payload,
            "human_summary": (
                f"Компенсация WB-поставки № {requested_supply_id}: +{total_receipt:g}; "
                f"исходная операция {original.get('operation_id')} сохраняется"
            ),
        }

    def apply_targeted_wb_supply_reversal(
        self,
        supply_id: str,
        *,
        apply: bool,
        confirmation_fingerprint: str,
        created_by: str,
    ) -> dict[str, Any]:
        if not apply:
            raise TargetedWbSupplyReconciliationError(
                "explicit_apply_required",
                "Targeted reconciliation reversal requires an explicit apply flag",
            )
        requested_supply_id = str(supply_id or "").strip()
        if requested_supply_id != TARGETED_WB_RECONCILIATION_SUPPLY_ID:
            raise TargetedWbSupplyReconciliationError(
                "invalid_supply_id",
                f"Targeted reconciliation is bounded to WB supply_id {TARGETED_WB_RECONCILIATION_SUPPLY_ID}",
            )
        reversal_source_key = f"wb_supply_debit_reversal:supply:{requested_supply_id}"
        existing = self.runtime.load_ff_stock_operation_by_source_key(reversal_source_key)
        if existing is not None:
            existing_fingerprint = str((existing.get("diagnostics") or {}).get("dry_run_fingerprint") or "")
            if not confirmation_fingerprint or confirmation_fingerprint != existing_fingerprint:
                raise TargetedWbSupplyReconciliationError(
                    "stale_or_invalid_fingerprint",
                    "Confirmation fingerprint does not match the existing reversal operation",
                    details={"expected": existing_fingerprint, "provided": str(confirmation_fingerprint or "")},
                )
            existing["idempotent"] = True
            return {
                "status": "already_reversed",
                "idempotent": True,
                "operation": existing,
                "post_run_reconciliation": self._targeted_post_run_reconciliation(existing),
            }
        plan = self.plan_targeted_wb_supply_reversal(requested_supply_id)
        actual_fingerprint = str(plan.get("fingerprint") or "")
        if not confirmation_fingerprint or confirmation_fingerprint != actual_fingerprint:
            raise TargetedWbSupplyReconciliationError(
                "stale_or_invalid_fingerprint",
                "Reversal dry-run fingerprint is stale or does not match the current plan",
                details={"expected": actual_fingerprint, "provided": str(confirmation_fingerprint or "")},
            )
        guards = dict(plan.get("apply_guards") or {})
        operation = self.runtime.create_ff_stock_operation_guarded(
            operation_id="ffso_" + uuid4().hex[:20],
            operation_type=FF_STOCK_OPERATION_CORRECTION_RECEIPT,
            source_type=FF_STOCK_SOURCE_TARGETED_RECONCILIATION,
            source_key=reversal_source_key,
            source_object_id=str(guards.get("original_operation_id") or ""),
            source_object_label=f"Компенсация targeted WB-поставки № {requested_supply_id}",
            created_at=self.timestamp_factory(),
            created_by=str(created_by or "operator").strip() or "operator",
            warnings=[],
            diagnostics={
                "reason": "targeted_pre_activation_remediation_reversal",
                "supply_id": requested_supply_id,
                "compensates_operation_id": str(guards.get("original_operation_id") or ""),
                "compensates_source_key": str(guards.get("original_source_key") or ""),
                "dry_run_fingerprint": actual_fingerprint,
                "history_preserved": True,
            },
            lines=[dict(item) for item in guards.get("operation_lines") or []],
            expected_balances={int(key): float(value) for key, value in dict(guards.get("expected_balances") or {}).items()},
            expected_ledger_totals=dict(guards.get("expected_ledger_totals") or {}),
        )
        return {
            "status": "reversed" if not operation.get("idempotent") else "already_reversed",
            "idempotent": bool(operation.get("idempotent")),
            "fingerprint": actual_fingerprint,
            "operation": operation,
            "original_operation_id": str(guards.get("original_operation_id") or ""),
            "post_run_reconciliation": self._targeted_post_run_reconciliation(operation),
        }

    def _targeted_post_run_reconciliation(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        operation_with_lines = self.runtime.load_ff_stock_operation(str(operation.get("operation_id") or "")) or {}
        nm_ids = sorted({int(item.get("nm_id") or 0) for item in operation_with_lines.get("lines") or []})
        all_balances = self.runtime.list_ff_stock_balances()
        balances = {int(item.get("nm_id") or 0): float(item.get("balance") or 0.0) for item in all_balances}
        return {
            "operation_id": str(operation.get("operation_id") or ""),
            "source_key": str(operation.get("source_key") or ""),
            "affected_skus": [{"nm_id": nm_id, "current_balance": balances.get(nm_id, 0.0)} for nm_id in nm_ids],
            "ledger_total_after": sum(balances.values()),
            "negative_affected_skus": [nm_id for nm_id in nm_ids if balances.get(nm_id, 0.0) < -1e-9],
        }

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
        if not nomenclature:
            warnings.append(f"WB goods row {index}: nmID {nm_id} отсутствует в authoritative nomenclature")
            continue
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


def _targeted_wb_supply_goods_lines(
    raw_goods: list[Any],
    nomenclature_by_nm: Mapping[int, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    grouped: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_goods, start=1):
        if not isinstance(raw, Mapping):
            errors.append({"code": "wb_supply_goods_row_invalid", "row": index})
            continue
        item = dict(raw)
        nm_id = _optional_int(_first_present(item, "nmID", "nmId", "nm_id"))
        quantity = _optional_float(_first_present(item, "quantity", "qty"))
        if nm_id is None:
            errors.append({"code": "wb_supply_goods_nm_id_missing", "row": index})
            continue
        if quantity is None or quantity <= 0:
            errors.append(
                {
                    "code": "wb_supply_goods_quantity_not_positive",
                    "row": index,
                    "nm_id": nm_id,
                    "quantity": quantity,
                }
            )
            continue
        nomenclature = nomenclature_by_nm.get(nm_id)
        if nomenclature is None:
            errors.append({"code": "wb_supply_goods_nm_id_not_in_active_nomenclature", "row": index, "nm_id": nm_id})
            continue
        target = grouped.setdefault(
            nm_id,
            {
                **_nomenclature_public_fields(nomenclature),
                "nm_id": nm_id,
                "barcode": str(_first_present(item, "barcode", "barCode", "barcodeID") or nomenclature.get("barcode") or ""),
                "quantity_delta": 0.0,
                "raw": {"source": "wb_supplies_cache.raw_goods", "source_rows": []},
            },
        )
        target["quantity_delta"] -= float(quantity)
        target["raw"]["source_rows"].append(index)
    if len(grouped) < len(raw_goods) and not errors:
        warnings.append("Некоторые WB goods rows объединены по nmId")
    return [grouped[nm_id] for nm_id in sorted(grouped)], warnings, errors


def _targeted_supply_guard(*, record: Mapping[str, Any], normalized: Mapping[str, Any]) -> dict[str, Any]:
    raw_goods = record.get("raw_goods") if isinstance(record.get("raw_goods"), list) else None
    return {
        "supply_id": str(record.get("supply_id") or ""),
        "cache_key": str(record.get("cache_key") or normalized.get("cache_key") or ""),
        "wb_supply_id": str(record.get("wb_supply_id") or normalized.get("wb_supply_id") or ""),
        "preorder_id": str(record.get("preorder_id") or normalized.get("preorder_id") or ""),
        "normalized_supply_id": str(normalized.get("supply_id") or ""),
        "normalized_cache_key": str(normalized.get("cache_key") or ""),
        "status_id": int(_optional_int(normalized.get("status_id")) or 0),
        "virtual_type_id": normalized.get("virtual_type_id"),
        "type_label": str(normalized.get("type_label") or ""),
        "source_created_at": str(normalized.get("source_created_at") or ""),
        "supply_date": str(normalized.get("supply_date") or ""),
        "raw_goods": raw_goods,
        "raw_goods_hash": str(record.get("raw_goods_hash") or normalized.get("raw_goods_hash") or ""),
    }


def _targeted_nomenclature_guard(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(item.get("item_id") or ""),
        "nm_id": int(_optional_int(item.get("nm_id")) or 0),
        "is_active": bool(item.get("is_active")),
        "is_hidden": bool(item.get("is_hidden")),
        "updated_at": str(item.get("updated_at") or ""),
    }


def _stable_reconciliation_fingerprint(value: Mapping[str, Any]) -> str:
    body = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _wb_capital_quantities(
    raw_goods: list[Mapping[str, Any]],
) -> tuple[dict[int, float], dict[int, float], list[str]]:
    sent: dict[int, float] = {}
    accepted: dict[int, float] = {}
    problems: list[str] = []
    for index, raw in enumerate(raw_goods, start=1):
        item = dict(raw or {})
        nm_id = _optional_int(_first_present(item, "nmID", "nmId", "nm_id"))
        sent_qty = _optional_float(_first_present(item, "quantity", "qty", "sentQuantity", "sent_quantity"))
        accepted_raw = _first_present(item, "acceptedQuantity", "accepted_quantity", "acceptedQty")
        accepted_qty = _optional_float(accepted_raw)
        if nm_id is None:
            problems.append(f"WB goods row {index}: nmID missing")
            continue
        if sent_qty is None or sent_qty <= 0:
            problems.append(f"WB goods row {index}: sent quantity missing")
            continue
        sent[nm_id] = sent.get(nm_id, 0.0) + sent_qty
        if accepted_raw not in {None, ""}:
            if accepted_qty is None or accepted_qty < 0:
                problems.append(f"WB goods row {index}: accepted quantity invalid")
                continue
            accepted[nm_id] = accepted.get(nm_id, 0.0) + accepted_qty
    return sent, accepted, problems


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


def _wb_supply_debit_identity(
    *,
    record: Mapping[str, Any],
    normalized: Mapping[str, Any],
) -> tuple[str, str, str]:
    supply_id = str(
        _first_present(normalized, "supply_id", "wb_supply_id", "preorder_id")
        or _first_present(record, "supply_id", "wb_supply_id", "preorder_id")
        or ""
    ).strip()
    cache_key = str(
        _first_present(normalized, "cache_key")
        or _first_present(record, "cache_key")
        or supply_id
        or ""
    ).strip()
    identity_key = cache_key or supply_id
    source_key = f"wb_supply_debit:{identity_key}" if identity_key else ""
    return cache_key, supply_id, source_key


def _wb_supply_checkpoint_match_fields(
    *,
    checkpoint: Mapping[str, Any],
    cache_key: str,
    supply_id: str,
    source_key: str,
) -> list[str]:
    match_fields: list[str] = []
    if source_key and source_key in {str(item) for item in checkpoint.get("baseline_source_keys") or []}:
        match_fields.append("source_key")
    if cache_key and cache_key in {str(item) for item in checkpoint.get("baseline_cache_keys") or []}:
        match_fields.append("cache_key")
    if supply_id and supply_id in {str(item) for item in checkpoint.get("baseline_supply_ids") or []}:
        match_fields.append("supply_id")
    return match_fields


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


def _wb_acceptance_business_timestamp(
    *,
    record: Mapping[str, Any],
    normalized: Mapping[str, Any],
) -> datetime | None:
    sources: list[Mapping[str, Any]] = [normalized, record]
    for raw_key in ("raw_detail", "raw_list"):
        raw = record.get(raw_key)
        if isinstance(raw, Mapping):
            sources.append(raw)
    for source in sources:
        for field in (
            "actual_acceptance_date",
            "actualAcceptanceDate",
            "accepted_date",
            "acceptedDate",
            "fact_date",
            "factDate",
        ):
            if field in source:
                parsed = _parse_datetime_like(source.get(field))
                if parsed is not None:
                    return parsed
    return None


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


def _format_utc_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _normalize_operation_page_size(value: Any) -> int:
    try:
        normalized = int(float(str(value or FF_STOCK_OPERATION_DEFAULT_PAGE_SIZE).strip()))
    except (TypeError, ValueError):
        return FF_STOCK_OPERATION_DEFAULT_PAGE_SIZE
    return normalized if normalized in FF_STOCK_OPERATION_PAGE_SIZES else FF_STOCK_OPERATION_DEFAULT_PAGE_SIZE


def _normalize_operation_page(value: Any) -> int:
    try:
        normalized = int(float(str(value or 1).strip()))
    except (TypeError, ValueError):
        return 1
    return max(1, normalized)


def _normalize_operation_offset(value: Any) -> int:
    try:
        normalized = int(float(str(value or 0).strip()))
    except (TypeError, ValueError):
        return 0
    return max(0, normalized)


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
