#!/usr/bin/env python3
"""Contract smoke for warehouse source rules and atomic opening cutover."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.ff_stock_ledger import FfStockLedgerBlock  # noqa: E402
from packages.application.stocks_block import StocksBlock  # noqa: E402
from packages.application.warehouse_stocks import (  # noqa: E402
    OPENING_CUTOVER_ID,
    WarehouseOpeningSnapshotError,
    WarehouseStocksBlock,
)


NOW = "2026-07-18T08:00:00Z"
SNAPSHOT_DATE = "2026-07-18"


class _FakeStocksSource:
    def __init__(self, quantities: dict[int, int] | None = None) -> None:
        self.quantities = quantities or {101: 12, 102: 0, 103: 3}

    def fetch(self, request):
        return {
            "snapshot_date": SNAPSHOT_DATE,
            "requested_nm_ids": list(request.nm_ids),
            "data": {
                "requested_snapshot_date": request.snapshot_date,
                "fetched_at": NOW,
                "rows": [
                    {
                        "snapshot_date": SNAPSHOT_DATE,
                        "snapshot_ts": "2026-07-18 08:00:00",
                        "nmId": nm_id,
                        "stockCount": self.quantities[nm_id],
                        "warehouseName": "Коледино",
                        "regionName": "Центральный",
                    }
                    for nm_id in request.nm_ids
                ],
            },
        }


class _FailingStocksSource:
    def fetch(self, request):
        raise RuntimeError("injected official WB stock source failure")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="warehouse-stocks-smoke-") as temp_dir:
        root = Path(temp_dir)
        runtime = _seed_runtime(root / "runtime")
        block = _block(runtime)
        plan = block.build_opening_plan()
        _assert(plan["status"] == "dry_run_ready", "dry-run status")
        _assert(plan["cutover_id"] == OPENING_CUTOVER_ID, "stable cutover id")
        _assert(len(plan["documents"]) == 6, "exactly six opening documents")
        expected = {
            "production": (2, 15),
            "china_to_ff": (1, 7),
            "ff": (2, 21),
            "ff_to_wb": (3, 9),
            "wb": (2, 15),
            "wb_acceptance_discrepancy": (0, 0),
        }
        for document in plan["documents"]:
            key = document["warehouse_key"]
            _assert((document["sku_count"], int(document["total_quantity"])) == expected[key], key)
            _assert(document["average_unit_cost_rub"] is None, "document unit cost NULL")
            _assert(document["total_capital_rub"] is None, "document capital NULL")
            _assert(all(line["average_unit_cost_rub"] is None for line in document["lines"]), "line cost NULL")
            _assert(all(line["provenance"]["source_records"] for line in document["lines"]), "line provenance")
        production = _document(plan, "production")
        _assert(sum(int(line["quantity"]) for line in production["lines"]) == 15, "first payment activates full invoice")
        _assert("shipment-paid" in json.dumps(production, ensure_ascii=False), "paid invoice included")
        _assert("shipment-unpaid" not in json.dumps(production), "unpaid invoice excluded")
        _assert("shipment-shipped" not in json.dumps(production), "shipped invoice excluded from production")
        _assert("shipment-cancelled" not in json.dumps(production), "inactive invoice excluded")
        for source_key in ("production", "china_to_ff"):
            for line in _document(plan, source_key)["lines"]:
                contribution = sum(
                    int(item["line_quantity"])
                    for item in line["provenance"]["source_records"]
                )
                _assert(contribution == int(line["quantity"]), f"{source_key} provenance quantity sum")
        transit_wb = _document(plan, "ff_to_wb")
        _assert("wb-planned" not in json.dumps(transit_wb), "planned WB supply excluded")
        _assert("wb-transit-3" in json.dumps(transit_wb), "shipment-allowed WB supply included")
        _assert("wb-transit-4" in json.dumps(transit_wb), "WB receiving status remains post-gate transit")
        _assert("wb-transit-6" in json.dumps(transit_wb), "WB gate-shipped status remains post-gate transit")
        _assert("wb-accepted" not in json.dumps(transit_wb), "accepted WB supply excluded")
        for line in transit_wb["lines"]:
            contribution = sum(
                int(item["sent_quantity"])
                for item in line["provenance"]["source_records"]
            )
            _assert(contribution == int(line["quantity"]), "FF to WB provenance quantity sum")
        discrepancy = _document(plan, "wb_acceptance_discrepancy")
        _assert(
            discrepancy["lines"] == []
            and discrepancy["average_unit_cost_rub"] is None
            and discrepancy["total_capital_rub"] is None,
            "discrepancy opening is a zero quantity-only document",
        )
        _assert(
            discrepancy["provenance"]
            == {
                "basis_type": "management_warehouse_accounting_boundary",
                "opening_policy": "zero_at_cutover",
                "cutover_at": NOW,
                "algorithm_version": "warehouse_opening_v2_zero_discrepancy",
                "historical_backfill": False,
                "historical_acceptance_reconstruction": False,
                "historical_wb_acceptance_evaluated": False,
                "cost_defined": False,
                "capital_defined": False,
            },
            "discrepancy document policy provenance",
        )
        repeated_plan = block.build_opening_plan()
        _assert(repeated_plan["plan_fingerprint"] == plan["plan_fingerprint"], "repeated dry-run fingerprint")
        _assert(repeated_plan["documents"] == plan["documents"], "repeated dry-run documents")
        canonical_ff = {
            int(item["nm_id"]): int(item["quantity"])
            for item in FfStockLedgerBlock(runtime=runtime).current_balance_rows()
            if int(item["quantity"]) != 0
        }
        opening_ff = {int(item["nm_id"]): int(item["quantity"]) for item in _document(plan, "ff")["lines"]}
        _assert(opening_ff == canonical_ff, "opening FF reuses canonical ledger quantities")

        fingerprint = plan["plan_fingerprint"]
        applied = block.apply_opening_plan(
            plan,
            confirm_fingerprint=fingerprint,
            backup_dir=root / "backups-main",
        )
        _assert(applied["status"] == "ready", "apply ready")
        _assert(applied["reconciliation"]["document_count"] == 6, "six stored documents")
        _assert(applied["reconciliation"]["all_costs_null"], "stored costs NULL")
        _assert(applied["reconciliation"]["document_line_balance_equal"], "document balances equal")
        applied_discrepancy = _document(applied, "wb_acceptance_discrepancy")
        _assert(applied_discrepancy["lines"] == [], "stored discrepancy has no SKU lines")
        _assert(applied_discrepancy["provenance"] == discrepancy["provenance"], "stored document provenance")
        again = block.apply_opening_plan(
            plan,
            confirm_fingerprint=fingerprint,
            backup_dir=root / "backups-main",
        )
        _assert(again["idempotent"] is True, "second apply idempotent")
        with sqlite3.connect(runtime.db_path) as conn:
            _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_cutovers").fetchone()[0] == 1, "one cutover")
            _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_documents").fetchone()[0] == 6, "six documents")
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_ff_stock_operations(
                       operation_id,operation_type,source_type,source_key,created_at,sku_count,
                       total_quantity_delta,total_quantity_abs,diagnostics_json
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("ff-after-cutover", "manual_writeoff", "fixture", "fixture:ff-after-cutover", "2026-07-18T09:00:00Z", 1, -30, 30, "{}"),
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
                       operation_id,line_no,nm_id,barcode,sku,nomenclature_name,quantity_delta,raw_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("ff-after-cutover", 1, 101, "BC-101", "SKU-101", "Товар 101", -30, "{}"),
            )
            conn.commit()
        current_ff_detail = block.warehouse_detail("ff")
        current_ff = {
            int(item["nm_id"]): int(item["quantity"])
            for item in current_ff_detail["balances"]
        }
        _assert(current_ff == {101: -13, 102: 4}, "FF screen follows current canonical ledger after cutover")
        _assert(
            {int(item["nm_id"]): int(item["quantity"]) for item in current_ff_detail["documents"][0]["lines"]}
            == {101: 17, 102: 4},
            "FF opening document remains immutable after later ledger operations",
        )
        negative_ff_row = next(item for item in current_ff_detail["balances"] if int(item["nm_id"]) == 101)
        _assert(negative_ff_row["negative_balance"] is True, "FF negative balance flag preserved")
        _assert(negative_ff_row["warning"] == "Отрицательный остаток ФФ", "FF negative warning preserved")
        _assert(
            sum(int(item["quantity_delta"]) for item in negative_ff_row["provenance"]["source_records"]) == -13,
            "current FF provenance reconciles to current quantity",
        )
        ff_overview = next(item for item in block.overview()["warehouses"] if item["warehouse_key"] == "ff")
        _assert((ff_overview["sku_count"], int(ff_overview["total_quantity"])) == (2, -9), "FF overview is current")
        _assert(ff_overview["balance_mode"] == "current_canonical_ff_ledger", "FF current balance mode")

        rollback = block.rollback_opening_cutover(
            confirm_fingerprint=fingerprint,
            backup_dir=root / "backups-rollback",
        )
        _assert(rollback["status"] == "rolled_back", "bounded rollback")
        _assert(block.readback()["status"] == "not_initialized", "rollback readback")

        failure_runtime = _seed_runtime(root / "runtime-failure")
        failure_block = _block(failure_runtime)
        failure_plan = failure_block.build_opening_plan()
        try:
            failure_block.apply_opening_plan(
                failure_plan,
                confirm_fingerprint=failure_plan["plan_fingerprint"],
                backup_dir=root / "backups-failure",
                fail_after_documents=3,
            )
            raise AssertionError("injected apply failure did not fail")
        except RuntimeError as exc:
            _assert("injected" in str(exc), "expected injected failure")
        with sqlite3.connect(failure_runtime.db_path) as conn:
            _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_cutovers").fetchone()[0] == 0, "cutover rolled back")
            _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_documents").fetchone()[0] == 0, "documents rolled back")
        resumed = failure_block.apply_opening_plan(
            failure_plan,
            confirm_fingerprint=failure_plan["plan_fingerprint"],
            backup_dir=root / "backups-failure-resume",
        )
        _assert(resumed["status"] == "ready" and resumed["idempotent"] is False, "safe resume after partial failure")

        changed_runtime = _seed_runtime(root / "runtime-source-changed")
        changed_block = _block(changed_runtime)
        changed_plan = changed_block.build_opening_plan()
        with sqlite3.connect(changed_runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_ff_stock_operation_lines SET quantity_delta=quantity_delta+1 WHERE operation_id='ff-op' AND line_no=1"
            )
            conn.commit()
        try:
            changed_block.apply_opening_plan(
                changed_plan,
                confirm_fingerprint=changed_plan["plan_fingerprint"],
                backup_dir=root / "backups-source-changed",
            )
            raise AssertionError("changed source snapshot did not fail")
        except WarehouseOpeningSnapshotError as exc:
            _assert("source snapshot changed" in str(exc), "source drift diagnostic")

        locked_drift_runtime = _seed_runtime(root / "runtime-source-changed-after-backup")
        locked_drift_block = _block(locked_drift_runtime)
        locked_drift_plan = locked_drift_block.build_opening_plan()
        original_backup = locked_drift_block._backup_before_mutation

        def _backup_then_change(backup_dir: Path, *, purpose: str):
            backup_result = original_backup(backup_dir, purpose=purpose)
            with sqlite3.connect(locked_drift_runtime.db_path) as conn:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_ff_stock_operation_lines SET quantity_delta=quantity_delta+1 WHERE operation_id='ff-op' AND line_no=1"
                )
                conn.commit()
            return backup_result

        locked_drift_block._backup_before_mutation = _backup_then_change  # type: ignore[method-assign]
        try:
            locked_drift_block.apply_opening_plan(
                locked_drift_plan,
                confirm_fingerprint=locked_drift_plan["plan_fingerprint"],
                backup_dir=root / "backups-source-changed-after-backup",
            )
            raise AssertionError("source change after backup did not fail under apply lock")
        except WarehouseOpeningSnapshotError as exc:
            _assert("acquiring the apply lock" in str(exc), "locked source drift diagnostic")
        _assert(locked_drift_block.readback()["status"] == "not_initialized", "locked drift has no partial cutover")

        corrupt_runtime = _seed_runtime(root / "runtime-corrupt-readback")
        corrupt_block = _block(corrupt_runtime)
        corrupt_plan = corrupt_block.build_opening_plan()
        corrupt_block.apply_opening_plan(
            corrupt_plan,
            confirm_fingerprint=corrupt_plan["plan_fingerprint"],
            backup_dir=root / "backups-corrupt-readback",
        )
        with sqlite3.connect(corrupt_runtime.db_path) as conn:
            conn.execute(
                "DELETE FROM sheet_vitrina_v1_warehouse_documents WHERE document_id='whdoc_opening_v1_wb'"
            )
            conn.commit()
        try:
            corrupt_block.readback()
            raise AssertionError("partial stored opening state was accepted")
        except WarehouseOpeningSnapshotError:
            pass
        try:
            corrupt_block.apply_opening_plan(
                corrupt_plan,
                confirm_fingerprint=corrupt_plan["plan_fingerprint"],
                backup_dir=root / "backups-corrupt-readback",
            )
            raise AssertionError("partial stored opening state was treated as idempotent")
        except WarehouseOpeningSnapshotError:
            pass

        zero_runtime = _seed_runtime(root / "runtime-zero")
        with sqlite3.connect(zero_runtime.db_path) as conn:
            conn.execute("DELETE FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id='shipment-shipped'")
            conn.commit()
        zero_plan = _block(zero_runtime).build_opening_plan()
        zero_document = _document(zero_plan, "china_to_ff")
        _assert(zero_document["sku_count"] == 0 and zero_document["total_quantity"] == "0", "zero warehouse document")

        negative_runtime = _seed_runtime(root / "runtime-negative", doprinato_quantity=4)
        negative_diagnostic = _block(negative_runtime).diagnose_wb_acceptance_discrepancy(
            nm_ids=[101]
        )
        _assert(negative_diagnostic["status"] == "diagnostic", "bounded discrepancy diagnostic status")
        _assert(negative_diagnostic["negative_count"] == 1, "bounded diagnostic finds negative SKU")
        diagnostic_row = negative_diagnostic["rows"][0]
        _assert(
            (
                diagnostic_row["sent_quantity"],
                diagnostic_row["accepted_quantity"],
                diagnostic_row["doprinato_quantity"],
                diagnostic_row["discrepancy_quantity"],
            )
            == (10, 8, 4, -2),
            "bounded diagnostic uses exact discrepancy arithmetic",
        )
        _assert(len(diagnostic_row["source_records"]) == 2, "bounded diagnostic has both source records")
        diagnostic_records = {
            item["role"]: item for item in diagnostic_row["source_records"]
        }
        _assert(
            (
                diagnostic_records["ordinary_final_acceptance"]["sent_quantity"],
                diagnostic_records["ordinary_final_acceptance"]["accepted_quantity"],
                diagnostic_records["ordinary_final_acceptance"]["doprinato_quantity"],
                diagnostic_records["ordinary_final_acceptance"]["discrepancy_contribution"],
            )
            == ("10", "8", "0", "2"),
            "bounded diagnostic ordinary row arithmetic",
        )
        _assert(
            (
                diagnostic_records["doprinato"]["sent_quantity"],
                diagnostic_records["doprinato"]["accepted_quantity"],
                diagnostic_records["doprinato"]["doprinato_quantity"],
                diagnostic_records["doprinato"]["discrepancy_contribution"],
            )
            == ("0", "0", "4", "-4"),
            "bounded diagnostic doprinato row arithmetic",
        )
        negative_opening = _block(negative_runtime).build_opening_plan()
        _assert(
            _document(negative_opening, "wb_acceptance_discrepancy")["total_quantity"] == "0",
            "negative historical diagnostic does not affect opening",
        )

        missing_doprinato_runtime = _seed_runtime(root / "runtime-doprinato-missing")
        with sqlite3.connect(missing_doprinato_runtime.db_path) as conn:
            raw_goods = json.loads(
                conn.execute(
                    "SELECT raw_goods_json FROM sheet_vitrina_v1_wb_supplies WHERE supply_id='wb-doprinato'"
                ).fetchone()[0]
            )
            raw_goods[0].pop("acceptedQuantity", None)
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=?,raw_goods_hash=? WHERE supply_id='wb-doprinato'",
                (json.dumps(raw_goods, ensure_ascii=False), "hash:wb-doprinato:missing-accepted"),
            )
            conn.commit()
        missing_doprinato_plan = _block(missing_doprinato_runtime).build_opening_plan()
        _assert(
            int(_document(missing_doprinato_plan, "wb_acceptance_discrepancy")["total_quantity"]) == 0,
            "historical doprinato evidence is not evaluated by opening",
        )

        historical_116_runtime = _seed_runtime(root / "runtime-historical-116", doprinato_quantity=116)
        historical_116_plan = _block(historical_116_runtime).build_opening_plan()
        _assert(
            _document(historical_116_plan, "wb_acceptance_discrepancy")["lines"] == [],
            "historical doprinato 116 does not enter opening",
        )

        no_ordinary_runtime = _seed_runtime(root / "runtime-no-ordinary-acceptance", doprinato_quantity=116)
        with sqlite3.connect(no_ordinary_runtime.db_path) as conn:
            conn.execute("DELETE FROM sheet_vitrina_v1_wb_supplies WHERE supply_id='wb-accepted'")
            conn.commit()
        no_ordinary_plan = _block(no_ordinary_runtime).build_opening_plan()
        _assert(
            _document(no_ordinary_plan, "wb_acceptance_discrepancy")["total_quantity"] == "0",
            "missing ordinary acceptance does not block opening",
        )

        fingerprint_base_runtime = _seed_runtime(root / "runtime-fingerprint-base")
        fingerprint_base = _block(fingerprint_base_runtime).build_opening_plan()
        fingerprint_history_runtime = _seed_runtime(root / "runtime-fingerprint-history")
        with sqlite3.connect(fingerprint_history_runtime.db_path) as conn:
            historical_supply_ids = ("15210560", "15263901", "15351678", "15471955")
            historical_quantities = (10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 6)
            historical_rows = [
                _wb_row(
                    supply_id,
                    5,
                    [
                        {"nmID": 180330785, "quantity": quantity, "acceptedQuantity": quantity}
                        for quantity in historical_quantities[index * 3 : (index + 1) * 3]
                    ],
                    doprinato=True,
                )
                for index, supply_id in enumerate(historical_supply_ids)
            ]
            conn.executemany(
                """INSERT INTO sheet_vitrina_v1_wb_supplies(
                       supply_id,cache_key,wb_supply_id,preorder_id,normalized_row_json,
                       raw_goods_json,raw_goods_hash,status_id,source_created_at,supply_date,
                       updated_date,synced_at,last_list_synced_at,last_enriched_at,enrichment_status
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                historical_rows,
            )
            conn.execute(
                """UPDATE sheet_vitrina_v1_wb_supplies
                   SET normalized_row_json='{"status_id":"malformed-history"}'
                   WHERE supply_id='15210560'"""
            )
            conn.execute(
                """UPDATE sheet_vitrina_v1_wb_supplies_sync_state
                   SET last_synced_at='2026-07-18T09:00:00Z',
                       last_successful_sync_at='2026-07-18T09:00:00Z',
                       last_error='history-only refresh',
                       latest_synced_count=latest_synced_count+4,
                       backfill_complete=0,
                       latest_window_synced_at='2026-07-18T09:00:00Z',
                       last_mode='history-only'
                   WHERE slot=1"""
            )
            conn.commit()
        fingerprint_history = _block(fingerprint_history_runtime).build_opening_plan()
        _assert(
            fingerprint_history["plan_fingerprint"] == fingerprint_base["plan_fingerprint"],
            "historical 12 WB lines / 116 units do not enter opening fingerprint",
        )

        fingerprint_material_runtime = _seed_runtime(root / "runtime-fingerprint-material")
        with sqlite3.connect(fingerprint_material_runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_wb_supplies
                   SET raw_goods_json='[{"nmID":101,"quantity":7,"acceptedQuantity":0}]',
                       raw_goods_hash='hash:wb-transit-3:changed'
                   WHERE supply_id='wb-transit-3'"""
            )
            conn.commit()
        fingerprint_material = _block(fingerprint_material_runtime).build_opening_plan()
        _assert(
            fingerprint_material["plan_fingerprint"] != fingerprint_base["plan_fingerprint"],
            "material status-3 WB row changes opening fingerprint",
        )

        negative_wb_runtime = _seed_runtime(root / "runtime-negative-material")
        try:
            _block(
                negative_wb_runtime,
                stocks_source=_FakeStocksSource({101: -1, 102: 0, 103: 3}),
            ).build_opening_plan()
            raise AssertionError("negative material WB stock did not fail closed")
        except WarehouseOpeningSnapshotError as exc:
            _assert("negative current stock" in str(exc), "negative material source blocks cutover")

        failed_source_runtime = _seed_runtime(root / "runtime-failed-source")
        try:
            _block(failed_source_runtime, stocks_source=_FailingStocksSource()).build_opening_plan()
            raise AssertionError("real source failure did not fail dry-run")
        except RuntimeError as exc:
            _assert("official WB stock source failure" in str(exc), "real source error surfaced")
        _assert(
            len(failed_source_runtime.list_nomenclature_items(active_only=False)) == 3,
            "non-target runtime remains readable",
        )
        _assert(
            WarehouseStocksBlock(
                runtime=failed_source_runtime,
                stocks_block=StocksBlock(_FakeStocksSource()),
                timestamp_factory=lambda: NOW,
                now_factory=lambda: datetime(2026, 7, 18, 8, tzinfo=timezone.utc),
                wb_nomenclature_provider=lambda: failed_source_runtime.list_nomenclature_items(active_only=True),
            ).readback()["status"]
            == "not_initialized",
            "real source failure creates no partial opening",
        )

        schema_upgrade_runtime = _seed_runtime(root / "runtime-schema-upgrade")
        schema_upgrade_block = _block(schema_upgrade_runtime)
        _assert(schema_upgrade_block.readback()["status"] == "not_initialized", "warehouse schema created")
        with sqlite3.connect(schema_upgrade_runtime.db_path) as conn:
            conn.execute(
                "ALTER TABLE sheet_vitrina_v1_warehouse_documents DROP COLUMN provenance_json"
            )
            conn.commit()
        _assert(schema_upgrade_block.readback()["status"] == "not_initialized", "warehouse schema upgraded")
        with sqlite3.connect(schema_upgrade_runtime.db_path) as conn:
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(sheet_vitrina_v1_warehouse_documents)"
                ).fetchall()
            }
        _assert("provenance_json" in columns, "document provenance schema upgrade")

        invalid_fact_runtime = _seed_runtime(root / "runtime-invalid-supplier-fact")
        with sqlite3.connect(invalid_fact_runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_shipments SET actual_shipment_date='2026-07-25' WHERE shipment_id='shipment-shipped'"
            )
            conn.commit()
        try:
            _block(invalid_fact_runtime).build_opening_plan()
            raise AssertionError("future supplier shipment fact did not fail closed")
        except WarehouseOpeningSnapshotError as exc:
            _assert("non-occurred/invalid actual_shipment_date" in str(exc), "invalid supplier fact diagnostic")

        unmatched_line_runtime = _seed_runtime(root / "runtime-unmatched-supplier-line")
        with sqlite3.connect(unmatched_line_runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_supplier_shipment_lines
                   SET match_status='unmatched'
                   WHERE shipment_id='shipment-paid' AND internal_nm_id=101"""
            )
            conn.commit()
        try:
            _block(unmatched_line_runtime).build_opening_plan()
            raise AssertionError("unmatched supplier product line with stale nmID did not fail closed")
        except WarehouseOpeningSnapshotError as exc:
            _assert("untraceable product line" in str(exc), "supplier line match-status diagnostic")

    print("warehouse stocks smoke: ok")


