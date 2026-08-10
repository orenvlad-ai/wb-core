"""Deterministic dry-run/apply/readback/rollback checks for FF inventory."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_inventory_reconciliation import (  # noqa: E402
    FfInventoryReconciliation,
    FfInventoryReconciliationError,
    ensure_inventory_reconciliation_schema,
)
from packages.application.ff_document_workflow import (  # noqa: E402
    FfDocumentWorkflow,
    mark_ff_replay_economics,
)
from packages.application.ff_overhead_allocation import FfOverheadAllocation  # noqa: E402
from packages.application.ff_warehouse_documents import FfWarehouseDocumentView  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.simple_xlsx import (  # noqa: E402
    build_single_sheet_workbook_bytes,
    read_first_sheet_cells,
    read_first_sheet_rows,
)
from packages.application.warehouse_functional import ensure_warehouse_functional_schema  # noqa: E402


NOW = "2026-08-02T09:00:00Z"
BUSINESS_DATE = "2026-07-31"


def main() -> None:
    with TemporaryDirectory(prefix="ff-inventory-reconciliation-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_nomenclature(runtime, [101, 102, 103, 104])
        _seed_opening(runtime, {101: 10, 102: 20})
        _seed_ff_cost_version(
            runtime,
            {101: (10, 100), 102: (20, 200), 103: (0, 300), 104: (0, 400)},
        )
        workbook = _workbook(
            [(101, "SKU 101", 12), (102, "SKU 102", 18), (103, "SKU 103", 0), (104, "SKU 104", 0)]
        )
        block = FfInventoryReconciliation(runtime=runtime, timestamp_factory=lambda: NOW)

        template, template_name, template_type = block.build_template(
            business_date=BUSINESS_DATE
        )
        assert template.startswith(b"PK") and template_name.endswith(".xlsx")
        assert "spreadsheetml" in template_type
        template_rows = read_first_sheet_rows(template)
        template_cells = read_first_sheet_cells(template)
        assert template_rows[0] == [
            "nmId",
            "Штрихкод",
            "Комментарий SKU",
            "Остаток ФФ",
            "Дата остатка",
        ]
        assert template_rows[1][1] == _primary_barcode(101)
        assert len(template_rows[1][1]) > 15
        assert template_cells[1][1].kind == "text"
        assert template_cells[1][1].style_index == 1
        assert template_cells[1][1].raw_value == _primary_barcode(101)
        preview = block.create_preview(
            source_bytes=workbook,
            source_filename="manager.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert preview["apply_allowed"] is True and preview["preview_id"].startswith("ffip_")
        assert preview["manifest"]["source"]["header_profile"] == "legacy_nm_id_v1"

        plan = block.build_plan(
            source_bytes=workbook,
            source_filename="manager.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert plan["status"] == "ready" and plan["apply_allowed"] is True
        assert plan["manifest"]["before_total"] == "30"
        assert plan["manifest"]["target_total"] == "30"
        assert [item["operation_type"] for item in plan["manifest"]["documents"]] == [
            "inventory_receipt",
            "inventory_writeoff",
        ]
        by_nm = {int(item["nm_id"]): item for item in plan["manifest"]["per_sku"]}
        assert by_nm[101]["inventory_delta"] == "2" and by_nm[101]["unit_cost_rub"] == "100"
        assert by_nm[102]["inventory_delta"] == "-2" and by_nm[102]["unit_cost_rub"] == "200"
        assert plan["manifest"]["inventory_capital_delta_rub"] == "-200"

        applied = block.apply_plan(
            source_bytes=workbook,
            source_filename="manager.xlsx",
            business_date=BUSINESS_DATE,
            return_supply_ids=[],
            confirmation_fingerprint=plan["fingerprint"],
            approval_reference="github-comment:fixture-gate",
            created_by="smoke",
        )
        assert applied["status"] == "applied" and applied["readback"]["target_matches"] is True
        source_sha256 = "sha256:" + hashlib.sha256(workbook).hexdigest()
        readback = block.readback(source_sha256=source_sha256, business_date=BUSINESS_DATE)
        assert readback["status"] == "applied"
        assert readback["target_readback"]["target_matches"] is True
        assert readback["non_target_digest_matches"] is True
        inventory_page = FfWarehouseDocumentView(db_path=runtime.db_path).page(
            reason="inventory",
            limit=100,
        )
        parent = next(
            item
            for item in inventory_page["documents"]
            if item["document_type_label"] == "Инвентаризация склада FF"
        )
        children = [
            item
            for item in inventory_page["documents"]
            if item["document_type_label"]
            in {"Оприходование излишков", "Списание недостач"}
        ]
        assert parent["total_quantity"] == "0" and parent["total_capital_rub"] == "0"
        assert set(parent["linked_document_ids"]) == {
            item["document_id"] for item in children
        }
        assert sum(Decimal(item["total_capital_rub"]) for item in children) == Decimal("-200")
        with sqlite3.connect(runtime.db_path) as conn:
            audit = conn.execute(
                "SELECT source_file_blob,source_sha256,approval_reference FROM sheet_vitrina_v1_ff_inventory_reconciliations"
            ).fetchone()
            assert bytes(audit[0]) == workbook and str(audit[1]) == source_sha256
            assert str(audit[2]) == "github-comment:fixture-gate"
            raw_rows = [
                json.loads(str(row[0]))
                for row in conn.execute(
                    """
                    SELECT line.raw_json FROM sheet_vitrina_v1_ff_stock_operation_lines line
                    JOIN sheet_vitrina_v1_ff_stock_operations operation
                      ON operation.operation_id=line.operation_id
                    WHERE operation.source_type='inventory_reconciliation'
                    ORDER BY line.nm_id
                    """
                ).fetchall()
            ]
            assert [item["cost_snapshot"]["unit_cost_rub"] for item in raw_rows] == ["100", "200"]

        repeated = block.apply_plan(
            source_bytes=workbook,
            source_filename="manager.xlsx",
            business_date=BUSINESS_DATE,
            return_supply_ids=[],
            confirmation_fingerprint=plan["fingerprint"],
            approval_reference="github-comment:fixture-gate",
            created_by="smoke",
        )
        assert repeated["status"] == "already_applied" and repeated["idempotent"] is True

        rolled_back = block.rollback(
            confirmation_fingerprint=plan["fingerprint"],
            approval_reference="github-comment:fixture-rollback-gate",
            reason="fixture rollback proof",
            created_by="smoke",
        )
        assert rolled_back["status"] == "rolled_back"
        assert rolled_back["readback"]["target_matches"] is True
        correction_page = FfWarehouseDocumentView(db_path=runtime.db_path).page(
            reason="correction",
            limit=100,
        )
        assert correction_page["documents"]
        assert all(
            any(link.startswith("ffop:") for link in item["linked_document_ids"])
            for item in correction_page["documents"]
        )
        assert block.rollback(
            confirmation_fingerprint=plan["fingerprint"],
            approval_reference="github-comment:fixture-rollback-gate",
            reason="fixture rollback proof",
            created_by="smoke",
        )["idempotent"] is True

        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "DELETE FROM sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id='inventory-cost-v1' AND nm_id IN (103,104)"
            )
            conn.commit()

        _seed_explicit_inventory_cost_basis(
            runtime,
            nm_id=101,
            unit_cost=250,
            basis_kind="exact_original_source_debit",
            basis_version_id="inventory-original-v1",
        )
        original_basis_workbook = _workbook(
            [(101, "SKU 101", 11), (102, "SKU 102", 20), (103, "SKU 103", 0), (104, "SKU 104", 0)]
        )
        original_basis_plan = block.build_plan(
            source_bytes=original_basis_workbook,
            source_filename="exact-original-basis.xlsx",
            business_date=BUSINESS_DATE,
        )
        original_basis_row = next(
            item for item in original_basis_plan["manifest"]["per_sku"]
            if int(item["nm_id"]) == 101
        )
        assert original_basis_row["cost_basis"]["basis_kind"] == "exact_original_source_debit"
        assert original_basis_row["unit_cost_rub"] == "250"

        _seed_explicit_inventory_cost_basis(
            runtime,
            nm_id=103,
            unit_cost=0,
            basis_kind="business_approved_estimate",
            basis_version_id="zero-cost-must-not-qualify",
        )
        missing_cost_workbook = _workbook(
            [(101, "SKU 101", 10), (102, "SKU 102", 20), (103, "SKU 103", 1), (104, "SKU 104", 0)]
        )
        missing_cost = block.build_plan(
            source_bytes=missing_cost_workbook,
            source_filename="missing-cost.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert missing_cost["status"] == "blocked" and missing_cost["apply_allowed"] is False
        assert any(
            item.get("code") == "inventory_cost_basis_missing" and int(item.get("nm_id") or 0) == 103
            for item in missing_cost["blockers"]
        )
        _seed_certified_inbound_cost(runtime, nm_id=103, unit_cost=175)
        _seed_explicit_inventory_cost_basis(runtime, nm_id=104, unit_cost=225)
        certified_inbound = block.build_plan(
            source_bytes=missing_cost_workbook,
            source_filename="certified-inbound-cost.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert certified_inbound["status"] == "ready"
        inbound_row = next(
            item for item in certified_inbound["manifest"]["per_sku"]
            if int(item["nm_id"]) == 103
        )
        assert inbound_row["cost_basis"]["basis_kind"] == "latest_certified_inbound_landed_ff_cost"
        assert inbound_row["unit_cost_rub"] == "175"

        explicit_workbook = _workbook(
            [(101, "SKU 101", 10), (102, "SKU 102", 20), (103, "SKU 103", 0), (104, "SKU 104", 1)]
        )
        explicit_plan = block.build_plan(
            source_bytes=explicit_workbook,
            source_filename="approved-estimate.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert explicit_plan["status"] == "ready"
        explicit_row = next(
            item for item in explicit_plan["manifest"]["per_sku"]
            if int(item["nm_id"]) == 104
        )
        assert explicit_row["cost_basis"]["basis_kind"] == "business_approved_estimate"
        assert explicit_row["cost_basis"]["approval_reference"] == "github-comment:estimate-gate"

    with TemporaryDirectory(prefix="ff-inventory-return-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_nomenclature(runtime, [201])
        _seed_opening(runtime, {201: 10})
        _seed_ff_cost_version(runtime, {201: (1, 50)})
        _seed_return_supply(runtime, supply_id="supply-return", nm_id=201)
        workbook = _workbook([(201, "SKU 201", 6)])
        block = FfInventoryReconciliation(runtime=runtime, timestamp_factory=lambda: NOW)
        plan = block.build_plan(
            source_bytes=workbook,
            source_filename="return-manager.xlsx",
            business_date=BUSINESS_DATE,
            return_supply_ids=["supply-return"],
        )
        assert plan["status"] == "ready"
        assert [item["operation_type"] for item in plan["manifest"]["documents"]] == [
            "auto_return"
        ]
        assert plan["manifest"]["return_quantity"] == "6"
        assert plan["manifest"]["return_capital_delta_rub"] == "300"
        applied = block.apply_plan(
            source_bytes=workbook,
            source_filename="return-manager.xlsx",
            business_date=BUSINESS_DATE,
            return_supply_ids=["supply-return"],
            confirmation_fingerprint=plan["fingerprint"],
            approval_reference="github-comment:return-gate",
            created_by="smoke",
        )
        assert applied["readback"]["target_matches"] is True
        with sqlite3.connect(runtime.db_path) as conn:
            lifecycle = conn.execute(
                "SELECT lifecycle_state,return_operation_id FROM "
                "sheet_vitrina_v1_ff_stock_wb_supply_lifecycle WHERE supply_id='supply-return'"
            ).fetchone()
        assert lifecycle[0] == "returned" and lifecycle[1] == applied["operation_ids"][0]
        rolled_back = block.rollback(
            confirmation_fingerprint=plan["fingerprint"],
            approval_reference="github-comment:return-rollback-gate",
            reason="fixture return rollback",
            created_by="smoke",
        )
        assert rolled_back["readback"]["target_matches"] is True
        with sqlite3.connect(runtime.db_path) as conn:
            lifecycle = conn.execute(
                "SELECT lifecycle_state,return_operation_id,last_observation_id FROM "
                "sheet_vitrina_v1_ff_stock_wb_supply_lifecycle WHERE supply_id='supply-return'"
            ).fetchone()
        assert lifecycle[0] == "rollback_pending_reobservation"
        assert lifecycle[1] == "" and str(lifecycle[2]).startswith("rollback:")

    _test_barcode_identity_profiles()
    _test_production_target_confirm_and_internal_retries()
    _test_repeated_apply_never_reapplies_target_after_later_movement()
    print("ff_inventory_reconciliation_smoke: OK")


def _test_barcode_identity_profiles() -> None:
    with TemporaryDirectory(prefix="ff-inventory-barcode-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_nomenclature(runtime, [301, 302])
        _seed_opening(runtime, {301: 5, 302: 5})
        _seed_ff_cost_version(runtime, {301: (5, 100), 302: (5, 200)})
        block = FfInventoryReconciliation(runtime=runtime, timestamp_factory=lambda: NOW)

        template, _, _ = block.build_template(business_date=BUSINESS_DATE)
        template_rows = read_first_sheet_rows(template)
        template_cells = read_first_sheet_cells(template)
        assert template_rows[1][1] == _primary_barcode(301)
        assert len(template_rows[1][1]) > 15
        assert template_cells[1][1].kind == "text"
        assert template_cells[1][1].style_index == 1

        barcode_only = _barcode_workbook(
            [
                (None, _primary_barcode(301), "SKU 301 primary", 6),
                (None, "990302", "SKU 302 additional", 4),
            ]
        )
        preview = block.create_preview(
            source_bytes=barcode_only,
            source_filename="barcode-only.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert preview["apply_allowed"] is True
        barcode_rows = {int(item["nm_id"]): item for item in preview["manifest"]["per_sku"]}
        assert barcode_rows[301]["identity_source"] == "barcode"
        assert barcode_rows[301]["source_barcode"] == _primary_barcode(301)
        assert barcode_rows[302]["identity_source"] == "barcode"
        confirmed = block.confirm_preview(
            preview_id=preview["preview_id"],
            confirmation_fingerprint=preview["fingerprint"],
            created_by="smoke",
        )
        assert confirmed["status"] == "applied"
        assert confirmed["readback"]["target_matches"] is True

        matching_both = block.build_plan(
            source_bytes=_barcode_workbook(
                [
                    (301, _primary_barcode(301), "SKU 301", 6),
                    (302, _primary_barcode(302), "SKU 302", 4),
                ]
            ),
            source_filename="matching-both.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert matching_both["status"] == "ready"
        assert all(
            item["identity_source"] == "nm_id+barcode"
            for item in matching_both["manifest"]["per_sku"]
        )
        stale_identity_bytes = _barcode_workbook(
            [
                (301, _primary_barcode(301), "SKU 301", 6),
                (302, _primary_barcode(302), "SKU 302", 4),
            ]
        )
        stale_identity_preview = block.create_preview(
            source_bytes=stale_identity_bytes,
            source_filename="stale-identity.xlsx",
            business_date=BUSINESS_DATE,
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_nomenclature_items SET barcodes_json=? WHERE nm_id=302",
                (
                    json.dumps(
                        [_primary_barcode(302), "990302", "880302"],
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.commit()
        identity_confirmed = block.confirm_preview(
            preview_id=stale_identity_preview["preview_id"],
            confirmation_fingerprint=stale_identity_preview["fingerprint"],
            created_by="smoke",
        )
        assert identity_confirmed["status"] == "applied"
        assert identity_confirmed["readback"]["target_matches"] is True

        unknown = block.build_plan(
            source_bytes=_barcode_workbook(
                [
                    (301, _primary_barcode(301), "SKU 301", 6),
                    (302, _primary_barcode(302), "SKU 302", 4),
                    (None, "7777777700000", "Unknown", 0),
                ]
            ),
            source_filename="unknown-barcode.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert unknown["status"] == "blocked"
        assert any(item["code"] == "unknown_barcode" for item in unknown["blockers"])

        conflict = block.build_plan(
            source_bytes=_barcode_workbook(
                [
                    (301, _primary_barcode(302), "Conflict", 0),
                    (301, _primary_barcode(301), "SKU 301", 6),
                    (302, _primary_barcode(302), "SKU 302", 4),
                ]
            ),
            source_filename="conflicting-identity.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert conflict["status"] == "blocked"
        assert any(item["code"] == "nm_id_barcode_conflict" for item in conflict["blockers"])

        duplicate = block.build_plan(
            source_bytes=_barcode_workbook(
                [
                    (None, _primary_barcode(301), "SKU 301 primary", 6),
                    (None, "990301", "SKU 301 additional", 6),
                    (None, _primary_barcode(302), "SKU 302", 4),
                ]
            ),
            source_filename="duplicate-resolved-sku.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert duplicate["status"] == "blocked"
        assert any(item["code"] == "duplicate_resolved_sku" for item in duplicate["blockers"])

        empty_identity = block.build_plan(
            source_bytes=_barcode_workbook(
                [
                    (301, _primary_barcode(301), "SKU 301", 6),
                    (302, _primary_barcode(302), "SKU 302", 4),
                    (None, "", "No identity", 0),
                ]
            ),
            source_filename="empty-identity.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert any(
            item["code"] == "empty_inventory_identity"
            for item in empty_identity["blockers"]
        )

        missing = block.build_plan(
            source_bytes=_barcode_workbook(
                [(None, _primary_barcode(301), "SKU 301 only", 6)]
            ),
            source_filename="missing-active.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert any(
            item["code"] == "active_nomenclature_rows_missing"
            and item["nm_ids"] == [302]
            for item in missing["blockers"]
        )

        for unsafe_barcode, expected_code in (
            (460301, "barcode_must_be_text"),
            (460301.5, "barcode_must_be_text"),
            ("4.60301E+5", "scientific_notation_barcode"),
            ("barcode-301", "malformed_barcode"),
        ):
            try:
                block.build_plan(
                    source_bytes=_barcode_workbook(
                        [
                            (None, unsafe_barcode, "Unsafe barcode", 6),
                            (302, _primary_barcode(302), "SKU 302", 4),
                        ]
                    ),
                    source_filename="unsafe-barcode.xlsx",
                    business_date=BUSINESS_DATE,
                )
            except FfInventoryReconciliationError as exc:
                assert exc.code == "invalid_workbook_rows"
                assert any(item["code"] == expected_code for item in exc.details)
            else:
                raise AssertionError(f"unsafe barcode must fail closed: {unsafe_barcode!r}")

        with sqlite3.connect(runtime.db_path) as conn:
            for nm_id in (301, 302):
                conn.execute(
                    "UPDATE sheet_vitrina_v1_nomenclature_items SET barcodes_json=? WHERE nm_id=?",
                    (
                        json.dumps(
                            [_primary_barcode(nm_id), f"990{nm_id}", "7777777777777"],
                            ensure_ascii=False,
                        ),
                        nm_id,
                    ),
                )
            conn.commit()
        ambiguous = block.build_plan(
            source_bytes=_barcode_workbook(
                [
                    (301, _primary_barcode(301), "SKU 301", 6),
                    (302, _primary_barcode(302), "SKU 302", 4),
                    (None, "7777777777777", "Ambiguous", 0),
                ]
            ),
            source_filename="ambiguous-barcode.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert ambiguous["status"] == "blocked"
        assert any(item["code"] == "ambiguous_barcode" for item in ambiguous["blockers"])


def _test_production_target_confirm_and_internal_retries() -> None:
    with TemporaryDirectory(prefix="ff-inventory-production-regression-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_nomenclature(runtime, [501, 502])
        _seed_opening(runtime, {501: 30_000, 502: 23_750})
        _seed_ff_cost_version(runtime, {501: (30_000, 100), 502: (23_750, 125)})
        workbook = _workbook(
            [(501, "Production FF A", 30_100), (502, "Production FF B", 23_400)]
        )
        block = FfInventoryReconciliation(runtime=runtime, timestamp_factory=lambda: NOW)
        preview = block.create_preview(
            source_bytes=workbook,
            source_filename="production-stored.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert preview["manifest"]["before_total"] == "53750"
        assert preview["manifest"]["target_total"] == "53500"
        target_fingerprint = str(preview["fingerprint"])

        # Reproduce the deployed row shape: a ready v1 preview whose token was
        # calculated from the full semantic snapshot rather than target intent.
        legacy_plan = json.loads(json.dumps(preview, ensure_ascii=False))
        legacy_plan.pop("preview_id", None)
        legacy_plan.pop("source_sha256", None)
        legacy_plan["plan_version"] = "v1"
        legacy_plan["manifest"].pop("target_intent", None)
        legacy_plan["manifest"].pop("semantic_snapshot_fingerprint", None)
        legacy_fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(legacy_plan["manifest"], sort_keys=True).encode("utf-8")
        ).hexdigest()
        legacy_plan["fingerprint"] = legacy_fingerprint
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_ff_inventory_previews "
                "SET plan_fingerprint=?,plan_json=?,status='previewed' WHERE preview_id=?",
                (
                    legacy_fingerprint,
                    json.dumps(legacy_plan, ensure_ascii=False, sort_keys=True),
                    preview["preview_id"],
                ),
            )
            _publish_unrelated_functional_version(
                conn,
                balances={501: (30_000, 100), 502: (23_750, 125)},
            )
            conn.commit()

        fresh = block.build_plan(
            source_bytes=workbook,
            source_filename="production-stored.xlsx",
            business_date=BUSINESS_DATE,
        )
        assert fresh["fingerprint"] == target_fingerprint
        assert fresh["fingerprint"] != legacy_fingerprint
        confirmed = block.confirm_preview(
            preview_id=preview["preview_id"],
            confirmation_fingerprint=legacy_fingerprint,
            created_by="owner",
        )
        assert confirmed["status"] == "applied"
        assert confirmed["readback"]["actual_total"] == "53500"
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            reconciliation = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ff_inventory_reconciliations"
            ).fetchone()
            assert reconciliation is not None
            manifest = json.loads(str(reconciliation["manifest_json"]))
            assert manifest["before_total"] == "53750"
            assert manifest["inventory_quantity_delta"] == "-250"
            assert manifest["target_total"] == "53500"
            assert manifest["source_revisions"]["audit_active_functional_version"]["version_id"] == "inventory-unrelated-v2"
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_inventory_reconciliations"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations "
                "WHERE source_type='inventory_reconciliation'"
            ).fetchone()[0] == 2
            queue = conn.execute(
                "SELECT queue_id FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue "
                "WHERE stable_source_id=?",
                ("ff_inventory:" + str(reconciliation["reconciliation_id"]),),
            ).fetchone()
            assert queue is not None
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_targeted_recalc_queue "
                "SET status='complete',started_at=?,finished_at=?,error='' WHERE queue_id=?",
                (NOW, NOW, str(queue["queue_id"])),
            )
            conn.commit()
        assert mark_ff_replay_economics(
            runtime,
            queue_ids=[str(queue["queue_id"])],
            status="complete",
            occurred_at=NOW,
        ) == 1
        workflow = FfDocumentWorkflow(
            runtime=runtime,
            inventory=block,
            overhead=FfOverheadAllocation(runtime=runtime, timestamp_factory=lambda: NOW),
            timestamp_factory=lambda: NOW,
            start_workers=False,
        )
        final_status = workflow.inventory_status(preview_id=preview["preview_id"])
        assert final_status["state"] == "replay_complete"
        assert final_status["summary"]["before_total"] == "53750"
        assert final_status["summary"]["target_total"] == "53500"
        page = FfWarehouseDocumentView(db_path=runtime.db_path).page(reason="inventory", limit=20)
        parent = next(
            item for item in page["documents"]
            if item["document_type_label"] == "Инвентаризация склада FF"
        )
        children = [
            item for item in page["documents"]
            if item["document_type_label"] in {"Оприходование излишков", "Списание недостач"}
        ]
        assert len(children) == 2
        assert set(parent["linked_document_ids"]) == {
            item["document_id"] for item in children
        }

    with TemporaryDirectory(prefix="ff-inventory-confirm-race-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_nomenclature(runtime, [601])
        _seed_opening(runtime, {601: 10})
        _seed_ff_cost_version(runtime, {601: (10, 125)})
        workbook = _workbook([(601, "Race FF", 20)])
        block = FfInventoryReconciliation(runtime=runtime, timestamp_factory=lambda: NOW)
        preview = block.create_preview(
            source_bytes=workbook,
            source_filename="race.xlsx",
            business_date=BUSINESS_DATE,
        )
        original_build_plan = block.build_plan
        injected = {"count": 0}

        def build_plan_with_races(**kwargs: object) -> dict[str, object]:
            plan = original_build_plan(**kwargs)
            if kwargs.get("_confirmed_target_intent") is not None and not plan.get("idempotent") and injected["count"] < 2:
                injected["count"] += 1
                runtime.create_ff_stock_operation(
                    operation_id=f"ffso-confirm-race-{injected['count']}",
                    operation_type="manual_receipt",
                    source_type="manual_excel",
                    source_key=f"inventory-smoke:confirm-race:{injected['count']}",
                    source_object_id=f"confirm-race-{injected['count']}",
                    source_object_label="Concurrent inventory writer",
                    created_at=f"2026-08-02T09:00:0{injected['count']}Z",
                    business_effective_date=BUSINESS_DATE,
                    created_by="concurrent-writer",
                    warnings=[],
                    diagnostics={},
                    lines=[{"nm_id": 601, "quantity_delta": 1}],
                )
            return plan

        block.build_plan = build_plan_with_races  # type: ignore[method-assign]
        confirmed = block.confirm_preview(
            preview_id=preview["preview_id"],
            confirmation_fingerprint=preview["fingerprint"],
            created_by="owner",
        )
        assert injected["count"] == 2
        assert confirmed["readback"]["actual_total"] == "20"
        with sqlite3.connect(runtime.db_path) as conn:
            manifest = json.loads(
                str(conn.execute(
                    "SELECT manifest_json FROM sheet_vitrina_v1_ff_inventory_reconciliations"
                ).fetchone()[0])
            )
            assert manifest["before_total"] == "12"
            assert manifest["inventory_quantity_delta"] == "8"
            assert manifest["target_total"] == "20"
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_inventory_reconciliations"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations "
                "WHERE source_type='inventory_reconciliation'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue "
                "WHERE stable_source_id LIKE 'ff_inventory:%'"
            ).fetchone()[0] == 1


def _publish_unrelated_functional_version(
    conn: sqlite3.Connection,
    *,
    balances: dict[int, tuple[int, int]],
) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
            version_id,cutover_id,version_kind,effective_at,status,
            plan_fingerprint,local_source_digest,source_watermarks_json,
            created_at,business_effective_date,published_at
        ) VALUES('inventory-unrelated-v2','warehouse_functional_cutover_v1','fixture',
                 '2026-07-31T15:18:00Z','good','sha256:inventory-unrelated-v2',
                 'sha256:inventory-source-v2','{}','2026-07-31T15:18:00Z',
                 '2026-07-31','2026-07-31T15:18:00Z')
        """
    )
    conn.executemany(
        """
        INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
            version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
            cost_covered_quantity,quality,certified,wb_quantity,
            wb_in_way_to_client,wb_in_way_from_client,provenance_json
        ) VALUES('inventory-unrelated-v2','ff',?,?,?,?,?,'certified',1,'0','0','0','{}')
        """,
        [
            (nm_id, str(quantity), str(wac), str(quantity * wac), str(quantity))
            for nm_id, (quantity, wac) in sorted(balances.items())
        ],
    )
    conn.execute(
        "UPDATE sheet_vitrina_v1_warehouse_functional_active "
        "SET version_id='inventory-unrelated-v2',updated_at='2026-07-31T15:18:00Z' WHERE slot=1"
    )


