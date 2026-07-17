"""Unified warehouse balances and guarded opening-snapshot cutover."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from packages.adapters.stocks_block import HttpBackedStocksSource
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.ff_stock_ledger import FfStockLedgerBlock
from packages.application.stocks_block import StocksBlock, transform_legacy_payload
from packages.application.supplier_shipment_status import resolve_supplier_shipment_status
from packages.business_time import business_date_iso
from packages.contracts.cny_ledger import (
    CNY_DOCUMENT_STATUS_POSTED,
    CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT,
)
from packages.contracts.stocks_block import StocksRequest
from packages.contracts.supplier_shipments import (
    MATCH_STATUSES_WITH_AUTHORITATIVE_NM_ID,
    ORDER_STATUS_ACCEPTED_FF,
    ORDER_STATUS_IN_TRANSIT,
    ORDER_STATUS_PRODUCTION,
)


CONTRACT_NAME = "sheet_vitrina_v1_warehouses"
CONTRACT_VERSION = "v1"
OPENING_CUTOVER_ID = "warehouse_opening_v1"
OPENING_DOCUMENT_TYPE = "opening_balance"
OPENING_DOCUMENT_TYPE_LABEL = "Ввод начальных остатков"
OPENING_STATUS = "quantity_fixed_cost_unset"
OPENING_STATUS_LABEL = "Количество зафиксировано, стоимость не задана"
WAREHOUSE_API_SOURCE = "WB Seller Analytics Stocks API / api/analytics/v1/stocks-report/wb-warehouses"

WAREHOUSES: tuple[dict[str, Any], ...] = (
    {
        "key": "production",
        "name": "На производстве",
        "document_id": "whdoc_opening_v1_production",
        "document_number": "ВНО-000001",
        "source": "Проведённые платежи поставщику CNY + полный состав инвойсов",
    },
    {
        "key": "china_to_ff",
        "name": "В пути: Китай → FF",
        "document_id": "whdoc_opening_v1_china_to_ff",
        "document_number": "ВНО-000002",
        "source": "Реестр поставок поставщика: факт отгрузки без приёмки на FF",
    },
    {
        "key": "ff",
        "name": "Склад FF",
        "document_id": "whdoc_opening_v1_ff",
        "document_number": "ВНО-000003",
        "source": "Канонический append-only ledger остатков FF",
    },
    {
        "key": "ff_to_wb",
        "name": "В пути: FF → WB",
        "document_id": "whdoc_opening_v1_ff_to_wb",
        "document_number": "ВНО-000004",
        "source": "WB API / FBW Supplies goods composition; post-gate статусы 3/4/6 до финальной приёмки",
    },
    {
        "key": "wb",
        "name": "Склад WB",
        "document_id": "whdoc_opening_v1_wb",
        "document_number": "ВНО-000005",
        "source": WAREHOUSE_API_SOURCE,
    },
    {
        "key": "wb_acceptance_discrepancy",
        "name": "Расхождения приёмки WB",
        "document_id": "whdoc_opening_v1_wb_acceptance_discrepancy",
        "document_number": "ВНО-000006",
        "source": "Финально принятые WB-поставки: отправлено − принято − доприёмки, по SKU",
    },
)
WAREHOUSE_BY_KEY = {str(item["key"]): item for item in WAREHOUSES}
WB_POST_SHIPMENT_GATE_STATUS_IDS = {3, 4, 6}
WB_FINAL_ACCEPTED_STATUS_ID = 5
INACTIVE_SUPPLIER_STATUSES = {"cancelled", "canceled", "inactive", "deleted", "archived"}


class WarehouseOpeningSnapshotError(ValueError):
    """Fail-closed source, plan or cutover invariant error."""


class WarehouseStocksBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        stocks_block: StocksBlock | None = None,
        now_factory: Callable[[], datetime] | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        wb_nomenclature_provider: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
        ff_stock_ledger_block: FfStockLedgerBlock | None = None,
    ) -> None:
        self.runtime = runtime
        self.stocks_block = stocks_block or StocksBlock(HttpBackedStocksSource())
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.timestamp_factory = timestamp_factory or _utc_now_iso
        self.wb_nomenclature_provider = wb_nomenclature_provider
        self.ff_stock_ledger_block = ff_stock_ledger_block or FfStockLedgerBlock(
            runtime=runtime,
            timestamp_factory=self.timestamp_factory,
        )

    def overview(self) -> dict[str, Any]:
        self._ensure_source_schema()
        with _connect(self.runtime.db_path) as conn:
            _ensure_warehouse_schema(conn)
            cutover, documents = _load_validated_opening_state(conn)
        by_key = {str(item.get("warehouse_key") or ""): item for item in documents}
        summaries = [
            _warehouse_summary(definition, by_key.get(str(definition["key"])), cutover=cutover)
            for definition in WAREHOUSES
        ]
        if cutover and by_key.get("ff"):
            _, ff_summary = self._current_ff_balance_projection(
                document=by_key["ff"],
                cutover=cutover,
            )
            summaries = [
                ff_summary if item.get("warehouse_key") == "ff" else item
                for item in summaries
            ]
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready" if cutover else "not_initialized",
            "cutover": cutover,
            "warehouses": summaries,
        }

    def warehouse_detail(self, warehouse_key: str) -> dict[str, Any]:
        normalized_key = str(warehouse_key or "").strip()
        definition = WAREHOUSE_BY_KEY.get(normalized_key)
        if definition is None:
            raise WarehouseOpeningSnapshotError(f"unknown warehouse: {normalized_key}")
        self._ensure_source_schema()
        with _connect(self.runtime.db_path) as conn:
            _ensure_warehouse_schema(conn)
            cutover, documents = _load_validated_opening_state(conn)
            document = next(
                (item for item in documents if item.get("warehouse_key") == normalized_key),
                None,
            )
        summary = _warehouse_summary(definition, document, cutover=cutover)
        rows = list((document or {}).get("lines") or [])
        if normalized_key == "ff" and document and cutover:
            rows, summary = self._current_ff_balance_projection(
                document=document,
                cutover=cutover,
            )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready" if document else "not_initialized",
            "cutover": cutover,
            "warehouse": summary,
            "balances": rows,
            "documents": [document] if document else [],
            "legacy_ff_route": "/v1/sheet-vitrina-v1/supply/ff-stocks" if normalized_key == "ff" else None,
        }

    def _current_ff_balance_projection(
        self,
        *,
        document: Mapping[str, Any],
        cutover: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Project the live FF ledger without turning its opening document into mutable state."""

        canonical_rows = self.ff_stock_ledger_block.current_balance_rows()
        with _connect(self.runtime.db_path) as conn:
            operation_rows = _query_dicts(
                conn,
                """SELECT operation_id, operation_type, source_type,
                          source_object_id, source_object_label, created_at
                   FROM sheet_vitrina_v1_ff_stock_operations
                   ORDER BY created_at, operation_id""",
            )
            line_rows = _query_dicts(
                conn,
                """SELECT operation_id, line_no, nm_id, quantity_delta
                   FROM sheet_vitrina_v1_ff_stock_operation_lines
                   ORDER BY operation_id, line_no""",
            )
        operations = {
            str(item.get("operation_id") or ""): item
            for item in operation_rows
        }
        canonical_member_nm_ids = {
            int(item["nm_id"])
            for item in canonical_rows
            if _positive_int_or_none(item.get("nm_id")) is not None
        }
        quantities: defaultdict[int, Decimal] = defaultdict(Decimal)
        provenance: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
        for line in line_rows:
            nm_id = _positive_int_or_none(line.get("nm_id"))
            if nm_id is None or nm_id not in canonical_member_nm_ids:
                continue
            quantity = _decimal(line.get("quantity_delta"))
            if quantity == 0:
                continue
            operation = operations.get(str(line.get("operation_id") or ""), {})
            quantities[nm_id] += quantity
            provenance[nm_id].append(
                {
                    "operation_id": str(line.get("operation_id") or ""),
                    "line_no": int(line.get("line_no") or 0),
                    "quantity_delta": _decimal_text(quantity),
                    "operation_type": str(operation.get("operation_type") or ""),
                    "operation_source_type": str(operation.get("source_type") or ""),
                    "source_object_id": str(operation.get("source_object_id") or ""),
                    "source_object_label": str(operation.get("source_object_label") or ""),
                    "occurred_at": str(operation.get("created_at") or ""),
                }
            )
        canonical_by_nm = {
            int(item["nm_id"]): {**dict(item), "quantity": _decimal(item.get("quantity"))}
            for item in canonical_rows
            if _positive_int_or_none(item.get("nm_id")) is not None
            and _decimal(item.get("quantity")) != 0
        }
        ledger_nonzero = {
            nm_id: quantity for nm_id, quantity in quantities.items() if quantity != 0
        }
        canonical_quantities = {
            nm_id: _decimal(item["quantity"])
            for nm_id, item in canonical_by_nm.items()
        }
        if ledger_nonzero != canonical_quantities:
            raise WarehouseOpeningSnapshotError(
                "canonical FF ledger changed while reading the current balance; retry"
            )
        captured_at = self.timestamp_factory()
        rows = [
            {
                "line_id": f"ff_current_balance_{nm_id}",
                "document_id": "",
                "line_no": index,
                "nm_id": nm_id,
                "sku": str(item.get("sku") or ""),
                "nomenclature_name": str(item.get("nomenclature_name") or ""),
                "barcode": str(item.get("barcode") or ""),
                "quantity": _public_decimal(item["quantity"]),
                "average_unit_cost_rub": None,
                "capital_rub": None,
                "negative_balance": bool(item.get("negative_balance")),
                "warning": str(item.get("warning") or ""),
                "provenance": {
                    "warehouse_key": "ff",
                    "source_type": "canonical_ff_stock_ledger_current_balance",
                    "captured_at": captured_at,
                    "source_records": provenance[nm_id],
                },
            }
            for index, (nm_id, item) in enumerate(sorted(canonical_by_nm.items()), start=1)
        ]
        definition = WAREHOUSE_BY_KEY["ff"]
        summary = _warehouse_summary(definition, document, cutover=cutover)
        summary.update(
            {
                "updated_at": captured_at,
                "source_basis": str(definition["source"]) + " (текущий баланс)",
                "source_watermark": {
                    "captured_at": captured_at,
                    "latest_operation_at": max(
                        (str(item.get("created_at") or "") for item in operation_rows),
                        default="",
                    ),
                    "operation_count": len(operation_rows),
                    "contribution_line_count": len(line_rows),
                },
                "sku_count": len(rows),
                "total_quantity": _public_decimal(sum(canonical_quantities.values(), Decimal("0"))),
                "balance_mode": "current_canonical_ff_ledger",
            }
        )
        return rows, summary

    def build_opening_plan(self) -> dict[str, Any]:
        self._ensure_source_schema()
        existing = self.readback()
        if existing.get("status") == "ready":
            return {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "status": "already_initialized",
                "idempotent": True,
                "cutover": existing.get("cutover"),
                "documents": existing.get("documents"),
                "plan_fingerprint": str((existing.get("cutover") or {}).get("plan_fingerprint") or ""),
            }

        nomenclature_request = self._opening_nomenclature_request()
        wb_snapshot_payload = self._fetch_wb_stock_snapshot(nomenclature_request)
        canonical_ff_rows = self.ff_stock_ledger_block.current_balance_rows()
        cutover_at = self.timestamp_factory()
        local = self._read_local_source_snapshot(cutover_at=cutover_at)
        documents, warehouse_watermarks = self._build_documents(
            local=local,
            wb_snapshot_payload=wb_snapshot_payload,
            canonical_ff_rows=canonical_ff_rows,
            cutover_at=cutover_at,
        )
        plan = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "dry_run_ready",
            "cutover_id": OPENING_CUTOVER_ID,
            "cutover_at": cutover_at,
            "local_source_digest": local["source_digest"],
            "source_watermarks": {
                "local_runtime": local["watermarks"],
                **warehouse_watermarks,
            },
            "documents": documents,
        }
        plan["plan_fingerprint"] = _plan_fingerprint(plan)
        _validate_plan(plan)
        return plan

    def apply_opening_plan(
        self,
        plan: Mapping[str, Any],
        *,
        confirm_fingerprint: str,
        backup_dir: Path,
        fail_after_documents: int | None = None,
    ) -> dict[str, Any]:
        self._ensure_source_schema()
        normalized_plan = _json_clone(plan)
        _validate_plan(normalized_plan)
        fingerprint = str(normalized_plan.get("plan_fingerprint") or "")
        if not fingerprint or fingerprint != str(confirm_fingerprint or "").strip():
            raise WarehouseOpeningSnapshotError("exact dry-run plan fingerprint confirmation is required")
        if fingerprint != _plan_fingerprint(normalized_plan):
            raise WarehouseOpeningSnapshotError("opening plan fingerprint does not match plan content")

        existing = self.readback()
        if existing.get("status") == "ready":
            existing_fingerprint = str((existing.get("cutover") or {}).get("plan_fingerprint") or "")
            if existing_fingerprint != fingerprint:
                raise WarehouseOpeningSnapshotError(
                    "warehouse opening cutover already exists with a different fingerprint"
                )
            existing["idempotent"] = True
            return existing

        current_local = self._read_local_source_snapshot(
            cutover_at=str(normalized_plan.get("cutover_at") or "")
        )
        if current_local["source_digest"] != str(normalized_plan.get("local_source_digest") or ""):
            raise WarehouseOpeningSnapshotError(
                "local source snapshot changed after dry-run; build a fresh plan"
            )

        backup = self._backup_before_mutation(backup_dir, purpose="preapply")
        with _connect(self.runtime.db_path) as conn:
            _ensure_warehouse_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                locked_local = self._read_local_source_snapshot(
                    cutover_at=str(normalized_plan.get("cutover_at") or ""),
                    connection=conn,
                )
                if locked_local["source_digest"] != str(normalized_plan.get("local_source_digest") or ""):
                    raise WarehouseOpeningSnapshotError(
                        "local source snapshot changed while acquiring the apply lock; build a fresh plan"
                    )
                if _load_cutover(conn) is not None:
                    raise WarehouseOpeningSnapshotError("warehouse opening cutover appeared during apply")
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_warehouse_cutovers(
                        cutover_id, cutover_at, status, source_watermarks_json,
                        plan_fingerprint, apply_audit_json, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        OPENING_CUTOVER_ID,
                        str(normalized_plan["cutover_at"]),
                        "posted",
                        _json_dumps(normalized_plan.get("source_watermarks") or {}),
                        fingerprint,
                        _json_dumps({"backup": _public_backup_evidence(backup), "mode": "opening_apply"}),
                        self.timestamp_factory(),
                        self.timestamp_factory(),
                    ),
                )
                for index, document in enumerate(normalized_plan["documents"], start=1):
                    _insert_document(conn, document, cutover_id=OPENING_CUTOVER_ID)
                    if fail_after_documents is not None and index >= int(fail_after_documents):
                        raise RuntimeError("injected warehouse opening apply failure")
                _verify_applied_cutover(conn, normalized_plan)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        result = self.readback()
        result["idempotent"] = False
        result["backup"] = _public_backup_evidence(backup)
        return result

    def rollback_opening_cutover(
        self,
        *,
        confirm_fingerprint: str,
        backup_dir: Path,
    ) -> dict[str, Any]:
        existing = self.readback()
        if existing.get("status") != "ready":
            return {**existing, "status": "absent", "idempotent": True}
        fingerprint = str((existing.get("cutover") or {}).get("plan_fingerprint") or "")
        if fingerprint != str(confirm_fingerprint or "").strip():
            raise WarehouseOpeningSnapshotError("exact applied fingerprint is required for rollback")
        backup = self._backup_before_mutation(backup_dir, purpose="prerollback")
        with _connect(self.runtime.db_path) as conn:
            _ensure_warehouse_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM sheet_vitrina_v1_warehouse_cutovers WHERE cutover_id = ?",
                    (OPENING_CUTOVER_ID,),
                )
                remaining = conn.execute(
                    "SELECT COUNT(*) AS count FROM sheet_vitrina_v1_warehouse_documents WHERE cutover_id = ?",
                    (OPENING_CUTOVER_ID,),
                ).fetchone()
                if int(remaining["count"] or 0) != 0:
                    raise WarehouseOpeningSnapshotError("warehouse rollback left document rows")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "rolled_back",
            "cutover_id": OPENING_CUTOVER_ID,
            "plan_fingerprint": fingerprint,
            "backup": _public_backup_evidence(backup),
        }

    def readback(self) -> dict[str, Any]:
        self._ensure_source_schema()
        with _connect(self.runtime.db_path) as conn:
            _ensure_warehouse_schema(conn)
            cutover, documents = _load_validated_opening_state(conn)
        if cutover is None:
            return {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "status": "not_initialized",
                "cutover": None,
                "documents": [],
            }
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready",
            "cutover": cutover,
            "documents": documents,
            "reconciliation": _readback_reconciliation(documents),
        }

    def _ensure_source_schema(self) -> None:
        # Existing runtime owns all source tables; this call only runs its idempotent schema guard.
        self.runtime.list_nomenclature_items(active_only=False)

    def _opening_nomenclature_request(self) -> list[dict[str, Any]]:
        if self.wb_nomenclature_provider is not None:
            rows = list(self.wb_nomenclature_provider())
        else:
            current_state = self.runtime.load_current_state()
            nomenclature_by_nm = _nomenclature_index(
                self.runtime.list_nomenclature_items(active_only=False)
            )
            rows = []
            for item in current_state.config_v2:
                if not item.enabled:
                    continue
                nm_id = int(item.nm_id)
                nomenclature = nomenclature_by_nm.get(nm_id, {})
                rows.append(
                    {
                        **nomenclature,
                        "nm_id": nm_id,
                        "our_sku": str(nomenclature.get("our_sku") or item.display_name),
                        "nomenclature_name": str(
                            nomenclature.get("nomenclature_name") or item.display_name
                        ),
                    }
                )
        result = []
        seen: set[int] = set()
        for row in rows:
            nm_id = _positive_int_or_none(row.get("nm_id"))
            if nm_id is None or nm_id in seen:
                continue
            seen.add(nm_id)
            result.append(dict(row))
        if not result:
            raise WarehouseOpeningSnapshotError("active nomenclature has no canonical nmID for WB stock snapshot")
        return sorted(result, key=lambda item: int(item["nm_id"]))

    def _fetch_wb_stock_snapshot(self, nomenclature: list[Mapping[str, Any]]) -> dict[str, Any]:
        now = self.now_factory()
        nm_ids = [int(item["nm_id"]) for item in nomenclature]
        request = StocksRequest(
            snapshot_type="stocks",
            snapshot_date=business_date_iso(now),
            nm_ids=nm_ids,
            scenario="normal",
        )
        payload = _json_clone(self.stocks_block.fetch_payload(request))
        envelope = transform_legacy_payload(payload)
        result = envelope.result
        if result.kind != "success":
            raise WarehouseOpeningSnapshotError(
                "WB stock API snapshot coverage is incomplete: "
                + _json_dumps(
                    {
                        "requested_count": getattr(result, "requested_count", len(nm_ids)),
                        "covered_count": getattr(result, "covered_count", 0),
                        "missing_nm_ids": getattr(result, "missing_nm_ids", []),
                    }
                )
            )
        payload["canonical_items"] = [
            {
                "nm_id": int(item.nm_id),
                "quantity": _decimal_text(item.stock_total),
            }
            for item in result.items
        ]
        payload["nomenclature"] = [
            {
                "nm_id": int(item["nm_id"]),
                "barcode": str(item.get("barcode") or ""),
                "sku": str(item.get("our_sku") or item.get("vendor_code") or ""),
                "name": str(item.get("nomenclature_name") or item.get("wb_title") or ""),
            }
            for item in nomenclature
        ]
        return payload

    def _read_local_source_snapshot(
        self,
        *,
        cutover_at: str,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        if connection is None:
            with _connect(self.runtime.db_path) as owned_connection:
                owned_connection.execute("BEGIN")
                snapshot = self._read_local_source_snapshot(
                    cutover_at=cutover_at,
                    connection=owned_connection,
                )
                owned_connection.commit()
                return snapshot
        conn = connection
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required = {
            "sheet_vitrina_v1_supplier_shipments",
            "sheet_vitrina_v1_supplier_shipment_lines",
            "sheet_vitrina_v1_cny_documents",
            "sheet_vitrina_v1_ff_stock_operations",
            "sheet_vitrina_v1_ff_stock_operation_lines",
            "sheet_vitrina_v1_wb_supplies",
            "sheet_vitrina_v1_wb_supplies_sync_state",
            "sheet_vitrina_v1_nomenclature_items",
        }
        missing = sorted(required - tables)
        if missing:
            raise WarehouseOpeningSnapshotError("required source tables are missing: " + ", ".join(missing))
        snapshot = {
            "shipments": _query_dicts(
                conn,
                """SELECT shipment_id, created_at, updated_at, shipment_date,
                          actual_shipment_date, actual_ff_acceptance_date,
                          historical_status_exception, order_status, invoice_no,
                          invoice_date, invoice_document_id, match_status
                   FROM sheet_vitrina_v1_supplier_shipments ORDER BY shipment_id""",
            ),
            "shipment_lines": _query_dicts(
                conn,
                """SELECT line_id, shipment_id, line_type, sort_order, source_no,
                          barcode, internal_sku, internal_nm_id, internal_name,
                          qty, match_status
                   FROM sheet_vitrina_v1_supplier_shipment_lines
                   ORDER BY shipment_id, sort_order, line_id""",
            ),
            "posted_supplier_payments": _query_dicts(
                conn,
                """SELECT document_id, source_order_id, linked_financial_document_id,
                          operation_date, operation_datetime, status, document_number,
                          cny_amount, natural_key, file_sha256
                   FROM sheet_vitrina_v1_cny_documents
                   WHERE document_type = ? AND status = ?
                   ORDER BY source_order_id, operation_datetime, document_id""",
                (CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT, CNY_DOCUMENT_STATUS_POSTED),
            ),
            "ff_operations": _query_dicts(
                conn,
                """SELECT operation_id, operation_type, source_type, source_key,
                          source_object_id, source_object_label, created_at,
                          sku_count, total_quantity_delta, diagnostics_json
                   FROM sheet_vitrina_v1_ff_stock_operations
                   ORDER BY created_at, operation_id""",
            ),
            "ff_lines": _query_dicts(
                conn,
                """SELECT operation_id, line_no, nm_id, barcode, sku,
                          nomenclature_name, comment, group_name, quantity_delta
                   FROM sheet_vitrina_v1_ff_stock_operation_lines
                   ORDER BY operation_id, line_no""",
            ),
            "wb_supplies": _query_dicts(
                conn,
                """SELECT supply_id, cache_key, wb_supply_id, preorder_id,
                          normalized_row_json, raw_goods_json, raw_goods_hash,
                          status_id, source_created_at, supply_date, fact_date,
                          updated_date, synced_at, last_list_synced_at,
                          last_enriched_at, enrichment_status
                   FROM sheet_vitrina_v1_wb_supplies ORDER BY supply_id""",
            ),
            "wb_sync_state": _query_dicts(
                conn,
                """SELECT last_synced_at, last_successful_sync_at, last_error,
                          latest_synced_count, backfill_complete,
                          latest_window_synced_at, last_mode
                   FROM sheet_vitrina_v1_wb_supplies_sync_state WHERE slot = 1""",
            ),
            "nomenclature": _query_dicts(
                conn,
                """SELECT item_id, is_active, is_hidden, our_sku, nm_id,
                          barcode, barcodes_json, vendor_code, wb_title,
                          nomenclature_name, product_type, comment, updated_at
                   FROM sheet_vitrina_v1_nomenclature_items ORDER BY item_id""",
            ),
        }
        digest_payload = {key: value for key, value in snapshot.items()}
        source_digest = "sha256:" + hashlib.sha256(_json_dumps(digest_payload).encode("utf-8")).hexdigest()
        return {
            **snapshot,
            "source_digest": source_digest,
            "watermarks": {
                "captured_at": cutover_at,
                "source_digest": source_digest,
                "supplier_shipments": _watermark(snapshot["shipments"], "updated_at"),
                "posted_supplier_payments": _watermark(
                    snapshot["posted_supplier_payments"], "operation_datetime", fallback_key="operation_date"
                ),
                "ff_ledger": _watermark(snapshot["ff_operations"], "created_at"),
                "wb_supplies_cache": {
                    **_watermark(snapshot["wb_supplies"], "last_list_synced_at", fallback_key="synced_at"),
                    "sync_state": snapshot["wb_sync_state"][0] if snapshot["wb_sync_state"] else {},
                },
                "nomenclature": _watermark(snapshot["nomenclature"], "updated_at"),
            },
        }

    def _build_documents(
        self,
        *,
        local: Mapping[str, Any],
        wb_snapshot_payload: Mapping[str, Any],
        canonical_ff_rows: Iterable[Mapping[str, Any]],
        cutover_at: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        nomenclature_all = _nomenclature_index(local.get("nomenclature") or [])
        active_nm_ids = {
            int(item["nm_id"])
            for item in (local.get("nomenclature") or [])
            if _positive_int_or_none(item.get("nm_id")) is not None
            and bool(item.get("is_active"))
            and not bool(item.get("is_hidden"))
        }
        buckets: dict[str, dict[int, dict[str, Any]]] = {
            str(item["key"]): {} for item in WAREHOUSES
        }

        shipments_by_id = {
            str(item.get("shipment_id") or ""): dict(item)
            for item in (local.get("shipments") or [])
        }
        lines_by_shipment: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for line in local.get("shipment_lines") or []:
            lines_by_shipment[str(line.get("shipment_id") or "")].append(dict(line))
        payments_by_shipment: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for payment in local.get("posted_supplier_payments") or []:
            order_id = str(payment.get("source_order_id") or "").strip()
            if order_id:
                payments_by_shipment[order_id].append(dict(payment))
        cutover_datetime = datetime.fromisoformat(cutover_at.replace("Z", "+00:00"))
        cutover_business_date = business_date_iso(cutover_datetime)

        for shipment_id, shipment in shipments_by_id.items():
            persisted_status = str(shipment.get("order_status") or "").strip().lower()
            historical_exception = str(shipment.get("historical_status_exception") or "").strip()
            if persisted_status in INACTIVE_SUPPLIER_STATUSES or historical_exception:
                continue
            actual_shipment = str(shipment.get("actual_shipment_date") or "").strip()
            actual_ff_acceptance = str(shipment.get("actual_ff_acceptance_date") or "").strip()
            status_resolution = resolve_supplier_shipment_status(
                actual_shipment_date=actual_shipment,
                actual_ff_acceptance_date=actual_ff_acceptance,
                business_today=cutover_business_date,
                persisted_status=persisted_status,
                historical_status_exception=historical_exception,
            )
            if actual_shipment and status_resolution.order_status == ORDER_STATUS_PRODUCTION:
                raise WarehouseOpeningSnapshotError(
                    f"supplier shipment {shipment_id} has a non-occurred/invalid actual_shipment_date"
                )
            if actual_ff_acceptance and status_resolution.order_status != ORDER_STATUS_ACCEPTED_FF:
                raise WarehouseOpeningSnapshotError(
                    f"supplier shipment {shipment_id} has a non-occurred/invalid actual_ff_acceptance_date"
                )
            target_key = ""
            source_extra: dict[str, Any] = {}
            if (
                payments_by_shipment.get(shipment_id)
                and status_resolution.order_status == ORDER_STATUS_PRODUCTION
                and not actual_shipment
                and not actual_ff_acceptance
            ):
                target_key = "production"
                source_extra["posted_payments"] = [
                    {
                        "document_id": str(item.get("document_id") or ""),
                        "document_number": str(item.get("document_number") or ""),
                        "operation_datetime": str(
                            item.get("operation_datetime") or item.get("operation_date") or ""
                        ),
                        "natural_key": str(item.get("natural_key") or ""),
                        "file_sha256": str(item.get("file_sha256") or ""),
                    }
                    for item in payments_by_shipment[shipment_id]
                ]
            elif status_resolution.order_status == ORDER_STATUS_IN_TRANSIT:
                target_key = "china_to_ff"
            if not target_key:
                continue
            for line in _validated_supplier_product_lines(lines_by_shipment.get(shipment_id, []), shipment_id):
                nm_id = int(line["internal_nm_id"])
                _add_quantity(
                    buckets[target_key],
                    nm_id=nm_id,
                    quantity=_decimal(line["qty"]),
                    display=_display_for_nm(nm_id, nomenclature_all, fallback=line),
                    provenance={
                        "source_type": "supplier_invoice_line",
                        "shipment_id": shipment_id,
                        "invoice_no": str(shipment.get("invoice_no") or ""),
                        "invoice_document_id": str(shipment.get("invoice_document_id") or ""),
                        "line_id": str(line.get("line_id") or ""),
                        "source_no": str(line.get("source_no") or ""),
                        "line_quantity": _decimal_text(line.get("qty")),
                        "actual_shipment_date": actual_shipment or None,
                        "actual_ff_acceptance_date": actual_ff_acceptance or None,
                        "derived_order_status": status_resolution.order_status,
                        "status_source": status_resolution.status_source,
                        **source_extra,
                    },
                )

        ff_operations = {
            str(item.get("operation_id") or ""): dict(item)
            for item in (local.get("ff_operations") or [])
        }
        ff_snapshot_quantities: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        ff_snapshot_provenance: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for line in local.get("ff_lines") or []:
            nm_id = _positive_int_or_none(line.get("nm_id"))
            if nm_id is None or nm_id not in active_nm_ids:
                continue
            quantity = _decimal(line.get("quantity_delta"))
            if quantity == 0:
                continue
            operation = ff_operations.get(str(line.get("operation_id") or ""), {})
            ff_snapshot_quantities[nm_id] += quantity
            ff_snapshot_provenance[nm_id].append(
                {
                    "operation_id": str(line.get("operation_id") or ""),
                    "line_no": int(line.get("line_no") or 0),
                    "quantity_delta": _decimal_text(quantity),
                    "operation_type": str(operation.get("operation_type") or ""),
                    "operation_source_type": str(operation.get("source_type") or ""),
                    "source_key": str(operation.get("source_key") or ""),
                    "source_object_id": str(operation.get("source_object_id") or ""),
                }
            )
        canonical_ff_by_nm: dict[int, dict[str, Any]] = {}
        for row in canonical_ff_rows:
            nm_id = _positive_int_or_none(row.get("nm_id"))
            if nm_id is None:
                continue
            quantity = _decimal(row.get("quantity"))
            if quantity != 0:
                canonical_ff_by_nm[nm_id] = {**dict(row), "quantity": quantity}
        local_ff_nonzero = {
            nm_id: quantity for nm_id, quantity in ff_snapshot_quantities.items() if quantity != 0
        }
        canonical_ff_quantities = {
            nm_id: _decimal(row["quantity"]) for nm_id, row in canonical_ff_by_nm.items()
        }
        if local_ff_nonzero != canonical_ff_quantities:
            raise WarehouseOpeningSnapshotError(
                "canonical FF balance changed while building the opening snapshot; retry dry-run"
            )
        for nm_id, canonical_row in sorted(canonical_ff_by_nm.items()):
            _add_quantity(
                buckets["ff"],
                nm_id=nm_id,
                quantity=_decimal(canonical_row["quantity"]),
                display=_display_for_nm(nm_id, nomenclature_all, fallback=canonical_row),
                provenance={
                    "source_type": "canonical_ff_stock_ledger_balance",
                    "quantity": _decimal_text(canonical_row["quantity"]),
                    "component_lines": ff_snapshot_provenance[nm_id],
                },
            )

        discrepancy_regular: dict[int, dict[str, Any]] = {}
        discrepancy_doprinato: dict[int, dict[str, Any]] = {}
        for raw_record in local.get("wb_supplies") or []:
            record = _normalized_wb_record(raw_record)
            status_id = int(record.get("status_id") or 0)
            is_doprinato = _is_doprinato(record)
            goods = _validated_wb_goods(record)
            if status_id in WB_POST_SHIPMENT_GATE_STATUS_IDS and not is_doprinato:
                for good in goods:
                    nm_id = int(good["nm_id"])
                    quantity = _required_nonnegative_decimal(good.get("quantity"), "WB sent quantity")
                    if quantity == 0:
                        continue
                    _add_quantity(
                        buckets["ff_to_wb"],
                        nm_id=nm_id,
                        quantity=quantity,
                        display=_display_for_nm(nm_id, nomenclature_all, fallback=good),
                        provenance={
                            **_wb_goods_provenance(record, good, quantity_field="quantity"),
                            "sent_quantity": _decimal_text(quantity),
                        },
                    )
            if status_id != WB_FINAL_ACCEPTED_STATUS_ID:
                continue
            for good in goods:
                nm_id = int(good["nm_id"])
                if is_doprinato:
                    accepted = _optional_decimal(good.get("accepted_quantity"))
                    accepted_source = "acceptedQuantity"
                    if accepted is None or accepted == 0:
                        accepted = _required_nonnegative_decimal(good.get("quantity"), "WB doprinato quantity")
                        accepted_source = "quantity_fallback_existing_canonical_rule"
                    if accepted < 0:
                        raise WarehouseOpeningSnapshotError("WB doprinato accepted quantity is negative")
                    bucket = discrepancy_doprinato.setdefault(
                        nm_id,
                        {"quantity": Decimal("0"), "provenance": [], "display": _display_for_nm(nm_id, nomenclature_all, fallback=good)},
                    )
                    bucket["quantity"] += accepted
                    bucket["provenance"].append(
                        {
                            **_wb_goods_provenance(record, good, quantity_field=accepted_source),
                            "accepted_quantity": _decimal_text(accepted),
                        }
                    )
                else:
                    sent = _required_nonnegative_decimal(good.get("quantity"), "WB sent quantity")
                    accepted = _required_nonnegative_decimal(good.get("accepted_quantity"), "WB accepted quantity")
                    bucket = discrepancy_regular.setdefault(
                        nm_id,
                        {
                            "sent": Decimal("0"),
                            "accepted": Decimal("0"),
                            "provenance": [],
                            "display": _display_for_nm(nm_id, nomenclature_all, fallback=good),
                        },
                    )
                    bucket["sent"] += sent
                    bucket["accepted"] += accepted
                    bucket["provenance"].append(
                        {
                            **_wb_goods_provenance(record, good, quantity_field="quantity/acceptedQuantity"),
                            "sent_quantity": _decimal_text(sent),
                            "accepted_quantity": _decimal_text(accepted),
                            "source_discrepancy": _decimal_text(sent - accepted),
                        }
                    )

        for nm_id in sorted(set(discrepancy_regular) | set(discrepancy_doprinato)):
            regular = discrepancy_regular.get(
                nm_id,
                {"sent": Decimal("0"), "accepted": Decimal("0"), "provenance": [], "display": _display_for_nm(nm_id, nomenclature_all)},
            )
            doprinato = discrepancy_doprinato.get(
                nm_id,
                {"quantity": Decimal("0"), "provenance": [], "display": regular["display"]},
            )
            quantity = regular["sent"] - regular["accepted"] - doprinato["quantity"]
            if quantity < 0:
                raise WarehouseOpeningSnapshotError(
                    "negative WB acceptance discrepancy for nmID "
                    f"{nm_id}: sent={_decimal_text(regular['sent'])}, "
                    f"accepted={_decimal_text(regular['accepted'])}, "
                    f"doprinato={_decimal_text(doprinato['quantity'])}"
                )
            if quantity == 0:
                continue
            _add_quantity(
                buckets["wb_acceptance_discrepancy"],
                nm_id=nm_id,
                quantity=quantity,
                display=regular["display"] or doprinato["display"],
                provenance={
                    "source_type": "wb_acceptance_discrepancy_by_sku",
                    "sent_quantity": _decimal_text(regular["sent"]),
                    "accepted_quantity": _decimal_text(regular["accepted"]),
                    "doprinato_quantity": _decimal_text(doprinato["quantity"]),
                    "final_quantity": _decimal_text(quantity),
                    "final_supply_lines": regular["provenance"],
                    "doprinato_lines": doprinato["provenance"],
                },
            )

        wb_rows = list(((wb_snapshot_payload.get("data") or {}).get("rows") or []))
        wb_rows_by_nm: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in wb_rows:
            if not isinstance(row, Mapping):
                continue
            nm_id = _positive_int_or_none(row.get("nmId"))
            if nm_id is not None:
                wb_rows_by_nm[nm_id].append(dict(row))
        wb_nomenclature = {
            int(item["nm_id"]): dict(item)
            for item in wb_snapshot_payload.get("nomenclature") or []
        }
        for item in wb_snapshot_payload.get("canonical_items") or []:
            nm_id = int(item["nm_id"])
            quantity = _decimal(item.get("quantity"))
            if quantity < 0:
                raise WarehouseOpeningSnapshotError(
                    f"WB stock API returned negative current stock for nmID {nm_id}: {_decimal_text(quantity)}"
                )
            if quantity == 0:
                continue
            evidence_rows = [
                {
                    "snapshot_date": str(row.get("snapshot_date") or ""),
                    "snapshot_ts": str(row.get("snapshot_ts") or ""),
                    "warehouse_name": str(row.get("warehouseName") or ""),
                    "region_name": str(row.get("regionName") or ""),
                    "quantity": _decimal_text(row.get("stockCount")),
                }
                for row in wb_rows_by_nm.get(nm_id, [])
            ]
            _add_quantity(
                buckets["wb"],
                nm_id=nm_id,
                quantity=quantity,
                display=_display_for_nm(nm_id, nomenclature_all, fallback=wb_nomenclature.get(nm_id, {})),
                provenance={
                    "source_type": "wb_stocks_api_snapshot",
                    "endpoint": "/api/analytics/v1/stocks-report/wb-warehouses",
                    "rows": evidence_rows,
                },
            )

        documents = [
            _build_document(definition, buckets[str(definition["key"])], cutover_at=cutover_at)
            for definition in WAREHOUSES
        ]
        wb_data = wb_snapshot_payload.get("data") or {}
        wb_snapshot_watermark = {
            "source": WAREHOUSE_API_SOURCE,
            "snapshot_date": str(wb_snapshot_payload.get("snapshot_date") or ""),
            "requested_snapshot_date": str(wb_data.get("requested_snapshot_date") or ""),
            "fetched_at": str(wb_data.get("fetched_at") or _latest_snapshot_ts(wb_rows)),
            "requested_nm_id_count": len(wb_snapshot_payload.get("requested_nm_ids") or []),
            "covered_nm_id_count": len(wb_snapshot_payload.get("canonical_items") or []),
            "payload_digest": "sha256:" + hashlib.sha256(_json_dumps(wb_snapshot_payload).encode("utf-8")).hexdigest(),
        }
        warehouse_watermarks = {
            "warehouse_sources": {
                "production": {
                    "supplier_shipments": local["watermarks"]["supplier_shipments"],
                    "posted_supplier_payments": local["watermarks"]["posted_supplier_payments"],
                    "nomenclature": local["watermarks"]["nomenclature"],
                    "local_source_digest": local["source_digest"],
                },
                "china_to_ff": {
                    "supplier_shipments": local["watermarks"]["supplier_shipments"],
                    "nomenclature": local["watermarks"]["nomenclature"],
                    "local_source_digest": local["source_digest"],
                },
                "ff": {
                    "ff_ledger": local["watermarks"]["ff_ledger"],
                    "nomenclature": local["watermarks"]["nomenclature"],
                    "local_source_digest": local["source_digest"],
                },
                "ff_to_wb": {
                    "wb_supplies_cache": local["watermarks"]["wb_supplies_cache"],
                    "nomenclature": local["watermarks"]["nomenclature"],
                    "local_source_digest": local["source_digest"],
                },
                "wb": {
                    **wb_snapshot_watermark,
                    "nomenclature": local["watermarks"]["nomenclature"],
                    "local_source_digest": local["source_digest"],
                },
                "wb_acceptance_discrepancy": {
                    "wb_supplies_cache": local["watermarks"]["wb_supplies_cache"],
                    "nomenclature": local["watermarks"]["nomenclature"],
                    "local_source_digest": local["source_digest"],
                },
            }
        }
        for document in documents:
            key = str(document["warehouse_key"])
            document["source_watermark"] = warehouse_watermarks["warehouse_sources"][key]
        return documents, warehouse_watermarks

    def _backup_before_mutation(self, backup_dir: Path, *, purpose: str) -> dict[str, Any]:
        target_dir = Path(backup_dir)
        if not target_dir.is_absolute():
            raise WarehouseOpeningSnapshotError("backup_dir must be absolute")
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.timestamp_factory().replace(":", "").replace("-", "").replace("+", "_")
        destination = target_dir / f"{OPENING_CUTOVER_ID}-{purpose}-{stamp}.sqlite3"
        backup = self.runtime.backup_database(destination)
        destination.chmod(0o600)
        return backup


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_warehouse_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_cutovers (
            cutover_id TEXT PRIMARY KEY,
            cutover_at TEXT NOT NULL,
            status TEXT NOT NULL,
            source_watermarks_json TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL UNIQUE,
            apply_audit_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_documents (
            document_id TEXT PRIMARY KEY,
            document_number TEXT NOT NULL UNIQUE,
            cutover_id TEXT REFERENCES sheet_vitrina_v1_warehouse_cutovers(cutover_id) ON DELETE CASCADE,
            document_type TEXT NOT NULL,
            document_type_label TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            warehouse_key TEXT NOT NULL,
            warehouse_name TEXT NOT NULL,
            warehouse_from_key TEXT,
            warehouse_to_key TEXT,
            source_basis TEXT NOT NULL,
            source_watermark_json TEXT NOT NULL,
            sku_count INTEGER NOT NULL,
            total_quantity TEXT NOT NULL,
            average_unit_cost_rub REAL,
            total_cost_rub REAL,
            total_capital_rub REAL,
            status TEXT NOT NULL,
            status_label TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(cutover_id, warehouse_key, document_type)
        );

        CREATE INDEX IF NOT EXISTS warehouse_documents_by_warehouse_time
        ON sheet_vitrina_v1_warehouse_documents(warehouse_key, occurred_at DESC, document_id DESC);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_document_lines (
            line_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_warehouse_documents(document_id) ON DELETE CASCADE,
            line_no INTEGER NOT NULL,
            nm_id INTEGER NOT NULL,
            sku TEXT,
            nomenclature_name TEXT,
            barcode TEXT,
            quantity TEXT NOT NULL,
            average_unit_cost_rub REAL,
            capital_rub REAL,
            provenance_json TEXT NOT NULL,
            UNIQUE(document_id, nm_id)
        );

        CREATE INDEX IF NOT EXISTS warehouse_document_lines_by_document
        ON sheet_vitrina_v1_warehouse_document_lines(document_id, line_no);
        """
    )


def _insert_document(conn: sqlite3.Connection, document: Mapping[str, Any], *, cutover_id: str) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_warehouse_documents(
            document_id, document_number, cutover_id, document_type,
            document_type_label, occurred_at, warehouse_key, warehouse_name,
            warehouse_from_key, warehouse_to_key, source_basis,
            source_watermark_json, sku_count, total_quantity,
            average_unit_cost_rub, total_cost_rub, total_capital_rub,
            status, status_label, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(document["document_id"]),
            str(document["document_number"]),
            cutover_id,
            str(document["document_type"]),
            str(document["document_type_label"]),
            str(document["occurred_at"]),
            str(document["warehouse_key"]),
            str(document["warehouse_name"]),
            document.get("warehouse_from_key"),
            document.get("warehouse_to_key"),
            str(document["source_basis"]),
            _json_dumps(document.get("source_watermark") or {}),
            int(document["sku_count"]),
            str(document["total_quantity"]),
            document.get("average_unit_cost_rub"),
            document.get("total_cost_rub"),
            document.get("total_capital_rub"),
            str(document["status"]),
            str(document["status_label"]),
            str(document["created_at"]),
            str(document["updated_at"]),
        ),
    )
    conn.executemany(
        """
        INSERT INTO sheet_vitrina_v1_warehouse_document_lines(
            line_id, document_id, line_no, nm_id, sku, nomenclature_name,
            barcode, quantity, average_unit_cost_rub, capital_rub, provenance_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                str(line["line_id"]),
                str(document["document_id"]),
                int(line["line_no"]),
                int(line["nm_id"]),
                str(line.get("sku") or ""),
                str(line.get("nomenclature_name") or ""),
                str(line.get("barcode") or ""),
                str(line["quantity"]),
                line.get("average_unit_cost_rub"),
                line.get("capital_rub"),
                _json_dumps(line.get("provenance") or {}),
            )
            for line in document.get("lines") or []
        ],
    )


def _load_cutover(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_cutovers WHERE cutover_id = ?",
        (OPENING_CUTOVER_ID,),
    ).fetchone()
    if row is None:
        return None
    return {
        "cutover_id": str(row["cutover_id"]),
        "cutover_at": str(row["cutover_at"]),
        "status": str(row["status"]),
        "source_watermarks": _json_loads(row["source_watermarks_json"], {}),
        "plan_fingerprint": str(row["plan_fingerprint"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _load_documents(conn: sqlite3.Connection, *, include_lines: bool) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM sheet_vitrina_v1_warehouse_documents
           WHERE cutover_id = ? ORDER BY document_number""",
        (OPENING_CUTOVER_ID,),
    ).fetchall()
    return [_document_from_row(conn, row, include_lines=include_lines) for row in rows]


def _load_validated_opening_state(
    conn: sqlite3.Connection,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    cutover = _load_cutover(conn)
    documents = _load_documents(conn, include_lines=True)
    if cutover is None:
        if documents:
            raise WarehouseOpeningSnapshotError("opening documents exist without their cutover")
        return None, []
    if cutover.get("status") != "posted":
        raise WarehouseOpeningSnapshotError("stored warehouse opening cutover is not posted")
    canonical_documents: list[dict[str, Any]] = []
    for document in documents:
        if document.get("cutover_id") != OPENING_CUTOVER_ID:
            raise WarehouseOpeningSnapshotError("stored opening document has an invalid cutover id")
        canonical_document = {
            key: value for key, value in document.items() if key != "cutover_id"
        }
        canonical_document["total_quantity"] = _decimal_text(document.get("total_quantity"))
        canonical_document["lines"] = [
            {
                **{
                    key: value
                    for key, value in line.items()
                    if key != "document_id"
                },
                "quantity": _decimal_text(line.get("quantity")),
            }
            for line in document.get("lines") or []
        ]
        canonical_documents.append(canonical_document)
    source_watermarks = cutover.get("source_watermarks")
    if not isinstance(source_watermarks, Mapping):
        raise WarehouseOpeningSnapshotError("stored warehouse source watermarks are invalid")
    local_source_digest = str(
        ((source_watermarks.get("local_runtime") or {}).get("source_digest") or "")
        if isinstance(source_watermarks.get("local_runtime"), Mapping)
        else ""
    )
    reconstructed_plan = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "cutover_id": str(cutover.get("cutover_id") or ""),
        "cutover_at": str(cutover.get("cutover_at") or ""),
        "local_source_digest": local_source_digest,
        "source_watermarks": source_watermarks,
        "documents": canonical_documents,
    }
    _validate_plan(reconstructed_plan)
    if _plan_fingerprint(reconstructed_plan) != str(cutover.get("plan_fingerprint") or ""):
        raise WarehouseOpeningSnapshotError("stored warehouse opening fingerprint reconciliation failed")
    return cutover, documents


def _document_from_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_lines: bool,
) -> dict[str, Any]:
    payload = {
        "document_id": str(row["document_id"]),
        "document_number": str(row["document_number"]),
        "cutover_id": str(row["cutover_id"]),
        "document_type": str(row["document_type"]),
        "document_type_label": str(row["document_type_label"]),
        "occurred_at": str(row["occurred_at"]),
        "warehouse_key": str(row["warehouse_key"]),
        "warehouse_name": str(row["warehouse_name"]),
        "warehouse_from_key": row["warehouse_from_key"],
        "warehouse_to_key": row["warehouse_to_key"],
        "source_basis": str(row["source_basis"]),
        "source_watermark": _json_loads(row["source_watermark_json"], {}),
        "sku_count": int(row["sku_count"]),
        "total_quantity": _public_decimal(row["total_quantity"]),
        "average_unit_cost_rub": row["average_unit_cost_rub"],
        "total_cost_rub": row["total_cost_rub"],
        "total_capital_rub": row["total_capital_rub"],
        "status": str(row["status"]),
        "status_label": str(row["status_label"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    if include_lines:
        line_rows = conn.execute(
            """SELECT * FROM sheet_vitrina_v1_warehouse_document_lines
               WHERE document_id = ? ORDER BY line_no""",
            (payload["document_id"],),
        ).fetchall()
        payload["lines"] = [
            {
                "line_id": str(line["line_id"]),
                "document_id": str(line["document_id"]),
                "line_no": int(line["line_no"]),
                "nm_id": int(line["nm_id"]),
                "sku": str(line["sku"] or ""),
                "nomenclature_name": str(line["nomenclature_name"] or ""),
                "barcode": str(line["barcode"] or ""),
                "quantity": _public_decimal(line["quantity"]),
                "average_unit_cost_rub": line["average_unit_cost_rub"],
                "capital_rub": line["capital_rub"],
                "provenance": _json_loads(line["provenance_json"], {}),
            }
            for line in line_rows
        ]
    return payload


def _warehouse_summary(
    definition: Mapping[str, Any],
    document: Mapping[str, Any] | None,
    *,
    cutover: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "warehouse_key": str(definition["key"]),
        "warehouse_name": str(definition["name"]),
        "updated_at": str((cutover or {}).get("cutover_at") or ""),
        "source_basis": str((document or {}).get("source_basis") or definition["source"]),
        "source_watermark": (document or {}).get("source_watermark") or {},
        "sku_count": int((document or {}).get("sku_count") or 0),
        "total_quantity": (document or {}).get("total_quantity") if document else 0,
        "average_unit_cost_rub": None,
        "total_cost_rub": None,
        "total_capital_rub": None,
        "status": str((document or {}).get("status") or "not_initialized"),
        "status_label": str((document or {}).get("status_label") or "Начальные остатки ещё не зафиксированы"),
        "document_id": str((document or {}).get("document_id") or ""),
        "document_number": str((document or {}).get("document_number") or ""),
    }


def _build_document(
    definition: Mapping[str, Any],
    bucket: Mapping[int, Mapping[str, Any]],
    *,
    cutover_at: str,
) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    for line_no, nm_id in enumerate(sorted(bucket), start=1):
        item = bucket[nm_id]
        quantity = _decimal(item["quantity"])
        if quantity == 0:
            continue
        line_id = "whline_" + hashlib.sha256(
            f"{definition['document_id']}:{nm_id}".encode("utf-8")
        ).hexdigest()[:20]
        lines.append(
            {
                "line_id": line_id,
                "line_no": len(lines) + 1,
                "nm_id": int(nm_id),
                "sku": str(item.get("sku") or ""),
                "nomenclature_name": str(item.get("nomenclature_name") or ""),
                "barcode": str(item.get("barcode") or ""),
                "quantity": _decimal_text(quantity),
                "average_unit_cost_rub": None,
                "capital_rub": None,
                "provenance": {
                    "warehouse_key": str(definition["key"]),
                    "source_records": list(item.get("provenance") or []),
                },
            }
        )
    total = sum((_decimal(line["quantity"]) for line in lines), Decimal("0"))
    return {
        "document_id": str(definition["document_id"]),
        "document_number": str(definition["document_number"]),
        "document_type": OPENING_DOCUMENT_TYPE,
        "document_type_label": OPENING_DOCUMENT_TYPE_LABEL,
        "occurred_at": cutover_at,
        "warehouse_key": str(definition["key"]),
        "warehouse_name": str(definition["name"]),
        "warehouse_from_key": None,
        "warehouse_to_key": str(definition["key"]),
        "source_basis": str(definition["source"]),
        "source_watermark": {},
        "sku_count": len(lines),
        "total_quantity": _decimal_text(total),
        "average_unit_cost_rub": None,
        "total_cost_rub": None,
        "total_capital_rub": None,
        "status": OPENING_STATUS,
        "status_label": OPENING_STATUS_LABEL,
        "created_at": cutover_at,
        "updated_at": cutover_at,
        "lines": lines,
    }


def _add_quantity(
    bucket: dict[int, dict[str, Any]],
    *,
    nm_id: int,
    quantity: Decimal,
    display: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> None:
    item = bucket.setdefault(
        int(nm_id),
        {
            "quantity": Decimal("0"),
            "sku": str(display.get("sku") or ""),
            "nomenclature_name": str(display.get("nomenclature_name") or ""),
            "barcode": str(display.get("barcode") or ""),
            "provenance": [],
        },
    )
    item["quantity"] += quantity
    item["provenance"].append(dict(provenance))


def _validated_supplier_product_lines(lines: Iterable[Mapping[str, Any]], shipment_id: str) -> list[dict[str, Any]]:
    result = []
    for line in lines:
        if str(line.get("line_type") or "") != "product":
            continue
        match_status = str(line.get("match_status") or "").strip()
        nm_id = _positive_int_or_none(line.get("internal_nm_id"))
        quantity = _optional_decimal(line.get("qty"))
        if (
            match_status not in MATCH_STATUSES_WITH_AUTHORITATIVE_NM_ID
            or nm_id is None
            or quantity is None
            or quantity <= 0
        ):
            raise WarehouseOpeningSnapshotError(
                f"supplier invoice {shipment_id} has an untraceable product line {line.get('line_id')}"
            )
        result.append(dict(line))
    if not result:
        raise WarehouseOpeningSnapshotError(f"supplier invoice {shipment_id} has no positive product lines")
    return result


def _normalized_wb_record(raw_record: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _json_loads(raw_record.get("normalized_row_json"), {})
    raw_goods = _json_loads(raw_record.get("raw_goods_json"), [])
    return {
        **normalized,
        "supply_id": str(raw_record.get("supply_id") or normalized.get("supply_id") or ""),
        "cache_key": str(raw_record.get("cache_key") or normalized.get("cache_key") or ""),
        "wb_supply_id": str(raw_record.get("wb_supply_id") or normalized.get("wb_supply_id") or ""),
        "preorder_id": str(raw_record.get("preorder_id") or normalized.get("preorder_id") or ""),
        "status_id": int(normalized.get("status_id") or raw_record.get("status_id") or 0),
        "raw_goods": raw_goods if isinstance(raw_goods, list) else [],
        "raw_goods_hash": str(raw_record.get("raw_goods_hash") or normalized.get("raw_goods_hash") or ""),
        "last_enriched_at": str(raw_record.get("last_enriched_at") or normalized.get("last_enriched_at") or ""),
        "last_list_synced_at": str(raw_record.get("last_list_synced_at") or normalized.get("last_list_synced_at") or ""),
    }


def _validated_wb_goods(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_goods = record.get("raw_goods")
    if not isinstance(raw_goods, list) or not raw_goods:
        if int(record.get("status_id") or 0) in WB_POST_SHIPMENT_GATE_STATUS_IDS | {WB_FINAL_ACCEPTED_STATUS_ID}:
            raise WarehouseOpeningSnapshotError(
                f"WB supply {record.get('supply_id')} has no traceable goods composition"
            )
        return []
    result = []
    for raw_index, raw in enumerate(raw_goods):
        if not isinstance(raw, Mapping):
            continue
        nm_id = _positive_int_or_none(_first(raw, "nmID", "nmId", "nm_id"))
        if nm_id is None:
            raise WarehouseOpeningSnapshotError(
                f"WB supply {record.get('supply_id')} goods row {raw_index} has no canonical nmID"
            )
        result.append(
            {
                "nm_id": nm_id,
                "barcode": str(_first(raw, "barcode", "barCode", "barcodeID") or ""),
                "sku": str(_first(raw, "vendorCode", "vendor_code", "supplierArticle", "article") or ""),
                "nomenclature_name": "",
                "quantity": _first(raw, "quantity", "qty"),
                "accepted_quantity": _first(raw, "acceptedQuantity", "accepted_quantity"),
                "raw_index": raw_index,
            }
        )
    return result


def _wb_goods_provenance(
    record: Mapping[str, Any],
    good: Mapping[str, Any],
    *,
    quantity_field: str,
) -> dict[str, Any]:
    return {
        "source_type": "wb_supply_goods_line",
        "supply_id": str(record.get("supply_id") or ""),
        "wb_supply_id": str(record.get("wb_supply_id") or ""),
        "cache_key": str(record.get("cache_key") or ""),
        "status_id": int(record.get("status_id") or 0),
        "status_label": str(record.get("status_label") or record.get("status_display") or ""),
        "virtual_type_id": record.get("virtual_type_id"),
        "type_label": str(record.get("type_label") or ""),
        "goods_raw_index": int(good.get("raw_index") or 0),
        "goods_hash": str(record.get("raw_goods_hash") or ""),
        "quantity_field": quantity_field,
        "last_list_synced_at": str(record.get("last_list_synced_at") or ""),
        "last_enriched_at": str(record.get("last_enriched_at") or ""),
    }


def _is_doprinato(record: Mapping[str, Any]) -> bool:
    return int(record.get("virtual_type_id") or 0) == 5 or str(record.get("type_label") or "").strip() == "Допринято"


def _nomenclature_index(rows: Iterable[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    result = {}
    for row in rows:
        nm_id = _positive_int_or_none(row.get("nm_id"))
        if nm_id is None:
            continue
        current = result.get(nm_id)
        if current is None or (bool(row.get("is_active")) and not bool(current.get("is_active"))):
            result[nm_id] = dict(row)
    return result


def _display_for_nm(
    nm_id: int,
    nomenclature: Mapping[int, Mapping[str, Any]],
    *,
    fallback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    item = dict(nomenclature.get(int(nm_id)) or {})
    source = dict(fallback or {})
    return {
        "sku": str(item.get("our_sku") or item.get("vendor_code") or source.get("internal_sku") or source.get("sku") or ""),
        "nomenclature_name": str(item.get("nomenclature_name") or item.get("wb_title") or source.get("internal_name") or source.get("nomenclature_name") or ""),
        "barcode": str(item.get("barcode") or source.get("barcode") or ""),
    }


def _validate_plan(plan: Mapping[str, Any]) -> None:
    if str(plan.get("cutover_id") or "") != OPENING_CUTOVER_ID:
        raise WarehouseOpeningSnapshotError("unexpected warehouse opening cutover_id")
    cutover_at = str(plan.get("cutover_at") or "").strip()
    if not cutover_at:
        raise WarehouseOpeningSnapshotError("opening plan cutover_at is required")
    if not str(plan.get("local_source_digest") or "").startswith("sha256:"):
        raise WarehouseOpeningSnapshotError("opening plan local source digest is required")
    if not isinstance(plan.get("source_watermarks"), Mapping):
        raise WarehouseOpeningSnapshotError("opening plan source watermarks are required")
    documents = list(plan.get("documents") or [])
    if len(documents) != len(WAREHOUSES):
        raise WarehouseOpeningSnapshotError("opening plan must contain exactly six documents")
    expected_keys = [str(item["key"]) for item in WAREHOUSES]
    if [str(item.get("warehouse_key") or "") for item in documents] != expected_keys:
        raise WarehouseOpeningSnapshotError("opening documents must follow the canonical six-warehouse order")
    document_ids: set[str] = set()
    document_numbers: set[str] = set()
    for definition, document in zip(WAREHOUSES, documents):
        document_id = str(document.get("document_id") or "")
        document_number = str(document.get("document_number") or "")
        if not document_id or document_id in document_ids or not document_number or document_number in document_numbers:
            raise WarehouseOpeningSnapshotError("opening document ids and numbers must be stable and unique")
        document_ids.add(document_id)
        document_numbers.add(document_number)
        expected_header = {
            "document_id": str(definition["document_id"]),
            "document_number": str(definition["document_number"]),
            "document_type": OPENING_DOCUMENT_TYPE,
            "document_type_label": OPENING_DOCUMENT_TYPE_LABEL,
            "warehouse_key": str(definition["key"]),
            "warehouse_name": str(definition["name"]),
            "warehouse_from_key": None,
            "warehouse_to_key": str(definition["key"]),
            "source_basis": str(definition["source"]),
            "status": OPENING_STATUS,
            "status_label": OPENING_STATUS_LABEL,
        }
        for key, expected_value in expected_header.items():
            if document.get(key) != expected_value:
                raise WarehouseOpeningSnapshotError(
                    f"document {document_id or definition['document_id']} has invalid {key}"
                )
        if str(document.get("occurred_at") or "") != cutover_at:
            raise WarehouseOpeningSnapshotError(f"document {document_id} does not share cutover_at")
        if not isinstance(document.get("source_watermark"), Mapping) or not document.get("source_watermark"):
            raise WarehouseOpeningSnapshotError(f"document {document_id} source watermark is required")
        if document.get("average_unit_cost_rub") is not None or document.get("total_cost_rub") is not None or document.get("total_capital_rub") is not None:
            raise WarehouseOpeningSnapshotError("opening document cost and capital must be NULL")
        lines = list(document.get("lines") or [])
        if int(document.get("sku_count") or 0) != len(lines):
            raise WarehouseOpeningSnapshotError(f"document {document_id} sku_count does not match lines")
        total = sum((_decimal(item.get("quantity")) for item in lines), Decimal("0"))
        if total != _decimal(document.get("total_quantity")):
            raise WarehouseOpeningSnapshotError(f"document {document_id} total quantity does not match lines")
        seen_nm_ids: set[int] = set()
        seen_line_ids: set[str] = set()
        for line_no, line in enumerate(lines, start=1):
            nm_id = _positive_int_or_none(line.get("nm_id"))
            if nm_id is None or nm_id in seen_nm_ids:
                raise WarehouseOpeningSnapshotError(f"document {document_id} has duplicate/invalid nmID")
            seen_nm_ids.add(nm_id)
            expected_line_id = "whline_" + hashlib.sha256(
                f"{document_id}:{nm_id}".encode("utf-8")
            ).hexdigest()[:20]
            line_id = str(line.get("line_id") or "")
            if line_id != expected_line_id or line_id in seen_line_ids:
                raise WarehouseOpeningSnapshotError(f"document {document_id} has invalid line id")
            seen_line_ids.add(line_id)
            if int(line.get("line_no") or 0) != line_no:
                raise WarehouseOpeningSnapshotError(f"document {document_id} line order is invalid")
            if _decimal(line.get("quantity")) == 0:
                raise WarehouseOpeningSnapshotError(f"document {document_id} must not store zero lines")
            if line.get("average_unit_cost_rub") is not None or line.get("capital_rub") is not None:
                raise WarehouseOpeningSnapshotError("opening line cost and capital must be NULL")
            provenance = line.get("provenance")
            if not isinstance(provenance, Mapping) or not list(provenance.get("source_records") or []):
                raise WarehouseOpeningSnapshotError(f"document {document_id} line provenance is required")


def _verify_applied_cutover(conn: sqlite3.Connection, plan: Mapping[str, Any]) -> None:
    rows = conn.execute(
        """SELECT d.document_id, d.sku_count, d.total_quantity,
                  COUNT(l.line_id) AS line_count
           FROM sheet_vitrina_v1_warehouse_documents d
           LEFT JOIN sheet_vitrina_v1_warehouse_document_lines l ON l.document_id = d.document_id
           WHERE d.cutover_id = ?
           GROUP BY d.document_id, d.sku_count, d.total_quantity""",
        (OPENING_CUTOVER_ID,),
    ).fetchall()
    if len(rows) != len(WAREHOUSES):
        raise WarehouseOpeningSnapshotError("atomic apply did not create exactly six documents")
    expected = {str(item["document_id"]): item for item in plan.get("documents") or []}
    for row in rows:
        document = expected.get(str(row["document_id"]))
        if document is None:
            raise WarehouseOpeningSnapshotError("atomic apply created an unexpected document")
        if int(row["line_count"] or 0) != int(row["sku_count"] or 0):
            raise WarehouseOpeningSnapshotError("applied document line count mismatch")
        line_rows = conn.execute(
            "SELECT quantity FROM sheet_vitrina_v1_warehouse_document_lines WHERE document_id = ?",
            (str(row["document_id"]),),
        ).fetchall()
        exact_total = sum((_decimal(item["quantity"]) for item in line_rows), Decimal("0"))
        if exact_total != _decimal(row["total_quantity"]):
            raise WarehouseOpeningSnapshotError("applied document quantity mismatch")


def _readback_reconciliation(documents: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "document_count": len(documents),
        "warehouse_count": len({str(item.get("warehouse_key") or "") for item in documents}),
        "unique_document_id_count": len({str(item.get("document_id") or "") for item in documents}),
        "all_costs_null": all(
            item.get("average_unit_cost_rub") is None
            and item.get("total_cost_rub") is None
            and item.get("total_capital_rub") is None
            and all(
                line.get("average_unit_cost_rub") is None and line.get("capital_rub") is None
                for line in item.get("lines") or []
            )
            for item in documents
        ),
        "document_line_balance_equal": all(
            _decimal(item.get("total_quantity"))
            == sum((_decimal(line.get("quantity")) for line in item.get("lines") or []), Decimal("0"))
            for item in documents
        ),
    }


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in plan.items()
        if key not in {"status", "plan_fingerprint", "idempotent"}
    }
    return "sha256:" + hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _watermark(rows: list[Mapping[str, Any]], key: str, *, fallback_key: str = "") -> dict[str, Any]:
    values = [
        str(item.get(key) or (item.get(fallback_key) if fallback_key else "") or "")
        for item in rows
    ]
    values = [value for value in values if value]
    return {"row_count": len(rows), "max_timestamp": max(values) if values else ""}


def _latest_snapshot_ts(rows: Iterable[Mapping[str, Any]]) -> str:
    values = [str(item.get("snapshot_ts") or "") for item in rows if str(item.get("snapshot_ts") or "")]
    return max(values) if values else ""


def _query_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _public_backup_evidence(backup: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "filename": Path(str(backup.get("path") or "")).name,
        "size_bytes": int(backup.get("size_bytes") or 0),
        "sha256": str(backup.get("sha256") or ""),
        "integrity_check": str(backup.get("integrity_check") or ""),
        "mode": "0600",
    }


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    raise TypeError(f"unsupported JSON value: {type(value)!r}")


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


def _json_loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return _json_clone(value)
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value if value is not None and value != "" else "0"))
    except (InvalidOperation, ValueError) as exc:
        raise WarehouseOpeningSnapshotError(f"quantity is not numeric: {value!r}") from exc
    if not result.is_finite():
        raise WarehouseOpeningSnapshotError(f"quantity is not finite: {value!r}")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value)


def _required_nonnegative_decimal(value: Any, field_name: str) -> Decimal:
    result = _optional_decimal(value)
    if result is None:
        raise WarehouseOpeningSnapshotError(f"{field_name} is missing")
    if result < 0:
        raise WarehouseOpeningSnapshotError(f"{field_name} is negative")
    return result


def _decimal_text(value: Any) -> str:
    number = _decimal(value)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _public_decimal(value: Any) -> int | float:
    number = _decimal(value)
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
