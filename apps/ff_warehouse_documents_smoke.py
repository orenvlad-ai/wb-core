"""Server-side FF business document projection and legacy compatibility smoke."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_stock_ledger import FfStockLedgerBlock  # noqa: E402
from packages.application.ff_warehouse_documents import FfWarehouseDocumentView  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.warehouse_functional import ensure_warehouse_functional_schema  # noqa: E402


NOW = "2026-08-02T09:00:00Z"


def main() -> None:
    with TemporaryDirectory(prefix="ff-warehouse-documents-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_business_documents(runtime)
        _seed_technical_documents(runtime)
        view = FfWarehouseDocumentView(db_path=runtime.db_path)

        default_page = view.page(limit=100)
        assert view.count_business_documents() == default_page["page"]["total_count"]
        labels = [item["document_type_label"] for item in default_page["documents"]]
        assert "Поступление на склад FF" in labels
        assert "Отгрузка FF → WB" in labels
        assert "Резервирование под поставку WB" in labels
        assert "Технический документ" not in labels
        receipt = next(item for item in default_page["documents"] if item["reason"] == "supplier_receipt")
        shipment = next(item for item in default_page["documents"] if item["reason"] == "wb_shipment")
        reservation = next(item for item in default_page["documents"] if item["effect"] == "reservation")
        assert receipt["warehouse_from_label"] == "Китай" and receipt["warehouse_to_label"] == "Склад FF"
        assert receipt["transfer_identity"] == "warehouse-transfer:supplier_shipment:shipment-cn-42"
        assert shipment["warehouse_from_label"] == "Склад FF" and shipment["warehouse_to_label"] == "Поставка WB"
        assert shipment["transfer_identity"] == "warehouse-transfer:wb_supply:supply-wb-77"
        assert reservation["total_quantity"] == "0" and reservation["status_label"] == "Не меняет физический остаток"
        assert sum(1 for item in default_page["documents"] if item["reason"] == "supplier_receipt") == 1

        incoming = view.page(effect="incoming", limit=100)
        assert incoming["page"]["total_count"] >= 1
        assert all(item["effect"] == "incoming" for item in incoming["documents"])
        outgoing = view.page(effect="outgoing", limit=100)
        assert all(item["effect"] == "outgoing" for item in outgoing["documents"])
        reason = view.page(reason="supplier_receipt", limit=100)
        assert reason["page"]["total_count"] == 1
        searched_supply = view.page(search="supply-wb-77", limit=100)
        assert searched_supply["page"]["total_count"] == 2  # physical shipment + reservation
        searched_invoice = view.page(search="INV-CN-42", limit=100)
        assert searched_invoice["page"]["total_count"] == 1
        searched_nm = view.page(search="101", limit=100)
        assert searched_nm["page"]["total_count"] >= 3
        date_page = view.page(
            business_date_from="2026-08-02",
            business_date_to="2026-08-02",
            limit=100,
        )
        assert date_page["documents"] and all(item["business_date"] == "2026-08-02" for item in date_page["documents"])

        first = view.page(page=1, limit=10)
        second = view.page(page=2, limit=10)
        assert first["page"]["total_count"] == default_page["page"]["total_count"]
        assert first["page"]["page_count"] >= 3 and first["page"]["has_next"] is True
        assert {item["document_id"] for item in first["documents"]}.isdisjoint(
            {item["document_id"] for item in second["documents"]}
        )

        technical = view.page(include_technical=True, limit=100)
        technical_rows = [item for item in technical["documents"] if item["reason"] == "technical"]
        assert len(technical_rows) == 2
        assert technical["hidden_technical_count"] == 2
        assert sum(1 for item in technical["documents"] if item["reason"] == "supplier_receipt") == 1

        detail = view.detail(receipt["document_id"])["document"]
        assert detail["created_at"] and detail["actor"] == "supplier-sync"
        assert detail["lines"][0]["sku"] == "SKU-101"
        assert detail["lines"][0]["average_unit_cost_rub"] == "100"

        legacy = FfStockLedgerBlock(runtime=runtime).get_status(
            operations_limit=100,
            show_technical_archive=True,
        )
        assert legacy["status"] == "ok"
        legacy_ids = {item["operation_id"] for item in legacy["operations"]}
        assert {"ffso-receipt-cn-42", "ffso-shipment-wb-77"}.issubset(legacy_ids)

    print("ff_warehouse_documents_smoke: OK")


def _seed_business_documents(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.create_ff_stock_operation(
        operation_id="ffso-receipt-cn-42",
        operation_type="auto_receipt",
        source_type="supplier_shipment",
        source_key="supplier_shipment:shipment-cn-42:acceptance",
        source_object_id="shipment-cn-42",
        source_object_label="Invoice INV-CN-42",
        created_at="2026-08-01T08:00:00Z",
        business_effective_date="2026-08-01",
        created_by="supplier-sync",
        warnings=[],
        diagnostics={"invoice_no": "INV-CN-42"},
        lines=[
            {
                "nm_id": 101,
                "barcode": "460101",
                "sku": "SKU-101",
                "nomenclature_name": "Товар 101",
                "quantity_delta": 10,
                "raw": {"cost_snapshot": {"unit_cost_rub": "100", "capital_delta_rub": "1000", "quality": "certified_supplier_landed", "provenance": {"invoice": "INV-CN-42"}}},
            }
        ],
    )
    runtime.create_ff_stock_operation(
        operation_id="ffso-shipment-wb-77",
        operation_type="auto_writeoff",
        source_type="wb_supply",
        source_key="wb_supply_debit:supply:supply-wb-77",
        source_object_id="supply-wb-77",
        source_object_label="WB-поставка supply-wb-77",
        created_at="2026-08-02T08:00:00Z",
        business_effective_date="2026-08-02",
        created_by="wb-sync",
        warnings=[],
        diagnostics={"supply_id": "supply-wb-77"},
        lines=[
            {
                "nm_id": 101,
                "barcode": "460101",
                "sku": "SKU-101",
                "nomenclature_name": "Товар 101",
                "quantity_delta": -2,
                "raw": {"cost_snapshot": {"unit_cost_rub": "100", "capital_delta_rub": "-200", "quality": "exact_original_ff_debit", "provenance": {"supply_id": "supply-wb-77"}}},
            }
        ],
    )
    runtime.create_ff_stock_reservation_operation(
        operation_id="ffsr-supply-wb-77-reserve",
        source_key="wb_supply_reservation:supply-wb-77:reserve:r1",
        supply_id="supply-wb-77",
        supply_revision="sha256:supply-wb-77-r1",
        operation_type="reserve",
        created_at="2026-08-02T07:00:00Z",
        diagnostics={"source": "fixture"},
        lines=[{"nm_id": 101, "quantity_delta": 2}],
        expected_current={},
    )
    for index in range(28):
        runtime.create_ff_stock_operation(
            operation_id=f"ffso-manual-{index:02d}",
            operation_type="manual_receipt",
            source_type="manual_excel",
            source_key=f"manual-fixture:{index:02d}",
            source_object_id=f"manual-{index:02d}",
            source_object_label=f"Ручной документ {index:02d}",
            created_at=f"2026-07-{index + 1:02d}T06:00:00Z",
            business_effective_date=f"2026-07-{index + 1:02d}",
            created_by="manual-smoke",
            warnings=[],
            diagnostics={},
            lines=[{"nm_id": 200 + index, "sku": f"SKU-{200 + index}", "quantity_delta": 1}],
        )


def _seed_technical_documents(runtime: RegistryUploadDbBackedRuntime) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        for index in (1, 2):
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_warehouse_functional_documents(
                    document_id,version_id,warehouse_key,document_type,occurred_at,
                    source_id,source_fingerprint,quantity,capital_rub,provenance_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"technical-sync-{index}",
                    f"sync-version-{index}",
                    "ff",
                    "warehouse_sync",
                    "2026-08-02",
                    "same-ledger-source",
                    f"sha256:sync-{index}",
                    "8",
                    "800",
                    "{}",
                    f"2026-08-02T0{index}:00:00Z",
                ),
            )
        conn.commit()


if __name__ == "__main__":
    main()
