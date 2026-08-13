"""Smoke checks for the management proxy WB cost contour."""

from __future__ import annotations

from pathlib import Path
from decimal import Decimal
import json
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.our_wb_costs import (  # noqa: E402
    TRANSIT_DIRECT_ZERO_CONFIRMED,
    TRANSIT_NOT_FOUND,
    TRANSIT_SESSION_EXPIRED,
    TRANSIT_SOURCE_ERROR,
    TRANSIT_UPDATING,
    WB_COST_STATUS_CONFIRMED,
    WB_COST_STATUS_ESTIMATED,
    OurWbCostBlock,
    classify_wb_supply_transit,
)
from packages.application.ff_pool_surfaces import FfPoolSurface  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
)
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    SlotLookups,
    TemporalLiveSources,
    _MetricEvaluator,
)
from packages.application.sheet_vitrina_v1_onec_stocks import (  # noqa: E402
    ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY,
    ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
    ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY,
    extend_metrics_with_onec_stock_metrics,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (  # noqa: E402
    OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
    extend_metrics_with_our_wb_cost_metrics,
)
from packages.application.supplier_shipments import SupplierShipmentsBlock  # noqa: E402
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item  # noqa: E402
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)
from packages.contracts.supplier_shipments import ORDER_STATUS_ACCEPTED_FF, ORDER_STATUS_PRODUCTION  # noqa: E402


NOW = "2026-07-07T07:00:00Z"
SUPPLIER_BARCODE = "1497413000000"
BUNDLE_FIXTURE = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"