def _block(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    stocks_source: _FakeStocksSource | _FailingStocksSource | None = None,
) -> WarehouseStocksBlock:
    return WarehouseStocksBlock(
        runtime=runtime,
        stocks_block=StocksBlock(stocks_source or _FakeStocksSource()),
        now_factory=lambda: datetime(2026, 7, 18, 8, tzinfo=timezone.utc),
        timestamp_factory=lambda: NOW,
        wb_nomenclature_provider=lambda: runtime.list_nomenclature_items(active_only=True),
    )


def _seed_runtime(runtime_dir: Path, *, doprinato_quantity: int = 1) -> RegistryUploadDbBackedRuntime:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    runtime.list_nomenclature_items(active_only=False)
    for nm_id, hidden in ((101, False), (102, False), (103, True)):
        runtime.save_nomenclature_item(
            {
                "item_id": f"item-{nm_id}",
                "is_active": True,
                "is_hidden": hidden,
                "our_sku": f"SKU-{nm_id}",
                "nm_id": nm_id,
                "barcode": f"4600000000{nm_id}",
                "nomenclature_name": f"Товар {nm_id}",
                "product_type": "fixture",
                "match_key": f"sku-{nm_id}",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
    with sqlite3.connect(runtime.db_path) as conn:
        _seed_supplier(conn)
        _seed_ff(conn)
        _seed_wb(conn, doprinato_quantity=doprinato_quantity)
        conn.commit()
    return runtime


def _seed_supplier(conn: sqlite3.Connection) -> None:
    shipments = [
        ("shipment-paid", "", "", "production", 0),
        ("shipment-unpaid", "", "", "production", 0),
        ("shipment-shipped", "2026-07-10", "", "production", 0),
        ("shipment-accepted", "2026-07-09", "2026-07-12", "production", 0),
        ("shipment-cancelled", "", "", "cancelled", 0),
    ]
    conn.executemany(
        """INSERT INTO sheet_vitrina_v1_supplier_shipments(
               shipment_id,created_at,updated_at,shipment_date,actual_shipment_date,
               actual_ff_acceptance_date,historical_status_exception,order_status,
               invoice_no,match_status,warnings_json,errors_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (sid, NOW, NOW, "2026-07-01", shipped or None, accepted or None, "", status, f"INV-{sid}", "matched", "[]", "[]")
            for sid, shipped, accepted, status, _ in shipments
        ],
    )
    lines = [
        ("shipment-paid", 101, 10), ("shipment-paid", 102, 5),
        ("shipment-unpaid", 101, 99), ("shipment-shipped", 101, 7),
        ("shipment-accepted", 101, 30), ("shipment-cancelled", 101, 40),
    ]
    conn.executemany(
        """INSERT INTO sheet_vitrina_v1_supplier_shipment_lines(
               line_id,shipment_id,line_type,sort_order,source_no,barcode,internal_sku,
               internal_nm_id,internal_name,qty,match_status,manual_override,raw_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (f"line-{sid}-{index}", sid, "product", index, str(index), f"BC-{nm}", f"SKU-{nm}", nm, f"Товар {nm}", qty, "matched", 0, "{}")
            for index, (sid, nm, qty) in enumerate(lines, start=1)
        ],
    )
    paid = ["shipment-paid", "shipment-shipped", "shipment-cancelled"]
    conn.executemany(
        """INSERT INTO sheet_vitrina_v1_cny_documents(
               document_id,document_type,source,source_order_id,file_sha256,natural_key,
               uploaded_at,created_at,updated_at,operation_date,operation_datetime,status,
               document_number,cny_amount,parsed_payload_json,raw_parse_json,warnings_json,errors_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (f"pay-{sid}", "supplier_cny_payment", "fixture", sid, f"hash-{sid}", f"natural-{sid}", NOW, NOW, NOW, "2026-07-02", NOW, "posted", f"PAY-{sid}", "1", "{}", "{}", "[]", "[]")
            for sid in paid
        ],
    )


def _seed_ff(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_ff_stock_operations(
               operation_id,operation_type,source_type,source_key,created_at,sku_count,
               total_quantity_delta,total_quantity_abs,diagnostics_json
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        ("ff-op", "receipt", "fixture", "fixture:ff-op", NOW, 3, 71, 77, "{}"),
    )
    conn.executemany(
        """INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
               operation_id,line_no,nm_id,barcode,sku,nomenclature_name,quantity_delta,raw_json
           ) VALUES(?,?,?,?,?,?,?,?)""",
        [
            ("ff-op", 1, 101, "BC-101", "SKU-101", "Товар 101", 20, "{}"),
            ("ff-op", 2, 101, "BC-101", "SKU-101", "Товар 101", -3, "{}"),
            ("ff-op", 3, 102, "BC-102", "SKU-102", "Товар 102", 4, "{}"),
            ("ff-op", 4, 103, "BC-103", "SKU-103", "Скрытый товар 103", 50, "{}"),
        ],
    )


def _seed_wb(conn: sqlite3.Connection, *, doprinato_quantity: int) -> None:
    rows = [
        _wb_row("wb-transit-3", 3, [{"nmID": 101, "quantity": 6, "acceptedQuantity": 0}]),
        _wb_row("wb-transit-4", 4, [{"nmID": 102, "quantity": 2, "acceptedQuantity": 0}]),
        _wb_row("wb-transit-6", 6, [{"nmID": 103, "quantity": 1, "acceptedQuantity": 0}]),
        _wb_row("wb-planned", 2, [{"nmID": 101, "quantity": 50, "acceptedQuantity": 0}]),
        _wb_row("wb-accepted", 5, [{"nmID": 101, "quantity": 10, "acceptedQuantity": 8}, {"nmID": 102, "quantity": 5, "acceptedQuantity": 4}]),
        _wb_row("wb-doprinato", 5, [{"nmID": 101, "quantity": doprinato_quantity, "acceptedQuantity": 0}], doprinato=True),
    ]
    conn.executemany(
        """INSERT INTO sheet_vitrina_v1_wb_supplies(
               supply_id,cache_key,wb_supply_id,preorder_id,normalized_row_json,
               raw_goods_json,raw_goods_hash,status_id,source_created_at,supply_date,
               updated_date,synced_at,last_list_synced_at,last_enriched_at,enrichment_status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.execute(
        """INSERT OR REPLACE INTO sheet_vitrina_v1_wb_supplies_sync_state(
               slot,last_synced_at,last_successful_sync_at,latest_synced_count,
               backfill_complete,latest_window_synced_at,last_mode
           ) VALUES(1,?,?,?,?,?,?)""",
        (NOW, NOW, len(rows), 1, NOW, "fixture"),
    )


def _wb_row(supply_id: str, status_id: int, goods: list[dict], *, doprinato: bool = False) -> tuple:
    status_labels = {
        2: "Запланировано",
        3: "Отгрузка разрешена",
        4: "Идёт приёмка",
        5: "Принято",
        6: "Отгружено на воротах",
    }
    normalized = {
        "supply_id": supply_id,
        "wb_supply_id": supply_id,
        "status_id": status_id,
        "status_label": status_labels[status_id],
        "virtual_type_id": 5 if doprinato else 0,
        "type_label": "Допринято" if doprinato else "Обычная",
    }
    return (
        supply_id, f"cache:{supply_id}", supply_id, "", json.dumps(normalized, ensure_ascii=False),
        json.dumps(goods, ensure_ascii=False), f"hash:{supply_id}", status_id, NOW, "2026-07-15",
        NOW, NOW, NOW, NOW, "ready",
    )


def _document(plan: dict, key: str) -> dict:
    return next(item for item in plan["documents"] if item["warehouse_key"] == key)


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    main()