def _test_repeated_apply_never_reapplies_target_after_later_movement() -> None:
    with TemporaryDirectory(prefix="ff-inventory-repeat-drift-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_nomenclature(runtime, [101])
        _seed_opening(runtime, {101: 10})
        _seed_ff_cost_version(runtime, {101: (10, 100)})
        workbook = _workbook([(101, "SKU 101", 12)])
        block = FfInventoryReconciliation(runtime=runtime, timestamp_factory=lambda: NOW)
        plan = block.build_plan(
            source_bytes=workbook,
            source_filename="manager-drift.xlsx",
            business_date=BUSINESS_DATE,
        )
        block.apply_plan(
            source_bytes=workbook,
            source_filename="manager-drift.xlsx",
            business_date=BUSINESS_DATE,
            return_supply_ids=[],
            confirmation_fingerprint=plan["fingerprint"],
            approval_reference="github-comment:fixture-gate",
            created_by="smoke",
        )
        runtime.create_ff_stock_operation(
            operation_id="ffso-after-reconciliation-drift",
            operation_type="manual_receipt",
            source_type="manual_excel",
            source_key="inventory-smoke:post-apply-drift",
            source_object_id="inventory-smoke-drift",
            source_object_label="Inventory smoke target drift",
            created_at="2026-08-02T10:00:00Z",
            business_effective_date="2026-08-02",
            created_by="smoke",
            warnings=[],
            diagnostics={},
            lines=[{"nm_id": 101, "quantity_delta": 1}],
        )
        repeated = block.apply_plan(
            source_bytes=workbook,
            source_filename="manager-drift.xlsx",
            business_date=BUSINESS_DATE,
            return_supply_ids=[],
            confirmation_fingerprint=plan["fingerprint"],
            approval_reference="github-comment:fixture-gate",
            created_by="smoke",
        )
        assert repeated["status"] == "already_applied"
        assert repeated["idempotent"] is True
        assert repeated["readback"]["target_matches"] is False
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_inventory_reconciliations"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations "
                "WHERE source_type='inventory_reconciliation'"
            ).fetchone()[0] == 1