def main() -> None:
    with TemporaryDirectory(prefix="our-wb-costs-smoke-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        runtime.save_nomenclature_item(
            {
                "item_id": "our-wb-costs-supplier-sku-1",
                "is_active": True,
                "our_sku": "SKU-1",
                "nm_id": 497413000,
                "barcode": SUPPLIER_BARCODE,
                "nomenclature_name": "SKU 1",
                "purchase_price_yuan": 90,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        _seed_supplier_shipment(runtime)
        _seed_financial_inputs(runtime)
        _seed_supplier_shipment(runtime, shipment_id="sup_preview", actual_ff_acceptance_date="")
        _seed_financial_inputs(runtime, shipment_id="sup_preview")

        block = OurWbCostBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        _shipment, acceptance_lines, _revision = FfPoolSurface(
            db_path=runtime.db_path,
            runtime_dir=runtime.runtime_dir,
            timestamp_factory=lambda: NOW,
        ).supplier_shipment_source("sup_preview")
        if Decimal(acceptance_lines[0]["capital_rub"]) != Decimal("12600"):
            raise AssertionError("guided acceptance template must use a non-persisting exact cost preview")
        preview = block.preview_supplier_ff_cost_layer("sup_smoke")
        if float(preview["lines"][0]["qty"]) != 10:
            raise AssertionError("cost preview must calculate without a persisted accepted layer")
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            if conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_supplier_ff_cost_layers").fetchone()[0]:
                raise AssertionError("cost preview must not persist a supplier FF layer")
        materialized = block.materialize_supplier_ff_cost_layer(
            "sup_smoke", accepted_quantities_by_nm={497413000: 7}
        )
        if not materialized or not materialized.get("materialized"):
            raise AssertionError(f"first FF cost materialization must write a layer, got {materialized}")
        second = block.materialize_supplier_ff_cost_layer(
            "sup_smoke", accepted_quantities_by_nm={497413000: 7}
        )
        if second is None or second.get("materialized"):
            raise AssertionError(f"second FF cost materialization must be idempotent, got {second}")
        _assert_supplier_ff_reconciliation(runtime)
        _seed_wb_supply(runtime)
        if block.materialize_wb_supply_cost_layers(opening_date="2026-07-01") != 1:
            raise AssertionError("WB supply cost layer materialization must write one SKU layer")
        _assert_wb_supply_cost_layer(runtime)
        _seed_wb_supply(
            runtime,
            supply_id="transit_enriched",
            status_id=4,
            goods=[{"nmID": 497413000, "quantity": 10, "acceptedQuantity": 0}],
            has_transit_cost_marker=True,
            acceptance_cost=None,
            cost_total=None,
        )
        if block.materialize_wb_supply_cost_layers(opening_date="2026-07-01") != 1:
            raise AssertionError("transit supply without cost must materialize one fail-closed layer")
        _assert_transit_supply_cost(runtime, expected_status="not_requested", expected_per_unit=None)
        runtime.upsert_wb_supply_transit_cost_enrichment(
            {
                "supply_id": "transit_enriched",
                "amount": 1000,
                "currency": "RUB",
                "amount_label": "1 000 ₽",
                "is_transit": True,
                "source": "display_text",
                "evidence_type": "unverified",
                "confidence": "high",
                "fetched_at": NOW,
                "status": "success",
                "error": "",
                "source_endpoint_path": "/untrusted",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        if block.materialize_wb_supply_cost_layers(opening_date="2026-07-01") != 0:
            raise AssertionError(
                "positive display text without canonical source evidence must remain fail-closed"
            )
        _assert_transit_supply_cost(
            runtime,
            expected_status="not_requested",
            expected_per_unit=None,
        )
        runtime.upsert_wb_supply_transit_cost_enrichment(
            {
                "supply_id": "transit_enriched",
                "amount": 1000,
                "currency": "RUB",
                "amount_label": "1 000 ₽",
                "is_transit": True,
                "source": "seller_portal_browser",
                "evidence_type": "network_json",
                "confidence": "high",
                "fetched_at": NOW,
                "status": "success",
                "error": "",
                "source_endpoint_path": "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        if block.materialize_wb_supply_cost_layers(opening_date="2026-07-01") != 1:
            raise AssertionError("confirmed supplemental transit evidence must rebuild one canonical cost layer")
        _assert_transit_supply_cost(runtime, expected_status="transit_confirmed", expected_per_unit=100.0)
        if block.materialize_wb_supply_cost_layers(opening_date="2026-07-01") != 0:
            raise AssertionError("repeated transit evidence materialization must be a no-op")
        runtime.upsert_wb_supply_transit_cost_enrichment(
            {
                "supply_id": "transit_enriched",
                "amount": 0,
                "currency": "RUB",
                "amount_label": "0 ₽",
                "is_transit": True,
                "source": "seller_portal_browser",
                "evidence_type": "network_json",
                "confidence": "high",
                "fetched_at": NOW,
                "status": "success",
                "error": "",
                "source_endpoint_path": "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        if block.materialize_wb_supply_cost_layers(opening_date="2026-07-01") != 1:
            raise AssertionError("confirmed zero transit evidence must version one canonical layer")
        _assert_transit_supply_cost(
            runtime,
            expected_status="direct_zero_confirmed",
            expected_per_unit=0.0,
        )
        _seed_wb_supply(
            runtime,
            supply_id="transit_official",
            status_id=4,
            goods=[{"nmID": 497413000, "quantity": 10, "acceptedQuantity": 0}],
            has_transit_cost_marker=True,
            acceptance_cost=0,
            transit_cost=800,
            cost_total=800,
        )
        if block.materialize_wb_supply_cost_layers(opening_date="2026-07-01") != 1:
            raise AssertionError("official normalized transit fact must materialize one canonical cost layer")
        _assert_transit_supply_cost(
            runtime,
            supply_id="transit_official",
            expected_status="transit_confirmed",
            expected_per_unit=80.0,
        )
        _seed_wb_supply(
            runtime,
            supply_id="receiving_accepted_qty",
            status_id=4,
            goods=[{"nmID": 497413000, "quantity": 10, "acceptedQuantity": 7}],
        )
        _seed_wb_supply(
            runtime,
            supply_id="planned_qty_only",
            status_id=4,
            goods=[{"nmID": 497413000, "quantity": 10}],
        )
        _seed_wb_supply(
            runtime,
            supply_id="status6_planned_qty_only",
            status_id=6,
            goods=[{"nmID": 497413000, "quantity": 10}],
        )
        if block.materialize_wb_supply_cost_layers(opening_date="2026-07-01") != 3:
            raise AssertionError("non-final WB quantities must still materialize as non-confirmed layers")
        _assert_wb_quantity_source_status(runtime)
        _assert_physical_inbound_gate_and_idempotency()
        _assert_daily_state_rolls_iso_timestamp_inbound(runtime, block)
        _assert_confirmed_share_partial_bucket_math()
        _assert_total_confirmed_share_is_quantity_weighted()

        supplier_block = SupplierShipmentsBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        try:
            supplier_block.update_order_status("sup_smoke", ORDER_STATUS_ACCEPTED_FF)
        except ValueError as exc:
            if "status-only PATCH" not in str(exc):
                raise
        else:
            raise AssertionError("status-only accepted_ff PATCH must be rejected")

        _seed_supplier_shipment(runtime, shipment_id="sup_trigger", actual_ff_acceptance_date="")
        _seed_financial_inputs(runtime, shipment_id="sup_trigger")
        updated = supplier_block.update_shipment(
            "sup_trigger",
            {
                "shipment_date": "2026-06-20",
                "actual_shipment_date": "2026-06-21",
                "actual_ff_acceptance_date": "2026-06-24",
                "approx_yuan_rate": "11",
                "metadata": {
                    "invoice_no": "26GN310",
                    "invoice_date": "2026-06-20",
                    "contract_no": "CN-1",
                    "contract_date": "2026-06-01",
                    "supplier_name": "Supplier",
                    "customer_name": "Customer",
                    "currency": "CNY",
                },
                "lines": [_supplier_line_payload("sup_trigger_line_1")],
                "warnings": [],
                "errors": [],
            },
        )
        if updated.get("order_status") != ORDER_STATUS_ACCEPTED_FF:
            raise AssertionError(f"actual_ff_acceptance_date must trigger accepted_ff, got {updated.get('order_status')}")
        if not block.has_current_supplier_ff_cost_layer("sup_trigger"):
            raise AssertionError("actual_ff_acceptance_date save must materialize supplier FF cost layer")
        try:
            supplier_block.update_shipment(
                "sup_trigger",
                {
                    **updated,
                    "actual_ff_acceptance_date": "",
                    "metadata": updated.get("metadata") or {},
                    "lines": updated.get("lines") or [],
                },
            )
        except ValueError as exc:
            if "cannot be cleared or changed" not in str(exc):
                raise
        else:
            raise AssertionError("clearing actual_ff_acceptance_date after materialization must be blocked")

        direct = classify_wb_supply_transit(
            {
                "supply_id": "40431461",
                "warehouseName": "Электросталь",
                "has_transit_cost_marker": 0,
                "transit_warehouse_id": None,
                "transit_warehouse_name": "",
                "acceptanceCost": 0,
                "cost_total": 0,
                "cost_evidence": "detail.acceptanceCost",
            },
            denominator=36420,
        )
        if direct.status != TRANSIT_DIRECT_ZERO_CONFIRMED or direct.per_unit != 0:
            raise AssertionError(f"40431461-like direct supply must be direct_zero_confirmed, got {direct}")
        for source_status, expected in (
            ("running", TRANSIT_UPDATING),
            ("not_found", TRANSIT_NOT_FOUND),
            ("failed", TRANSIT_SOURCE_ERROR),
            ("session_expired", TRANSIT_SESSION_EXPIRED),
        ):
            classified = classify_wb_supply_transit(
                {
                    "supply_id": "transit-state-" + source_status,
                    "has_transit_cost_marker": 1,
                    "transit_warehouse_id": 10,
                    "seller_portal_transit_cost_status": source_status,
                },
                denominator=100,
            )
            if (
                classified.status != expected
                or classified.amount_total is not None
                or classified.per_unit is not None
            ):
                raise AssertionError(
                    f"transit state {source_status} became cost evidence: {classified}"
                )

        _assert_proxy_profit_3_evaluator()

    print("our_wb_costs_smoke: ok")


def _seed_supplier_shipment(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    shipment_id: str = "sup_smoke",
    actual_ff_acceptance_date: str = "2026-06-24",
) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": shipment_id,
            "created_at": NOW,
            "updated_at": NOW,
            "shipment_date": "2026-06-20",
            "actual_shipment_date": "2026-06-21",
            "actual_ff_acceptance_date": actual_ff_acceptance_date,
            "order_status": ORDER_STATUS_ACCEPTED_FF if actual_ff_acceptance_date else ORDER_STATUS_PRODUCTION,
            "invoice_no": "26GN310",
            "invoice_date": "2026-06-20",
            "contract_no": "CN-1",
            "contract_date": "2026-06-01",
            "supplier_name": "Supplier",
            "customer_name": "Customer",
            "currency": "CNY",
            "approx_yuan_rate": 11.0,
            "product_qty_total": 10.0,
            "product_amount_total": 900.0,
            "extras_amount_total": 100.0,
            "invoice_amount_total": 1000.0,
            "declared_invoice_total": 1000.0,
            "match_status": "all_matched",
            "warnings": [],
            "errors": [],
        },
        lines=[_supplier_line_payload(f"{shipment_id}_line_1")],
    )


def _supplier_line_payload(line_id: str) -> dict[str, object]:
    return {
        "line_id": line_id,
        "line_type": "product",
        "barcode": SUPPLIER_BARCODE,
        "sort_order": 1,
        "source_no": "1",
        "product_type": "glass",
        "model_raw": "iPhone 15",
        "model_normalized": "iphone 15",
        "match_key": "iphone 15",
        "internal_sku": "SKU-1",
        "internal_nm_id": 497413000,
        "internal_name": "SKU 1",
        "qty": 10.0,
        "unit_price": 90.0,
        "amount": 900.0,
        "currency": "CNY",
        "comment": "",
        "match_status": "matched_by_barcode",
        "manual_override": False,
        "invoice_price_yuan_snapshot": 90.0,
        "reference_purchase_price_yuan_snapshot": 90.0,
        "raw": {},
    }


def _seed_financial_inputs(runtime: RegistryUploadDbBackedRuntime, *, shipment_id: str = "sup_smoke") -> None:
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_supplier_shipments
            SET cny_payment_currency_rub_cost = ?,
                cny_bank_fee_rub = ?,
                cny_calculation_status = 'ok',
                cny_calculated_at = ?
            WHERE shipment_id = ?
            """,
            ("11000", "100", NOW, shipment_id),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_financial_documents (
                document_id, supplier_order_id, document_type, original_filename, stored_file_path,
                file_content_type, file_sha256, uploaded_at, updated_at, parse_status,
                document_number, document_date, currency, total_amount, total_amount_rub,
                raw_parse_json, normalized_parse_json, warnings_json, errors_json
            ) VALUES (?, ?, 'logistics_invoice', 'logistics.pdf', '/tmp/logistics.pdf',
                'application/pdf', ?, ?, ?, 'ok', 'L-1', '2026-06-25', 'RUB', 1000, 1000,
                '{}', '{}', '[]', '[]')
            """,
            (f"{shipment_id}_logistics", shipment_id, f"sha-{shipment_id}-log", NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_financial_documents (
                document_id, supplier_order_id, document_type, original_filename, stored_file_path,
                file_content_type, file_sha256, uploaded_at, updated_at, parse_status,
                document_number, document_date, currency, total_amount, total_amount_rub,
                raw_parse_json, normalized_parse_json, warnings_json, errors_json
            ) VALUES (?, ?, 'customs_declaration', 'customs.pdf', '/tmp/customs.pdf',
                'application/pdf', ?, ?, ?, 'ok', 'C-1', '2026-06-26', 'RUB', 500, 500,
                '{}', '{}', '[]', '[]')
            """,
            (f"{shipment_id}_customs", shipment_id, f"sha-{shipment_id}-customs", NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_financial_expense_lines (
                line_id, financial_document_id, supplier_order_id, sort_order, category, amount,
                currency, amount_rub, included_in_logistics_efficiency, included_in_customs_total,
                raw_json
            ) VALUES (?, ?, ?, 1, 'logistics', 1000, 'RUB', 1000, 1, 0, '{}')
            """,
            (f"{shipment_id}_logistics_line", f"{shipment_id}_logistics", shipment_id),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_financial_expense_lines (
                line_id, financial_document_id, supplier_order_id, sort_order, category, amount,
                currency, amount_rub, included_in_logistics_efficiency, included_in_customs_total,
                raw_json
            ) VALUES (?, ?, ?, 1, 'customs', 500, 'RUB', 500, 0, 1, '{}')
            """,
            (f"{shipment_id}_customs_line", f"{shipment_id}_customs", shipment_id),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_financial_documents (
                document_id, supplier_order_id, document_type, original_filename, stored_file_path,
                file_content_type, file_sha256, uploaded_at, updated_at, parse_status,
                document_number, document_date, currency, total_amount, total_amount_rub,
                raw_parse_json, normalized_parse_json, warnings_json, errors_json
            ) VALUES (?, ?, 'logistics_invoice', 'archive-136.pdf', '/tmp/archive-136.pdf',
                'application/pdf', ?, ?, ?, 'excluded', '136', '2026-06-25', 'RUB', 1075030, 1075030,
                '{}', '{}', '[]', '[]')
            """,
            (
                f"{shipment_id}_archive_136",
                shipment_id,
                f"sha-{shipment_id}-archive-136",
                NOW,
                NOW,
            ),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_financial_expense_lines (
                line_id, financial_document_id, supplier_order_id, sort_order, category, amount,
                currency, amount_rub, included_in_logistics_efficiency, included_in_customs_total,
                raw_json
            ) VALUES (?, ?, ?, 1, 'logistics', 1075030, 'RUB', 1075030, 1, 0, '{}')
            """,
            (
                f"{shipment_id}_archive_136_line",
                f"{shipment_id}_archive_136",
                shipment_id,
            ),
        )


def _seed_wb_supply(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    supply_id: str = "40431461",
    status_id: int = 5,
    goods: list[dict[str, object]] | None = None,
    has_transit_cost_marker: bool = False,
    acceptance_cost: float | None = 0,
    transit_cost: float | None = None,
    cost_total: float | None = 0,
) -> None:
    goods_payload = goods or [{"nmID": 497413000, "quantity": 10, "acceptedQuantity": 10}]
    quantity_total = sum(float(item.get("quantity") or item.get("acceptedQuantity") or 0) for item in goods_payload)
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_wb_supplies (
                supply_id,
                cache_key,
                wb_supply_id,
                normalized_row_json,
                raw_goods_json,
                quantity_for_size_filter,
                supply_date,
                fact_date,
                synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                supply_id,
                f"supply:{supply_id}",
                supply_id,
                json.dumps(
                    {
                        "supply_id": supply_id,
                        "status_id": status_id,
                        "warehouseName": "Электросталь",
                        "has_transit_cost_marker": 1 if has_transit_cost_marker else 0,
                        "transit_warehouse_id": 507 if has_transit_cost_marker else None,
                        "acceptanceCost": acceptance_cost,
                        "transitCost": transit_cost,
                        "cost_total": cost_total,
                        "cost_evidence": "detail.acceptanceCost",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(goods_payload, ensure_ascii=False),
                quantity_total,
                "2026-07-03",
                "2026-07-03",
                NOW,
            ),
        )


def _assert_wb_supply_cost_layer(runtime: RegistryUploadDbBackedRuntime) -> None:
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT transit_cost_status, transit_per_unit_rub, our_wb_unit_cost_rub, source_status
            FROM sheet_vitrina_v1_wb_supply_cost_layers
            WHERE wb_supply_id = '40431461' AND nm_id = 497413000 AND is_current = 1
            """
        ).fetchone()
    if row is None:
        raise AssertionError("WB supply cost layer missing")
    if row["transit_cost_status"] != TRANSIT_DIRECT_ZERO_CONFIRMED or float(row["transit_per_unit_rub"]) != 0.0:
        raise AssertionError(f"direct WB supply must have confirmed zero transit, got {dict(row)}")
    if row["our_wb_unit_cost_rub"] is None:
        raise AssertionError("WB supply cost layer must calculate our_wb_unit_cost_rub")


def _assert_transit_supply_cost(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    supply_id: str = "transit_enriched",
    expected_status: str,
    expected_per_unit: float | None,
) -> None:
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT transit_cost_status, transit_amount_total, transit_per_unit_rub,
                   our_wb_unit_cost_rub, missing_reason
            FROM sheet_vitrina_v1_wb_supply_cost_layers
            WHERE wb_supply_id = ? AND nm_id = 497413000 AND is_current = 1
            """,
            (supply_id,),
        ).fetchone()
    if row is None or row["transit_cost_status"] != expected_status:
        raise AssertionError(
            f"transit enrichment canonical status mismatch: {dict(row) if row else None}"
        )
    if expected_per_unit is None:
        if (
            row["transit_amount_total"] is not None
            or row["transit_per_unit_rub"] is not None
            or row["our_wb_unit_cost_rub"] is not None
            or str(row["missing_reason"] or "") != (
            "transit_marker_present_but_cost_missing"
            )
        ):
            raise AssertionError(f"missing transit cost must remain fail-closed: {dict(row)}")
    else:
        _assert_close(
            float(row["transit_per_unit_rub"]),
            expected_per_unit,
            "supplemental transit cost per full packed composition",
        )
        if row["our_wb_unit_cost_rub"] is None:
            raise AssertionError("confirmed transit evidence must produce canonical WB unit cost")


def _assert_wb_quantity_source_status(runtime: RegistryUploadDbBackedRuntime) -> None:
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        rows = {
            str(row["wb_supply_id"]): dict(row)
            for row in conn.execute(
                """
                SELECT wb_supply_id, accepted_qty, qty_denominator, source_status, component_status_json
                FROM sheet_vitrina_v1_wb_supply_cost_layers
                WHERE wb_supply_id IN ('receiving_accepted_qty', 'planned_qty_only', 'status6_planned_qty_only')
                  AND nm_id = 497413000
                  AND is_current = 1
                """
            ).fetchall()
        }
    receiving = rows.get("receiving_accepted_qty")
    planned = rows.get("planned_qty_only")
    shipped_planned = rows.get("status6_planned_qty_only")
    if receiving is None or planned is None or shipped_planned is None:
        raise AssertionError(f"quantity source regression layers missing, got {rows}")
    if (
        receiving["source_status"] == WB_COST_STATUS_CONFIRMED
        or planned["source_status"] == WB_COST_STATUS_CONFIRMED
        or shipped_planned["source_status"] == WB_COST_STATUS_CONFIRMED
    ):
        raise AssertionError(f"non-final/planned quantity must not become confirmed, got {rows}")
    if (
        float(receiving["accepted_qty"]) != 7.0
        or float(planned["accepted_qty"]) != 0.0
        or float(shipped_planned["accepted_qty"]) != 0.0
    ):
        raise AssertionError(f"accepted quantity must never fall back to packed quantity, got {rows}")
    if any(float(item["qty_denominator"]) != 10.0 for item in (receiving, planned, shipped_planned)):
        raise AssertionError(f"all pre-acceptance expense denominators must use full packed quantity, got {rows}")
    receiving_components = json.loads(str(receiving["component_status_json"]))
    planned_components = json.loads(str(planned["component_status_json"]))
    shipped_planned_components = json.loads(str(shipped_planned["component_status_json"]))
    if receiving_components.get("wb_quantity_final_accepted") is not False:
        raise AssertionError(f"receiving status must not be final accepted, got {receiving_components}")
    if planned_components.get("wb_quantity_source") != "accepted_quantity_missing":
        raise AssertionError(f"missing accepted quantity must remain explicit, got {planned_components}")
    if shipped_planned_components.get("wb_supply_status_id") != 6:
        raise AssertionError(f"status 6 planned-only evidence must be preserved, got {shipped_planned_components}")
    if shipped_planned_components.get("wb_quantity_final_accepted") is not False:
        raise AssertionError(f"status 6 planned-only quantity must not be final accepted, got {shipped_planned_components}")


def _assert_physical_inbound_gate_and_idempotency() -> None:
    with TemporaryDirectory(prefix="our-wb-costs-physical-inbound-smoke-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-07-01T00:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"physical-inbound fixture bundle must be accepted, got {accepted}")
        current_state = runtime.load_current_state()
        transitioned_nm = 888200001
        status6_nm = 888200002
        unknown_cost_nm = 888200003
        opening_stocks = {
            transitioned_nm: 100,
            status6_nm: 100,
            unknown_cost_nm: 100,
        }
        for as_of_date in ("2026-07-01", "2026-07-02"):
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{as_of_date}T06:00:00Z",
                plan=_daily_stock_plan_many(as_of_date=as_of_date, stock_by_nm=opening_stocks),
            )
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            for nm_id, label in (
                (transitioned_nm, "status 4 to 5"),
                (status6_nm, "status 6 planned"),
                (unknown_cost_nm, "accepted unknown cost"),
            ):
                _insert_opening_baseline(
                    conn,
                    nm_id=nm_id,
                    display_name=label,
                    stock_qty=100,
                    unit_cost=100,
                    source_status="opening_confirmed_supply",
                    confirmed_qty=100,
                    estimated_qty=0,
                    fallback_qty=0,
                )
            _insert_wb_cost_layer(
                conn,
                layer_id="planned_status4_v1",
                supply_id="planned_status4",
                nm_id=transitioned_nm,
                qty=20,
                supply_date="2026-07-02",
                accepted_date="2026-07-02T08:00:00+03:00",
                unit_cost=110,
                source_status=WB_COST_STATUS_ESTIMATED,
                status_id=4,
                final_accepted=False,
            )
            _insert_wb_cost_layer(
                conn,
                layer_id="planned_status6_v1",
                supply_id="planned_status6",
                nm_id=status6_nm,
                qty=20,
                supply_date="2026-07-02",
                accepted_date="2026-07-02T08:00:00+03:00",
                unit_cost=110,
                source_status=WB_COST_STATUS_ESTIMATED,
                status_id=6,
                quantity_source="quantity",
                final_accepted=False,
            )
        block = OurWbCostBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        block.materialize_daily_state(opening_date="2026-07-01")
        july2 = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-02")
        for nm_id in (transitioned_nm, status6_nm):
            _assert_close(float(july2[nm_id]["confirmed_qty"]), 100.0, "planned/open confirmed bucket")
            _assert_close(float(july2[nm_id]["estimated_qty"]), 0.0, "planned/open estimated bucket")

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-07-03T06:00:00Z",
            plan=_daily_stock_plan_many(
                as_of_date="2026-07-03",
                stock_by_nm={
                    transitioned_nm: 120,
                    status6_nm: 100,
                    unknown_cost_nm: 120,
                },
            ),
        )
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_supply_cost_layers
                SET is_current = 0, superseded_at = ?
                WHERE wb_supply_cost_layer_id = 'planned_status4_v1'
                """,
                (NOW,),
            )
            _insert_wb_cost_layer(
                conn,
                layer_id="planned_status4_v2",
                supply_id="planned_status4",
                nm_id=transitioned_nm,
                qty=20,
                supply_date="2026-07-02",
                accepted_date="2026-07-03T08:00:00+03:00",
                unit_cost=110,
                source_status=WB_COST_STATUS_CONFIRMED,
                status_id=5,
                final_accepted=True,
                version=2,
            )
            _insert_wb_cost_layer(
                conn,
                layer_id="accepted_unknown_cost_v1",
                supply_id="accepted_unknown_cost",
                nm_id=unknown_cost_nm,
                qty=20,
                supply_date="2026-07-03",
                accepted_date="2026-07-03T09:00:00+03:00",
                unit_cost=None,
                source_status="needs_review",
                status_id=5,
                final_accepted=True,
            )
        first_rebuild_count = block.materialize_daily_state(opening_date="2026-07-01")
        july3 = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-03")
        transitioned = july3[transitioned_nm]
        _assert_close(float(transitioned["confirmed_qty"]), 120.0, "status 4 to 5 confirmed once")
        _assert_close(float(transitioned["estimated_qty"]), 0.0, "status 4 to 5 no stale estimate")
        status6 = july3[status6_nm]
        _assert_close(float(status6["confirmed_qty"]), 100.0, "status 6 no physical inbound")
        _assert_close(float(status6["estimated_qty"]), 0.0, "status 6 no estimated inbound")
        unknown = july3[unknown_cost_nm]
        _assert_close(float(unknown["confirmed_qty"]), 100.0, "unknown cost preserves confirmed base")
        _assert_close(float(unknown["estimated_qty"]), 20.0, "unknown cost explicit estimated bucket")
        _assert_close(
            float(unknown["stock_qty"])
            - float(unknown["confirmed_qty"])
            - float(unknown["estimated_qty"])
            - float(unknown["fallback_qty"]),
            0.0,
            "unknown cost unbucketed quantity",
        )
        if first_rebuild_count <= 0:
            raise AssertionError("physical inbound transition must update daily state")
        if block.materialize_daily_state(opening_date="2026-07-01") != 0:
            raise AssertionError("unchanged physical daily rebuild must be idempotent")
        block.rebuild_all(opening_date="2026-07-01")
        second_full_rebuild = block.rebuild_all(opening_date="2026-07-01")
        if any(
            (
                second_full_rebuild.supplier_layers_materialized,
                second_full_rebuild.wb_supply_layers_materialized,
                second_full_rebuild.opening_rows_materialized,
                second_full_rebuild.daily_state_rows_materialized,
            )
        ):
            raise AssertionError(f"unchanged full our-WB rebuild must be idempotent, got {second_full_rebuild}")


def _assert_daily_state_rolls_iso_timestamp_inbound(
    runtime: RegistryUploadDbBackedRuntime,
    block: OurWbCostBlock,
) -> None:
    nm_id = 888000001
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    accepted = runtime.ingest_bundle(bundle, activated_at="2026-07-01T00:00:00Z")
    if accepted.status != "accepted":
        raise AssertionError(f"daily rolling fixture bundle must be accepted, got {accepted}")
    current_state = runtime.load_current_state()
    for as_of_date, stock_qty in (
        ("2026-07-01", 10),
        ("2026-07-02", 0),
        ("2026-07-03", 0),
        ("2026-07-04", 20),
    ):
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=f"{as_of_date}T06:00:00Z",
            plan=_daily_stock_plan(as_of_date=as_of_date, nm_id=nm_id, stock_qty=stock_qty),
        )
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_wb_opening_baseline (
                as_of_date,
                nm_id,
                display_name,
                opening_stock_qty,
                opening_unit_cost_rub,
                source_priority,
                source_status,
                confirmed_qty,
                estimated_qty,
                fallback_qty,
                component_status_json,
                calculated_at,
                inputs_hash
            ) VALUES ('2026-07-01', ?, 'SKU 1', 10, 100, 1, 'opening_confirmed_supply', 10, 0, 0, '{}', ?, 'opening')
            """,
            (nm_id, NOW),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_wb_supply_cost_layers (
                wb_supply_cost_layer_id,
                wb_supply_id,
                nm_id,
                accepted_qty,
                qty_denominator,
                supply_date,
                accepted_date,
                sku_ff_unit_cost_rub,
                transit_cost_status,
                transit_per_unit_rub,
                ff_services_amount_total,
                ff_services_per_unit_rub,
                ff_storage_amount_total,
                ff_storage_per_unit_rub,
                our_wb_unit_cost_rub,
                source_status,
                component_status_json,
                calculated_at,
                inputs_hash,
                version,
                is_current
            ) VALUES (
                'wbcost_iso_ts_888000001_1',
                'iso_ts',
                ?,
                20,
                20,
                '2026-07-03T00:00:00+03:00',
                '2026-07-03',
                100,
                'direct_zero_confirmed',
                0,
                0,
                0,
                0,
                0,
                110,
                'confirmed',
                '{"wb_quantity_final_accepted": true, "wb_quantity_source": "acceptedQuantity", "wb_supply_status_id": 5}',
                ?,
                'iso-ts-layer',
                1,
                1
            )
            """,
            (nm_id, NOW),
        )
    daily_rows = block.materialize_daily_state(opening_date="2026-07-01")
    if daily_rows < 4:
        raise AssertionError(f"daily state materialization must cover fixture dates, got {daily_rows}")
    zero_stock_state = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-03").get(nm_id)
    if zero_stock_state is None:
        raise AssertionError("daily state must include ISO timestamp inbound SKU row on zero-stock day")
    if float(zero_stock_state["stock_qty"]) != 0.0 or float(zero_stock_state["confirmed_qty"]) != 0.0:
        raise AssertionError(f"zero-stock day must not persist off-stock confirmed bucket, got {zero_stock_state}")
    if zero_stock_state["confirmed_share_pct"] is not None:
        raise AssertionError(f"zero-stock day confirmed share must stay blank, got {zero_stock_state}")
    state = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-04").get(nm_id)
    if state is None:
        raise AssertionError("daily state must include ISO timestamp inbound SKU row")
    if float(state["confirmed_qty"]) != 20.0 or float(state["confirmed_share_pct"]) != 1.0:
        raise AssertionError(f"ISO timestamp supply_date must carry into confirmed bucket when stock appears, got {state}")
    if abs(float(state["our_wb_unit_cost_rub"]) - 110.0) > 0.000001:
        raise AssertionError(f"ISO timestamp inbound cost must drive post-gap unit cost, got {state}")


def _assert_confirmed_share_partial_bucket_math() -> None:
    with TemporaryDirectory(prefix="our-wb-costs-partial-share-smoke-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        block = OurWbCostBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-07-01T00:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"partial-share fixture bundle must be accepted, got {accepted}")
        current_state = runtime.load_current_state()
        fallback_then_confirmed_nm = 888100001
        confirmed_then_estimated_nm = 888100002
        stock_by_date = {
            "2026-07-01": {
                fallback_then_confirmed_nm: 100,
                confirmed_then_estimated_nm: 100,
            },
            "2026-07-02": {
                fallback_then_confirmed_nm: 150,
                confirmed_then_estimated_nm: 150,
            },
            "2026-07-03": {
                fallback_then_confirmed_nm: 120,
                confirmed_then_estimated_nm: 150,
            },
            "2026-07-04": {
                fallback_then_confirmed_nm: 0,
                confirmed_then_estimated_nm: 150,
            },
        }
        for as_of_date, stocks in stock_by_date.items():
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{as_of_date}T06:00:00Z",
                plan=_daily_stock_plan_many(as_of_date=as_of_date, stock_by_nm=stocks),
            )
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            _insert_opening_baseline(
                conn,
                nm_id=fallback_then_confirmed_nm,
                display_name="fallback opening then confirmed inbound",
                stock_qty=100,
                unit_cost=80,
                source_status="metric11_2026_07_01_fallback",
                confirmed_qty=0,
                estimated_qty=0,
                fallback_qty=100,
            )
            _insert_opening_baseline(
                conn,
                nm_id=confirmed_then_estimated_nm,
                display_name="confirmed opening then estimated inbound",
                stock_qty=100,
                unit_cost=120,
                source_status="opening_confirmed_supply",
                confirmed_qty=100,
                estimated_qty=0,
                fallback_qty=0,
            )
            _insert_wb_cost_layer(
                conn,
                layer_id="partial_confirmed_inbound",
                supply_id="partial_confirmed_inbound",
                nm_id=fallback_then_confirmed_nm,
                qty=50,
                supply_date="2026-07-02T00:00:00+03:00",
                unit_cost=100,
                source_status=WB_COST_STATUS_CONFIRMED,
            )
            _insert_wb_cost_layer(
                conn,
                layer_id="partial_estimated_inbound",
                supply_id="partial_estimated_inbound",
                nm_id=confirmed_then_estimated_nm,
                qty=50,
                supply_date="2026-07-02",
                unit_cost=130,
                source_status=WB_COST_STATUS_ESTIMATED,
            )
        daily_rows = block.materialize_daily_state(opening_date="2026-07-01")
        if daily_rows < 8:
            raise AssertionError(f"partial-share daily state must materialize fixture rows, got {daily_rows}")

        july1 = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-01")[fallback_then_confirmed_nm]
        if float(july1["stock_qty"]) != 100.0 or float(july1["confirmed_share_pct"]) != 0.0:
            raise AssertionError(f"fallback opening stock must show 0% confirmed share, got {july1}")
        july2 = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-02")[fallback_then_confirmed_nm]
        _assert_close(float(july2["confirmed_qty"]), 50.0, "fallback+confirmed confirmed_qty")
        _assert_close(float(july2["fallback_qty"]), 100.0, "fallback+confirmed fallback_qty")
        _assert_close(float(july2["confirmed_share_pct"]), 1.0 / 3.0, "fallback+confirmed share")
        july3 = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-03")[fallback_then_confirmed_nm]
        _assert_close(float(july3["confirmed_qty"]), 40.0, "scaled confirmed_qty")
        _assert_close(float(july3["fallback_qty"]), 80.0, "scaled fallback_qty")
        _assert_close(float(july3["confirmed_share_pct"]), 1.0 / 3.0, "scaled confirmed share")
        july4 = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-04")[fallback_then_confirmed_nm]
        if float(july4["stock_qty"]) != 0.0 or july4["confirmed_share_pct"] is not None:
            raise AssertionError(f"zero stock must display blank confirmed share, got {july4}")

        estimated_mix = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-02")[confirmed_then_estimated_nm]
        _assert_close(float(estimated_mix["confirmed_qty"]), 100.0, "confirmed+estimated confirmed_qty")
        _assert_close(float(estimated_mix["estimated_qty"]), 50.0, "confirmed+estimated estimated_qty")
        _assert_close(float(estimated_mix["confirmed_share_pct"]), 2.0 / 3.0, "confirmed+estimated share")


def _assert_total_confirmed_share_is_quantity_weighted() -> None:
    config = [
        ConfigV2Item(nm_id=900000001, enabled=True, display_name="huge confirmed", group="A", display_order=1),
        ConfigV2Item(nm_id=900000002, enabled=True, display_name="tiny fallback", group="B", display_order=2),
    ]
    metrics = extend_metrics_with_our_wb_cost_metrics([])
    metrics_by_key = {item.metric_key: item for item in metrics}
    temporal_slot = SheetVitrinaV1TemporalSlot(slot_key="after", slot_label="after", column_date="2026-07-02")
    evaluator = _MetricEvaluator(
        enabled_config=config,
        metrics_by_key=metrics_by_key,
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=[temporal_slot],
            statuses=[],
            slot_lookups={
                "after": SlotLookups(
                    seller_funnel_lookup={},
                    history_lookup={},
                    web_lookup={},
                    prices_lookup={},
                    sf_period_lookup={},
                    spp_lookup={},
                    ads_bids_lookup={},
                    stocks_lookup={},
                    onec_stocks_lookup={},
                    ads_compact_lookup={},
                    fin_lookup={},
                    fin_storage_fee_total=None,
                    cost_price_lookup={},
                    promo_lookup={},
                    our_wb_cost_lookup={
                        900000001: {
                            "our_wb_unit_cost_rub": 100.0,
                            "stock_qty": 1000.0,
                            "confirmed_qty": 1000.0,
                            "confirmed_share_pct": 1.0,
                        },
                        900000002: {
                            "our_wb_unit_cost_rub": 100.0,
                            "stock_qty": 1.0,
                            "confirmed_qty": 0.0,
                            "confirmed_share_pct": 0.0,
                        },
                    },
                    column_date="2026-07-02",
                )
            },
            source_temporal_policies={},
        ),
    )
    huge_share = evaluator.resolve_sku(OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY, 900000001, "after")
    tiny_share = evaluator.resolve_sku(OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY, 900000002, "after")
    if huge_share != 1.0 or tiny_share != 0.0:
        raise AssertionError(f"SKU shares must read daily-state values, got {huge_share=} {tiny_share=}")
    total_share = evaluator.resolve_total(TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY, "after")
    _assert_close(float(total_share or 0.0), 1000.0 / 1001.0, "TOTAL confirmed share must be quantity weighted")
    if abs(float(total_share or 0.0) - 0.5) < 0.01:
        raise AssertionError(f"TOTAL confirmed share must not average SKU percentages, got {total_share}")


def _daily_stock_plan(*, as_of_date: str, nm_id: int, stock_qty: float) -> SheetVitrinaV1Envelope:
    return _daily_stock_plan_many(as_of_date=as_of_date, stock_by_nm={nm_id: stock_qty})


def _daily_stock_plan_many(*, as_of_date: str, stock_by_nm: dict[int, float]) -> SheetVitrinaV1Envelope:
    rows = [[f"SKU {nm_id}: Остаток", f"SKU:{nm_id}|stock_total", stock_qty] for nm_id, stock_qty in stock_by_nm.items()]
    return SheetVitrinaV1Envelope(
        plan_version="daily-roll-fixture",
        snapshot_id=f"daily-roll-{as_of_date}",
        as_of_date=as_of_date,
        date_columns=[as_of_date],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="closed_day",
                slot_label="Closed day",
                column_date=as_of_date,
            )
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:C2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", as_of_date],
                rows=rows,
                row_count=len(rows),
                column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=[
                    "source_key",
                    "kind",
                    "freshness",
                    "snapshot_date",
                    "date",
                    "date_from",
                    "date_to",
                    "requested_count",
                    "covered_count",
                    "missing_nm_ids",
                    "note",
                ],
                rows=[
                    [
                        "stock_total[closed_day]",
                        "success",
                        as_of_date,
                        as_of_date,
                        as_of_date,
                        as_of_date,
                        as_of_date,
                        1,
                        1,
                        "",
                        "",
                    ]
                ],
                row_count=1,
                column_count=11,
            ),
        ],
    )


