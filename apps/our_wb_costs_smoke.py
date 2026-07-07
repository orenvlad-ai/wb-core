"""Smoke checks for the management proxy WB cost contour."""

from __future__ import annotations

from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.our_wb_costs import (  # noqa: E402
    TRANSIT_DIRECT_ZERO_CONFIRMED,
    WB_COST_STATUS_CONFIRMED,
    OurWbCostBlock,
    classify_wb_supply_transit,
)
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
    ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
    ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY,
    extend_metrics_with_onec_stock_metrics,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (  # noqa: E402
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    extend_metrics_with_our_wb_cost_metrics,
)
from packages.application.supplier_shipments import SupplierShipmentsBlock  # noqa: E402
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item  # noqa: E402
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot  # noqa: E402
from packages.contracts.supplier_shipments import ORDER_STATUS_ACCEPTED_FF, ORDER_STATUS_PRODUCTION  # noqa: E402


NOW = "2026-07-07T07:00:00Z"


def main() -> None:
    with TemporaryDirectory(prefix="our-wb-costs-smoke-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_supplier_shipment(runtime)
        _seed_financial_inputs(runtime)

        block = OurWbCostBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        materialized = block.materialize_supplier_ff_cost_layer("sup_smoke")
        if not materialized or not materialized.get("materialized"):
            raise AssertionError(f"first FF cost materialization must write a layer, got {materialized}")
        second = block.materialize_supplier_ff_cost_layer("sup_smoke")
        if second is None or second.get("materialized"):
            raise AssertionError(f"second FF cost materialization must be idempotent, got {second}")
        _assert_supplier_ff_reconciliation(runtime)
        _seed_wb_supply(runtime)
        if block.materialize_wb_supply_cost_layers(opening_date="2026-07-01") != 1:
            raise AssertionError("WB supply cost layer materialization must write one SKU layer")
        _assert_wb_supply_cost_layer(runtime)
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
        if block.materialize_wb_supply_cost_layers(opening_date="2026-07-01") != 2:
            raise AssertionError("non-final WB quantities must still materialize as non-confirmed layers")
        _assert_wb_quantity_source_status(runtime)

        supplier_block = SupplierShipmentsBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        try:
            supplier_block.update_order_status("sup_smoke", ORDER_STATUS_ACCEPTED_FF)
        except ValueError as exc:
            if "actual_ff_acceptance_date" not in str(exc):
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
        "match_status": "matched",
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


def _seed_wb_supply(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    supply_id: str = "40431461",
    status_id: int = 5,
    goods: list[dict[str, object]] | None = None,
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
                        "has_transit_cost_marker": 0,
                        "acceptanceCost": 0,
                        "cost_total": 0,
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


def _assert_wb_quantity_source_status(runtime: RegistryUploadDbBackedRuntime) -> None:
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        rows = {
            str(row["wb_supply_id"]): dict(row)
            for row in conn.execute(
                """
                SELECT wb_supply_id, accepted_qty, source_status, component_status_json
                FROM sheet_vitrina_v1_wb_supply_cost_layers
                WHERE wb_supply_id IN ('receiving_accepted_qty', 'planned_qty_only')
                  AND nm_id = 497413000
                  AND is_current = 1
                """
            ).fetchall()
        }
    receiving = rows.get("receiving_accepted_qty")
    planned = rows.get("planned_qty_only")
    if receiving is None or planned is None:
        raise AssertionError(f"quantity source regression layers missing, got {rows}")
    if receiving["source_status"] == WB_COST_STATUS_CONFIRMED or planned["source_status"] == WB_COST_STATUS_CONFIRMED:
        raise AssertionError(f"non-final/planned quantity must not become confirmed, got {rows}")
    if float(receiving["accepted_qty"]) != 7.0 or float(planned["accepted_qty"]) != 10.0:
        raise AssertionError(f"quantity values must preserve accepted/planned evidence, got {rows}")
    receiving_components = json.loads(str(receiving["component_status_json"]))
    planned_components = json.loads(str(planned["component_status_json"]))
    if receiving_components.get("wb_quantity_final_accepted") is not False:
        raise AssertionError(f"receiving status must not be final accepted, got {receiving_components}")
    if planned_components.get("wb_quantity_source") != "quantity":
        raise AssertionError(f"planned fallback source must be explicit, got {planned_components}")


def _assert_supplier_ff_reconciliation(runtime: RegistryUploadDbBackedRuntime) -> None:
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT status, weighted_avg_ff_unit_cost_rub, reconciliation_status
            FROM sheet_vitrina_v1_supplier_ff_cost_layers
            WHERE supplier_shipment_id = 'sup_smoke' AND is_current = 1
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


def _assert_proxy_profit_3_evaluator() -> None:
    config = [ConfigV2Item(nm_id=497413000, enabled=True, display_name="SKU 1", group="A", display_order=1)]
    base_metrics = [
        _metric("orderSum"),
        _metric("orderCount"),
        _metric("ads_sum"),
    ]
    metrics = extend_metrics_with_our_wb_cost_metrics(extend_metrics_with_onec_stock_metrics(base_metrics))
    metrics_by_key = {item.metric_key: item for item in metrics}
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
    if before_proxy3 != before_proxy2:
        raise AssertionError(f"proxy3 before opening must equal proxy2, got {before_proxy3} vs {before_proxy2}")
    after_proxy3 = evaluator.resolve_sku(OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, 497413000, "after")
    expected_after = 1000.0 * 0.5096 - 2.0 * 0.91 * 80.0 - 10.0
    if abs(float(after_proxy3 or 0.0) - expected_after) > 0.000001:
        raise AssertionError(f"proxy3 after opening must use our WB cost, got {after_proxy3}")
    total_after = evaluator.resolve_total(OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY, "after")
    if total_after != after_proxy3:
        raise AssertionError(f"total proxy3 must sum SKU proxy3, got {total_after} vs {after_proxy3}")


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


def _slot_lookup(*, column_date: str, onec_cost: float, our_cost: float) -> SlotLookups:
    return SlotLookups(
        seller_funnel_lookup={},
        history_lookup={497413000: {"orderSum": 1000.0, "orderCount": 2.0}},
        web_lookup={},
        prices_lookup={},
        sf_period_lookup={},
        spp_lookup={},
        ads_bids_lookup={},
        stocks_lookup={},
        onec_stocks_lookup={497413000: {ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY: onec_cost}},
        ads_compact_lookup={497413000: SimpleNamespace(ads_sum=10.0)},
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
            }
        },
        column_date=column_date,
    )


if __name__ == "__main__":
    main()