def _workbook(rows: list[tuple[int, str, int]]) -> bytes:
    return build_single_sheet_workbook_bytes(
        "Остатки ФФ",
        [
            ["nmId", "Комментарий SKU", "Остаток ФФ", "Дата остатка"],
            *[[nm_id, comment, quantity, BUSINESS_DATE] for nm_id, comment, quantity in rows],
        ],
    )


def _barcode_workbook(
    rows: list[tuple[int | None, object, str, int]],
) -> bytes:
    return build_single_sheet_workbook_bytes(
        "Инвентаризация FF",
        [
            ["nmId", "Штрихкод", "Комментарий SKU", "Остаток ФФ", "Дата остатка"],
            *[
                [nm_id, barcode, comment, quantity, BUSINESS_DATE]
                for nm_id, barcode, comment, quantity in rows
            ],
        ],
    )


def _primary_barcode(nm_id: int) -> str:
    return f"00000000000000000000000{nm_id}"


def _seed_nomenclature(runtime: RegistryUploadDbBackedRuntime, nm_ids: list[int]) -> None:
    runtime.save_sku_group(
        {
            "group_key": "inventory-smoke",
            "label": "Inventory smoke",
            "is_active": True,
            "is_system": False,
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    runtime.save_nomenclature_items_atomic(
        [
            {
                "item_id": f"inventory-smoke-{nm_id}",
                "is_active": True,
                "is_hidden": False,
                "our_sku": f"INV-{nm_id}",
                "nm_id": nm_id,
                "barcode": _primary_barcode(nm_id),
                "barcodes": [_primary_barcode(nm_id), f"990{nm_id}"],
                "nomenclature_name": f"Inventory SKU {nm_id}",
                "product_type": "inventory-smoke",
                "match_key": f"inventory-{nm_id}",
                "comment": "",
                "created_at": NOW,
                "updated_at": NOW,
            }
            for nm_id in nm_ids
        ]
    )


def _seed_opening(runtime: RegistryUploadDbBackedRuntime, balances: dict[int, int]) -> None:
    runtime.create_ff_stock_operation(
        operation_id="ffso-inventory-opening",
        operation_type="manual_receipt",
        source_type="manual_excel",
        source_key="inventory-smoke:opening",
        source_object_id="inventory-smoke",
        source_object_label="Inventory smoke opening",
        created_at="2026-07-30T09:00:00Z",
        business_effective_date="2026-07-30",
        created_by="smoke",
        warnings=[],
        diagnostics={},
        lines=[{"nm_id": nm_id, "quantity_delta": quantity} for nm_id, quantity in balances.items()],
    )


def _seed_ff_cost_version(
    runtime: RegistryUploadDbBackedRuntime,
    values: dict[int, tuple[int, int]],
) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_functional_schema(conn)
        ensure_inventory_reconciliation_schema(conn)
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                version_id,cutover_id,version_kind,effective_at,status,
                plan_fingerprint,local_source_digest,source_watermarks_json,
                created_at,business_effective_date,published_at
            ) VALUES('inventory-cost-v1','warehouse_functional_cutover_v1','fixture',
                     '2026-07-31T08:00:00Z','good','sha256:inventory-cost-v1',
                     'sha256:inventory-source','{}','2026-07-31T08:00:00Z',
                     '2026-07-31','2026-07-31T08:00:00Z')
            """
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id,updated_at) VALUES(1,'inventory-cost-v1',?)",
            (NOW,),
        )
        for nm_id, (quantity, wac) in values.items():
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                    version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                    cost_covered_quantity,quality,certified,wb_quantity,
                    wb_in_way_to_client,wb_in_way_from_client,provenance_json
                ) VALUES('inventory-cost-v1','ff',?,?,?,?,?,'certified',1,'0','0','0','{}')
                """,
                (nm_id, str(quantity), str(wac), str(quantity * wac), str(quantity)),
            )
        conn.commit()


