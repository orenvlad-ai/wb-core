"""Request-scoped, query-only supplier views using the canonical cost engine.

No live entrypoint initialization, schema bootstrap, document migration, PDF
parser, or background worker belongs in this process. Each view pins one short
operational snapshot. The write endpoints continue to use the legacy service.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from packages.application import registry_upload_db_backed_runtime as db
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.supplier_shipments import SupplierShipmentsBlock, _with_invoice_download_path, _assert_supplier_shipment_active
from packages.application.supplier_financial_documents import SupplierFinancialDocumentsBlock
from packages.application.supplier_shipment_factual_correction import (
    CORRECTION_TABLE, ACTIVE_CORRECTION_STATUSES, FINAL_CORRECTION_STATUSES,
)
from packages.application.storage_registry import StoreRegistry
from packages.application.cny_ledger import CnyLedgerBlock
from packages.application.supplier_expense_allocation import project_supplier_order_expense_allocation
from packages.application.warehouse_functional import load_supplier_line_cost_breakdown, supplier_cost_summary_fields
from packages.application.web_vitrina_window_read_context import window_read_context, borrowed_operational_connection


class ViewRuntime(db.RegistryUploadDbBackedRuntime):
    """Narrow the global ledger readers to the selected order and its fee refs."""

    def __init__(self, runtime_dir: Path):
        # Do not register a writable operational connection or initialize stores.
        self.runtime_dir = Path(runtime_dir)
        self.db_path = StoreRegistry(self.runtime_dir).resolve("operational")
        self.shipment_id = ""
        self.costs = {}
        self.details = {}

    def cost(self, shipment_id):
        if shipment_id not in self.costs:
            self.costs[shipment_id] = load_supplier_line_cost_breakdown(runtime=self, shipment_id=shipment_id)
        return self.costs[shipment_id]

    def load_supplier_shipment(self, shipment_id):
        if shipment_id not in self.details:
            self.details[shipment_id] = super().load_supplier_shipment(shipment_id)
        return deepcopy(self.details[shipment_id])

    def _rows(self, sql, args=()):
        conn = borrowed_operational_connection(self.db_path)
        if conn is None:
            raise RuntimeError("supplier view requires a pinned read context")
        return conn.execute(sql, args).fetchall()

    def list_cny_documents(self):
        if not self.shipment_id:
            raise RuntimeError("supplier ledger read requires an order")
        # A fee may reference a currency operation not directly assigned to this
        # order. Include those explicit natural keys rather than the whole account.
        keys = [str((line.get("raw") or {}).get("cny_ledger_natural_key") or "")
                for line in self.list_supplier_financial_expense_lines(self.shipment_id)]
        keys = sorted({key for key in keys if key})
        extra = " OR natural_key IN (" + ",".join("?" for _ in keys) + ")" if keys else ""
        rows = self._rows("SELECT * FROM sheet_vitrina_v1_cny_documents WHERE source_order_id=?" + extra +
            " ORDER BY COALESCE(NULLIF(operation_datetime,''),NULLIF(operation_date,''),uploaded_at),uploaded_at,document_id",
            (self.shipment_id, *keys))
        return [db._cny_document_to_dict(row) for row in rows]

    def list_cny_ledger_operations(self):
        ids = [item["document_id"] for item in self.list_cny_documents()]
        extra = " OR source_document_id IN (" + ",".join("?" for _ in ids) + ")" if ids else ""
        rows = self._rows("SELECT * FROM sheet_vitrina_v1_cny_ledger_operations WHERE source_order_id=?" + extra +
                          " ORDER BY sequence_key,operation_id", (self.shipment_id, *ids))
        return [db._cny_ledger_operation_to_dict(row) for row in rows]

    def list_supplier_bank_operation_assignments(self):
        rows = self._rows("SELECT * FROM sheet_vitrina_v1_supplier_bank_operation_assignments WHERE supplier_order_id=? ORDER BY semantic_operation_id", (self.shipment_id,))
        return [{**dict(row), "raw": json.loads(row["raw_json"] or "{}")} for row in rows]


class ViewShipments(SupplierShipmentsBlock):
    def get_shipment(self, shipment_id):
        cache = getattr(self, "_view_cards", None)
        if cache is None:
            cache = self._view_cards = {}
        if shipment_id not in cache:
            cache[shipment_id] = super().get_shipment(shipment_id)
        return deepcopy(cache[shipment_id])

    def migrate_existing_supplier_shipments_into_trade_documents(self):
        # Migration is a write operation, never a prerequisite of viewing.
        return {}

    def _sku_groups(self):
        return self.runtime.list_sku_groups(include_inactive=True)

    def _with_supplier_cost_summary(self, payload):
        proof = self.runtime.cost(str(payload.get("shipment_id") or ""))
        return {**payload, "supplier_cost_breakdown": proof, **supplier_cost_summary_fields(proof)}

    def list_shipments(self):
        return {
            "contract_name": "sheet_vitrina_v1_supplier_shipments", "status": "ok",
            "target_facility_options": self.runtime.list_active_ff_facilities(),
            "shipments": [{**self._with_approx_cost_fields(self._with_document_fields(_with_invoice_download_path(row))),
                           **supplier_cost_summary_fields(self.runtime.cost(row["shipment_id"])),
                           "expense_allocation": project_supplier_order_expense_allocation(self.runtime.cost(row["shipment_id"]))}
                          for row in self.runtime.list_supplier_shipments()],
        }


class ViewFinancialDocuments(SupplierFinancialDocumentsBlock):
    def __init__(self, runtime):
        # The legacy constructor expires previews (a write); the view must not.
        self.runtime = runtime

    def list_documents(self, supplier_order_id, *, refresh_saved_parses=False):
        return super().list_documents(supplier_order_id, refresh_saved_parses=False)

    def _with_canonical_exact_cost(self, shipment_id, summary):
        proof = self.runtime.cost(shipment_id)
        return {**summary, "per_unit": {**(summary.get("per_unit") or {}),
                **supplier_cost_summary_fields(proof), "supplier_cost_breakdown": proof},
                "supplier_cost_breakdown": proof}


class SupplierView(RegistryUploadHttpEntrypoint):
    def __init__(self, runtime_dir):
        # Intentionally do not initialize the broad live entrypoint: its
        # constructors bootstrap schemas and start unrelated subsystems.
        self.runtime = ViewRuntime(Path(runtime_dir))
        self.supplier_shipments_block = ViewShipments(runtime=self.runtime)
        self.supplier_financial_documents_block = ViewFinancialDocuments(self.runtime)
        cny = CnyLedgerBlock(runtime=self.runtime)
        self.cny_ledger_block = SimpleNamespace(get_status=lambda: {"documents": [cny._with_download_path(d) for d in self.runtime.list_cny_documents()]})

    @contextmanager
    def snapshot(self):
        with window_read_context(self.runtime.db_path) as context:
            connection = context.borrow(self.runtime.db_path)
            import time
            deadline = time.monotonic() + 5
            connection.execute("PRAGMA busy_timeout=1000")
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            yield self

    def correction(self, shipment_id):
        conn = borrowed_operational_connection(self.runtime.db_path)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (CORRECTION_TABLE,)).fetchone():
            return None
        # Do not even load report_json: it can contain megabytes of replay proof.
        columns = "correction_id,shipment_id,status,phase,progress_text,old_value,new_value,error_code,error_message,requested_at,updated_at"
        # Completion notifications need these two small fields, not replay rows.
        safe_report = "CASE WHEN json_valid(report_json) THEN report_json ELSE '{}' END"
        columns += f",json_extract({safe_report},'$.scope') AS scope_json,json_extract({safe_report},'$.business_projection') AS projection_json"
        row = conn.execute(f"SELECT {columns} FROM {CORRECTION_TABLE} WHERE shipment_id=? ORDER BY requested_at DESC,correction_id DESC LIMIT 1", (shipment_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        report = {"scope": json.loads(result.pop("scope_json") or "{}"),
                  "business_projection": json.loads(result.pop("projection_json") or "{}")}
        return {**result, "report": report, "active": result["status"] in ACTIVE_CORRECTION_STATUSES,
                "finished": result["status"] in FINAL_CORRECTION_STATUSES}

    def read(self, shipment_id="", section=""):
        self.runtime.shipment_id = shipment_id
        if not shipment_id:
            if section == "contracts":
                return {"documents": [self.supplier_shipments_block._with_document_download_path(d)
                    for d in self.runtime.list_trade_documents() if d.get("document_type") == "contract"]}
            return self.supplier_shipments_block.list_shipments()
        # Validate existence/archive status before any secondary read.
        if section == "correction":
            stored = self.runtime.load_supplier_shipment(shipment_id)
            if stored is None:
                raise ValueError("supplier shipment not found")
            _assert_supplier_shipment_active(stored, shipment_id=shipment_id)
            correction = self.correction(shipment_id)
            if correction is None:
                raise ValueError("supplier correction not found")
            return correction
        detail = self.supplier_shipments_block.get_shipment(shipment_id)
        if section == "documents":
            # Reuse the same detail/proof within this snapshot. No reparsing.
            result = self.handle_supplier_order_documents_list_request(shipment_id, refresh_saved_parses=False)
            result.pop("shipment", None)  # Already owned by the card in the UI.
            result["summary"].pop("supplier_cost_breakdown", None)
            return result
        detail["factual_date_correction"] = self.correction(shipment_id)
        detail["expense_allocation"] = project_supplier_order_expense_allocation(detail.get("supplier_cost_breakdown") or {})
        # The UI derives these arrays from lines. Preserve one full canonical
        # proof for its explainers and cost-edit validation.
        detail.pop("product_lines", None)
        detail.pop("extra_lines", None)
        return detail