def _insert_opening_baseline(
    conn,
    *,
    nm_id: int,
    display_name: str,
    stock_qty: float,
    unit_cost: float,
    source_status: str,
    confirmed_qty: float,
    estimated_qty: float,
    fallback_qty: float,
) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_wb_opening_baseline (
            as_of_date,
            nm_id,
            display_name,
            opening_stock_qty,
            opening_unit_cost_rub,
            source_priority,
            source_status,
            confirmed_qty,
            estimated_qty,
            fallback_qty,
            component_status_json,
            calculated_at,
            inputs_hash
        ) VALUES ('2026-07-01', ?, ?, ?, ?, 1, ?, ?, ?, ?, '{}', ?, ?)
        """,
        (
            nm_id,
            display_name,
            stock_qty,
            unit_cost,
            source_status,
            confirmed_qty,
            estimated_qty,
            fallback_qty,
            NOW,
            f"opening-{nm_id}",
        ),
    )


def _insert_wb_cost_layer(
    conn,
    *,
    layer_id: str,
    supply_id: str,
    nm_id: int,
    qty: float,
    supply_date: str,
    unit_cost: float | None,
    source_status: str,
    accepted_date: str | None = None,
    status_id: int = 5,
    quantity_source: str = "acceptedQuantity",
    final_accepted: bool = True,
    version: int = 1,
    is_current: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_wb_supply_cost_layers (
            wb_supply_cost_layer_id,
            wb_supply_id,
            nm_id,
            accepted_qty,
            qty_denominator,
            supply_date,
            accepted_date,
            sku_ff_unit_cost_rub,
            transit_cost_status,
            transit_per_unit_rub,
            ff_services_amount_total,
            ff_services_per_unit_rub,
            ff_storage_amount_total,
            ff_storage_per_unit_rub,
            our_wb_unit_cost_rub,
            source_status,
            component_status_json,
            calculated_at,
            inputs_hash,
            version,
            is_current
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'direct_zero_confirmed', 0, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            layer_id,
            supply_id,
            nm_id,
            qty,
            qty,
            supply_date,
            accepted_date or str(supply_date)[:10],
            unit_cost,
            unit_cost,
            source_status,
            json.dumps(
                {
                    "fixture_source_status": source_status,
                    "wb_supply_status_id": status_id,
                    "wb_quantity_source": quantity_source,
                    "wb_quantity_final_accepted": final_accepted,
                },
                ensure_ascii=False,
            ),
            NOW,
            f"layer-{layer_id}-v{version}",
            version,
            1 if is_current else 0,
        ),
    )


def _assert_close(actual: float, expected: float, label: str, *, tolerance: float = 0.000001) -> None:
    if abs(actual - expected) > tolerance:
        raise AssertionError(f"{label}: expected {expected}, got {actual}")


def _assert_supplier_ff_reconciliation(runtime: RegistryUploadDbBackedRuntime) -> None:
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT layer.status,layer.weighted_avg_ff_unit_cost_rub,layer.reconciliation_status,
                   line.qty AS accepted_qty
            FROM sheet_vitrina_v1_supplier_ff_cost_layers layer
            JOIN sheet_vitrina_v1_supplier_ff_cost_layer_lines line ON line.layer_id=layer.layer_id
            WHERE layer.supplier_shipment_id = 'sup_smoke' AND layer.is_current = 1
            """
        ).fetchone()
    if row is None:
        raise AssertionError("supplier FF cost layer missing")
    expected = 1260.0
    actual = float(row["weighted_avg_ff_unit_cost_rub"])
    if abs(actual - expected) > 0.000001:
        raise AssertionError(f"weighted SKU FF average must reconcile to {expected}, got {actual}")
    if row["reconciliation_status"] != "ok":
        raise AssertionError(f"reconciliation must be ok, got {row['reconciliation_status']}")
    if float(row["accepted_qty"]) != 7:
        raise AssertionError("supplier FF cost layer must retain only actual accepted quantity")