def _seed_return_supply(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    supply_id: str,
    nm_id: int,
) -> None:
    runtime.create_ff_stock_operation(
        operation_id="ffso-return-original-debit",
        operation_type="auto_writeoff",
        source_type="wb_supply",
        source_key=f"wb_supply_debit:supply:{supply_id}",
        source_object_id=supply_id,
        source_object_label="Return fixture original debit",
        created_at="2026-07-29T08:00:00Z",
        business_effective_date="2026-07-29",
        created_by="smoke",
        diagnostics={"supply_id": supply_id},
        lines=[
            {
                "nm_id": nm_id,
                "quantity_delta": -10,
                "raw": {
                    "cost_snapshot": {
                        "unit_cost_rub": "50",
                        "capital_delta_rub": "-500",
                        "quality": "exact_original_ff_debit",
                        "provenance": {"supply_id": supply_id},
                    }
                },
            }
        ],
    )
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                version_id,cutover_id,version_kind,effective_at,status,
                plan_fingerprint,local_source_digest,source_watermarks_json,
                created_at,business_effective_date,published_at
            ) VALUES('inventory-cost-early','warehouse_functional_cutover_v1','fixture',
                     '2026-07-29T09:00:00Z','good','sha256:inventory-cost-early',
                     'sha256:inventory-source-early','{}','2026-07-29T09:00:00Z',
                     '2026-07-29','2026-07-29T09:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                cost_covered_quantity,quality,certified,wb_quantity,
                wb_in_way_to_client,wb_in_way_from_client,provenance_json
            ) VALUES('inventory-cost-early','ff_to_wb',?,'10','50','500','10',
                     'exact_original_ff_debit',1,'0','0','0',?)
            """,
            (
                nm_id,
                json.dumps(
                    {
                        "supply": {
                            "supply_id": supply_id,
                            "ff_wac_at_ledger_debit_rub": "50",
                            "flow_quantity": "10",
                            "packed_quantity": "10",
                            "accepted_quantity": "0",
                            "flow_capital_rub": "500",
                            "source_revision": "sha256:return-fixture-early",
                        }
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                cost_covered_quantity,quality,certified,wb_quantity,
                wb_in_way_to_client,wb_in_way_from_client,provenance_json
            ) VALUES('inventory-cost-v1','ff_to_wb',?,'6','50','300','6',
                     'exact_original_ff_debit',1,'0','0','0',?)
            """,
            (
                nm_id,
                json.dumps(
                    {
                        "supply": {
                            "supply_id": supply_id,
                            "ff_wac_at_ledger_debit_rub": "50",
                            "flow_quantity": "10",
                            "packed_quantity": "10",
                            "accepted_quantity": "4",
                            "flow_capital_rub": "500",
                            "source_revision": "sha256:return-fixture",
                        }
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_ff_stock_wb_supply_lifecycle(
                supply_id,first_seen_complete_snapshot_at,last_seen_complete_snapshot_at,
                last_observation_id,last_observation_at,
                consecutive_missing_complete_snapshots,lifecycle_state,
                original_debit_operation_id,return_operation_id,return_source_revision,
                last_record_json,diagnostics_json,updated_at
            ) VALUES(?,?,?,?,?,2,'missing_confirmed',?,'','',?,'{}',?)
            """,
            (
                supply_id,
                "2026-07-29T08:00:00Z",
                "2026-07-29T08:00:00Z",
                "complete-snapshot-2",
                "2026-07-31T08:00:00Z",
                "ffso-return-original-debit",
                json.dumps({"supply_id": supply_id}),
                "2026-07-31T08:00:00Z",
            ),
        )
        conn.commit()


def _seed_certified_inbound_cost(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    nm_id: int,
    unit_cost: int,
) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_shipments(
                shipment_id,created_at,updated_at,shipment_date,
                actual_ff_acceptance_date,order_status,expenses_complete,
                product_qty_total,match_status,warnings_json,errors_json
            ) VALUES('inventory-certified-inbound',?,?,?,'2026-07-30',
                     'accepted_ff',1,10,'matched_by_barcode','[]','[]')
            """,
            (NOW, NOW, "2026-07-25"),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_ff_cost_layers(
                layer_id,supplier_shipment_id,status,accepted_ff_date,
                calculated_at,effective_cny_rate,invoice_amount_total_cny,
                invoice_extras_total_cny,product_qty_total,
                common_expense_pool_rub,common_expense_per_unit_rub,
                weighted_avg_ff_unit_cost_rub,reconciliation_status,
                reconciliation_delta_rub,inputs_hash,version,is_current,
                supersedes_layer_id,superseded_at,source_status_json,
                component_status_json
            ) VALUES('inventory-certified-layer','inventory-certified-inbound',
                     'confirmed','2026-07-30',?,1,1,0,10,0,0,?,
                     'ok',0,'sha256:inventory-certified-layer',1,1,NULL,NULL,'{}','{}')
            """,
            (NOW, unit_cost),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_ff_cost_layer_lines(
                layer_line_id,layer_id,supplier_shipment_id,supplier_line_id,
                nm_id,sku,display_name,qty,invoice_unit_price_cny,
                sku_purchase_cost_rub,allocated_common_expenses_per_unit_rub,
                sku_ff_unit_cost_rub,line_total_cost_rub,allocation_method,
                source_status,missing_reason
            ) VALUES('inventory-certified-line','inventory-certified-layer',
                     'inventory-certified-inbound','inventory-certified-source',
                     ?,?,?,10,1,?,0,?,?, 'fixture','confirmed',NULL)
            """,
            (nm_id, f"INV-{nm_id}", f"Inventory SKU {nm_id}", unit_cost, unit_cost, unit_cost * 10),
        )
        conn.commit()


def _seed_explicit_inventory_cost_basis(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    nm_id: int,
    unit_cost: int,
    basis_kind: str = "business_approved_estimate",
    basis_version_id: str = "inventory-estimate-v1",
) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_ff_inventory_cost_bases(
                basis_version_id,nm_id,effective_from,unit_cost_rub,basis_kind,
                quality,source_reference,approval_reference,provenance_json,
                status,created_at
            ) VALUES(?,?,'2026-07-31',?,?,?,
                     'manager-estimate-fixture','github-comment:estimate-gate',?,
                     'active',?)
            """,
            (
                basis_version_id,
                nm_id,
                str(unit_cost),
                basis_kind,
                basis_kind,
                json.dumps(
                    {
                        "basis": "separate versioned business estimate",
                        "stage": "ff",
                        "approved_by": "owner-fixture",
                    },
                    ensure_ascii=False,
                ),
                NOW,
            ),
        )
        conn.commit()


if __name__ == "__main__":
    main()