def _assert_proxy_profit_3_evaluator() -> None:
    config = [
        ConfigV2Item(nm_id=497413000, enabled=True, display_name="SKU 1", group="A", display_order=1),
        ConfigV2Item(nm_id=497413001, enabled=True, display_name="SKU 2", group="A", display_order=2),
    ]
    base_metrics = [
        _metric("orderSum"),
        _metric("orderCount"),
        _metric("ads_sum"),
        MetricV2Item(
            metric_key="total_orderSum",
            enabled=True,
            scope="TOTAL",
            label_ru="total_orderSum",
            calc_type="metric",
            calc_ref="orderSum",
            show_in_data=True,
            format="number",
            display_order=1,
            section="test",
        ),
    ]
    metrics = extend_metrics_with_our_wb_cost_metrics(extend_metrics_with_onec_stock_metrics(base_metrics))
    metrics_by_key = {item.metric_key: item for item in metrics}
    sku_margin_metric = metrics_by_key[OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY]
    total_margin_metric = metrics_by_key[OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY]
    if (
        sku_margin_metric.label_ru != "Прокси маржинальность 3, %"
        or total_margin_metric.label_ru != "Прокси маржинальность 3 всего, %"
        or sku_margin_metric.format != "percent"
        or total_margin_metric.format != "percent"
        or sku_margin_metric.section != "Экономика"
        or total_margin_metric.section != "Экономика"
        or sku_margin_metric.calc_type != "metric"
        or total_margin_metric.calc_type != "metric"
        or not sku_margin_metric.enabled
        or not total_margin_metric.enabled
        or not sku_margin_metric.show_in_data
        or not total_margin_metric.show_in_data
    ):
        raise AssertionError(f"proxy margin 3 catalog metadata mismatch: {sku_margin_metric}, {total_margin_metric}")
    if (
        sku_margin_metric.display_order
        != metrics_by_key[OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY].display_order + 1
        or total_margin_metric.display_order
        != metrics_by_key[OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY].display_order + 1
    ):
        raise AssertionError("proxy margin 3 rows must immediately follow proxy profit 3 in each scope")
    temporal_slots = [
        SheetVitrinaV1TemporalSlot(slot_key="before", slot_label="before", column_date="2026-06-30"),
        SheetVitrinaV1TemporalSlot(slot_key="after", slot_label="after", column_date="2026-07-02"),
    ]
    lookups = {
        "before": _slot_lookup(
            column_date="2026-06-30",
            onec_cost=100.0,
            our_cost=80.0,
        ),
        "after": _slot_lookup(
            column_date="2026-07-02",
            onec_cost=100.0,
            our_cost=80.0,
        ),
    }
    evaluator = _MetricEvaluator(
        enabled_config=config,
        metrics_by_key=metrics_by_key,
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=temporal_slots,
            statuses=[],
            slot_lookups=lookups,
            source_temporal_policies={},
        ),
    )
    before_proxy2 = evaluator.resolve_sku(ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY, 497413000, "before")
    before_proxy3 = evaluator.resolve_sku(OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, 497413000, "before")
    expected_before = 1000.0 * 0.91 * 0.56 - 2.0 * 0.91 * 80.0 - 10.0
    _assert_close(float(before_proxy3 or 0.0), expected_before, "retrospective proxy3")
    if before_proxy3 == before_proxy2:
        raise AssertionError("proxy3 before opening must not substitute proxy2")
    after_proxy3 = evaluator.resolve_sku(OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, 497413000, "after")
    expected_after = 1000.0 * 0.5096 - 2.0 * 0.91 * 80.0 - 10.0
    if abs(float(after_proxy3 or 0.0) - expected_after) > 0.000001:
        raise AssertionError(f"proxy3 after opening must use our WB cost, got {after_proxy3}")
    after_margin3 = evaluator.resolve_sku(OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY, 497413000, "after")
    _assert_close(float(after_margin3 or 0.0), expected_after / (1000.0 * 0.91), "SKU proxy margin 3")
    before_margin2 = evaluator.resolve_sku(ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY, 497413000, "before")
    before_margin3 = evaluator.resolve_sku(OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY, 497413000, "before")
    _assert_close(
        float(before_margin3 or 0.0),
        expected_before / (1000.0 * 0.91),
        "retrospective proxy margin 3",
    )
    if before_margin3 == before_margin2:
        raise AssertionError("margin3 before opening must not substitute margin2")
    total_after = evaluator.resolve_total(OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY, "after")
    second_after = evaluator.resolve_sku(OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, 497413001, "after")
    _assert_close(float(total_after or 0.0), float(after_proxy3 or 0.0) + float(second_after or 0.0), "total proxy3")
    total_margin3 = evaluator.resolve_total(OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY, "after")
    expected_total_margin = float(total_after or 0.0) / (1100.0 * 0.91)
    _assert_close(float(total_margin3 or 0.0), expected_total_margin, "TOTAL proxy margin 3 ratio of aggregates")
    row_average = (
        float(after_margin3 or 0.0)
        + float(evaluator.resolve_sku(OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY, 497413001, "after") or 0.0)
    ) / 2.0
    if abs(float(total_margin3 or 0.0) - row_average) < 0.000001:
        raise AssertionError("TOTAL proxy margin 3 must not average SKU percentages")

    zero_evaluator = _MetricEvaluator(
        enabled_config=config[:1],
        metrics_by_key=metrics_by_key,
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=[SheetVitrinaV1TemporalSlot(slot_key="zero", slot_label="zero", column_date="2026-07-02")],
            statuses=[],
            slot_lookups={
                "zero": _slot_lookup(
                    column_date="2026-07-02",
                    onec_cost=100.0,
                    our_cost=80.0,
                    first_order_sum=0.0,
                    first_order_count=0.0,
                    first_ads_sum=0.0,
                )
            },
            source_temporal_policies={},
        ),
    )
    if zero_evaluator.resolve_sku(OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY, 497413000, "zero") is not None:
        raise AssertionError("SKU proxy margin 3 zero denominator must return null")
    if zero_evaluator.resolve_total(OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY, "zero") is not None:
        raise AssertionError("TOTAL proxy margin 3 zero denominator must return null")

    missing_evaluator = _MetricEvaluator(
        enabled_config=config[:1],
        metrics_by_key=metrics_by_key,
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=[SheetVitrinaV1TemporalSlot(slot_key="missing", slot_label="missing", column_date="2026-07-02")],
            statuses=[],
            slot_lookups={
                "missing": _slot_lookup(
                    column_date="2026-07-02",
                    onec_cost=100.0,
                    our_cost=80.0,
                    first_order_sum=None,
                )
            },
            source_temporal_policies={},
        ),
    )
    if missing_evaluator.resolve_sku(OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY, 497413000, "missing") is not None:
        raise AssertionError("SKU proxy margin 3 missing operand must return None")
    if missing_evaluator.resolve_total(OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY, "missing") is not None:
        raise AssertionError("TOTAL proxy margin 3 missing operand must return None")

    partial_lookup = _slot_lookup(column_date="2026-07-02", onec_cost=100.0, our_cost=80.0)
    partial_lookup.our_wb_cost_lookup.pop(497413001)
    partial_evaluator = _MetricEvaluator(
        enabled_config=config,
        metrics_by_key=metrics_by_key,
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=[SheetVitrinaV1TemporalSlot(slot_key="partial", slot_label="partial", column_date="2026-07-02")],
            statuses=[],
            slot_lookups={"partial": partial_lookup},
            source_temporal_policies={},
        ),
    )
    if partial_evaluator.resolve_sku(OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, 497413000, "partial") is None:
        raise AssertionError("complete SKU proxy 3 must remain calculable")
    if partial_evaluator.resolve_total(OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY, "partial") is not None:
        raise AssertionError("TOTAL proxy 3 must not turn one missing SKU operand into zero")


def _metric(metric_key: str) -> MetricV2Item:
    return MetricV2Item(
        metric_key=metric_key,
        enabled=True,
        scope="SKU",
        label_ru=metric_key,
        calc_type="metric",
        calc_ref=metric_key,
        show_in_data=True,
        format="number",
        display_order=1,
        section="test",
    )


def _slot_lookup(
    *,
    column_date: str,
    onec_cost: float,
    our_cost: float,
    first_order_sum: float | None = 1000.0,
    first_order_count: float = 2.0,
    first_ads_sum: float = 10.0,
) -> SlotLookups:
    first_history = {"orderCount": first_order_count}
    if first_order_sum is not None:
        first_history["orderSum"] = first_order_sum
    return SlotLookups(
        seller_funnel_lookup={},
        history_lookup={
            497413000: first_history,
            497413001: {"orderSum": 100.0, "orderCount": 1.0},
        },
        web_lookup={},
        prices_lookup={},
        sf_period_lookup={},
        spp_lookup={},
        ads_bids_lookup={},
        stocks_lookup={},
        onec_stocks_lookup={
            497413000: {ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY: onec_cost},
            497413001: {ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY: onec_cost},
        },
        ads_compact_lookup={
            497413000: SimpleNamespace(ads_sum=first_ads_sum),
            497413001: SimpleNamespace(ads_sum=5.0),
        },
        fin_lookup={},
        fin_storage_fee_total=None,
        cost_price_lookup={},
        promo_lookup={},
        our_wb_cost_lookup={
            497413000: {
                "our_wb_unit_cost_rub": our_cost,
                "stock_qty": 5.0,
                "confirmed_qty": 5.0,
                "confirmed_share_pct": 1.0,
            },
            497413001: {
                "our_wb_unit_cost_rub": our_cost,
                "stock_qty": 5.0,
                "confirmed_qty": 5.0,
                "confirmed_share_pct": 1.0,
            },
        },
        column_date=column_date,
    )


if __name__ == "__main__":
    main()
